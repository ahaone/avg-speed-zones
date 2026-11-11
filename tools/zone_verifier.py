#!/usr/bin/env python3
"""Verify BGTOLL endpoint chainage against АПИ points and optional RIA links."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
from typing import Any, Iterable
import xml.etree.ElementTree as ET

try:
    from .bgtoll_inventory import parse_inventory
except ImportError:
    from bgtoll_inventory import parse_inventory


ROAD_LABELS = {
    'АМ "Тракия"': "A-1",
    'АМ "Хемус"': "A-2",
    'АМ "Струма"': "A-3",
    'АМ "Марица"': "A-4",
    'АМ "Черно море"': "A-5",
    'АМ "Европа" (Северна скоростна тангента)': "A-6",
}
_NUMBERED_ROAD_RE = re.compile(r"^(I|II|III)-(\d+)$")
_CHAINAGE_RE = re.compile(r"км\s*\d+\s*\+\s*\d{1,3}", re.IGNORECASE)
_REPAIR_RE = re.compile(r"/Пътен участък в ремонт/", re.IGNORECASE)
EARTH_RADIUS_M = 6_371_008.8
MAX_CHAINAGE_GAP_M = 2_000  # Permit at most two missing 1-km intervals before refusing interpolation.
RIA_ALIGNMENT_TOLERANCE_M = 250  # Provisional endpoint-to-centreline review threshold from the task contract.


def map_road_code(label: str) -> str | None:
    """Map an exact official road label to АПИ RoadID; unknown labels fail closed."""
    if label in ROAD_LABELS:
        return ROAD_LABELS[label]
    match = _NUMBERED_ROAD_RE.fullmatch(label.strip())
    if match is None:
        return None
    prefix, number = match.groups()
    expected_digits = {"I": 1, "II": 2, "III": 4}[prefix]
    if len(number) != expected_digits:
        return None
    return number


def _finite_number(value: Any, field: str) -> float:
    """Convert a JSON numeric field to a finite float, rejecting booleans and NaN."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _haversine_m(a: dict[str, float], b: dict[str, float]) -> float:
    """Return the great-circle distance in metres between two latitude/longitude pairs."""
    lat1, lon1 = math.radians(a["lat"]), math.radians(a["lon"])
    lat2, lon2 = math.radians(b["lat"]), math.radians(b["lon"])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(value)))


class ChainageIndex:
    """Validate АПИ km-point records and interpolate only across plausible brackets."""

    def __init__(self, records: Iterable[list[Any]], max_gap_m: int = MAX_CHAINAGE_GAP_M) -> None:
        """Load [RoadID, km_m, lat, lon] rows and retain conflicting duplicate markers."""
        if isinstance(max_gap_m, bool) or not isinstance(max_gap_m, int) or max_gap_m <= 0:
            raise ValueError("max_gap_m must be a positive integer")
        self.max_gap_m = max_gap_m
        gathered: dict[str, dict[int, set[tuple[float, float]]]] = defaultdict(lambda: defaultdict(set))
        for ordinal, record in enumerate(records):
            if not isinstance(record, (list, tuple)) or len(record) != 4:
                raise ValueError(f"chainage record {ordinal} must have four fields")
            road_value, km_value, lat_value, lon_value = record
            if not isinstance(road_value, (str, int)) or isinstance(road_value, bool):
                raise ValueError(f"chainage record {ordinal} has an invalid RoadID")
            road_id = str(road_value).strip()
            if not road_id:
                raise ValueError(f"chainage record {ordinal} has an empty RoadID")
            if isinstance(km_value, bool) or not isinstance(km_value, int) or km_value < 0:
                raise ValueError(f"chainage record {ordinal} has invalid km_m")
            lat = _finite_number(lat_value, f"record {ordinal} latitude")
            lon = _finite_number(lon_value, f"record {ordinal} longitude")
            if not -90 <= lat <= 90 or not -180 <= lon <= 180:
                raise ValueError(f"chainage record {ordinal} has coordinates outside WGS84 ranges")
            gathered[road_id][km_value].add((lat, lon))
        if not gathered:
            raise ValueError("chainage point input is empty")
        self.points = {
            road: {km: tuple(sorted(coords)) for km, coords in sorted(markers.items())}
            for road, markers in gathered.items()
        }

    @classmethod
    def from_file(cls, path: Path, max_gap_m: int = MAX_CHAINAGE_GAP_M) -> "ChainageIndex":
        """Read and validate a UTF-8 JSON array of official chainage point records."""
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise ValueError(f"cannot read chainage points: {error}") from error
        if not isinstance(payload, list):
            raise ValueError("chainage point document must be a JSON array")
        return cls(payload, max_gap_m=max_gap_m)

    def interpolate(self, road_id: str, km_m: int) -> dict[str, Any]:
        """Return exact/interpolated WGS84 coordinates or an explicit fail-closed status."""
        if isinstance(km_m, bool) or not isinstance(km_m, int):
            raise ValueError("requested km_m must be an integer")
        markers = self.points.get(str(road_id))
        if not markers:
            return {"status": "CHAINAGE_UNVERIFIED", "reason": "ROAD_ID_NOT_IN_CHAINAGE"}
        kms = list(markers)
        if km_m < kms[0] or km_m > kms[-1]:
            return {
                "status": "CHAINAGE_UNVERIFIED",
                "reason": "OUTSIDE_CHAINAGE_COVERAGE",
                "coverage_km_m": [kms[0], kms[-1]],
            }
        if km_m in markers:
            coords = markers[km_m]
            if len(coords) != 1:
                return self._ambiguous(km_m, km_m, coords)
            lat, lon = coords[0]
            return {
                "status": "CHAINAGE_OK",
                "coordinates": {"lat": lat, "lon": lon},
                "bracketing_km_m": [km_m, km_m],
                "fraction": 0.0,
                "residual_m": 0,
            }
        upper_index = next(index for index, value in enumerate(kms) if value > km_m)
        lower_km, upper_km = kms[upper_index - 1], kms[upper_index]
        lower_coords, upper_coords = markers[lower_km], markers[upper_km]
        if len(lower_coords) != 1 or len(upper_coords) != 1:
            return self._ambiguous(lower_km, upper_km, lower_coords, upper_coords)
        gap = upper_km - lower_km
        if gap > self.max_gap_m:
            return {
                "status": "CHAINAGE_GAP",
                "reason": "IMPLAUSIBLE_CHAINAGE_GAP",
                "bracketing_km_m": [lower_km, upper_km],
                "gap_m": gap,
            }
        fraction = (km_m - lower_km) / gap
        (lat0, lon0), (lat1, lon1) = lower_coords[0], upper_coords[0]
        return {
            "status": "CHAINAGE_OK",
            "coordinates": {
                "lat": lat0 + (lat1 - lat0) * fraction,
                "lon": lon0 + (lon1 - lon0) * fraction,
            },
            "bracketing_km_m": [lower_km, upper_km],
            "fraction": fraction,
            "residual_m": min(km_m - lower_km, upper_km - km_m),
        }

    @staticmethod
    def _ambiguous(
        lower_km: int,
        upper_km: int,
        lower_coords: tuple[tuple[float, float], ...],
        upper_coords: tuple[tuple[float, float], ...] | None = None,
    ) -> dict[str, Any]:
        """Build an auditable ambiguity result for one or both conflicting markers."""
        coordinates_by_km = [(lower_km, lower_coords)]
        if upper_km != lower_km and upper_coords is not None:
            coordinates_by_km.append((upper_km, upper_coords))
        return {
            "status": "CHAINAGE_AMBIGUOUS",
            "reason": "CONFLICTING_COORDINATES_AT_BRACKET",
            "bracketing_km_m": [lower_km, upper_km],
            "conflicting_coordinates": [
                {"km_m": km, "coordinates": [{"lat": lat, "lon": lon} for lat, lon in coords]}
                for km, coords in coordinates_by_km
                if len(coords) != 1
            ],
        }


def _local_name(tag: str) -> str:
    """Return an XML element's local name without its namespace URI."""
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def load_ria_road_links(path: Path) -> dict[str, list[tuple[tuple[float, float], ...]]]:
    """Parse RoadLink local IDs and EPSG:4258 centreline vertices using stdlib XML."""
    result: dict[str, list[tuple[tuple[float, float], ...]]] = defaultdict(list)
    try:
        context = ET.iterparse(Path(path), events=("end",))
        for _, element in context:
            if _local_name(element.tag) != "RoadLink":
                continue
            local_id = None
            for child in element.iter():
                if _local_name(child.tag) == "localId" and child.text:
                    local_id = child.text.strip()
                    break
            if not local_id:
                element.clear()
                continue
            positions: list[tuple[float, float]] = []
            for pos_list in element.iter():
                if _local_name(pos_list.tag) != "posList" or not pos_list.text:
                    continue
                values = [float(value) for value in pos_list.text.split()]
                dimension_raw = pos_list.attrib.get("srsDimension")
                if dimension_raw is None:
                    dimension_raw = next((ancestor.attrib.get("srsDimension") for ancestor in element.iter()
                                          if _local_name(ancestor.tag) == "LineString" and ancestor.attrib.get("srsDimension")), "2")
                dimension = int(dimension_raw)
                if dimension < 2 or len(values) % dimension:
                    raise ValueError(f"invalid posList dimension for RIA road {local_id}")
                srs_name = pos_list.attrib.get("srsName")
                if not srs_name:
                    srs_name = next((ancestor.attrib.get("srsName") for ancestor in element.iter()
                                     if _local_name(ancestor.tag) in {"LineString", "MultiCurve"} and ancestor.attrib.get("srsName")), "")
                if srs_name and not any(crs in srs_name.upper() for crs in ("EPSG/0/4258", "EPSG/0/4326", "EPSG::4258", "EPSG::4326")):
                    continue
                positions.extend((values[i], values[i + 1]) for i in range(0, len(values), dimension))
            if len(positions) >= 2:
                if not all(-90 <= lat <= 90 and -180 <= lon <= 180 for lat, lon in positions):
                    raise ValueError(f"RIA RoadLink {local_id} is not geographic latitude/longitude")
                result[local_id].append(tuple(positions))
            element.clear()
    except (OSError, ET.ParseError, ValueError) as error:
        raise ValueError(f"cannot parse RIA RoadLink GML: {error}") from error
    return dict(result)


def _ria_road_id_matches(local_id: str, road_code: str) -> bool:
    """Match an INSPIRE local ID prefix while excluding similarly numbered roads."""
    if road_code.startswith("A-"):
        prefix = road_code
    else:
        road_class = {1: "I", 2: "II", 4: "III"}.get(len(road_code))
        if road_class is None or not road_code.isdigit():
            return False
        prefix = f"{road_class}-{road_code}"
    return re.match(rf"^{re.escape(prefix)}(?:$|[ _])", local_id, re.IGNORECASE) is not None


def _distance_to_segment_m(point: dict[str, float], start: tuple[float, float], end: tuple[float, float]) -> float:
    """Approximate point-to-segment distance in a local tangent plane near the query."""
    lat0, lon0 = math.radians(point["lat"]), math.radians(point["lon"])
    scale_x = EARTH_RADIUS_M * math.cos(lat0) * math.pi / 180
    scale_y = EARTH_RADIUS_M * math.pi / 180
    sx, sy = (start[1] - point["lon"]) * scale_x, (start[0] - point["lat"]) * scale_y
    ex, ey = (end[1] - point["lon"]) * scale_x, (end[0] - point["lat"]) * scale_y
    dx, dy = ex - sx, ey - sy
    length_squared = dx * dx + dy * dy
    fraction = 0.0 if length_squared == 0 else max(0.0, min(1.0, -(sx * dx + sy * dy) / length_squared))
    return math.hypot(sx + fraction * dx, sy + fraction * dy)


def _distance_to_road(point: dict[str, float], geometries: list[tuple[tuple[float, float], ...]]) -> float:
    """Find the nearest line-segment distance from an endpoint to matched RIA links."""
    nearest = math.inf
    for geometry in geometries:
        for start, end in zip(geometry, geometry[1:]):
            nearest = min(nearest, _distance_to_segment_m(point, start, end))
    return nearest


def _endpoint_label(label: str) -> str:
    """Normalize endpoint labels for reciprocal pairing without changing source text."""
    without_repair = _REPAIR_RE.sub("", label)
    without_chainage = _CHAINAGE_RE.sub("", without_repair)
    without_chainage = re.sub(r"[()]", "", without_chainage)
    return " ".join(without_chainage.casefold().split())


def _section_conflict(row_records: list[dict[str, Any]]) -> str | None:
    """Return the explicit known C1/C2 quarantine reason for a matching section."""
    if not row_records:
        return None
    road = row_records[0]["road_code"]
    endpoints = {
        _endpoint_label(row["raw_labels"][endpoint])
        for row in row_records for endpoint in ("start", "end")
    }
    if road == "1" and any("жеглица" in value for value in endpoints) and any("срацимирово" in value for value in endpoints):
        return "KNOWN_CONFLICT_C1"
    if road == "A-2" and any("девня" in value for value in endpoints) and any("игнатиево" in value for value in endpoints):
        return "KNOWN_CONFLICT_C2"
    return None


def verify_inventory(
    inventory: dict[str, Any],
    chainage: ChainageIndex,
    ria_links: dict[str, list[tuple[tuple[float, float], ...]]] | None = None,
    ria_error: str | None = None,
) -> dict[str, Any]:
    """Verify every parsed directional row and section while preserving source values."""
    verified_rows: list[dict[str, Any]] = []
    row_by_direction: dict[str, dict[str, Any]] = {}
    for source_row in inventory["rows"]:
        road_code = map_road_code(source_row["road_label"])
        row_reasons: list[str] = []
        endpoint_audit: dict[str, dict[str, Any] | None] = {"start": None, "end": None}
        mapped_coordinates: dict[str, dict[str, float] | None] = {"start": None, "end": None}
        if road_code is None:
            row_reasons.append("ROAD_CODE_UNKNOWN")
            row_status = "ROAD_CODE_UNKNOWN"
        else:
            endpoint_statuses: list[str] = []
            for endpoint, field in (("start", "start_chainage_m"), ("end", "end_chainage_m")):
                km_m = source_row[field]
                audit: dict[str, Any]
                if km_m is None:
                    audit = {"status": "CHAINAGE_UNVERIFIED", "reason": "OFFICIAL_CHAINAGE_MISSING"}
                else:
                    audit = chainage.interpolate(road_code, km_m)
                endpoint_audit[endpoint] = audit
                endpoint_statuses.append(audit["status"])
                if audit["status"] == "CHAINAGE_OK":
                    mapped_coordinates[endpoint] = audit["coordinates"]
            errors = [status for status in endpoint_statuses if status != "CHAINAGE_OK"]
            if errors:
                precedence = ("CHAINAGE_AMBIGUOUS", "CHAINAGE_GAP", "CHAINAGE_UNVERIFIED")
                row_status = next(status for status in precedence if status in errors)
                row_reasons.extend(dict.fromkeys(errors))
            else:
                row_status = "CHAINAGE_OK"
        start_m, end_m = source_row["start_chainage_m"], source_row["end_chainage_m"]
        declared_span = abs(end_m - start_m) if start_m is not None and end_m is not None else None
        coordinate_distance = None
        if mapped_coordinates["start"] is not None and mapped_coordinates["end"] is not None:
            coordinate_distance = _haversine_m(mapped_coordinates["start"], mapped_coordinates["end"])
        row = {
            "direction_key": source_row["direction_key"],
            "section_key": source_row["section_key"],
            "official_status": source_row["status"],
            "raw_labels": {
                "road": source_row["road_raw"],
                "start": source_row["start_raw"],
                "end": source_row["end_raw"],
            },
            "road_code": road_code,
            "chainage_m": {"start": start_m, "end": end_m},
            "declared_span_m": declared_span,
            "mapped_coordinates": mapped_coordinates,
            "endpoint_audit": endpoint_audit,
            "coordinate_distance_m": coordinate_distance,
            "status": row_status,
            "reasons": row_reasons,
        }
        verified_rows.append(row)
        row_by_direction[row["direction_key"]] = row

    verified_sections: list[dict[str, Any]] = []
    for source_section in inventory["sections"]:
        rows = [row_by_direction[key] for key in source_section["direction_keys"] if key in row_by_direction]
        reasons: list[str] = []
        requires: list[str] = ["RIA_ALIGNMENT"]
        direction_records = [{
            "direction_key": row["direction_key"],
            "official_status": row["official_status"],
            "start_label": row["raw_labels"]["start"],
            "end_label": row["raw_labels"]["end"],
            "start_chainage_m": row["chainage_m"]["start"],
            "end_chainage_m": row["chainage_m"]["end"],
            "declared_span_m": row["declared_span_m"],
            "mapped_span_m": row["coordinate_distance_m"],
            "status": row["status"],
            "reasons": row["reasons"],
        } for row in rows]
        if len(rows) != 2:
            reasons.append("SECTION_DIRECTION_COUNT")
        if len(rows) == 2:
            endpoints = [
                sorted((_endpoint_label(row["raw_labels"]["start"]), _endpoint_label(row["raw_labels"]["end"])))
                for row in rows
            ]
            if endpoints[0] != endpoints[1]:
                reasons.append("DIRECTION_MISMATCH")
            spans = [row["declared_span_m"] for row in rows]
            if None in spans or abs(spans[0] - spans[1]) > 2:
                reasons.append("DIRECTION_MISMATCH")
        if any(row["status"] != "CHAINAGE_OK" for row in rows):
            reasons.extend(reason for row in rows for reason in row["reasons"])
        chainage_ok = len(rows) == 2 and all(row["status"] == "CHAINAGE_OK" for row in rows)
        conflict = _section_conflict(rows)
        if conflict:
            reasons.append(conflict)
        road_codes = {row["road_code"] for row in rows}
        road_code = next(iter(road_codes)) if len(road_codes) == 1 else None
        declared_spans = [row["declared_span_m"] for row in rows if row["declared_span_m"] is not None]
        mapped_spans = [row["coordinate_distance_m"] for row in rows if row["coordinate_distance_m"] is not None]
        endpoints_raw = []
        if rows:
            endpoints_raw = [rows[0]["raw_labels"]["start"], rows[0]["raw_labels"]["end"]]

        ria_alignment = "UNVERIFIED"
        ria_median_distance = None
        ria_road_link_ids: list[str] = []
        if ria_links is not None and road_code is not None:
            ria_road_link_ids = sorted(local_id for local_id in ria_links if _ria_road_id_matches(local_id, road_code))
            matched = [geometry for local_id in ria_road_link_ids for geometry in ria_links[local_id]]
            if not matched:
                ria_alignment = "ROAD_NOT_FOUND"
                reasons.append("RIA_ROAD_NOT_FOUND")
            else:
                distances = [
                    _distance_to_road(point, matched)
                    for row in rows for point in row["mapped_coordinates"].values() if point is not None
                ]
                if not distances:
                    ria_alignment = "UNVERIFIED"
                    if ria_error:
                        reasons.append("RIA_ALIGNMENT_UNVERIFIED")
                else:
                    ria_median_distance = sorted(distances)[len(distances) // 2] if len(distances) % 2 else sum(sorted(distances)[len(distances) // 2 - 1:len(distances) // 2 + 1]) / 2
                    ria_alignment = "OK" if ria_median_distance <= RIA_ALIGNMENT_TOLERANCE_M else "MISMATCH"
                    if ria_alignment == "MISMATCH":
                        reasons.append("RIA_MISMATCH")
        elif ria_links is not None:
            ria_alignment = "ROAD_NOT_FOUND"
            reasons.append("RIA_ROAD_NOT_FOUND")
        elif ria_error:
            reasons.append("RIA_ALIGNMENT_UNVERIFIED")

        structural_review = any(reason in reasons for reason in ("SECTION_DIRECTION_COUNT", "DIRECTION_MISMATCH"))
        if ria_alignment != "UNVERIFIED":
            requires = []
        if conflict:
            status = "QUARANTINE"
        elif structural_review or not chainage_ok:
            status = "REVIEW"
        elif ria_alignment == "OK":
            status = "PASS"
            requires = []
        else:
            status = "REVIEW"
            if ria_alignment == "UNVERIFIED":
                reasons.append("RIA_ALIGNMENT_UNVERIFIED")
        verified_sections.append({
            "section_key": source_section["section_key"],
            "road_label": source_section["road_label"],
            "road_code": road_code,
            "endpoints": endpoints_raw,
            "declared_span_m": max(declared_spans) if declared_spans else None,
            "mapped_span_m": sum(mapped_spans) / len(mapped_spans) if mapped_spans else None,
            "chainage_status": "CHAINAGE_OK" if chainage_ok else "REVIEW",
            "directions": direction_records,
            "ria_alignment": ria_alignment,
            "ria_road_link_ids": ria_road_link_ids,
            "ria_median_distance_m": ria_median_distance,
            "status": status,
            "reasons": sorted(set(reasons)),
            "requires": requires if status != "QUARANTINE" else [],
        })

    chainage_ok_sections = sum(section["chainage_status"] == "CHAINAGE_OK" for section in verified_sections)
    summary = {
        "rows": len(verified_rows),
        "sections": len(verified_sections),
        "mapped_rows": sum(row["status"] == "CHAINAGE_OK" for row in verified_rows),
        "unresolved_rows": sum(row["status"] != "CHAINAGE_OK" for row in verified_rows),
        "chainage_ok_sections": chainage_ok_sections,
        "chainage_unverified_sections": sum(section["chainage_status"] != "CHAINAGE_OK" for section in verified_sections),
        "review_sections": sum(section["status"] == "REVIEW" for section in verified_sections),
        "quarantine_sections": sum(section["status"] == "QUARANTINE" for section in verified_sections),
        "pass_sections": sum(section["status"] == "PASS" for section in verified_sections),
    }
    return {"summary": summary, "rows": verified_rows, "sections": verified_sections}


def build_report(source_path: Path, km_points_path: Path, ria_path: Path | None = None) -> dict[str, Any]:
    """Build a provenance-rich report from archived BGTOLL, АПИ, and optional RIA inputs."""
    source_bytes = Path(source_path).read_bytes()
    km_bytes = Path(km_points_path).read_bytes()
    inventory = parse_inventory(source_bytes)
    chainage = ChainageIndex.from_file(km_points_path)
    ria_links = None
    ria_error = None
    if ria_path is not None:
        try:
            ria_links = load_ria_road_links(ria_path)
        except ValueError as error:
            ria_error = str(error)
    result = verify_inventory(inventory, chainage, ria_links=ria_links, ria_error=ria_error)
    return {
        "source_path": str(source_path),
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "km_points_path": str(km_points_path),
        "km_points_sha256": hashlib.sha256(km_bytes).hexdigest(),
        "ria_road_links_path": str(ria_path) if ria_path is not None else None,
        "ria_error": ria_error,
        **result,
    }


def main(argv: list[str] | None = None) -> int:
    """Run the verifier CLI and write its complete JSON report to the requested path."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="archived official BGTOLL HTML")
    parser.add_argument("--km-points", required=True, type=Path, help="official АПИ km_points JSON")
    parser.add_argument("--output", required=True, type=Path, help="destination JSON verification report")
    parser.add_argument("--ria-road-links", type=Path, help="optional RIA INSPIRE RoadLink GML")
    args = parser.parse_args(argv)
    try:
        report = build_report(args.source, args.km_points, args.ria_road_links)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        args.output.write_text(encoded, encoding="utf-8")
        print(json.dumps(report["summary"], ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as error:
        print(f"zone verification failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
