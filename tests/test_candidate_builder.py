"""Tests for deterministic, fail-closed geometry/candidate assembly."""
from __future__ import annotations

import contextlib
import copy

import io
import json
import tempfile
import unittest
from pathlib import Path

from tools import candidate_builder, publish_bundle


CERTIFICATE_SHA = "a" * 64


def _coordinates(index: int) -> tuple[dict[str, float], dict[str, float]]:
    """Return distinct, exactly 0.01-degree endpoints for one synthetic section."""
    latitude = 42.0 + index * 0.02
    longitude = 23.0
    return ({"lat": latitude, "lon": longitude}, {"lat": latitude + 0.01, "lon": longitude})


def _direction(key: str, start: str, end: str, start_m: int, end_m: int, status: str) -> dict:
    """Create one verifier-shaped directional row summary."""
    return {
        "direction_key": key,
        "official_status": status,
        "start_label": start,
        "end_label": end,
        "start_chainage_m": start_m,
        "end_chainage_m": end_m,
        "declared_span_m": abs(end_m - start_m),
        "status": "CHAINAGE_OK",
        "reasons": [],
    }


def make_inputs(*, target_road: str = "I-1", target_labels: tuple[str, str] = ("Alpha", "Beta"), conflict: str | None = None) -> tuple[dict, str, dict]:
    """Build a complete 82-row/41-section Phase 2 report and matching synthetic KML."""
    rows: list[dict] = []
    sections: list[dict] = []
    line_records: list[tuple[str, str, str, str, list[dict[str, float]]]] = []
    evidence_images: list[dict] = []

    for index in range(41):
        section_key = "target" if index == 0 else f"dummy-{index:02d}"
        start_coord, end_coord = _coordinates(index)
        if index == 0:
            road_label = target_road
            road_code = {"I-1": "1", "A-2": "A-2"}.get(target_road, candidate_builder.map_road_code(target_road))
            start_label, end_label = target_labels
            forward_status = "CERTIFIED"
            reverse_status = "CERTIFIED"
            start_coord, end_coord = _coordinates(0)
        else:
            road_label, road_code = 'АМ "Тракия"', "A-1"
            start_label, end_label = f"Dummy {index} A", f"Dummy {index} B"
            forward_status = reverse_status = "PENDING_VERIFICATION"

        if road_code is None:
            raise AssertionError(f"unsupported test road {target_road}")
        forward_key, reverse_key = f"{section_key}:forward", f"{section_key}:reverse"
        dirs = [
            _direction(forward_key, start_label, end_label, 0, 1000, forward_status),
            _direction(reverse_key, end_label, start_label, 1000, 0, reverse_status),
        ]
        section_reasons = [conflict] if index == 0 and conflict else []
        sections.append({
            "section_key": section_key,
            "road_label": road_label,
            "road_code": road_code,
            "endpoints": [start_label, end_label],
            "declared_span_m": 1000,
            "mapped_span_m": 1111.95,
            "chainage_status": "CHAINAGE_OK",
            "directions": dirs,
            "ria_alignment": "OK",
            "ria_road_link_ids": [road_code],
            "ria_median_distance_m": 0.0,
            "status": "QUARANTINE" if section_reasons else "PASS",
            "reasons": section_reasons,
            "requires": [],
        })

        for direction_key, direction, start_point, end_point in (
            (forward_key, dirs[0], start_coord, end_coord),
            (reverse_key, dirs[1], end_coord, start_coord),
        ):
            rows.append({
                "direction_key": direction_key,
                "section_key": section_key,
                "official_status": direction["official_status"],
                "raw_labels": {"road": road_label, "start": direction["start_label"], "end": direction["end_label"]},
                "road_code": road_code,
                "chainage_m": {"start": direction["start_chainage_m"], "end": direction["end_chainage_m"]},
                "declared_span_m": 1000,
                "mapped_coordinates": {"start": start_point, "end": end_point},
                "endpoint_audit": {"start": {"status": "CHAINAGE_OK"}, "end": {"status": "CHAINAGE_OK"}},
                "coordinate_distance_m": 1111.95,
                "status": "CHAINAGE_OK",
                "reasons": [],
            })

        line_records.append((section_key, road_code, start_label, end_label, [start_coord, end_coord]))
        if index == 0:
            for direction_key in (forward_key, reverse_key):
                evidence_images.append({
                    "direction_key_claim": direction_key,
                    "status": "SINGLE_CALLOUT",
                    "candidate_cert_len_m": 1112,
                    "image_sha256": CERTIFICATE_SHA,
                    "image_path": f"/tmp/{direction_key}.jpg",
                    "cert_url": f"https://example.invalid/{direction_key}.jpg",
                })

    kml_lines = [
        _kml_line(f"{start_label} - {end_label}", road_code, points)
        for _, road_code, start_label, end_label, points in line_records
    ]
    if conflict == "KNOWN_CONFLICT_C1":
        kml_lines[0] = _kml_line("Жеглица - Срацимирово", "1", list(_coordinates(0)))
    elif conflict == "KNOWN_CONFLICT_C2":
        kml_lines[0] = _kml_line("Девня - Игнатиево", "A-2", list(_coordinates(0)))
    kml = """<?xml version="1.0" encoding="UTF-8"?>
<kml xmlns="http://www.opengis.net/kml/2.2"><Document>
<Placemark><name>camera, not a zone</name><Point><coordinates>23,42,0</coordinates></Point></Placemark>
""" + "\n".join(kml_lines) + "</Document></kml>"
    verification = {
        "generated_at": "2026-10-24T12:00:00Z",
        "source_path": "/archive/bgtoll.html",
        "source_sha256": "b" * 64,
        "km_points_path": "/archive/km_points.json",
        "km_points_sha256": "c" * 64,
        "ria_road_links_path": "/archive/roadlinks.gml",
        "ria_road_links_sha256": "d" * 64,
        "summary": {"rows": 82, "sections": 41},
        "rows": rows,
        "sections": sections,
    }
    evidence = {"generated_at": "2026-10-24T12:00:00Z", "engine": "tesseract", "images": evidence_images}
    return verification, kml, evidence


def _kml_line(name: str, road_code: str, points: list[dict[str, float]]) -> str:
    """Serialize a synthetic KML line with a road identity in its description."""
    coordinates = " ".join(f"{point['lon']},{point['lat']},0" for point in points)
    return (
        "<Placemark>"
        f"<name>{name}</name><description><![CDATA[Път {road_code}<br>Категория А,А2,В - 90 km/h]]></description>"
        f"<LineString><coordinates>{coordinates}</coordinates></LineString>"
        "</Placemark>"
    )


def _build(verification: dict, kml: str, evidence: dict | None = None, **kwargs) -> dict:
    """Build a candidate from the synthetic documents with a fixed generation time."""
    return candidate_builder.build_candidate(
        verification,
        kml,
        certificate_evidence=evidence,
        generated_at="2026-10-24T13:00:00Z",
        **kwargs,
    )


class CandidateBuilderTests(unittest.TestCase):
    """Exercise matching, reversal, provenance, release gates, and determinism."""

    def test_correct_match_normalizes_orientation_and_preserves_source_order(self) -> None:
        """Match by road and mapped endpoints, then reverse vertices to low chainage first."""
        verification, kml, evidence = make_inputs()
        # Reverse only the target LineString; do not alter its labels or its road code.
        target = _kml_line("Alpha - Beta", "1", [_coordinates(0)[1], _coordinates(0)[0]])
        kml = kml.replace(_kml_line("Alpha - Beta", "1", list(_coordinates(0))), target)
        candidate = _build(verification, kml, evidence)
        section = next(item for item in candidate["sections"] if item["section_key"] == "target")
        geometry = section["geometry"]
        self.assertEqual(candidate["geometry_inventory"]["line_count"], 41)
        self.assertEqual(geometry["source_line_index"], 0)
        self.assertTrue(geometry["source_reversed"])
        self.assertEqual(geometry["source_vertices"][0][:2], [23.0, 42.01])
        self.assertAlmostEqual(geometry["normalized_polyline"][0][0], 42.0)
        self.assertAlmostEqual(geometry["normalized_polyline"][-1][0], 42.01)
        self.assertGreater(geometry["geometry_len_m"], 1110)
        self.assertEqual(section["span_m"], 1000)
        self.assertEqual(section["directions"]["forward"]["cert_len_m"], 1112)
        self.assertEqual(section["directions"]["forward"]["release_state"], "ACTIVE")
        self.assertEqual(len(candidate["zones"]), 1)

    def test_duplicate_line_for_one_section_is_quarantined(self) -> None:
        """Reject two lines matching one physical section without explicit mapping."""
        verification, kml, evidence = make_inputs()
        duplicate = _kml_line("Alpha - Beta", "1", list(_coordinates(0)))
        candidate = _build(verification, kml.replace("</Document>", duplicate + "</Document>"), evidence)
        section = next(item for item in candidate["sections"] if item["section_key"] == "target")
        self.assertEqual(section["release_state"], "QUARANTINED")
        self.assertIn("DUPLICATE_LINES_FOR_SECTION", section["reasons"])
        self.assertEqual(candidate["zones"], [])
        self.assertEqual(candidate["geometry_inventory"]["duplicate_line_count"], 2)

    def test_unmatched_extra_line_is_held_and_blocks_legacy_projection(self) -> None:
        """Retain a road/coordinate-unmatched line as a quarantine reconciliation issue."""
        verification, kml, evidence = make_inputs()
        extra = _kml_line("Unknown road", "II-99", [{"lat": 48.0, "lon": 27.0}, {"lat": 48.01, "lon": 27.0}])
        candidate = _build(verification, kml.replace("</Document>", extra + "</Document>"), evidence)
        self.assertEqual(candidate["geometry_inventory"]["unmatched_line_count"], 1)
        self.assertEqual(candidate["geometry_inventory"]["reconciliation_status"], "QUARANTINE")
        self.assertEqual(candidate["zones"], [])

    def test_missing_certificate_evidence_holds_direction(self) -> None:
        """Never infer certificate length when the explicit direction binding is absent."""
        verification, kml, evidence = make_inputs()
        evidence["images"] = [image for image in evidence["images"] if image["direction_key_claim"] != "target:reverse"]
        candidate = _build(verification, kml, evidence)
        target = next(item for item in candidate["sections"] if item["section_key"] == "target")
        self.assertEqual(target["directions"]["reverse"]["cert_len_m"], None)
        self.assertEqual(target["directions"]["reverse"]["gates"]["length"], "REVIEW")
        self.assertEqual(target["release_state"], "UNDER_VERIFICATION")
        self.assertEqual(candidate["zones"], [])

    def test_geometry_length_mismatch_over_two_percent_is_quarantined(self) -> None:
        """Use certificate callout only, and quarantine a >2% geometry discrepancy."""
        verification, kml, evidence = make_inputs()
        for image in evidence["images"]:
            image["candidate_cert_len_m"] = 1200
        candidate = _build(verification, kml, evidence)
        target = next(item for item in candidate["sections"] if item["section_key"] == "target")
        self.assertEqual(target["directions"]["forward"]["gates"]["length"], "QUARANTINE")
        self.assertEqual(target["directions"]["forward"]["release_state"], "QUARANTINED")
        self.assertEqual(candidate["zones"], [])

    def test_c1_and_c2_are_explicit_quarantines(self) -> None:
        """Keep both known source conflicts quarantined without inferring a correction."""
        for road, labels, conflict in (
            ("I-1", ("Жеглица", "Срацимирово"), "KNOWN_CONFLICT_C1"),
            ("A-2", ("Девня", "Игнатиево"), "KNOWN_CONFLICT_C2"),
        ):
            with self.subTest(conflict=conflict):
                verification, kml, evidence = make_inputs(target_road=road, target_labels=labels, conflict=conflict)
                candidate = _build(verification, kml, evidence)
                target = next(item for item in candidate["sections"] if item["section_key"] == "target")
                self.assertEqual(target["release_state"], "QUARANTINED")
                self.assertIn(conflict, target["reasons"])
                self.assertEqual(candidate["zones"], [])

    def test_legacy_zone_requires_both_directions_to_pass(self) -> None:
        """Project only a two-direction, certified, active, evidence-bound section."""
        verification, kml, evidence = make_inputs()
        candidate = _build(verification, kml, evidence)
        target = next(item for item in candidate["sections"] if item["section_key"] == "target")
        self.assertEqual([zone["id"] for zone in candidate["zones"]], ["target"], repr(target["directions"]))
        bundle, _, summary = publish_bundle.build_artifacts(candidate)
        self.assertEqual(json.loads(bundle)["zones"][0]["id"], "target")
        self.assertEqual(summary["zone_count"], 1)
        verification, kml, evidence = make_inputs()
        evidence["images"] = [image for image in evidence["images"] if image["direction_key_claim"] != "target:reverse"]
        held = _build(verification, kml, evidence)
        self.assertEqual(held["zones"], [])

    def test_pending_direction_never_becomes_active(self) -> None:
        """Preserve official pending state even if matching certificate evidence exists."""
        verification, kml, evidence = make_inputs()
        target = next(section for section in verification["sections"] if section["section_key"] == "target")
        reverse_key = target["directions"][1]["direction_key"]
        target["directions"][1]["official_status"] = "PENDING_VERIFICATION"
        row = next(row for row in verification["rows"] if row["direction_key"] == reverse_key)
        row["official_status"] = "PENDING_VERIFICATION"
        candidate = _build(verification, kml, evidence)
        section = next(item for item in candidate["sections"] if item["section_key"] == "target")
        self.assertEqual(section["directions"]["reverse"]["status"], "PENDING_VERIFICATION")
        self.assertNotEqual(section["directions"]["reverse"]["release_state"], "ACTIVE")
        self.assertEqual(candidate["zones"], [])

    def test_row_section_official_status_conflict_is_quarantined(self) -> None:
        """Do not promote a pending Phase 2 row from a conflicting section summary."""
        verification, kml, evidence = make_inputs()
        target = next(section for section in verification["sections"] if section["section_key"] == "target")
        reverse_key = target["directions"][1]["direction_key"]
        row = next(row for row in verification["rows"] if row["direction_key"] == reverse_key)
        row["official_status"] = "PENDING_VERIFICATION"

        candidate = _build(verification, kml, evidence)
        section = next(item for item in candidate["sections"] if item["section_key"] == "target")
        reverse = section["directions"]["reverse"]
        self.assertEqual(reverse["status"], "PENDING_VERIFICATION")
        self.assertEqual(reverse["release_state"], "QUARANTINED")
        self.assertIn("OFFICIAL_STATUS_CONFLICT", reverse["gate_details"]["reasons"])
        self.assertEqual(candidate["zones"], [])

    def test_ambiguous_or_integer_certificate_evidence_is_held(self) -> None:
        """Require exactly SINGLE_CALLOUT evidence plus an exact direction claim."""
        for status in ("AMBIGUOUS", "UNREADABLE", "INTEGER_REVIEW"):
            with self.subTest(status=status):
                verification, kml, evidence = make_inputs()
                evidence["images"][0]["status"] = status
                candidate = _build(verification, kml, evidence)
                target = next(item for item in candidate["sections"] if item["section_key"] == "target")
                self.assertIsNone(target["directions"]["forward"]["cert_len_m"])
                self.assertNotEqual(target["directions"]["forward"]["release_state"], "ACTIVE")

    def test_endpoint_review_and_quarantine_bands_are_applied(self) -> None:
        """Keep 150–500 m endpoints in review and endpoints over 500 m quarantined."""
        verification, kml, evidence = make_inputs()
        # Shift the target line by approximately 200 m north while preserving road identity.
        offset = 200 / 111195
        shifted = _kml_line(
            "Alpha - Beta", "1",
            [{"lat": 42.0 + offset, "lon": 23.0}, {"lat": 42.01 + offset, "lon": 23.0}],
        )
        original = _kml_line("Alpha - Beta", "1", list(_coordinates(0)))
        candidate = _build(verification, kml.replace(original, shifted), evidence)
        target = next(item for item in candidate["sections"] if item["section_key"] == "target")
        self.assertEqual(target["directions"]["forward"]["gates"]["chainage"], "REVIEW")
        far = _kml_line(
            "Alpha - Beta", "1",
            [{"lat": 42.0 + 0.006, "lon": 23.0}, {"lat": 42.01 + 0.006, "lon": 23.0}],
        )
        candidate = _build(verification, kml.replace(original, far), evidence)
        target = next(item for item in candidate["sections"] if item["section_key"] == "target")
        self.assertEqual(target["release_state"], "QUARANTINED")
        self.assertEqual(candidate["zones"], [])

    def test_unverified_ria_never_passes(self) -> None:
        """Treat absent RIA alignment as REVIEW, not PASS or ACTIVE."""
        verification, kml, evidence = make_inputs()
        section = next(item for item in verification["sections"] if item["section_key"] == "target")
        section["ria_alignment"] = "UNVERIFIED"
        candidate = _build(verification, kml, evidence)
        target = next(item for item in candidate["sections"] if item["section_key"] == "target")
        self.assertEqual(target["directions"]["forward"]["gates"]["ria"], "REVIEW")
        self.assertNotEqual(target["directions"]["forward"]["release_state"], "ACTIVE")

    def test_output_is_deterministic_for_fixed_generation_timestamp(self) -> None:
        """Serialize identical evidence to identical candidate objects and bytes."""
        verification, kml, evidence = make_inputs()
        first = _build(verification, kml, evidence)
        second = _build(copy.deepcopy(verification), str(kml), copy.deepcopy(evidence))
        self.assertEqual(candidate_builder.canonical_json(first), candidate_builder.canonical_json(second))

    def test_cli_dry_run_does_not_write_and_write_refuses_zones_json(self) -> None:
        """Keep the CLI dry by default and forbid overwriting the legacy bundle name."""
        verification, kml, evidence = make_inputs()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            verification_path = root / "verification.json"
            kml_path = root / "candidate.kml"
            evidence_path = root / "certificates.json"
            output_path = root / "candidate-v5.json"
            verification_path.write_text(json.dumps(verification), encoding="utf-8")
            kml_path.write_text(kml, encoding="utf-8")
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                result = candidate_builder.main([
                    "--verification", str(verification_path), "--kml", str(kml_path),
                    "--certificate-evidence", str(evidence_path), "--output", str(output_path),
                ])
            self.assertEqual(result, 0)
            self.assertFalse(output_path.exists())
            summary = json.loads(stdout.getvalue())
            self.assertFalse(summary["wrote"])
            self.assertEqual(summary["section_count"], 41)
            self.assertEqual(candidate_builder.main([
                "--verification", str(verification_path), "--kml", str(kml_path),
                "--certificate-evidence", str(evidence_path), "--output", str(root / "zones.json"), "--write",
            ]), 2)

    def test_phase_two_counts_must_remain_82_rows_and_41_sections(self) -> None:
        """Stop on an inventory count drift instead of silently accepting a new baseline."""
        verification, kml, evidence = make_inputs()
        verification["summary"]["rows"] = 81
        with self.assertRaisesRegex(ValueError, "82 rows/41 sections"):
            _build(verification, kml, evidence)


if __name__ == "__main__":
    unittest.main()
