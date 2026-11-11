"""Tests for BGTOLL row inventory, certificate provenance, and freshness gates."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from tools.bgtoll_inventory import fetch_certificates, parse_inventory


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "data" / "bgtoll-vaprosi-i-otgovori.html"
CERTIFICATE_BYTES = b"\xff\xd8\xff\xdb\x00\x43"  # Minimal JPEG signature for MIME validation.


def fixture_html(certified_links: tuple[str, str] = ("/one.jpg", "/two.jpg"), pending_link: str = "") -> str:
    """Build a small official-shaped source with two status tables and directional rows."""
    return f"""<!doctype html><html><body>
<h3>Сертифицирани отсечки за средна скорост:</h3>
<table><thead><tr><th>Път</th><th>Начална точка</th><th>Крайна точка</th><th>Карта (изображение)</th></tr></thead><tbody>
<tr><td><a href=\"https://wrong.example/map.jpg\">I-2</a></td><td>Белокопитово (км 340+293) /Пътен участък в ремонт/</td><td>Каспичан (км 361+581)</td><td><a href=\"{certified_links[0]}\">Вижте</a></td></tr>
<tr><td>I-2</td><td>Каспичан (км 361+581)</td><td>Белокопитово (км 340+293)</td><td><a href=\"{certified_links[1]}\">Вижте</a></td></tr>
</tbody></table>
<h3>Отсечки, за които предстои последваща метрологична проверка:</h3>
<table><thead><tr><th>Път</th><th>Начална точка</th><th>Крайна точка</th><th>Карта (изображение)</th></tr></thead><tbody>
<tr><td>АМ &quot;Хемус&quot;</td><td>Девня (км 399+703)</td><td>Игнатиево (км 418+126)</td><td>{f'<a href="{pending_link}">Вижте</a>' if pending_link else ''}</td></tr>
</tbody></table></body></html>"""


class _CertificateHandler(BaseHTTPRequestHandler):
    """Serve deterministic certificate responses for real HTTP fetch tests."""

    retry_counts: dict[str, int] = {}
    retry_lock = threading.Lock()

    def do_GET(self) -> None:
        """Return a valid image, a dead link, or an HTML error body by path."""
        if self.path == "/retry-once.jpg":
            with self.retry_lock:
                attempts = self.retry_counts.get(self.path, 0) + 1
                self.retry_counts[self.path] = attempts
            if attempts == 1:
                self.send_response(429)
                self.send_header("Retry-After", "0")
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"rate limited")
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(CERTIFICATE_BYTES)
            return
        if self.path == "/good.jpg":
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            self.wfile.write(CERTIFICATE_BYTES)
            return
        if self.path == "/not-image.jpg":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<html>not an image</html>")
            return
        self.send_response(404)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"not found")

    def log_message(self, format: str, *args: Any) -> None:
        """Keep test HTTP server request logging out of test output."""
        return


class BgtollInventoryTests(unittest.TestCase):
    """Exercise parsing, semantic hashing, row-bound links, and image quarantine."""

    @classmethod
    def setUpClass(cls) -> None:
        """Start a local HTTP server used by certificate-fetch tests."""
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _CertificateHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.server_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls) -> None:
        """Stop the local HTTP server after the test class completes."""
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=2)

    def test_archived_snapshot_has_expected_direction_and_section_counts(self) -> None:
        """Parse the archived official snapshot into its reviewed 82/41 inventory."""
        inventory = parse_inventory(ARCHIVE.read_bytes())

        self.assertEqual(len(inventory["rows"]), 82)
        self.assertEqual(len(inventory["sections"]), 41)
        self.assertEqual(inventory["status_counts"], {"CERTIFIED": 73, "PENDING_VERIFICATION": 9})
        self.assertEqual(len({row["direction_key"] for row in inventory["rows"]}), 82)

    def test_directional_rows_pair_to_one_physical_section(self) -> None:
        """Pair opposite endpoints while retaining distinct deterministic direction keys."""
        inventory = parse_inventory(fixture_html().encode())
        forward, reverse = inventory["rows"][:2]

        self.assertEqual(forward["section_key"], reverse["section_key"])
        self.assertNotEqual(forward["direction_key"], reverse["direction_key"])
        self.assertEqual(forward["start_chainage_m"], 340293)
        self.assertEqual(forward["end_chainage_m"], 361581)
        self.assertEqual(reverse["start_chainage_m"], 361581)
        self.assertEqual(forward["status"], "CERTIFIED")
        self.assertEqual(inventory["rows"][2]["status"], "PENDING_VERIFICATION")

    def test_repair_annotation_is_retained_raw_and_removed_from_matching_label(self) -> None:
        """Preserve exact repair text while excluding only that marker from normalization."""
        row = parse_inventory(fixture_html().encode())["rows"][0]

        self.assertIn("/Пътен участък в ремонт/", row["start_raw"])
        self.assertIn("/Пътен участък в ремонт/", row["raw_row_text"])
        self.assertEqual(row["start_label"], "Белокопитово (км 340+293)")
        self.assertEqual(row["start_annotations"], ["/Пътен участък в ремонт/"])

    def test_certificate_url_is_taken_only_from_the_fourth_cell(self) -> None:
        """Ignore an unrelated first-cell link and resolve the fourth-cell URL only."""
        inventory = parse_inventory(fixture_html().encode())

        self.assertEqual(inventory["rows"][0]["cert_url"], "https://www.bgtoll.bg/one.jpg")
        self.assertNotIn("wrong.example", inventory["rows"][0]["cert_url"])

    def test_missing_invalid_and_ambiguous_certificate_references_are_explicit(self) -> None:
        """Keep rows with absent, external, or multiple certificate links and quarantine them."""
        missing = parse_inventory(fixture_html(pending_link="").encode())["rows"][2]
        external_html = fixture_html(pending_link="https://untrusted.example/cert.jpg")
        external = parse_inventory(external_html.encode())["rows"][2]
        ambiguous_html = fixture_html().replace(
            '<a href="/one.jpg">Вижте</a>',
            '<a href="/one.jpg">first</a><a href="/two.jpg">second</a>',
        )
        ambiguous = parse_inventory(ambiguous_html.encode())["rows"][0]

        self.assertIsNone(missing["cert_url"])
        self.assertEqual(missing["cert_url_issue"], "missing_url")
        self.assertIsNone(external["cert_url"])
        self.assertEqual(external["cert_url_issue"], "invalid_url")
        self.assertIsNone(ambiguous["cert_url"])
        self.assertEqual(ambiguous["cert_url_issue"], "ambiguous_url")

    def test_wrapper_only_source_change_keeps_semantic_hash_but_changes_raw_hash(self) -> None:
        """Ignore bytes outside the official row tuples while retaining raw provenance hash."""
        original = ARCHIVE.read_bytes()
        wrapped = original + b"\n<!-- wrapper-only capture marker -->\n"
        baseline_inventory = parse_inventory(original)
        wrapped_inventory = parse_inventory(wrapped)

        self.assertNotEqual(baseline_inventory["raw_sha256"], wrapped_inventory["raw_sha256"])
        self.assertEqual(baseline_inventory["semantic_sha256"], wrapped_inventory["semantic_sha256"])

    def test_semantic_hash_changes_for_row_text_status_or_certificate_change(self) -> None:
        """Hash all row-local status, text, and certificate URL semantics."""
        baseline = parse_inventory(fixture_html().encode())
        changed_text = parse_inventory(fixture_html().replace("Каспичан", "Каспичан-ново").encode())
        changed_url = parse_inventory(fixture_html(certified_links=("/changed.jpg", "/two.jpg")).encode())
        changed_certificate_cell_text = parse_inventory(
            fixture_html().replace("Вижте</a>", "Отворете документа</a>", 1).encode()
        )

        self.assertNotEqual(baseline["semantic_sha256"], changed_text["semantic_sha256"])
        self.assertNotEqual(baseline["semantic_sha256"], changed_url["semantic_sha256"])
        self.assertNotEqual(baseline["semantic_sha256"], changed_certificate_cell_text["semantic_sha256"])

    def test_live_semantic_rows_match_archived_official_snapshot(self) -> None:
        """Ensure the supplied current capture has no semantic row diff from the archive."""
        current_path = ROOT / ".work" / "bgtoll-current.html"
        if not current_path.exists():
            self.skipTest("No current source capture was supplied in this workspace")
        archived = parse_inventory(ARCHIVE.read_bytes())
        current = parse_inventory(current_path.read_bytes())

        self.assertEqual(current["semantic_sha256"], archived["semantic_sha256"])

    def test_fetch_records_http_mime_bytes_hash_and_quarantine_reasons(self) -> None:
        """Fetch each row URL and keep successful, dead, non-image, and missing rows visible."""
        rows = [
            {"direction_key": "direction-good", "cert_url": f"{self.server_url}/good.jpg", "cert_url_issue": None},
            {"direction_key": "direction-dead", "cert_url": f"{self.server_url}/dead.jpg", "cert_url_issue": None},
            {"direction_key": "direction-html", "cert_url": f"{self.server_url}/not-image.jpg", "cert_url_issue": None},
            {"direction_key": "direction-missing", "cert_url": None, "cert_url_issue": "missing_url"},
        ]
        with tempfile.TemporaryDirectory() as directory:
            result = fetch_certificates(rows, output_dir=Path(directory), timeout=3)
            by_key = {entry["direction_key"]: entry for entry in result}

            good = by_key["direction-good"]
            self.assertEqual(good["http_status"], 200)
            self.assertEqual(good["mime_type"], "image/jpeg")
            self.assertEqual(good["byte_count"], len(CERTIFICATE_BYTES))
            self.assertEqual(good["sha256"], hashlib.sha256(CERTIFICATE_BYTES).hexdigest())
            self.assertFalse(good["quarantined"])
            self.assertTrue(Path(good["stored_path"]).is_file())
            self.assertEqual(by_key["direction-dead"]["http_status"], 404)
            self.assertEqual(by_key["direction-dead"]["quarantine_reason"], "http_error")
            self.assertEqual(by_key["direction-html"]["quarantine_reason"], "non_image")
            self.assertEqual(by_key["direction-missing"]["quarantine_reason"], "missing_url")
            self.assertEqual(len(result), len(rows))

    def test_fetch_retries_http_429_using_retry_after(self) -> None:
        """Retry a temporarily rate-limited certificate before quarantining it."""
        _CertificateHandler.retry_counts.clear()
        rows = [{
            "direction_key": "direction-retry",
            "cert_url": f"{self.server_url}/retry-once.jpg",
            "cert_url_issue": None,
        }]

        with tempfile.TemporaryDirectory() as directory:
            result = fetch_certificates(
                rows,
                output_dir=Path(directory),
                timeout=3,
                max_retries=1,
                retry_delay=0.01,
            )[0]

        self.assertEqual(_CertificateHandler.retry_counts["/retry-once.jpg"], 2)
        self.assertEqual(result["http_status"], 200)
        self.assertFalse(result["quarantined"])


class BundleFreshnessTests(unittest.TestCase):
    """Verify semantic-only source gating and durable last-success behavior."""

    def run_freshness(self, source: Path, baseline: Path, state: Path) -> subprocess.CompletedProcess[str]:
        """Run the freshness CLI against explicit source, baseline, and state paths."""
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "tools" / "bundle-freshness.py"),
                "--source",
                str(source),
                "--baseline",
                str(baseline),
                "--state",
                str(state),
                "--retrieved-at",
                "2026-10-24T10:00:00+03:00",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )

    def test_wrapper_churn_passes_and_records_raw_and_semantic_hashes(self) -> None:
        """Permit wrapper-only bytes changes but persist raw and semantic provenance."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline.html"
            source = root / "source.html"
            state = root / "freshness.json"
            baseline.write_bytes(ARCHIVE.read_bytes())
            source.write_bytes(ARCHIVE.read_bytes() + b"<!-- capture wrapper changed -->")

            completed = self.run_freshness(source, baseline, state)
            record = json.loads(state.read_text(encoding="utf-8"))

        self.assertEqual(completed.returncode, 0, completed.stderr + completed.stdout)
        self.assertNotEqual(record["raw_sha256"], record["baseline_raw_sha256"])
        self.assertEqual(record["semantic_sha256"], record["baseline_semantic_sha256"])
        self.assertEqual(record["row_count"], 82)
        self.assertTrue(record["inventory_ok"])
        self.assertEqual(record["last_success"]["row_count"], 82)

    def test_semantic_change_fails_closed_with_exact_row_diff_and_keeps_last_success(self) -> None:
        """Reject changed source semantics and preserve the prior successful timestamp/hash."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline.html"
            source = root / "source.html"
            state = root / "freshness.json"
            baseline.write_bytes(ARCHIVE.read_bytes())
            source.write_bytes(ARCHIVE.read_bytes())
            first = self.run_freshness(source, baseline, state)
            before = json.loads(state.read_text(encoding="utf-8"))["last_success"]
            changed = ARCHIVE.read_bytes().replace("Крушовица (км 19+460)".encode(), "Крушовица-нова (км 19+460)".encode(), 1)
            source.write_bytes(changed)

            completed = self.run_freshness(source, baseline, state)
            record = json.loads(state.read_text(encoding="utf-8"))

        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(record["inventory_ok"])
        self.assertEqual(record["last_success"], before)
        self.assertTrue(record["row_diff"]["added"])
        self.assertTrue(record["row_diff"]["removed"])
        self.assertIn("Крушовица-нова", json.dumps(record["row_diff"], ensure_ascii=False))

    def test_failed_parse_fails_closed_and_retains_last_success(self) -> None:
        """Record malformed source attempts without replacing the last successful inventory."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            baseline = root / "baseline.html"
            source = root / "source.html"
            state = root / "freshness.json"
            baseline.write_bytes(ARCHIVE.read_bytes())
            source.write_bytes(ARCHIVE.read_bytes())
            first = self.run_freshness(source, baseline, state)
            before = json.loads(state.read_text(encoding="utf-8"))["last_success"]
            source.write_text("<html><body>not the official inventory</body></html>", encoding="utf-8")

            completed = self.run_freshness(source, baseline, state)
            record = json.loads(state.read_text(encoding="utf-8"))

        self.assertEqual(first.returncode, 0, first.stderr + first.stdout)
        self.assertNotEqual(completed.returncode, 0)
        self.assertFalse(record["inventory_ok"])
        self.assertEqual(record["last_success"], before)
        self.assertTrue(record["errors"])


if __name__ == "__main__":
    unittest.main()
