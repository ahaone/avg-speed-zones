"""Tests for fail-closed schema-1 bundle publishing."""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import publish_bundle  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


class PublisherTests(unittest.TestCase):
    """Exercise validation, deterministic serialization, and safe publication."""

    def load_valid(self) -> dict:
        """Load the small synthetic complete dual-direction candidate."""
        return json.loads((FIXTURES / "publisher-valid.json").read_text(encoding="utf-8"))

    def test_schema_one_preserves_legacy_zone_fields_and_coordinate_order(self) -> None:
        """Accept schema 1 without changing the v0.2.4 zone shape or vertices."""
        candidate = self.load_valid()
        bundle, _, _ = publish_bundle.build_artifacts(candidate)
        result = json.loads(bundle)
        self.assertEqual(result["schema_version"], 1)
        self.assertEqual(result["bundle_version"], "v5")
        required = {
            "id", "name_bg", "road", "cert_len_m", "polyline",
            "start_heading", "limit_by_category",
        }
        by_id = {zone["id"]: zone for zone in candidate["zones"]}
        for published in result["zones"]:
            original = by_id[published["id"]]
            self.assertEqual(set(published), set(original))
            self.assertTrue(required.issubset(published))
            self.assertEqual(published["polyline"], original["polyline"])
            self.assertEqual(published["id"], original["id"])

    def test_output_is_byte_deterministic(self) -> None:
        """Repeated serialization of one candidate yields identical artifact bytes."""
        candidate = self.load_valid()
        first = publish_bundle.build_artifacts(candidate)
        second = publish_bundle.build_artifacts(candidate)
        self.assertEqual(first[0], second[0])
        self.assertEqual(first[1], second[1])
        self.assertTrue(first[0].endswith(b"\n"))

    def test_dual_certified_active_gate_clean_section_is_legacy_projected(self) -> None:
        """Include a legacy zone only when both matching directions qualify."""
        candidate = self.load_valid()
        bundle, _, summary = publish_bundle.build_artifacts(candidate)
        payload = json.loads(bundle)
        self.assertEqual([zone["id"] for zone in payload["zones"]], ["synthetic-zone-a", "synthetic-zone-b"])
        self.assertEqual(summary["zone_count"], 2)

    def test_pending_and_quarantined_sections_remain_additive_not_legacy(self) -> None:
        """Retain pending/quarantined section details while excluding its legacy zone."""
        candidate = self.load_valid()
        candidate["zones"] = [z for z in candidate["zones"] if z["id"] != "synthetic-zone-a"]
        section = next(s for s in candidate["sections"] if s["section_key"] == "synthetic-zone-a")
        section["directions"]["forward"].update(
            status="PENDING_VERIFICATION", release_state="UNDER_VERIFICATION"
        )
        section["directions"]["reverse"].update(
            release_state="QUARANTINED",
            gates={"chainage": "QUARANTINE", "ria": "UNVERIFIED", "length": "REVIEW"},
        )
        bundle, _, summary = publish_bundle.build_artifacts(candidate)
        payload = json.loads(bundle)
        self.assertEqual([zone["id"] for zone in payload["zones"]], ["synthetic-zone-b"])
        self.assertEqual(len(payload["sections"]), 2)
        self.assertEqual(summary["hold_count"], 1)
        section_out = next(s for s in payload["sections"] if s["section_key"] == "synthetic-zone-a")
        self.assertEqual(section_out["directions"]["forward"]["status"], "PENDING_VERIFICATION")
        self.assertEqual(section_out["directions"]["reverse"]["release_state"], "QUARANTINED")

    def test_retired_release_state_is_accepted_but_not_legacy(self) -> None:
        """Accept a local retired tombstone while excluding it from the legacy projection."""
        candidate = self.load_valid()
        candidate["zones"] = [z for z in candidate["zones"] if z["id"] != "synthetic-zone-a"]
        section = next(s for s in candidate["sections"] if s["section_key"] == "synthetic-zone-a")
        section["directions"]["forward"]["release_state"] = "RETIRED"
        section["directions"]["reverse"]["release_state"] = "RETIRED"
        bundle, _, summary = publish_bundle.build_artifacts(candidate)
        payload = json.loads(bundle)
        self.assertEqual([zone["id"] for zone in payload["zones"]], ["synthetic-zone-b"])
        self.assertEqual(summary["hold_count"], 1)
        section_out = next(s for s in payload["sections"] if s["section_key"] == "synthetic-zone-a")
        self.assertEqual(section_out["directions"]["forward"]["release_state"], "RETIRED")
        self.assertEqual(section_out["directions"]["reverse"]["release_state"], "RETIRED")

    def test_one_direction_incomplete_section_is_held_without_blocking_complete_sections(self) -> None:
        """Hold a one-direction addition while publishing unrelated complete zones."""
        candidate = self.load_valid()
        candidate["sections"].append({
            "section_key": "new-incomplete-section",
            "road": "S-3",
            "name_bg": "Нова непълна секция",
            "directions": {
                "forward": {
                    "name_bg": "Напред",
                    "status": "CERTIFIED",
                    "release_state": "UNDER_VERIFICATION",
                    "cert_len_m": None,
                    "cert_url": None,
                    "cert_sha256": None,
                    "gates": {"chainage": "PASS", "ria": "UNVERIFIED", "length": "REVIEW"},
                }
            },
        })
        bundle, _, summary = publish_bundle.build_artifacts(candidate)
        payload = json.loads(bundle)
        self.assertEqual(len(payload["zones"]), 2)
        self.assertEqual(summary["hold_count"], 1)
        held = payload["sections"][-1]
        self.assertEqual(set(held["directions"]), {"forward"})
        self.assertEqual(held["directions"]["forward"]["release_state"], "UNDER_VERIFICATION")

    def test_legacy_zone_with_ineligible_section_is_rejected(self) -> None:
        """Reject instead of silently filtering a zone lacking certificate gates."""
        candidate = self.load_valid()
        section = next(s for s in candidate["sections"] if s["section_key"] == "synthetic-zone-a")
        section["directions"]["forward"]["cert_sha256"] = None
        with self.assertRaises(publish_bundle.ValidationError):
            publish_bundle.build_artifacts(candidate)

    def test_legacy_zone_must_match_both_direction_certificate_lengths(self) -> None:
        """Reject a legacy section where the zone certificate length disagrees by direction."""
        candidate = self.load_valid()
        candidate["sections"][0]["directions"]["reverse"]["cert_len_m"] += 1
        with self.assertRaises(publish_bundle.ValidationError):
            publish_bundle.build_artifacts(candidate)

    def test_equal_and_downgrade_versions_are_rejected(self) -> None:
        """Require candidate version to be strictly newer than an existing bundle."""
        candidate = self.load_valid()
        for previous in ("v5", "v6"):
            with self.subTest(previous=previous), self.assertRaises(publish_bundle.ValidationError):
                publish_bundle.build_artifacts(candidate, previous_version=previous)

    def test_malformed_json_missing_fields_and_bad_hash_are_rejected(self) -> None:
        """Reject malformed documents, missing required keys, and incomplete provenance."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "candidate.json"
            path.write_text('{"schema_version":', encoding="utf-8")
            with self.assertRaises(publish_bundle.ValidationError):
                publish_bundle.load_candidate(path)
        candidate = self.load_valid()
        del candidate["schema_version"]
        with self.assertRaises(publish_bundle.ValidationError):
            publish_bundle.build_artifacts(candidate)
        candidate = self.load_valid()
        del candidate["source"]["snapshot_sha256"]
        with self.assertRaises(publish_bundle.ValidationError):
            publish_bundle.build_artifacts(candidate)
        candidate = self.load_valid()
        candidate["source"]["snapshot_sha256"] = "not-a-sha256"
        with self.assertRaises(publish_bundle.ValidationError):
            publish_bundle.build_artifacts(candidate)

    def test_manifest_hash_matches_exact_bundle_bytes(self) -> None:
        """Bind the sidecar digest and byte count to the precise serialized bundle."""
        bundle, manifest_bytes, _ = publish_bundle.build_artifacts(self.load_valid())
        manifest = json.loads(manifest_bytes)
        self.assertEqual(manifest["bundle_sha256"], hashlib.sha256(bundle).hexdigest())
        self.assertEqual(manifest["bundle_bytes"], len(bundle))
        self.assertEqual(manifest["manifest_version"], 1)
        self.assertEqual(manifest["counts"]["zones"], 2)
        self.assertEqual(manifest["counts"]["directions"], 4)
        self.assertEqual(manifest["counts"]["status_counts"]["CERTIFIED"], 4)
        self.assertEqual(manifest["counts"]["status_counts"]["PENDING_VERIFICATION"], 0)

    def test_dry_run_does_not_create_output_directory_or_files(self) -> None:
        """Default CLI validation reports a summary without touching output paths."""
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "not-created"
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = publish_bundle.main([
                    "--candidate", str(FIXTURES / "publisher-valid.json"),
                    "--output-dir", str(output_dir),
                ])
            self.assertEqual(result, 0)
            self.assertFalse(output_dir.exists())
            summary = json.loads(stdout.getvalue())
            self.assertFalse(summary["wrote"])
            self.assertEqual(summary["candidate_version"], "v5")

    def test_atomic_failure_restores_prior_pair_and_keeps_backup(self) -> None:
        """A manifest replace failure rolls back the newly replaced bundle."""
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            old_bundle = b'{"bundle_version":"v4"}\n'
            old_manifest = b'{"bundle_sha256":"old"}\n'
            old_backup = b'{"bundle_version":"v3"}\n'
            (output_dir / "zones.json").write_bytes(old_bundle)
            (output_dir / "manifest.json").write_bytes(old_manifest)
            (output_dir / "zones.json.bak").write_bytes(old_backup)
            real_replace = publish_bundle.os.replace
            calls = 0

            def fail_manifest_once(source: str, destination: str) -> None:
                """Fail exactly the third replace, then allow rollback to proceed."""
                nonlocal calls
                calls += 1
                if calls == 3:
                    raise OSError("simulated manifest replace failure")
                real_replace(source, destination)

            with mock.patch.object(publish_bundle.os, "replace", side_effect=fail_manifest_once):
                with self.assertRaises(OSError):
                    publish_bundle.publish_files(
                        output_dir,
                        *publish_bundle.build_artifacts(self.load_valid())[:2],
                    )
            self.assertEqual((output_dir / "zones.json").read_bytes(), old_bundle)
            self.assertEqual((output_dir / "manifest.json").read_bytes(), old_manifest)
            self.assertEqual((output_dir / "zones.json.bak").read_bytes(), old_backup)
            self.assertEqual(sorted(p.name for p in output_dir.iterdir()), [
                "manifest.json", "zones.json", "zones.json.bak"
            ])

    def test_atomic_publish_creates_bundle_manifest_and_last_known_good_backup(self) -> None:
        """Publish a complete pair and preserve previous zones as zones.json.bak."""
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            old_bundle = b'{"bundle_version":"v4"}\n'
            (output_dir / "zones.json").write_bytes(old_bundle)
            bundle, manifest, _ = publish_bundle.build_artifacts(self.load_valid())
            publish_bundle.publish_files(output_dir, bundle, manifest)
            self.assertEqual((output_dir / "zones.json").read_bytes(), bundle)
            self.assertEqual((output_dir / "zones.json.bak").read_bytes(), old_bundle)
            self.assertEqual((output_dir / "manifest.json").read_bytes(), manifest)

    def test_atomic_failure_before_bundle_replace_keeps_existing_backup(self) -> None:
        """Preserve prior artifacts even if backup preparation itself fails."""
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory)
            old_bundle = b'{"bundle_version":"v4"}\n'
            old_manifest = b'{"bundle_sha256":"old"}\n'
            old_backup = b'{"bundle_version":"v3"}\n'
            (output_dir / "zones.json").write_bytes(old_bundle)
            (output_dir / "manifest.json").write_bytes(old_manifest)
            (output_dir / "zones.json.bak").write_bytes(old_backup)
            real_replace = publish_bundle.os.replace

            def fail_backup(source: str, destination: str) -> None:
                """Fail on backup replacement before the visible bundle pair changes."""
                if Path(destination) == output_dir / "zones.json.bak":
                    raise OSError("simulated backup replace failure")
                real_replace(source, destination)

            with mock.patch.object(publish_bundle.os, "replace", side_effect=fail_backup):
                with self.assertRaises(OSError):
                    publish_bundle.publish_files(
                        output_dir,
                        *publish_bundle.build_artifacts(self.load_valid())[:2],
                    )
            self.assertEqual((output_dir / "zones.json").read_bytes(), old_bundle)
            self.assertEqual((output_dir / "manifest.json").read_bytes(), old_manifest)
            self.assertEqual((output_dir / "zones.json.bak").read_bytes(), old_backup)
            self.assertEqual(sorted(p.name for p in output_dir.iterdir()), [
                "manifest.json", "zones.json", "zones.json.bak"
            ])

    def test_bundle_without_optional_sections_remains_valid(self) -> None:
        """Retain the legacy-only schema-1 path when no sections catalogue is supplied."""
        candidate = json.loads((FIXTURES / "publisher-legacy-only.json").read_text(encoding="utf-8"))
        bundle, manifest_bytes, _ = publish_bundle.build_artifacts(candidate)
        payload = json.loads(bundle)
        self.assertNotIn("sections", payload)
        self.assertEqual(len(payload["zones"]), 1)
        self.assertEqual(json.loads(manifest_bytes)["counts"]["sections"], 0)

    def test_utc_timestamps_reject_date_only_and_nonzero_offsets(self) -> None:
        """Reject non-UTC timestamps that datetime.fromisoformat otherwise accepts."""
        for value in ("2026-10-24", "2026-10-24T12:00:00+01:00"):
            candidate = self.load_valid()
            candidate["generated_at"] = value
            with self.subTest(value=value), self.assertRaises(publish_bundle.ValidationError):
                publish_bundle.build_artifacts(candidate)

    def test_duplicate_zone_ids_and_nonfinite_coordinates_are_rejected(self) -> None:
        """Reject duplicate identifiers and coordinates outside finite numeric JSON."""
        candidate = self.load_valid()
        candidate["zones"].append(copy.deepcopy(candidate["zones"][0]))
        with self.assertRaises(publish_bundle.ValidationError):
            publish_bundle.build_artifacts(candidate)
        candidate = self.load_valid()
        candidate["zones"][0]["polyline"][0][0] = float("nan")
        with self.assertRaises(publish_bundle.ValidationError):
            publish_bundle.build_artifacts(candidate)

    def test_duplicate_section_keys_are_rejected(self) -> None:
        """Reject repeated section identity even if their roads differ."""
        candidate = self.load_valid()
        duplicate = copy.deepcopy(candidate["sections"][0])
        duplicate["road"] = "S-99"
        candidate["sections"].append(duplicate)
        with self.assertRaises(publish_bundle.ValidationError):
            publish_bundle.build_artifacts(candidate)


if __name__ == "__main__":
    unittest.main()
