#!/usr/bin/env python3
"""Assemble fail-closed schema-1 geometry candidates from local evidence."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import re
import sys
from typing import Any
import xml.etree.ElementTree as ET

try:
    from .zone_verifier import EARTH_RADIUS_M, map_road_code
except ImportError:
    from zone_verifier import EARTH_RADIUS_M, map_road_code


ENDPOINT_MATCH_TOLERANCE_M = 500.0
ENDPOINT_PASS_TOLERANCE_M = 150.0
LENGTH_RELATIVE_TOLERANCE = 0.02
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CHAINAGE_RE = re.compile(r"км\s*\d+\s*\+\s*\d{1,3}", re.IGNORECASE)
_REPAIR_RE = re.compile(r"/Пътен участък в ремонт/", re.IGNORECASE)
_ROAD_CODE_RE = re.compile(r"(?<![\w])(?:A-[1-6]|(?:I|II|III)-\d{1,4}|\d{1,4})(?![\w])", re.IGNORECASE)
_ENDPOINT_SEPARATOR_RE = re.compile(r"\s*[-–—]\s*")
_LIMIT_RE = re.compile(
    r"Категория\s+(?P<categories>[^<\r\n]+?)\s*[-–]\s*"
    r"(?P<limit>\d+|забранено)\s*(?:km/h|км/ч)?",
    re.IGNORECASE,
)


class CandidateError(ValueError):
    """Raised when input evidence is malformed or baseline identity is unsafe."""


def _finite_number(value: Any, field: str) -> float:
    """Convert a JSON numeric value to a finite float, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CandidateError(f"{field} must be numeric")
    converted = float(value)
    if not math.isfinite(converted):
        raise CandidateError(f"{field} must be finite")
    return converted


def _sha256(value: Any) -> bool:
    """Return whether value is a syntactically valid SHA-256 hex digest."""
    return isinstance(value, str) and _SHA256_RE.fullmatch(value) is not None


def _canonical_json(value: Any) -> str:
    """Serialize JSON with stable keys, preserved Unicode, finite numbers, and newline."""
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"


def canonical_json(value: Any) -> str:
    """Expose deterministic candidate serialization for callers and tests."""
    return _canonical_json(value)


def _load_json(path: Path) -> tuple[Any, bytes]:
    """Load a UTF-8 JSON file and return its value plus the exact source bytes."""
    try:
        source_bytes = Path(path).read_bytes()
        value = json.loads(source_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise CandidateError(f"cannot read JSON from {path}: {error}") from error
    return value, source_bytes


def _local_name(tag: str) -> str:
    """Return an XML element's local name without its namespace URI."""
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _normalise_endpoint_label(value: str) -> str:
    """Normalize whitespace and remove only known chainage/repair annotations."""
    value = _REPAIR_RE.sub("", value)
    value = _CHAINAGE_RE.sub("", value)
    value = re.sub(r"[()]", " ", value)
    value = re.sub(r"\s+", " ", value)
    return value.strip(" \t\r\n-–—").casefold()


def _road_identity(description: str) -> str | None:
    """Extract an explicit road code from a KML description and map its identity."""
    plain = html.unescape(re.sub(r"<[^>]*>", " ", description))
    match = _ROAD_CODE_RE.search(plain)
    if match is None:
        return None
    token = match.group(0).upper()
    if re.fullmatch(r"A-[1-6]", token):
        return token
    if token.isdigit():
        return token
    return map_road_code(token)


def _parse_coordinate_text(text: str, context: str) -> list[list[float]]:
    """Parse a KML coordinate list into source-order [longitude, latitude, ...] tuples."""
    vertices: list[list[float]] = []
    for index, token in enumerate(text.split()):
        parts = token.split(",")
        if len(parts) < 2 or len(parts) > 3 or not parts[0] or not parts[1]:
            raise CandidateError(f"{context} coordinate {index} is malformed")
        lon = _finite_number(float(parts[0]), f"{context} longitude {index}")
        lat = _finite_number(float(parts[1]), f"{context} latitude {index}")
        if not -180 <= lon <= 180 or not -90 <= lat <= 90:
            raise CandidateError(f"{context} coordinate {index} is outside WGS84 bounds")
        point = [lon, lat]
        if len(parts) == 3 and parts[2] != "":
            try:
                altitude = float(parts[2])
            except ValueError:
                altitude = math.nan
            # KML frequently includes altitude 0; it is irrelevant to road
            # geometry and must not alter source-order audit or length math.
            if not math.isfinite(altitude):
                raise CandidateError(f"{context} altitude {index} must be finite")
        vertices.append(point)
    if len(vertices) < 2:
        raise CandidateError(f"{context} must contain at least two coordinates")
    return vertices


def parse_kml(kml: str | bytes) -> dict[str, Any]:
    """Parse KML placemarks into ordered line and point inventories using stdlib XML."""
    try:
        root = ET.fromstring(kml)
    except (ET.ParseError, ValueError) as error:
        raise CandidateError(f"cannot parse KML: {error}") from error

    placemarks = [element for element in root.iter() if _local_name(element.tag) == "Placemark"]
    lines: list[dict[str, Any]] = []
    points: list[dict[str, Any]] = []
    for placemark_index, placemark in enumerate(placemarks):
        name_element = next((item for item in placemark if _local_name(item.tag) == "name"), None)
        description_element = next((item for item in placemark if _local_name(item.tag) == "description"), None)
        name = "" if name_element is None or name_element.text is None else name_element.text.strip()
        description = "" if description_element is None or description_element.text is None else description_element.text
        road_code = _road_identity(description)

        line_elements = [item for item in placemark.iter() if _local_name(item.tag) == "LineString"]
        for line_in_placemark, line_element in enumerate(line_elements):
            coordinates_element = next(
                (item for item in line_element.iter() if _local_name(item.tag) == "coordinates"), None
            )
            if coordinates_element is None or coordinates_element.text is None:
                raise CandidateError(f"Placemark {placemark_index} LineString {line_in_placemark} has no coordinates")
            vertices = _parse_coordinate_text(
                coordinates_element.text,
                f"Placemark {placemark_index} LineString {line_in_placemark}",
            )
            labels = [part.strip() for part in _ENDPOINT_SEPARATOR_RE.split(name) if part.strip()]
            endpoint_labels = labels if len(labels) == 2 else None
            lines.append({
                "source_line_index": len(lines),
                "source_placemark_index": placemark_index,
                "source_line_in_placemark": line_in_placemark,
                "name": name,
                "description": description,
                "road_code": road_code,
                "endpoint_labels": endpoint_labels,
                "source_vertices": vertices,
                "match_status": "UNMATCHED",
                "section_key": None,
                "endpoint_distances_m": None,
                "match_reasons": [],
            })

        point_elements = [item for item in placemark.iter() if _local_name(item.tag) == "Point"]
        for point_in_placemark, point_element in enumerate(point_elements):
            coordinates_element = next(
                (item for item in point_element.iter() if _local_name(item.tag) == "coordinates"), None
            )
            if coordinates_element is None or coordinates_element.text is None:
                raise CandidateError(f"Placemark {placemark_index} Point {point_in_placemark} has no coordinates")
            points.append({
                "source_placemark_index": placemark_index,
                "source_point_in_placemark": point_in_placemark,
                "name": name,
                "description": description,
                "source_vertices": [
                    [float(value) for value in token.split(",")]
                    for token in coordinates_element.text.split()
                ],
            })
    return {
        "placemark_count": len(placemarks),
        "line_count": len(lines),
        "point_placemark_count": sum(
            1 for placemark in placemarks
            if any(_local_name(item.tag) == "Point" for item in placemark.iter())
        ),
        "point_count": len(points),
        "lines": lines,
        "points": points,
    }


def _haversine_m(a: dict[str, float], b: dict[str, float]) -> float:
    """Return great-circle distance in metres between latitude/longitude objects."""
    lat1, lon1 = math.radians(a["lat"]), math.radians(a["lon"])
    lat2, lon2 = math.radians(b["lat"]), math.radians(b["lon"])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    value = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(value)))


def _validate_coordinate(value: Any, field: str) -> dict[str, float] | None:
    """Return a valid WGS84 latitude/longitude mapping or None for absent evidence."""
    if value is None:
        return None
    if not isinstance(value, dict) or "lat" not in value or "lon" not in value:
        raise CandidateError(f"{field} must contain lat/lon or be null")
    lat = _finite_number(value["lat"], f"{field}.lat")
    lon = _finite_number(value["lon"], f"{field}.lon")
    if not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise CandidateError(f"{field} is outside WGS84 bounds")
    return {"lat": lat, "lon": lon}


def _section_identity(section: dict[str, Any], rows_by_key: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Extract road, unordered label pair, and low/high mapped coordinates for matching."""
    key = section.get("section_key")
    if not isinstance(key, str) or not key:
        raise CandidateError("verification section requires section_key")
    directions = section.get("directions")
    if not isinstance(directions, list) or len(directions) != 2:
        raise CandidateError(f"verification section {key} must have exactly two directions")
    records: list[dict[str, Any]] = []
    coords_by_label: dict[str, list[dict[str, float]]] = defaultdict(list)
    for direction in directions:
        if not isinstance(direction, dict):
            raise CandidateError(f"verification section {key} has a malformed direction")
        direction_key = direction.get("direction_key")
        row = rows_by_key.get(direction_key)
        if row is None:
            raise CandidateError(f"verification section {key} references an absent direction row")
        labels = row.get("raw_labels")
        if not isinstance(labels, dict) or not isinstance(labels.get("start"), str) or not isinstance(labels.get("end"), str):
            raise CandidateError(f"verification direction {direction_key} has malformed endpoint labels")
        start_label = labels["start"]
        end_label = labels["end"]
        start_identity = _normalise_endpoint_label(start_label)
        end_identity = _normalise_endpoint_label(end_label)
        if not start_identity or not end_identity:
            raise CandidateError(f"verification direction {direction_key} has an empty endpoint identity")
        chainage = row.get("chainage_m")
        if not isinstance(chainage, dict):
            raise CandidateError(f"verification direction {direction_key} has malformed chainage")
        start_m, end_m = chainage.get("start"), chainage.get("end")
        for field, value in (("start", start_m), ("end", end_m)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                raise CandidateError(f"verification direction {direction_key} has invalid {field} chainage")
        mapped = row.get("mapped_coordinates")
        if not isinstance(mapped, dict):
            raise CandidateError(f"verification direction {direction_key} has malformed mapped_coordinates")
        start_coord = _validate_coordinate(mapped.get("start"), f"{direction_key}.mapped_coordinates.start")
        end_coord = _validate_coordinate(mapped.get("end"), f"{direction_key}.mapped_coordinates.end")
        if start_coord is not None:
            coords_by_label[start_identity].append(start_coord)
        if end_coord is not None:
            coords_by_label[end_identity].append(end_coord)
        records.append({
            "direction_key": direction_key,
            "start_label": start_label,
            "end_label": end_label,
            "start_identity": start_identity,
            "end_identity": end_identity,
            "start_chainage_m": start_m,
            "end_chainage_m": end_m,
            "row": row,
            "direction": direction,
        })

    row_road_codes = {
        row.get("road_code") for row in (record["row"] for record in records)
        if isinstance(row.get("road_code"), str)
    }
    if len(row_road_codes) > 1:
        raise CandidateError(f"verification section {key} has inconsistent directional road codes")

    labels = {record["start_identity"] for record in records} | {record["end_identity"] for record in records}
    structural_direction_mismatch = len(labels) != 2
    if structural_direction_mismatch:
        # Preserve an auditable held section if verifier rows themselves do not
        # agree on the endpoint pair; do not invent a direction from labels.
        labels = {record["start_identity"] for record in records} | {record["end_identity"] for record in records}
        if len(labels) < 2:
            labels.update({"__missing_low_endpoint__", "__missing_high_endpoint__"})
    low_identity = high_identity = None
    ascending = [
        record for record in records
        if record["start_chainage_m"] is not None
        and record["end_chainage_m"] is not None
        and record["start_chainage_m"] < record["end_chainage_m"]
    ]
    if ascending:
        low_identity, high_identity = ascending[0]["start_identity"], ascending[0]["end_identity"]
    else:
        # Identity remains matchable, but orientation is unproven and its gate stays held.
        low_identity, high_identity = sorted(labels)

    label_values: dict[str, str] = {}
    for record in records:
        label_values.setdefault(record["start_identity"], record["start_label"])
        label_values.setdefault(record["end_identity"], record["end_label"])

    def consistent_coordinate(label: str) -> dict[str, float] | None:
        """Return one mapped endpoint only if reciprocal rows agree within one metre."""
        candidates = coords_by_label.get(label, [])
        if not candidates:
            return None
        first = candidates[0]
        if any(_haversine_m(first, item) > 1 for item in candidates[1:]):
            return None
        return first

    low_coordinate = consistent_coordinate(low_identity)
    high_coordinate = consistent_coordinate(high_identity)
    coordinate_conflict = bool(
        coords_by_label.get(low_identity)
        and low_coordinate is None
        or coords_by_label.get(high_identity)
        and high_coordinate is None
    )
    road_code = section.get("road_code") or (next(iter(row_road_codes)) if row_road_codes else None)
    if not isinstance(road_code, str) or not road_code:
        road_code = None
    return {
        "section_key": key,
        "road_label": section.get("road_label") or "Unknown road",
        "road_code": road_code,
        "endpoint_labels": [label_values[low_identity], label_values[high_identity]],
        "endpoint_identities": [low_identity, high_identity],
        "mapped_coordinates": {"low": low_coordinate, "high": high_coordinate},
        "coordinate_conflict": coordinate_conflict,
        "orientation_proven": bool(ascending) and not structural_direction_mismatch,
        "structural_direction_mismatch": structural_direction_mismatch,
        "directions": records,
        "section": section,
    }


def _endpoint_assignment(
    vertices: list[list[float]], mapped: dict[str, dict[str, float] | None]
) -> tuple[bool, bool, float | None, float | None]:
    """Choose the uniquely closer endpoint assignment and report low/high errors."""
    low, high = mapped.get("low"), mapped.get("high")
    if low is None or high is None:
        return False, False, None, None
    first = {"lon": vertices[0][0], "lat": vertices[0][1]}
    last = {"lon": vertices[-1][0], "lat": vertices[-1][1]}
    direct_low, direct_high = _haversine_m(first, low), _haversine_m(last, high)
    reverse_low, reverse_high = _haversine_m(last, low), _haversine_m(first, high)
    direct_total, reverse_total = direct_low + direct_high, reverse_low + reverse_high
    if abs(direct_total - reverse_total) < 0.01:
        return False, False, min(direct_low, reverse_low), min(direct_high, reverse_high)
    source_reversed = reverse_total < direct_total
    low_error, high_error = (reverse_low, reverse_high) if source_reversed else (direct_low, direct_high)
    eligible = max(low_error, high_error) <= ENDPOINT_MATCH_TOLERANCE_M
    return eligible, source_reversed, low_error, high_error


def _name_matches(line: dict[str, Any], identity: dict[str, Any]) -> bool:
    """Require exact normalized unordered endpoint labels, not a display-name-only match."""
    labels = line.get("endpoint_labels")
    if not isinstance(labels, list) or len(labels) != 2:
        return False
    line_pair = sorted(_normalise_endpoint_label(item) for item in labels)
    section_pair = sorted(identity["endpoint_identities"])
    return line_pair == section_pair


def _match_lines(
    line_inventory: dict[str, Any], identities: list[dict[str, Any]]
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[str]]]:
    """Match lines by exact road/endpoint identities and mapped coordinates only."""
    lines_by_section: dict[str, list[dict[str, Any]]] = defaultdict(list)
    issues_by_section: dict[str, list[str]] = defaultdict(list)
    lines = line_inventory["lines"]
    for line in lines:
        label_candidates = [
            identity for identity in identities
            if line["road_code"] is not None
            and identity["road_code"] == line["road_code"]
            and _name_matches(line, identity)
        ]
        if not label_candidates and line.get("endpoint_labels"):
            label_pair = sorted(_normalise_endpoint_label(value) for value in line["endpoint_labels"])
            label_candidates = [
                identity for identity in identities
                if line["road_code"] is not None
                and identity["road_code"] == line["road_code"]
                and all(any(candidate == source or candidate in source or source in candidate
                            for source in identity["endpoint_identities"]) for candidate in label_pair)
            ]
        eligible: list[tuple[dict[str, Any], bool, float, float]] = []
        near_misses: list[tuple[dict[str, Any], float, float]] = []
        for identity in label_candidates:
            is_eligible, reversed_source, low_error, high_error = _endpoint_assignment(
                line["source_vertices"], identity["mapped_coordinates"]
            )
            if low_error is None or high_error is None:
                issues_by_section[identity["section_key"]].append("MAPPED_ENDPOINT_UNAVAILABLE")
                continue
            line["endpoint_distances_m"] = {"low": low_error, "high": high_error}
            if is_eligible:
                eligible.append((identity, reversed_source, low_error, high_error))
            else:
                near_misses.append((identity, low_error, high_error))
        if len(eligible) == 1:
            identity, reversed_source, low_error, high_error = eligible[0]
            line.update({
                "match_status": "MATCHED",
                "section_key": identity["section_key"],
                "source_reversed": reversed_source if identity["orientation_proven"] else None,
                "endpoint_distances_m": {"low": low_error, "high": high_error},
            })
            if identity["coordinate_conflict"]:
                line["match_status"] = "AMBIGUOUS"
                line["match_reasons"].append("RECIPROCAL_ENDPOINT_COORDINATES_DISAGREE")
                issues_by_section[identity["section_key"]].append("RECIPROCAL_ENDPOINT_COORDINATES_DISAGREE")
            elif not identity["orientation_proven"]:
                line["match_reasons"].append("LOW_HIGH_ORIENTATION_UNPROVEN")
                issues_by_section[identity["section_key"]].append("LOW_HIGH_ORIENTATION_UNPROVEN")
            else:
                lines_by_section[identity["section_key"]].append(line)
        elif len(eligible) > 1:
            line["match_status"] = "AMBIGUOUS"
            line["match_reasons"].append("AMBIGUOUS_SECTION_MATCH")
            for identity, _, _, _ in eligible:
                issues_by_section[identity["section_key"]].append("AMBIGUOUS_SECTION_MATCH")
        elif len(label_candidates) == 1 and near_misses:
            identity, low_error, high_error = near_misses[0]
            line["match_status"] = "ENDPOINT_MISMATCH"
            line["section_key"] = identity["section_key"]
            line["endpoint_distances_m"] = {"low": low_error, "high": high_error}
            line["match_reasons"].append("ENDPOINT_OUTSIDE_MATCH_TOLERANCE")
            issues_by_section[identity["section_key"]].append("ENDPOINT_OUTSIDE_MATCH_TOLERANCE")
        elif len(near_misses) > 1:
            line["match_status"] = "AMBIGUOUS"
            line["match_reasons"].append("AMBIGUOUS_ENDPOINT_MATCH")
            for identity, _, _ in near_misses:
                issues_by_section[identity["section_key"]].append("AMBIGUOUS_ENDPOINT_MATCH")
        else:
            reason = "ROAD_OR_ENDPOINT_LABEL_UNMATCHED" if not label_candidates else "MAPPED_ENDPOINT_UNAVAILABLE"
            line["match_status"] = "UNMATCHED"
            line["match_reasons"].append(reason)
            if len(label_candidates) == 1:
                issues_by_section[label_candidates[0]["section_key"]].append("MISSING_OR_AMBIGUOUS_GEOMETRY_LINE")
            elif line["road_code"] is not None:
                same_road = [identity for identity in identities if identity["road_code"] == line["road_code"]]
                endpoint_matches = [identity for identity in same_road if _name_matches(line, identity)]
                candidate_sections = endpoint_matches or same_road
                if len(candidate_sections) == 1:
                    issues_by_section[candidate_sections[0]["section_key"]].append("MISSING_OR_AMBIGUOUS_GEOMETRY_LINE")
    return lines_by_section, issues_by_section


def _parse_limits(description: str) -> dict[str, int | None]:
    """Extract explicitly stated speed-category limits from KML description text."""
    plain = html.unescape(description).replace("<br>", "\n").replace("<br/>", "\n")
    limits: dict[str, int | None] = {}
    for match in _LIMIT_RE.finditer(plain):
        raw_value = match.group("limit").strip()
        value = None if raw_value.casefold() == "забранено" else int(raw_value)
        for category in re.split(r"[,;]\s*", match.group("categories").strip()):
            clean_category = category.strip()
            if clean_category:
                limits[clean_category] = value
    return limits


def _initial_direction_names(identity: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map verified official chainage orientation to stable forward/reverse keys."""
    result: dict[str, dict[str, Any]] = {}
    for record in identity["directions"]:
        start_m, end_m = record["start_chainage_m"], record["end_chainage_m"]
        if start_m is not None and end_m is not None and start_m != end_m:
            direction_name = "forward" if start_m < end_m else "reverse"
            if direction_name not in result:
                result[direction_name] = record
                continue
        # Invalid/non-reciprocal chainage cannot establish direction. Retain rows
        # deterministically for audit, but every affected direction remains held.
        remaining = next((name for name in ("forward", "reverse") if name not in result), None)
        if remaining is not None:
            result[remaining] = record
    return result


def _certificate_index(evidence: Any) -> tuple[dict[str, list[dict[str, Any]]], list[dict[str, Any]]]:
    """Index certificate records only by an explicit exact direction-key claim."""
    if evidence is None:
        return {}, []
    if not isinstance(evidence, dict) or not isinstance(evidence.get("images"), list):
        raise CandidateError("certificate evidence must contain an images array")
    by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unmapped: list[dict[str, Any]] = []
    for index, item in enumerate(evidence["images"]):
        if not isinstance(item, dict):
            raise CandidateError(f"certificate evidence image {index} must be an object")
        claim = item.get("direction_key_claim")
        if claim is None:
            claim = item.get("direction_key")
        if isinstance(claim, str) and claim:
            by_key[claim].append(item)
        else:
            unmapped.append(item)
    return dict(by_key), unmapped


def _bound_certificate(
    direction_key: str,
    by_key: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """Return the one explicitly bound SINGLE_CALLOUT record, or a held audit result."""
    records = by_key.get(direction_key, [])
    if len(records) != 1:
        return {
            "status": "MISSING" if not records else "AMBIGUOUS_BINDING",
            "cert_len_m": None,
            "cert_sha256": None,
            "cert_url": None,
            "image_path": None,
            "direction_mapping": direction_key if records else None,
        }
    evidence = records[0]
    status = evidence.get("status")
    candidate_length = evidence.get("candidate_cert_len_m")
    usable = (
        status == "SINGLE_CALLOUT"
        and isinstance(candidate_length, int)
        and not isinstance(candidate_length, bool)
        and candidate_length > 0
    )
    cert_len = candidate_length if usable else None
    image_path = evidence.get("image_path")
    cert_url = evidence.get("cert_url")
    if not isinstance(cert_url, str) or not cert_url.strip():
        cert_url = None
    digest = evidence.get("image_sha256")
    return {
        "status": status if isinstance(status, str) else "UNREADABLE",
        "cert_len_m": cert_len,
        "cert_sha256": digest if _sha256(digest) else None,
        "cert_url": cert_url,
        "image_path": image_path if isinstance(image_path, str) else None,
        "direction_mapping": direction_key,
        "raw_callouts": evidence.get("matches", []),
        "human_confirmed": evidence.get("human_confirmed", False),
        "release_eligible": evidence.get("release_eligible", False),
    }


def _bearing_degrees(polyline: list[list[float]]) -> float:
    """Calculate initial bearing from first to second normalized [lat, lon] point."""
    lat1, lon1 = (math.radians(value) for value in polyline[0])
    lat2, lon2 = (math.radians(value) for value in polyline[1])
    delta_lon = lon2 - lon1
    y = math.sin(delta_lon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(delta_lon)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _geometry_length_m(vertices: list[list[float]]) -> float:
    """Compute geodesic length over every source-order line segment."""
    total = 0.0
    for first, second in zip(vertices, vertices[1:]):
        total += _haversine_m(
            {"lat": first[1], "lon": first[0]},
            {"lat": second[1], "lon": second[0]},
        )
    return total


def _direction_record(
    identity: dict[str, Any],
    record: dict[str, Any],
    direction_name: str,
    geometry: dict[str, Any] | None,
    issues: list[str],
    certificate: dict[str, Any],
) -> dict[str, Any]:
    """Build one publisher-compatible directional record with independent gates."""
    source_direction = record["direction"]
    official_status = source_direction.get("official_status", record["row"].get("official_status"))
    if official_status not in {"CERTIFIED", "PENDING_VERIFICATION"}:
        official_status = "PENDING_VERIFICATION"
        issues = list(issues) + ["OFFICIAL_STATUS_UNKNOWN"]
    source_section = identity["section"]
    section_reasons = source_section.get("reasons", [])
    if not isinstance(section_reasons, list):
        section_reasons = []
    conflict_reasons = [reason for reason in section_reasons if reason in {"KNOWN_CONFLICT_C1", "KNOWN_CONFLICT_C2"}]

    chainage_state = "PASS"
    if source_section.get("chainage_status") != "CHAINAGE_OK" or not identity["orientation_proven"]:
        chainage_state = "REVIEW"
    if identity["coordinate_conflict"]:
        chainage_state = "QUARANTINE"
    endpoint_distances = geometry.get("endpoint_distances_m") if geometry else None
    if geometry is None or endpoint_distances is None:
        chainage_state = "QUARANTINE"
    elif max(endpoint_distances.values()) > ENDPOINT_MATCH_TOLERANCE_M:
        chainage_state = "QUARANTINE"
    elif max(endpoint_distances.values()) > ENDPOINT_PASS_TOLERANCE_M:
        chainage_state = "REVIEW"
    if any(issue in {
        "DUPLICATE_LINES_FOR_SECTION", "AMBIGUOUS_SECTION_MATCH",
        "RECIPROCAL_ENDPOINT_COORDINATES_DISAGREE", "LOW_HIGH_ORIENTATION_UNPROVEN",
    } for issue in issues):
        chainage_state = "QUARANTINE"
    if conflict_reasons:
        chainage_state = "QUARANTINE"

    ria_state = "PASS" if source_section.get("ria_alignment") == "OK" else "REVIEW"
    certificate_length = certificate.get("cert_len_m")
    geometry_length = geometry.get("geometry_len_m") if geometry else None
    if certificate_length is None or geometry_length is None:
        length_state = "REVIEW"
    else:
        relative_difference = abs(geometry_length / certificate_length - 1)
        length_state = "QUARANTINE" if relative_difference > LENGTH_RELATIVE_TOLERANCE else "PASS"

    gates = {"chainage": chainage_state, "ria": ria_state, "length": length_state}
    reasons = list(dict.fromkeys(issues + conflict_reasons))
    if chainage_state != "PASS":
        reasons.append("CHAINAGE_OR_ENDPOINT_GATE_" + chainage_state)
    if ria_state != "PASS":
        reasons.extend(source_section.get("reasons", []))
        reasons.append("RIA_ALIGNMENT_UNVERIFIED" if source_section.get("ria_alignment") == "UNVERIFIED" else "RIA_ALIGNMENT_NOT_PASS")
    if length_state != "PASS":
        reasons.append("CERTIFICATE_LENGTH_UNAVAILABLE" if certificate_length is None else "GEOMETRY_CERTIFICATE_LENGTH_MISMATCH")

    status = official_status
    all_pass = all(value == "PASS" for value in gates.values())
    if conflict_reasons or any(value == "QUARANTINE" for value in gates.values()):
        release_state = "QUARANTINED"
    elif official_status == "PENDING_VERIFICATION":
        release_state = "UNDER_VERIFICATION"
        reasons.append("BGTOLL_DIRECTION_PENDING")
    elif all_pass and certificate.get("cert_sha256") and certificate.get("cert_url"):
        release_state = "ACTIVE"
    else:
        release_state = "UNDER_VERIFICATION"

    name_bg = f"{record['start_label']} – {record['end_label']}"
    return {
        "name_bg": name_bg,
        "status": status,
        "release_state": release_state,
        "cert_len_m": certificate_length,
        "cert_url": certificate.get("cert_url"),
        "cert_sha256": certificate.get("cert_sha256"),
        "gates": gates,
        "direction_key": record["direction_key"],
        "direction_mapping": direction_name,
        "chainage_m": {"start": record["start_chainage_m"], "end": record["end_chainage_m"]},
        "span_m": abs(record["end_chainage_m"] - record["start_chainage_m"])
        if record["start_chainage_m"] is not None and record["end_chainage_m"] is not None else None,
        "evidence": {"certificate": certificate},
        "gate_details": {
            "endpoint_distances_m": endpoint_distances,
            "ria_alignment": source_section.get("ria_alignment"),
            "geometry_len_m": geometry_length,
            "relative_length_difference": (
                abs(geometry_length / certificate_length - 1)
                if geometry_length is not None and certificate_length else None
            ),
            "reasons": sorted(set(reasons)),
        },
    }


def build_candidate(
    verification: dict[str, Any],
    kml: str | bytes,
    certificate_evidence: dict[str, Any] | None = None,
    *,
    generated_at: str | None = None,
    bundle_version: str = "v5",
    verification_sha256: str | None = None,
    kml_sha256: str | None = None,
    certificate_evidence_sha256: str | None = None,
) -> dict[str, Any]:
    """Assemble auditable held/active candidate records from verified local evidence."""
    if not isinstance(verification, dict):
        raise CandidateError("verification report must be a JSON object")
    summary = verification.get("summary")
    if not isinstance(summary, dict) or summary.get("rows") != 82 or summary.get("sections") != 41:
        raise CandidateError("Phase 2 counts differ from required 82 rows/41 sections baseline")
    rows = verification.get("rows")
    source_sections = verification.get("sections")
    if not isinstance(rows, list) or len(rows) != 82 or not isinstance(source_sections, list) or len(source_sections) != 41:
        raise CandidateError("Phase 2 counts differ from required 82 rows/41 sections baseline")
    rows_by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("direction_key"), str):
            raise CandidateError("verification report contains a malformed direction row")
        if row["direction_key"] in rows_by_key:
            raise CandidateError(f"duplicate Phase 2 direction_key: {row['direction_key']}")
        rows_by_key[row["direction_key"]] = row
    if len(rows_by_key) != 82:
        raise CandidateError("Phase 2 direction rows are not unique")

    identities = [_section_identity(section, rows_by_key) for section in source_sections]
    section_keys = [identity["section_key"] for identity in identities]
    if len(set(section_keys)) != 41:
        raise CandidateError("Phase 2 section keys are not unique")
    parsed = parse_kml(kml)
    lines_by_section, issues_by_section = _match_lines(parsed, identities)
    evidence_by_key, unmapped_evidence = _certificate_index(certificate_evidence)

    candidate_sections: list[dict[str, Any]] = []
    zones: list[dict[str, Any]] = []
    matched_line_indices: set[int] = set()
    duplicate_lines = 0
    unmatched_line_count = 0
    for identity in identities:
        key = identity["section_key"]
        matches = lines_by_section.get(key, [])
        section_issues = list(issues_by_section.get(key, []))
        line: dict[str, Any] | None = None
        geometry: dict[str, Any] | None = None
        if len(matches) > 1:
            duplicate_lines += len(matches)
            section_issues.append("DUPLICATE_LINES_FOR_SECTION")
            for match in matches:
                match["match_status"] = "DUPLICATE"
                match["match_reasons"].append("DUPLICATE_LINES_FOR_SECTION")
        elif len(matches) == 1:
            line = matches[0]
            matched_line_indices.add(line["source_line_index"])
            vertices = line["source_vertices"]
            mapped = identity["mapped_coordinates"]
            endpoint_distances = line.get("endpoint_distances_m")
            source_reversed = line.get("source_reversed")
            orientation_proven = identity["orientation_proven"] and isinstance(source_reversed, bool)
            normalized_vertices = list(reversed(vertices)) if source_reversed is True else list(vertices)
            if not orientation_proven:
                section_issues.append("LOW_HIGH_ORIENTATION_UNPROVEN")
            normalized_polyline = [[vertex[1], vertex[0]] for vertex in normalized_vertices]
            geometry_length = _geometry_length_m(normalized_vertices)
            geometry = {
                "source_line_index": line["source_line_index"],
                "source_placemark_index": line["source_placemark_index"],
                "source_name": line["name"],
                "source_road_code": line["road_code"],
                "source_reversed": source_reversed if orientation_proven else None,
                "orientation_status": "PROVEN_LOW_TO_HIGH" if orientation_proven else "UNPROVEN",
                "source_vertices": copy_vertices(vertices),
                "normalized_polyline": normalized_polyline,
                "geometry_len_m": geometry_length,
                "endpoint_distances_m": endpoint_distances,
                "mapped_endpoints": mapped,
                "start_heading": _bearing_degrees(normalized_polyline),
                "limit_by_category": _parse_limits(line["description"]),
            }
        else:
            section_issues.append("MISSING_OR_AMBIGUOUS_GEOMETRY_LINE")

        if identity["coordinate_conflict"]:
            section_issues.append("RECIPROCAL_ENDPOINT_COORDINATES_DISAGREE")
        if not identity["orientation_proven"]:
            section_issues.append("LOW_HIGH_ORIENTATION_UNPROVEN")
        section_source = identity["section"]
        source_reasons = section_source.get("reasons", [])
        if isinstance(source_reasons, list):
            section_issues.extend(
                reason for reason in source_reasons
                if reason in {"KNOWN_CONFLICT_C1", "KNOWN_CONFLICT_C2"}
            )

        named_directions = _initial_direction_names(identity)
        if set(named_directions) != {"forward", "reverse"}:
            section_issues.append("OFFICIAL_DIRECTION_MAPPING_UNPROVEN")
        direction_records: dict[str, dict[str, Any]] = {}
        for direction_name in ("forward", "reverse"):
            record = named_directions.get(direction_name)
            if record is None:
                continue
            certificate = _bound_certificate(record["direction_key"], evidence_by_key)
            direction_issues = list(section_issues)
            if not identity["orientation_proven"]:
                direction_issues.append("OFFICIAL_DIRECTION_MAPPING_UNPROVEN")
            direction_records[direction_name] = _direction_record(
                identity, record, direction_name, geometry, direction_issues, certificate,
            )

        direction_states = [item["release_state"] for item in direction_records.values()]
        if any(state == "QUARANTINED" for state in direction_states):
            section_release = "QUARANTINED"
            section_status = "QUARANTINE"
        elif len(direction_records) != 2 or any(state != "ACTIVE" for state in direction_states):
            section_release = "UNDER_VERIFICATION"
            section_status = "REVIEW"
        else:
            section_release = "ACTIVE"
            section_status = "PASS"
        section_source = identity["section"]
        start_name, end_name = identity["endpoint_labels"]
        section_output = {
            "section_key": key,
            "road": identity["road_code"] or str(identity["road_label"]),
            "name_bg": f"{start_name} – {end_name}",
            "status": section_status,
            "release_state": section_release,
            "span_m": section_source.get("declared_span_m"),
            "geometry_len_m": geometry.get("geometry_len_m") if geometry else None,
            "geometry": geometry,
            "directions": direction_records,
            "evidence": {
                "phase2_section": {
                    "chainage_status": section_source.get("chainage_status"),
                    "mapped_span_m": section_source.get("mapped_span_m"),
                    "ria_alignment": section_source.get("ria_alignment"),
                    "ria_road_link_ids": section_source.get("ria_road_link_ids", []),
                    "reasons": section_source.get("reasons", []),
                },
                "geometry_line_index": line["source_line_index"] if line is not None else None,
            },
            "reasons": sorted(set(section_issues)),
        }
        candidate_sections.append(section_output)

        # Existing clients receive only the exact legacy shape, and only after the
        # conservative two-direction certificate/gate projection is proven safe.
        if (
            len(direction_records) == 2
            and all(direction_records[name]["status"] == "CERTIFIED" for name in ("forward", "reverse"))
            and all(direction_records[name]["release_state"] == "ACTIVE" for name in ("forward", "reverse"))
            and all(all(value == "PASS" for value in direction_records[name]["gates"].values()) for name in ("forward", "reverse"))
            and direction_records["forward"]["cert_len_m"] == direction_records["reverse"]["cert_len_m"]
            and geometry is not None
            and geometry["limit_by_category"]
        ):
            zones.append({
                "id": key,
                "name_bg": section_output["name_bg"],
                "road": section_output["road"],
                "cert_len_m": direction_records["forward"]["cert_len_m"],
                "polyline": geometry["normalized_polyline"],
                "start_heading": geometry["start_heading"],
                "limit_by_category": geometry["limit_by_category"],
            })

    # Preserve all raw lines in KML order; unresolved/duplicate lines are held and
    # globally suppress the legacy projection until reconciliation is complete.
    for line in parsed["lines"]:
        if line is not None and line["source_line_index"] not in matched_line_indices and line["match_status"] != "DUPLICATE":
            unmatched_line_count += 1
    reconciliation_pass = (
        len(parsed["lines"]) == len(identities)
        and len(matched_line_indices) == len(identities)
        and unmatched_line_count == 0
        and duplicate_lines == 0
        and all(line["match_status"] == "MATCHED" for line in parsed["lines"])
    )
    # Unmatched extras block projection globally. An unexplained duplicate is
    # attributed to its exact section and holds that section without marking
    # every other unambiguous physical section as an artificial geometry miss.
    if unmatched_line_count > 0 or duplicate_lines > 0:
        zones = []

    if generated_at is None:
        generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    if not isinstance(bundle_version, str) or not re.fullmatch(r"v(?:0|[1-9][0-9]*)", bundle_version):
        raise CandidateError("bundle_version must match vN")

    source_path = verification.get("source_path")
    source_digest = verification.get("source_sha256")
    if not _sha256(source_digest):
        raise CandidateError("Phase 2 verification must include a valid source_sha256")
    if verification_sha256 is None:
        verification_sha256 = hashlib.sha256(_canonical_json(verification).encode("utf-8")).hexdigest()
    if kml_sha256 is None:
        kml_bytes = kml if isinstance(kml, bytes) else kml.encode("utf-8")
        kml_sha256 = hashlib.sha256(kml_bytes).hexdigest()
    if certificate_evidence_sha256 is None and certificate_evidence is not None:
        certificate_evidence_sha256 = hashlib.sha256(
            _canonical_json(certificate_evidence).encode("utf-8")
        ).hexdigest()
    source_hashes = {
        "verification_sha256": verification_sha256,
        "kml_sha256": kml_sha256,
        "certificate_evidence_sha256": certificate_evidence_sha256,
        "km_points_sha256": verification.get("km_points_sha256"),
        "ria_road_links_sha256": verification.get("ria_road_links_sha256"),
    }
    source = {
        "url": str(source_path or "phase2:zone_verifier"),
        "snapshot_sha256": source_digest,
        "retrieved_at": verification.get("generated_at") or generated_at,
        "sha256": source_hashes,
        "paths": {
            "verification": str(verification.get("verification_path") or ""),
            "kml": str(verification.get("kml_path") or ""),
            "bgtoll_snapshot": str(source_path or ""),
            "km_points": str(verification.get("km_points_path") or ""),
            "ria_road_links": str(verification.get("ria_road_links_path") or ""),
        },
    }
    status_counts = Counter(
        direction["status"]
        for section in candidate_sections
        for direction in section["directions"].values()
    )
    release_counts = Counter(
        direction["release_state"]
        for section in candidate_sections
        for direction in section["directions"].values()
    )
    line_inventory = parsed["lines"]
    geometry_inventory = {
        "placemark_count": parsed["placemark_count"],
        "line_count": parsed["line_count"],
        "point_placemark_count": parsed["point_placemark_count"],
        "point_count": parsed["point_count"],
        "matched_line_count": sum(line["match_status"] == "MATCHED" for line in line_inventory),
        "unmatched_line_count": unmatched_line_count,
        "duplicate_line_count": duplicate_lines,
        "reconciliation_status": "PASS" if reconciliation_pass else "QUARANTINE",
        "lines": line_inventory,
        "points": parsed["points"],
    }
    return {
        "schema_version": 1,
        "bundle_version": bundle_version,
        "generated_at": generated_at,
        "source": source,
        "geometry_inventory": geometry_inventory,
        "certificate_inventory": {
            "image_count": len(certificate_evidence.get("images", [])) if certificate_evidence else 0,
            "unmapped_image_count": len(unmapped_evidence),
        },
        "summary": {
            "row_count": 82,
            "section_count": 41,
            "direction_count": sum(len(section["directions"]) for section in candidate_sections),
            "geometry_line_count": parsed["line_count"],
            "geometry_matched_count": len(matched_line_indices),
            "geometry_unmatched_count": unmatched_line_count,
            "geometry_duplicate_count": duplicate_lines,
            "status_counts": {key: status_counts[key] for key in sorted(status_counts)},
            "release_state_counts": {key: release_counts[key] for key in sorted(release_counts)},
            "legacy_zone_count": len(zones),
        },
        "sections": candidate_sections,
        "zones": zones,
    }


def copy_vertices(vertices: list[list[float]]) -> list[list[float]]:
    """Copy nested numeric source vertices so output metadata cannot mutate KML parse data."""
    return [list(vertex) for vertex in vertices]


def main(argv: list[str] | None = None) -> int:
    """Run candidate assembly CLI, defaulting to dry-run and protecting zones.json."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verification", required=True, type=Path, help="Phase 2 JSON report")
    parser.add_argument("--kml", required=True, type=Path, help="local official KML snapshot")
    parser.add_argument("--certificate-evidence", type=Path, help="optional Phase 3b certificate evidence JSON")
    parser.add_argument("--output", required=True, type=Path, help="candidate output path (not written unless --write)")
    parser.add_argument("--bundle-version", default="v5", help="schema-1 bundle version (default: v5)")
    parser.add_argument("--write", action="store_true", help="write candidate JSON to --output")
    args = parser.parse_args(argv)
    if args.write and args.output.name.casefold() == "zones.json":
        print("candidate_builder: refusing to overwrite zones.json", file=sys.stderr)
        return 2
    try:
        verification, verification_bytes = _load_json(args.verification)
        kml_bytes = args.kml.read_bytes()
        certificate_evidence = None
        certificate_bytes = None
        if args.certificate_evidence is not None:
            certificate_evidence, certificate_bytes = _load_json(args.certificate_evidence)
        candidate = build_candidate(
            verification,
            kml_bytes,
            certificate_evidence,
            bundle_version=args.bundle_version,
            verification_sha256=hashlib.sha256(verification_bytes).hexdigest(),
            kml_sha256=hashlib.sha256(kml_bytes).hexdigest(),
            certificate_evidence_sha256=(hashlib.sha256(certificate_bytes).hexdigest() if certificate_bytes is not None else None),
        )
        candidate["source"]["paths"].update({
            "verification": str(args.verification),
            "kml": str(args.kml),
            "certificate_evidence": str(args.certificate_evidence or ""),
        })
        wrote = False
        if args.write:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(_canonical_json(candidate), encoding="utf-8")
            wrote = True
        summary = dict(candidate["summary"])
        summary["candidate_path"] = str(args.output)
        summary["wrote"] = wrote
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, UnicodeError, CandidateError, ValueError, TypeError, KeyError, OverflowError) as error:
        print(f"candidate_builder: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
