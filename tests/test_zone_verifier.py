"""Tests for the official chainage and RIA endpoint verifier."""
from __future__ import annotations

import json
import math
import tempfile
import unittest
from pathlib import Path

from tools.bgtoll_inventory import parse_inventory
from tools.zone_verifier import ChainageIndex, build_report, load_ria_road_links, map_road_code, verify_inventory


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "data" / "bgtoll-vaprosi-i-otgovori.html"


def paired_inventory(road: str = "I-1", start: str = "Alpha (км 0+000)", end: str = "Beta (км 1+000)") -> dict:
    """Return a minimal Phase 1 inventory containing two opposite directions."""
    source = f"""<!doctype html><html><body>
<h3>Сертифицирани отсечки за средна скорост:</h3>
<table><tr><th>Път</th><th>Начална точка</th><th>Крайна точка</th><th>Карта (изображение)</th></tr>
<tr><td>{road}</td><td>{start}</td><td>{end}</td><td></td></tr>
<tr><td>{road}</td><td>{end}</td><td>{start}</td><td></td></tr></table>
<h3>Отсечки, за които предстои последваща метрологична проверка:</h3>
<table><tr><th>Път</th><th>Начална точка</th><th>Крайна точка</th><th>Карта (изображение)</th></tr>
<tr><td>III-8407</td><td>Gamma (км 0+000)</td><td>Delta (км 1+000)</td><td></td></tr></table>
</body></html>"""
    parsed = parse_inventory(source)
    selected_rows = [row for row in parsed["rows"] if row["road_label"] == road]
    selected_keys = {row["section_key"] for row in selected_rows}
    return {"rows": selected_rows, "sections": [section for section in parsed["sections"] if section["section_key"] in selected_keys]}


def ria_xml(local_id: str, coordinates: tuple[tuple[float, float], ...]) -> str:
    """Serialize a tiny RIA RoadLink with EPSG:4258 latitude/longitude pairs."""
    positions = " ".join(f"{lat} {lon}" for lat, lon in coordinates)
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0"
 xmlns:tn-ro="http://inspire.ec.europa.eu/schemas/tn-ro/4.0"
 xmlns:net="http://inspire.ec.europa.eu/schemas/net/4.0"
 xmlns:base="http://inspire.ec.europa.eu/schemas/base/3.3"
 xmlns:gml="http://www.opengis.net/gml/3.2">
 <wfs:member><tn-ro:RoadLink><net:inspireId><base:Identifier>
  <base:localId>{local_id}</base:localId>
 </base:Identifier></net:inspireId><net:centrelineGeometry>
  <gml:LineString srsName="http://www.opengis.net/def/crs/EPSG/0/4258" srsDimension="2">
   <gml:posList>{positions}</gml:posList>
  </gml:LineString></net:centrelineGeometry></tn-ro:RoadLink></wfs:member>
</wfs:FeatureCollection>"""


class RoadCodeTests(unittest.TestCase):
    """Check every supported official road label form and fail-closed unknowns."""

    def test_all_twelve_supported_road_label_mappings(self) -> None:
        """Map motorway labels and all three numbered-road forms to API RoadID."""
        expected = {
            'АМ "Тракия"': "A-1",
            'АМ "Хемус"': "A-2",
            'АМ "Струма"': "A-3",
            'АМ "Марица"': "A-4",
            'АМ "Черно море"': "A-5",
            'АМ "Европа" (Северна скоростна тангента)': "A-6",
            "I-1": "1",
            "I-9": "9",
            "II-21": "21",
            "II-99": "99",
            "III-8407": "8407",
            "III-1000": "1000",
        }
        for label, code in expected.items():
            with self.subTest(label=label):
                self.assertEqual(map_road_code(label), code)

    def test_unknown_or_malformed_labels_are_not_guessed(self) -> None:
        """Reject unsupported formatting and road classes rather than infer IDs."""
        for label in ("I-10", "II-1", "III-123", "IV-1234", 'АМ "Европа"'):
            with self.subTest(label=label):
                self.assertIsNone(map_road_code(label))


class ChainageIndexTests(unittest.TestCase):
    """Exercise validated chainage records, interpolation, and ambiguity guards."""

    def setUp(self) -> None:
        """Create a two-point road whose interpolation is easy to audit."""
        self.index = ChainageIndex([
            ["1", 0, 42.0, 23.0],
            ["1", 1000, 42.01, 23.0],
        ])

    def test_exact_known_point_is_returned_without_extrapolation(self) -> None:
        """Return exact source coordinates and a zero residual at a known marker."""
        result = self.index.interpolate("1", 1000)
        self.assertEqual(result["status"], "CHAINAGE_OK")
        self.assertEqual(result["coordinates"], {"lat": 42.01, "lon": 23.0})
        self.assertEqual(result["bracketing_km_m"], [1000, 1000])
        self.assertEqual(result["residual_m"], 0)

    def test_between_markers_is_linearly_interpolated_with_audit_bracket(self) -> None:
        """Interpolate between bracketing points and report fraction/residual."""
        result = self.index.interpolate("1", 250)
        self.assertEqual(result["status"], "CHAINAGE_OK")
        self.assertAlmostEqual(result["coordinates"]["lat"], 42.0025)
        self.assertAlmostEqual(result["coordinates"]["lon"], 23.0)
        self.assertEqual(result["bracketing_km_m"], [0, 1000])
        self.assertEqual(result["fraction"], 0.25)
        self.assertEqual(result["residual_m"], 250)

    def test_outside_road_coverage_is_unverified(self) -> None:
        """Never extrapolate before the first or beyond the last known road marker."""
        for km_m in (-1, 1001):
            with self.subTest(km_m=km_m):
                self.assertEqual(self.index.interpolate("1", km_m)["status"], "CHAINAGE_UNVERIFIED")

    def test_identical_duplicate_records_are_deduplicated(self) -> None:
        """Accept repeated identical records as one source marker."""
        index = ChainageIndex([["1", 0, 42.0, 23.0], ["1", 0, 42.0, 23.0]])
        self.assertEqual(index.interpolate("1", 0)["status"], "CHAINAGE_OK")
        self.assertEqual(len(index.points["1"]), 1)

    def test_conflicting_coordinates_at_one_marker_are_ambiguous(self) -> None:
        """Refuse to choose between distinct coordinates for the same road and km."""
        index = ChainageIndex([
            ["1", 0, 42.0, 23.0], ["1", 1000, 42.01, 23.0],
            ["1", 1000, 42.02, 23.0], ["1", 2000, 42.03, 23.0],
        ])
        self.assertEqual(index.interpolate("1", 1000)["status"], "CHAINAGE_AMBIGUOUS")
        self.assertEqual(index.interpolate("1", 1500)["status"], "CHAINAGE_AMBIGUOUS")
        self.assertEqual(index.interpolate("1", 0)["status"], "CHAINAGE_OK")

    def test_implausibly_wide_bracket_is_a_gap(self) -> None:
        """Do not interpolate over a sparse gap beyond the documented limit."""
        index = ChainageIndex([["1", 0, 42.0, 23.0], ["1", 3000, 42.03, 23.0]])
        self.assertEqual(index.interpolate("1", 1500)["status"], "CHAINAGE_GAP")

    def test_invalid_records_are_rejected(self) -> None:
        """Reject malformed and out-of-range point records at load time."""
        for record in (["1", 0, 91, 23], ["1", -1, 42, 23], ["", 0, 42, 23], ["1", 0, math.nan, 23]):
            with self.subTest(record=record):
                with self.assertRaises(ValueError):
                    ChainageIndex([record])


class InventoryVerificationTests(unittest.TestCase):
    """Validate the archived official inventory and its section-level result."""

    @classmethod
    def setUpClass(cls) -> None:
        """Parse the reviewed snapshot and load its official chainage points once."""
        cls.inventory = parse_inventory(ARCHIVE.read_bytes())
        cls.chainage = ChainageIndex.from_file(ROOT / "data" / "reference" / "km_points.json")
        cls.result = verify_inventory(cls.inventory, cls.chainage)

    def test_archived_snapshot_has_expected_mapping_and_coverage_counts(self) -> None:
        """Retain the measured 82/41/78/4/39/2 coverage without offset fixes."""
        summary = self.result["summary"]
        self.assertEqual((summary["rows"], summary["sections"]), (82, 41))
        self.assertEqual((summary["mapped_rows"], summary["unresolved_rows"]), (78, 4))
        self.assertEqual((summary["chainage_ok_sections"], summary["chainage_unverified_sections"]), (39, 2))
        self.assertEqual(sum(row["status"] == "CHAINAGE_OK" for row in self.result["rows"]), 78)
        unresolved = [row for row in self.result["rows"] if row["status"] != "CHAINAGE_OK"]
        self.assertEqual(len(unresolved), 4)
        self.assertTrue(all(row["status"] == "CHAINAGE_UNVERIFIED" for row in unresolved))

    def test_c1_and_c2_are_explicit_quarantines(self) -> None:
        """Carry both known disputes as named quarantine reasons without resolving them."""
        sections = self.result["sections"]
        c1 = [section for section in sections if "KNOWN_CONFLICT_C1" in section["reasons"]]
        c2 = [section for section in sections if "KNOWN_CONFLICT_C2" in section["reasons"]]
        self.assertEqual(len(c1), 1)
        self.assertEqual(len(c2), 1)
        self.assertEqual(c1[0]["status"], "QUARANTINE")
        self.assertEqual(c2[0]["status"], "QUARANTINE")

    def test_absent_ria_keeps_all_non_quarantined_sections_in_review(self) -> None:
        """Missing optional RIA evidence is explicitly unverified and never a PASS."""
        self.assertTrue(all(section["ria_alignment"] == "UNVERIFIED" for section in self.result["sections"]))
        self.assertTrue(all(section["status"] != "PASS" for section in self.result["sections"]))
        self.assertTrue(all(
            "RIA_ALIGNMENT" in section["requires"]
            for section in self.result["sections"]
            if section["status"] == "REVIEW" and section["chainage_status"] == "CHAINAGE_OK"
        ))
        self.assertEqual(self.result["summary"]["review_sections"], 39)
        self.assertEqual(self.result["summary"]["quarantine_sections"], 2)

    def test_bad_ria_file_keeps_every_section_unverified(self) -> None:
        """Treat malformed optional GML as unverified rather than accepting a pass."""
        with tempfile.TemporaryDirectory() as directory:
            bad_gml = Path(directory) / "broken.gml"
            bad_gml.write_text("<not-closed>", encoding="utf-8")
            report = build_report(ARCHIVE, ROOT / "data" / "reference" / "km_points.json", bad_gml)
        self.assertIsNotNone(report["ria_error"])
        self.assertTrue(all(section["ria_alignment"] == "UNVERIFIED" for section in report["sections"]))
        self.assertTrue(all(section["status"] != "PASS" for section in report["sections"]))

    def test_empty_parsed_ria_collection_reports_road_not_found(self) -> None:
        """Treat a valid but empty RoadLink collection as a missing road reference."""
        inventory = paired_inventory()
        chainage = ChainageIndex([["1", 0, 42.0, 23.0], ["1", 1000, 42.01, 23.0]])
        result = verify_inventory(inventory, chainage, ria_links={})
        self.assertEqual(result["sections"][0]["ria_alignment"], "ROAD_NOT_FOUND")
        self.assertIn("RIA_ROAD_NOT_FOUND", result["sections"][0]["reasons"])

    def test_rows_preserve_source_labels_chainage_and_mapped_coordinates(self) -> None:
        """Expose source raw text unchanged alongside coordinates and source brackets."""
        original = self.inventory["rows"][0]
        verified = next(row for row in self.result["rows"] if row["direction_key"] == original["direction_key"])
        self.assertEqual(verified["raw_labels"]["road"], original["road_raw"])
        self.assertEqual(verified["raw_labels"]["start"], original["start_raw"])
        self.assertEqual(verified["chainage_m"]["start"], original["start_chainage_m"])
        if verified["status"] == "CHAINAGE_OK":
            self.assertIsNotNone(verified["mapped_coordinates"]["start"])
            self.assertIn("bracketing_km_m", verified["endpoint_audit"]["start"])

    def test_direction_span_mismatch_is_flagged(self) -> None:
        """Flag reciprocal named endpoints whose declared spans differ by more than 2 m."""
        inventory = paired_inventory()
        section_key = inventory["rows"][0]["section_key"]
        inventory["rows"][0]["end_chainage_m"] += 3
        chainage = ChainageIndex([["1", 0, 42, 23], ["1", 1000, 42.01, 23]])
        result = verify_inventory(inventory, chainage)
        section = next(item for item in result["sections"] if item["section_key"] == section_key)
        self.assertIn("DIRECTION_MISMATCH", section["reasons"])
        self.assertEqual(section["status"], "REVIEW")


class RiaAlignmentTests(unittest.TestCase):
    """Exercise standard-library GML parsing and the endpoint alignment threshold."""

    def verify_with_gml(self, coordinates: tuple[tuple[float, float], ...], local_id: str = "I-1") -> dict:
        """Parse a temporary synthetic RoadLink and verify its two BGTOLL directions."""
        inventory = paired_inventory()
        chainage = ChainageIndex([["1", 0, 42.0, 23.0], ["1", 1000, 42.01, 23.0]])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "roadlinks.gml"
            path.write_text(ria_xml(local_id, coordinates), encoding="utf-8")
            links = load_ria_road_links(path)
        return verify_inventory(inventory, chainage, ria_links=links)

    def test_synthetic_gml_aligned_centerline_passes_ria_gate(self) -> None:
        """Match the RoadLink by road code and measure both endpoint distances."""
        result = self.verify_with_gml(((42.0, 23.0), (42.005, 23.0), (42.01, 23.0)))
        section = result["sections"][0]
        self.assertEqual(section["ria_alignment"], "OK")
        self.assertLessEqual(section["ria_median_distance_m"], 1)
        self.assertEqual(section["status"], "PASS")
        self.assertEqual(section["requires"], [])

    def test_committed_synthetic_gml_fixture_is_parsed(self) -> None:
        """Parse a small checked-in GML fixture without network access."""
        links = load_ria_road_links(ROOT / "tests" / "fixtures" / "roadlinks-aligned.gml")
        self.assertIn("I-1", links)
        self.assertEqual(len(links["I-1"]), 1)
        self.assertEqual(len(links["I-1"][0]), 3)

    def test_synthetic_gml_matches_inspire_road_name_suffix(self) -> None:
        """Match canonical road code prefixes in INSPIRE IDs that include metadata suffixes."""
        result = self.verify_with_gml(((42.0, 23.0), (42.01, 23.0)), "I-1 (E-79)")
        self.assertEqual(result["sections"][0]["ria_alignment"], "OK")
        self.assertEqual(result["sections"][0]["ria_road_link_ids"], ["I-1 (E-79)"])

    def test_synthetic_gml_does_not_match_road_code_prefix_collision(self) -> None:
        """Reject a similarly named local link with an extra road-class suffix."""
        result = self.verify_with_gml(((42.0, 23.0), (42.01, 23.0)), "I-1L нов")
        self.assertEqual(result["sections"][0]["ria_alignment"], "ROAD_NOT_FOUND")
        self.assertEqual(result["sections"][0]["ria_road_link_ids"], [])

    def test_synthetic_gml_mismatch_and_missing_road_are_reviewed(self) -> None:
        """Reject a distant centerline and explicitly distinguish a missing road link."""
        far = self.verify_with_gml(((42.2, 23.2), (42.21, 23.2)))
        self.assertEqual(far["sections"][0]["ria_alignment"], "MISMATCH")
        self.assertEqual(far["sections"][0]["status"], "REVIEW")

        inventory = paired_inventory()
        chainage = ChainageIndex([["1", 0, 42.0, 23.0], ["1", 1000, 42.01, 23.0]])
        result = verify_inventory(inventory, chainage, ria_links={})
        self.assertEqual(result["sections"][0]["ria_alignment"], "ROAD_NOT_FOUND")
        self.assertIn("RIA_ROAD_NOT_FOUND", result["sections"][0]["reasons"])


if __name__ == "__main__":
    unittest.main()
