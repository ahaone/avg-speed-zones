"""Tests for the two-tier, bounded BGTOLL poller lifecycle."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
import urllib.error
from datetime import datetime, timedelta, timezone
from email.message import Message
from typing import Any

from tools.poll_bgtoll import MAP_URL, WIM_URL, PollConfig, run_poll

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "data" / "bgtoll-vaprosi-i-otgovori.html"
MAP_KML = """<?xml version=\"1.0\"?><kml xmlns=\"http://www.opengis.net/kml/2.2\"><Document><Folder><name>Certified</name><Placemark><name>North – South</name><description>Road A-1, limit 90</description><LineString><coordinates>25.0,42.0 25.1,42.1</coordinates></LineString></Placemark></Folder></Document></kml>""".encode("utf-8")


def _parse_minutes(deadline: str, now: datetime) -> int:
    """Return integer backoff duration in minutes for deterministic assertions."""
    blocked_until = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
    return int((blocked_until - now).total_seconds() // 60)


class _Response:
    """Provide the small urllib response surface used by the poller."""

    def __init__(self, body: bytes, status: int = 200, headers: dict[str, str] | None = None) -> None:
        """Initialize a context-manager-compatible fake HTTP response."""
        self.body = body
        self.status = status
        self.headers = Message()
        for name, value in (headers or {}).items():
            self.headers[name] = value

    def read(self, size: int = -1) -> bytes:
        """Return a bounded response body."""
        return self.body if size < 0 else self.body[:size]

    def getcode(self) -> int:
        """Return the mocked HTTP status code."""
        return self.status

    def __enter__(self) -> "_Response":
        """Return this response as a context manager."""
        return self

    def __exit__(self, *args: Any) -> None:
        """Close the mocked response context."""
        return None


class PollBgtollTests(unittest.TestCase):
    """Exercise map normalization, WIM cadence, source gates, and wrapper locking."""

    def make_config(self, state_dir: Path, **kwargs: Any) -> PollConfig:
        """Build an isolated poller configuration against reviewed local evidence."""
        return PollConfig(
            repo_root=ROOT,
            state_dir=state_dir,
            baseline_path=ARCHIVE,
            **kwargs,
        )

    def route_opener(self, map_body: bytes, wim_body: bytes, calls: list[str]) -> Any:
        """Return a fake opener that dispatches responses by the exact source URL."""
        def opener(request: Any, timeout: float) -> _Response:
            """Record the source request and return its mocked body."""
            calls.append(request.full_url)
            if request.full_url == MAP_URL:
                return _Response(map_body)
            if request.full_url == WIM_URL:
                return _Response(wim_body)
            raise AssertionError(f"unexpected URL: {request.full_url}")
        return opener

    def test_unchanged_sources_are_quiet_and_do_not_publish(self) -> None:
        """Unchanged map and WIM sources produce no semantic alerts or bundle writes."""
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            calls: list[str] = []
            opener = self.route_opener(MAP_KML, ARCHIVE.read_bytes(), calls)
            now = datetime(2026, 10, 24, tzinfo=timezone.utc)
            first_code, _ = run_poll(config, opener=opener, now=now)
            calls.clear()
            code, result = run_poll(config, opener=opener, now=now + timedelta(minutes=5))
            saved = json.loads(config.status_path.read_text(encoding="utf-8"))

        self.assertEqual(first_code, 0)
        self.assertEqual(code, 0)
        self.assertEqual(calls, [MAP_URL])
        self.assertEqual(result["state"], "UNCHANGED")
        self.assertEqual(result["alerts"], [])
        self.assertEqual(result["holds"], [])
        self.assertFalse(result["published"])
        self.assertEqual(saved["current_tier"], "MAP")

    def test_normalized_map_digest_ignores_xml_layout_but_tracks_placemark_semantics(self) -> None:
        """Whitespace/attribute layout is ignored while folder, description, and geometry matter."""
        from tools.poll_bgtoll import normalized_map_digest

        reformatted = MAP_KML.replace(b"><", b">\n  <")
        status_changed = MAP_KML.replace(b"Certified", b"Pending")
        geometry_changed = MAP_KML.replace(b"25.1,42.1", b"25.2,42.1")
        self.assertEqual(normalized_map_digest(MAP_KML), normalized_map_digest(reformatted))
        self.assertNotEqual(normalized_map_digest(MAP_KML), normalized_map_digest(status_changed))
        self.assertNotEqual(normalized_map_digest(MAP_KML), normalized_map_digest(geometry_changed))

    def test_map_change_debounces_before_requesting_wim(self) -> None:
        """One stable Tier-1 digest change coalesces to one delayed Tier-2 request."""
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            calls: list[str] = []
            opener = self.route_opener(MAP_KML, ARCHIVE.read_bytes(), calls)
            start = datetime(2026, 10, 24, tzinfo=timezone.utc)
            run_poll(config, opener=opener, now=start)
            status = json.loads(config.status_path.read_text(encoding="utf-8"))
            changed_map = MAP_KML.replace(b"Certified", b"Certified updated")
            calls.clear()
            second_code, second = run_poll(
                config, opener=self.route_opener(changed_map, ARCHIVE.read_bytes(), calls),
                now=start + timedelta(minutes=5),
            )
            self.assertEqual(second_code, 0)
            self.assertEqual(calls, [MAP_URL])
            self.assertTrue(second["map_changed"])
            self.assertEqual(second["state"], "HELD")
            calls.clear()
            third_code, third = run_poll(
                config, opener=self.route_opener(changed_map, ARCHIVE.read_bytes(), calls),
                now=start + timedelta(minutes=10),
            )
            saved = json.loads(config.status_path.read_text(encoding="utf-8"))

        self.assertEqual(third_code, 0)
        self.assertEqual(calls, [MAP_URL, WIM_URL])
        self.assertTrue(third["wim_requested"])
        self.assertFalse(third["published"])
        self.assertEqual(saved["map"]["pending_wim_digest"], None)

    def test_daily_wim_refresh_runs_when_map_is_unchanged(self) -> None:
        """The daily Tier-2 check occurs even when the map placemark digest is stable."""
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            calls: list[str] = []
            opener = self.route_opener(MAP_KML, ARCHIVE.read_bytes(), calls)
            start = datetime(2026, 10, 24, tzinfo=timezone.utc)
            run_poll(config, opener=opener, now=start)
            calls.clear()
            code, result = run_poll(config, opener=opener, now=start + timedelta(days=1, minutes=1))

        self.assertEqual(code, 0)
        self.assertEqual(calls, [MAP_URL, WIM_URL])
        self.assertTrue(result["wim_requested"])
        self.assertFalse(result["map_changed"])

    def test_wim_two_per_rolling_day_cap_and_minimum_interval(self) -> None:
        """Tier 2 never exceeds two rolling-day requests and enforces five-minute spacing."""
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            calls: list[str] = []
            opener = self.route_opener(MAP_KML, ARCHIVE.read_bytes(), calls)
            start = datetime(2026, 10, 24, tzinfo=timezone.utc)
            run_poll(config, opener=opener, now=start)
            status = json.loads(config.status_path.read_text(encoding="utf-8"))
            status["map"]["pending_wim_digest"] = status["map"]["last_good"]["semantic_sha256"]
            status["map"]["pending_wim_since"] = (start - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
            status["wim"]["request_times"] = [
                (start - timedelta(hours=23)).isoformat().replace("+00:00", "Z"),
                (start - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            ]
            config.status_path.write_text(json.dumps(status), encoding="utf-8")
            calls.clear()
            code, result = run_poll(config, opener=opener, now=start + timedelta(minutes=5))

        self.assertEqual(code, 0)
        self.assertEqual(calls, [MAP_URL])
        self.assertFalse(result["wim_requested"])
        self.assertIn("WIM_DAILY_REQUEST_CAP", result["holds"])

    def test_blocked_wim_has_no_retry_and_backoff_tick_makes_zero_requests(self) -> None:
        """Cloudflare blocking is SOURCE_BLOCKED and a later backoff tick is network-silent."""
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            calls: list[str] = []

            def blocked_opener(request: Any, timeout: float) -> Any:
                """Return valid map KML, then one Cloudflare challenge response."""
                calls.append(request.full_url)
                if request.full_url == MAP_URL:
                    return _Response(MAP_KML)
                headers = Message()
                headers["Server"] = "cloudflare"
                headers["CF-Ray"] = "fixture-ray"
                raise urllib.error.HTTPError(
                    request.full_url, 403, "Forbidden", headers,
                    io.BytesIO(b"Attention Required! You have been blocked."),
                )

            now = datetime(2026, 10, 24, tzinfo=timezone.utc)
            code, result = run_poll(config, opener=blocked_opener, now=now)
            after_first = len(calls)
            second_code, second = run_poll(config, opener=blocked_opener, now=now + timedelta(minutes=5))
            saved = json.loads(config.status_path.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "SOURCE_BLOCKED")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(after_first, 2)
        self.assertEqual(second_code, 3)
        self.assertEqual(len(calls), after_first)
        self.assertEqual(second["skip_reason"], "BACKOFF_ACTIVE")
        self.assertEqual(saved["wim"]["block_strikes"], 1)
        self.assertIsNone(saved["wim"].get("last_good"))

    def test_semantic_wim_change_alerts_and_never_automatically_publishes(self) -> None:
        """A changed row produces the Phase 1 diff/alert and retains the bundle by default."""
        changed = ARCHIVE.read_bytes().replace("Крушовица (км 19+460)".encode(), "Крушовица-нова (км 19+460)".encode(), 1)
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            calls: list[str] = []
            code, result = run_poll(
                config,
                opener=self.route_opener(MAP_KML, changed, calls),
                now=datetime(2026, 10, 24, tzinfo=timezone.utc),
            )
            saved = json.loads(config.status_path.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertTrue(result["source_changed"])
        self.assertTrue(result["diff"]["added"])
        self.assertTrue(result["diff"]["removed"])
        self.assertTrue(any(alert["code"] == "SEMANTIC_SOURCE_CHANGE" for alert in result["alerts"]))
        self.assertEqual(result["state"], "HELD")
        self.assertFalse(result["published"])
        self.assertIn("WIM_SEMANTIC_CHANGE_REQUIRES_REVIEW", result["holds"])
        self.assertIsNone(saved.get("last_good_bundle"))

    def test_incomplete_candidate_retains_last_good_bundle(self) -> None:
        """Missing local geometry evidence cannot replace a previously published bundle."""
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            previous = {"path": "/srv/avg-speed/zones.json", "sha256": "known-good"}
            config.state_dir.mkdir(parents=True, exist_ok=True)
            config.status_path.write_text(json.dumps({"last_good_bundle": previous}), encoding="utf-8")
            calls: list[str] = []
            code, result = run_poll(
                config,
                opener=self.route_opener(MAP_KML, ARCHIVE.read_bytes(), calls),
                now=datetime(2026, 10, 24, tzinfo=timezone.utc),
            )
            saved = json.loads(config.status_path.read_text(encoding="utf-8"))

        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "HELD")
        self.assertTrue(result["holds"])
        self.assertFalse(result["published"])
        self.assertEqual(saved["last_good_bundle"], previous)

    def test_publish_flag_does_not_publish_without_complete_candidate(self) -> None:
        """Explicit --publish remains held when the candidate gates are incomplete."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            publish_dir = root / "publish"
            publish_dir.mkdir()
            bundle = publish_dir / "zones.json"
            original = (ROOT / "zones.json").read_bytes()
            bundle.write_bytes(original)
            config = self.make_config(root / "state", publish_dir=publish_dir)
            calls: list[str] = []
            code, result = run_poll(
                config,
                opener=self.route_opener(MAP_KML, ARCHIVE.read_bytes(), calls),
                now=datetime(2026, 10, 24, tzinfo=timezone.utc),
                publish=True,
            )
            after_bundle = bundle.read_bytes()

        self.assertEqual(code, 0)
        self.assertEqual(result["state"], "HELD")
        self.assertFalse(result["published"])
        self.assertEqual(after_bundle, original)

    def test_malformed_wim_source_fails_and_preserves_last_good(self) -> None:
        """Malformed table input fails without publishing or advancing the last-good capture."""
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            config.state_dir.mkdir(parents=True, exist_ok=True)
            old = {"retrieved_at": "2026-10-23T00:00:00Z", "source_sha256": "prior"}
            config.status_path.write_text(json.dumps({"wim": {"last_good": old}}), encoding="utf-8")
            calls: list[str] = []
            code, result = run_poll(
                config,
                opener=self.route_opener(MAP_KML, b"<html>challenge or malformed content</html>", calls),
                now=datetime(2026, 10, 24, tzinfo=timezone.utc),
            )
            saved = json.loads(config.status_path.read_text(encoding="utf-8"))

        self.assertNotEqual(code, 0)
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(saved["wim"]["last_good"], old)
        self.assertFalse(result["published"])

    def test_malformed_map_is_failure_without_requesting_wim(self) -> None:
        """A malformed Tier-1 KML never triggers a Tier-2 request or candidate pipeline."""
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            calls: list[str] = []
            code, result = run_poll(
                config,
                opener=self.route_opener(b"<kml>", ARCHIVE.read_bytes(), calls),
                now=datetime(2026, 10, 24, tzinfo=timezone.utc),
            )

        self.assertNotEqual(code, 0)
        self.assertEqual(result["state"], "FAILED")
        self.assertEqual(calls, [MAP_URL])
        self.assertFalse(result["published"])

    def test_tick_inside_map_minimum_interval_makes_no_request(self) -> None:
        """A timer tick earlier than five minutes is skipped without network activity."""
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            calls: list[str] = []
            opener = self.route_opener(MAP_KML, ARCHIVE.read_bytes(), calls)
            start = datetime(2026, 10, 24, tzinfo=timezone.utc)
            run_poll(config, opener=opener, now=start)
            calls.clear()
            code, result = run_poll(config, opener=opener, now=start + timedelta(minutes=4))

        self.assertEqual(code, 0)
        self.assertEqual(calls, [])
        self.assertEqual(result["skip_reason"], "MAP_MINIMUM_INTERVAL")

    def test_wim_backoff_progresses_and_caps_at_six_hours(self) -> None:
        """Consecutive WIM failures advance 15m/30m/1h/2h, then cap at 6h."""
        from tools.poll_bgtoll import _failure_backoff

        now = datetime(2026, 10, 24, tzinfo=timezone.utc)
        wim: dict[str, Any] = {}
        expected = [15, 30, 60, 120, 240, 360, 360]
        for minutes in expected:
            deadline = _failure_backoff(wim, {}, now)
            self.assertEqual(_parse_minutes(deadline, now), minutes)
            now = datetime.fromisoformat(deadline.replace("Z", "+00:00"))

    def test_retry_after_cannot_reduce_the_progressive_backoff(self) -> None:
        """A short Retry-After cannot shorten the configured exponential backoff."""
        from tools.poll_bgtoll import _failure_backoff

        now = datetime(2026, 10, 24, tzinfo=timezone.utc)
        wim: dict[str, Any] = {}
        deadline = _failure_backoff(wim, {"retry_after": "1"}, now)
        self.assertEqual(_parse_minutes(deadline, now), 15)

    def test_wim_cap_is_hard_capped_at_two_even_if_config_asks_for_more(self) -> None:
        """Configuration cannot increase the approved rolling-24-hour request ceiling."""
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory), max_wim_requests_per_24h=10)
            calls: list[str] = []
            opener = self.route_opener(MAP_KML, ARCHIVE.read_bytes(), calls)
            start = datetime(2026, 10, 24, tzinfo=timezone.utc)
            run_poll(config, opener=opener, now=start)
            saved = json.loads(config.status_path.read_text(encoding="utf-8"))
            saved["map"]["pending_wim_digest"] = saved["map"]["last_good"]["semantic_sha256"]
            saved["map"]["pending_wim_since"] = (start - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
            saved["wim"]["last_good"]["retrieved_at"] = (start - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
            saved["wim"]["request_times"] = [
                (start - timedelta(hours=2)).isoformat().replace("+00:00", "Z"),
                (start - timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
            ]
            config.status_path.write_text(json.dumps(saved), encoding="utf-8")
            calls.clear()
            code, result = run_poll(config, opener=opener, now=start + timedelta(minutes=5))

        self.assertEqual(code, 0)
        self.assertEqual(calls, [MAP_URL])
        self.assertIn("WIM_DAILY_REQUEST_CAP", result["holds"])

    def test_no_map_wrapper_churn_capture_is_stored_for_same_semantic_digest(self) -> None:
        """Repeated map wrapper bytes update raw hash while reusing one semantic capture."""
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            calls: list[str] = []
            first_map = MAP_KML
            second_map = MAP_KML.replace(b"</Document>", b"<!-- wrapper churn --> </Document>")
            run_poll(config, opener=self.route_opener(first_map, ARCHIVE.read_bytes(), calls),
                     now=datetime(2026, 10, 24, tzinfo=timezone.utc))
            saved_before = json.loads(config.status_path.read_text(encoding="utf-8"))
            previous_capture = saved_before["map"]["last_good"]["snapshot_path"]
            previous_raw = saved_before["map"]["last_good"]["raw_sha256"]
            run_poll(config, opener=self.route_opener(second_map, ARCHIVE.read_bytes(), calls),
                     now=datetime(2026, 10, 24, 0, 5, tzinfo=timezone.utc))
            saved_after = json.loads(config.status_path.read_text(encoding="utf-8"))

        self.assertEqual(saved_after["map"]["last_good"]["snapshot_path"], previous_capture)
        self.assertNotEqual(saved_after["map"]["last_good"]["raw_sha256"], previous_raw)
        self.assertFalse(saved_after["map"].get("map_changed", False))

    def test_state_reports_current_tier_and_backoff_deadline(self) -> None:
        """Heartbeat reports the active backoff and stops all sources during its window."""
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            now = datetime(2026, 10, 24, tzinfo=timezone.utc)
            config.state_dir.mkdir(parents=True, exist_ok=True)
            config.status_path.write_text(json.dumps({
                "wim": {"blocked_until": (now + timedelta(minutes=15)).isoformat().replace("+00:00", "Z")},
                "map": {},
            }), encoding="utf-8")
            calls: list[str] = []
            code, result = run_poll(config, opener=self.route_opener(MAP_KML, ARCHIVE.read_bytes(), calls), now=now)
            saved = json.loads(config.status_path.read_text(encoding="utf-8"))

        self.assertEqual(code, 3)
        self.assertEqual(calls, [])
        self.assertEqual(result["current_tier"], "NONE")
        self.assertEqual(result["current_backoff"], saved["wim"]["blocked_until"])

    def test_malformed_source_state_is_not_silently_reset_after_prior_block(self) -> None:
        """A damaged status file is a failure, not a way to bypass persisted WIM limits."""
        with tempfile.TemporaryDirectory() as directory:
            config = self.make_config(Path(directory))
            config.state_dir.mkdir(parents=True, exist_ok=True)
            config.status_path.write_text("{broken", encoding="utf-8")
            calls: list[str] = []
            code, result = run_poll(config, opener=self.route_opener(MAP_KML, ARCHIVE.read_bytes(), calls),
                                   now=datetime(2026, 10, 24, tzinfo=timezone.utc))

        self.assertEqual(code, 1)
        self.assertEqual(calls, [])
        self.assertEqual(result["state"], "FAILED")
        self.assertTrue(any("STATE_INVALID" in hold for hold in result["holds"]))

    def test_stale_pid_lock_takeover(self) -> None:
        """The flock-backed wrapper safely replaces a stale PID file on the next run."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state_dir = root / "state"
            state_dir.mkdir()
            lock_path = state_dir / "poller.lock"
            pid_path = state_dir / "poller.pid"
            pid_path.write_text("999999999\n", encoding="ascii")
            fake_bin = root / "bin"
            fake_bin.mkdir()
            fake_python = fake_bin / "python3"
            fake_python.write_text("#!/bin/sh\nprintf '%s\\n' '{\"state\":\"FAKE\"}'\n", encoding="utf-8")
            fake_python.chmod(0o755)
            env = dict(os.environ)
            env["PATH"] = f"{fake_bin}:/usr/bin:/bin"
            env["AVG_SPEED_STATE_DIR"] = str(state_dir)
            env["AVG_SPEED_REPO_ROOT"] = str(ROOT)
            completed = subprocess.run(
                ["/bin/sh", str(ROOT / "scripts" / "avg-speed-poller.sh")],
                cwd=ROOT, env=env, capture_output=True, text=True, check=False,
            )
            lock_contents = pid_path.read_text(encoding="ascii").strip()

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(completed.stdout.strip(), '{"state":"FAKE"}')
        self.assertNotEqual(lock_contents, "999999999")
        self.assertTrue(lock_contents.isdigit())


if __name__ == "__main__":
    unittest.main()
