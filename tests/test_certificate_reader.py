"""Tests for conservative, local-only certificate callout extraction."""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import certificate_reader  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


class CalloutAnalysisTests(unittest.TestCase):
    """Exercise text-only classification without inferring route identity."""

    def test_image_hash_and_byte_count_match_exact_file_bytes(self) -> None:
        """Record the digest and length of the exact input bytes."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.bin"
            payload = b"synthetic certificate image payload\x00"
            path.write_bytes(payload)
            with mock.patch.object(certificate_reader, "_run_ocr", return_value=("17.8 km", None)):
                result = certificate_reader.read_image(path, engine_version="tesseract test")
        self.assertEqual(result["image_bytes"], len(payload))
        self.assertEqual(result["image_sha256"], hashlib.sha256(payload).hexdigest())
        self.assertEqual(result["engine_version"], "tesseract test")

    def test_tesseract_missing_is_per_image_unreadable(self) -> None:
        """Keep missing OCR executable an image-level failure instead of crashing."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.jpg"
            path.write_bytes(b"synthetic")
            with mock.patch.object(
                certificate_reader, "_run_ocr", return_value=("", "OCR executable not found: missing-tesseract")
            ):
                result = certificate_reader.read_image(path, executable="missing-tesseract")
        self.assertEqual(result["status"], "UNREADABLE")
        self.assertIsNone(result["candidate_cert_len_m"])
        self.assertIn("error", result)
        self.assertFalse(result["release_eligible"])

    def test_tesseract_error_is_per_image_unreadable(self) -> None:
        """Capture an OCR process failure as evidence without aborting the reader."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.jpg"
            path.write_bytes(b"synthetic")
            with mock.patch.object(
                certificate_reader, "_run_ocr", return_value=("", "simulated OCR failure")
            ):
                result = certificate_reader.read_image(path)
        self.assertEqual(result["status"], "UNREADABLE")
        self.assertEqual(result["error"], "simulated OCR failure")

    def test_falls_back_to_psm_six_only_when_first_pass_has_no_callout(self) -> None:
        """Use PSM 6 as a fallback only when PSM 11 yielded no kilometre token."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "image.jpg"
            path.write_bytes(b"synthetic")
            with mock.patch.object(
                certificate_reader,
                "_run_ocr",
                side_effect=[("No distance", None), ("21.8 km", None)],
            ) as run_ocr:
                result = certificate_reader.read_image(path)
        self.assertEqual([call.args[1] for call in run_ocr.call_args_list], [11, 6])
        self.assertEqual(result["psm"], 6)
        self.assertEqual(result["candidate_cert_len_m"], 21800)

    def test_manifest_preserves_direction_claim_and_never_releases(self) -> None:
        """Preserve supplied direction as an unverified claim only."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "certificate.jpg").write_bytes(b"synthetic")
            manifest = root / "manifest.json"
            manifest.write_text(
                json.dumps([{"direction_key": "section-123:forward", "image_path": "certificate.jpg"}]),
                encoding="utf-8",
            )
            with mock.patch.object(
                certificate_reader, "_run_ocr", return_value=("7.5 km", None)
            ):
                output = root / "evidence.json"
                status = certificate_reader.main([
                    "--manifest", str(manifest), "--output", str(output),
                ])
            evidence = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(status, 0)
        self.assertEqual(evidence["images"][0]["direction_key_claim"], "section-123:forward")
        self.assertFalse(evidence["images"][0]["human_confirmed"])
        self.assertFalse(evidence["images"][0]["release_eligible"])

    @unittest.skipUnless(
        Path("/usr/bin/tesseract").is_file()
        and Path("/home/hpuser/.hermes/cache/scratch/certs/zheglitsa_fwd.jpg").is_file(),
        "optional cached certificate and local Tesseract not present",
    )
    def test_cached_zheglitsa_certificate_has_stable_single_callout(self) -> None:
        """Check the optional local integration example without binding a row."""
        path = Path("/home/hpuser/.hermes/cache/scratch/certs/zheglitsa_fwd.jpg")
        result = certificate_reader.read_image(path)
        self.assertEqual(result["status"], "SINGLE_CALLOUT")
        self.assertEqual(result["candidate_cert_len_m"], 7500)
        self.assertFalse(result["release_eligible"])

    def test_all_candidates_are_never_confirmed_or_release_eligible(self) -> None:
        """Keep text-derived values in the non-authoritative candidate state."""
        for text in ("7.5 km", "7 km", "7.5 km and 8.5 km", "no callout"):
            with self.subTest(text=text):
                result = certificate_reader.analyze_text(text)
                self.assertNotIn("human_confirmed", result)
                self.assertNotIn("release_eligible", result)

    def test_decimal_kilometre_callouts_convert_to_integer_metres(self) -> None:
        """Normalize decimal points, decimal commas, and both km alphabets."""
        cases = {
            "7.5 km": 7500,
            "17.8 км": 17800,
            "21.8 KM": 21800,
            "4,25 km": 4250,
        }
        for text, expected_metres in cases.items():
            with self.subTest(text=text):
                result = certificate_reader.analyze_text(text)
                self.assertEqual(result["status"], "SINGLE_CALLOUT")
                self.assertEqual(result["candidate_cert_len_m"], expected_metres)
                self.assertEqual(result["matches"][0]["value_m"], expected_metres)

    def test_integer_kilometre_callout_requires_review(self) -> None:
        """Retain an integer candidate but mark it for human review."""
        result = certificate_reader.analyze_text("7 km")
        self.assertEqual(result["status"], "INTEGER_REVIEW")
        self.assertEqual(result["candidate_cert_len_m"], 7000)

    def test_distinct_callouts_are_ambiguous(self) -> None:
        """Refuse to choose between different lengths in the OCR text."""
        result = certificate_reader.analyze_text("7.5 km; 8.5 km")
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertIsNone(result["candidate_cert_len_m"])
        self.assertEqual([item["value_m"] for item in result["matches"]], [7500, 8500])

    def test_no_callout_is_unreadable(self) -> None:
        """Return no length candidate when OCR text has no kilometre callout."""
        result = certificate_reader.analyze_text("Route A — Route B, 17.8")
        self.assertEqual(result["status"], "UNREADABLE")
        self.assertIsNone(result["candidate_cert_len_m"])
        self.assertEqual(result["matches"], [])

    def test_map_labels_do_not_change_one_callout_classification(self) -> None:
        """Ignore unrelated map-label numbers and extract the single km callout."""
        text = (FIXTURES / "certificate-callout-sample.txt").read_text(encoding="utf-8")
        result = certificate_reader.analyze_text(text)
        self.assertEqual(result["status"], "SINGLE_CALLOUT")
        self.assertEqual(result["candidate_cert_len_m"], 17800)
        self.assertEqual(len(result["matches"]), 1)


if __name__ == "__main__":
    unittest.main()
