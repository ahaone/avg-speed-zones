#!/usr/bin/env python3
"""Validate, deterministically serialize, and atomically publish schema-1 bundles."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timedelta
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
from typing import Any


_VERSION_RE = re.compile(r"^v(0|[1-9][0-9]*)$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_UTC_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)$")
_LEGACY_FIELDS = {
    "id", "name_bg", "road", "cert_len_m", "polyline", "start_heading", "limit_by_category"
}
_STATUSES = {"CERTIFIED", "PENDING_VERIFICATION"}
_RELEASE_STATES = {"ACTIVE", "UNDER_VERIFICATION", "QUARANTINED", "RETIRED"}
_GATE_VALUES = {"PASS", "REVIEW", "UNVERIFIED", "QUARANTINE"}
_GATE_NAMES = {"chainage", "ria", "length"}
_MAX_JSON_INTEGER = (1 << 53) - 1


class ValidationError(ValueError):
    """Raised when a candidate cannot safely be represented as a bundle."""


def _require(condition: bool, message: str) -> None:
    """Raise a stable validation error when a contract predicate is false."""
    if not condition:
        raise ValidationError(message)


def _is_number(value: Any) -> bool:
    """Return true only for finite JSON numeric types, excluding booleans."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _parse_utc(value: Any, field: str) -> None:
    """Require a valid ISO-8601 timestamp whose timezone offset is UTC."""
    _require(isinstance(value, str) and _UTC_TIMESTAMP_RE.fullmatch(value) is not None,
             f"{field} must be ISO-8601 UTC")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as error:
        raise ValidationError(f"{field} must be ISO-8601 UTC") from error
    _require(parsed.tzinfo is not None and parsed.utcoffset() == timedelta(0),
             f"{field} must include a UTC timezone")


def _load_json(path: Path) -> Any:
    """Load JSON while rejecting duplicate object keys and non-standard constants."""
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        """Construct an object while failing closed on repeated keys."""
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValidationError(f"duplicate JSON object key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str) -> None:
        """Reject NaN and infinities accepted by Python's permissive decoder."""
        raise ValidationError(f"non-finite JSON number {value} is not allowed")

    def safe_integer(text: str) -> int:
        """Reject integers outside exact IEEE-754 JSON consumer range."""
        value = int(text)
        if abs(value) > _MAX_JSON_INTEGER:
            raise ValidationError("JSON integer exceeds the interoperable safe-integer range")
        return value

    try:
        return json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
            parse_int=safe_integer,
        )
    except ValidationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValidationError(f"cannot read JSON from {path}: {error}") from error


def load_candidate(path: Path) -> dict[str, Any]:
    """Load a candidate JSON object, rejecting malformed or non-object input."""
    candidate = _load_json(path)
    _require(isinstance(candidate, dict), "candidate must be a JSON object")
    return candidate


def _validate_json_value(value: Any, path: str = "candidate") -> None:
    """Ensure arbitrary additive fields remain finite, JSON-compatible values."""
    if isinstance(value, dict):
        for key, child in value.items():
            _require(isinstance(key, str), f"{path} object keys must be strings")
            _validate_json_value(child, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _validate_json_value(child, f"{path}[{index}]")
        return
    if isinstance(value, float):
        _require(math.isfinite(value), f"{path} must be a finite number")
        return
    if isinstance(value, int) and not isinstance(value, bool):
        _require(abs(value) <= _MAX_JSON_INTEGER,
                 f"{path} integer exceeds the interoperable safe-integer range")
        return
    _require(value is None or isinstance(value, (str, bool, int)),
             f"{path} has a value that is not JSON-compatible")


def _validate_source(source: Any) -> None:
    """Validate source provenance and require a SHA-256 digest."""
    _require(isinstance(source, dict), "source must be an object; provenance incomplete")
    required = {"url", "snapshot_sha256", "retrieved_at"}
    _require(required.issubset(source),
             f"source missing provenance fields: {', '.join(sorted(required - source.keys()))}")
    _require(isinstance(source.get("url"), str) and bool(source["url"].strip()),
             "source.url must be a non-empty string; provenance incomplete")
    _require(isinstance(source.get("snapshot_sha256"), str)
             and _SHA256_RE.fullmatch(source["snapshot_sha256"]) is not None,
             "source.snapshot_sha256 must be a 64-character SHA-256; provenance incomplete")
    _parse_utc(source.get("retrieved_at"), "source.retrieved_at")


def _validate_zone(zone: Any, index: int) -> None:
    """Validate the complete legacy zone shape and finite coordinate pairs."""
    prefix = f"zones[{index}]"
    _require(isinstance(zone, dict), f"{prefix} must be an object")
    missing = sorted(_LEGACY_FIELDS - zone.keys())
    _require(not missing, f"{prefix} missing legacy fields: {', '.join(missing)}")
    for field in ("id", "name_bg", "road"):
        _require(isinstance(zone[field], str) and bool(zone[field].strip()),
                 f"{prefix}.{field} must be a non-empty string")
    length = zone["cert_len_m"]
    _require(isinstance(length, int) and not isinstance(length, bool) and length > 0,
             f"{prefix}.cert_len_m must be a positive integer")
    heading = zone["start_heading"]
    _require(_is_number(heading) and 0 <= heading < 360,
             f"{prefix}.start_heading must be a finite number from 0 inclusive to 360 exclusive")
    limits = zone["limit_by_category"]
    _require(isinstance(limits, dict) and bool(limits), f"{prefix}.limit_by_category must be a non-empty object")
    for category, speed in limits.items():
        _require(isinstance(category, str) and bool(category), f"{prefix}.limit_by_category keys must be strings")
        _require(speed is None or (isinstance(speed, int) and not isinstance(speed, bool) and speed > 0),
                 f"{prefix}.limit_by_category[{category}] must be a positive integer or null")
    polyline = zone["polyline"]
    _require(isinstance(polyline, list) and len(polyline) >= 2,
             f"{prefix}.polyline must contain at least two coordinates")
    for point_index, point in enumerate(polyline):
        _require(isinstance(point, list) and len(point) == 2,
                 f"{prefix}.polyline[{point_index}] must be [latitude, longitude]")
        lat, lon = point
        _require(_is_number(lat) and _is_number(lon),
                 f"{prefix}.polyline[{point_index}] coordinates must be finite numbers")
        _require(-90 <= lat <= 90 and -180 <= lon <= 180,
                 f"{prefix}.polyline[{point_index}] coordinates outside WGS84 bounds")


def _direction_is_publishable(direction: dict[str, Any]) -> bool:
    """Return true only for an active, certified, evidence-bound, all-pass direction."""
    length = direction.get("cert_len_m")
    gates = direction.get("gates")
    return (
        direction.get("status") == "CERTIFIED"
        and direction.get("release_state") == "ACTIVE"
        and isinstance(length, int) and not isinstance(length, bool) and length > 0
        and isinstance(direction.get("cert_url"), str) and bool(direction["cert_url"].strip())
        and isinstance(direction.get("cert_sha256"), str)
        and _SHA256_RE.fullmatch(direction["cert_sha256"]) is not None
        and isinstance(gates, dict)
        and set(gates) == _GATE_NAMES
        and all(gates[name] == "PASS" for name in _GATE_NAMES)
    )


def _validate_direction(direction: Any, context: str) -> None:
    """Validate direction fields, evidence, enumerations, and release consistency."""
    _require(isinstance(direction, dict), f"{context} must be an object")
    required = {"name_bg", "status", "release_state", "cert_len_m", "cert_url", "cert_sha256", "gates"}
    _require(required.issubset(direction), f"{context} missing fields: {', '.join(sorted(required - direction.keys()))}")
    _require(isinstance(direction["name_bg"], str) and bool(direction["name_bg"].strip()),
             f"{context}.name_bg must be a non-empty string")
    _require(isinstance(direction["status"], str) and direction["status"] in _STATUSES,
             f"{context}.status has an invalid value")
    _require(isinstance(direction["release_state"], str)
             and direction["release_state"] in _RELEASE_STATES,
             f"{context}.release_state has an invalid value")
    length = direction["cert_len_m"]
    _require(length is None or (isinstance(length, int) and not isinstance(length, bool) and length > 0),
             f"{context}.cert_len_m must be a positive integer or null")
    url, digest = direction["cert_url"], direction["cert_sha256"]
    _require(url is None or (isinstance(url, str) and bool(url.strip())),
             f"{context}.cert_url must be a non-empty string or null")
    _require(digest is None or (isinstance(digest, str) and _SHA256_RE.fullmatch(digest) is not None),
             f"{context}.cert_sha256 must be a 64-character SHA-256 or null")
    # Non-active records may retain partial provenance, but ACTIVE always requires all evidence.
    evidence_complete = length is not None and url is not None and digest is not None
    gates = direction["gates"]
    _require(isinstance(gates, dict) and set(gates) == _GATE_NAMES,
             f"{context}.gates must contain exactly chainage, ria, and length")
    for gate_name, result in gates.items():
        _require(isinstance(result, str) and result in _GATE_VALUES,
                 f"{context}.gates.{gate_name} has an invalid result")
    if direction["release_state"] == "ACTIVE":
        _require(direction["status"] == "CERTIFIED", f"{context} cannot be ACTIVE unless CERTIFIED")
        _require(evidence_complete, f"{context} cannot be ACTIVE without certificate evidence")
        _require(all(gates[name] == "PASS" for name in _GATE_NAMES),
                 f"{context} cannot be ACTIVE unless all gates PASS")


def _validate_section(section: Any, index: int) -> set[str]:
    """Validate section identity and one or two directional records."""
    prefix = f"sections[{index}]"
    _require(isinstance(section, dict), f"{prefix} must be an object")
    required = {"section_key", "road", "name_bg", "directions"}
    _require(required.issubset(section), f"{prefix} missing fields: {', '.join(sorted(required - section.keys()))}")
    for field in ("section_key", "road", "name_bg"):
        _require(isinstance(section[field], str) and bool(section[field].strip()),
                 f"{prefix}.{field} must be a non-empty string")
    directions = section["directions"]
    _require(isinstance(directions, dict) and bool(directions) and set(directions) <= {"forward", "reverse"},
             f"{prefix}.directions must contain forward/reverse direction records")
    direction_ids = set(directions)
    for direction_name, direction in directions.items():
        _validate_direction(direction, f"{prefix}.directions.{direction_name}")
    if direction_ids != {"forward", "reverse"}:
        only = next(iter(directions.values()))
        _require(only["release_state"] in {"UNDER_VERIFICATION", "QUARANTINED"},
                 f"{prefix} with one direction must be held under verification or quarantined")
    return direction_ids


def _previous_version(path: Path | None) -> str | None:
    """Return existing previous bundle's validated version, or None when absent."""
    if path is None or not path.exists():
        return None
    previous = _load_json(path)
    _require(isinstance(previous, dict), "previous bundle must be a JSON object")
    version = previous.get("bundle_version")
    _require(isinstance(version, str) and _VERSION_RE.fullmatch(version) is not None,
             "previous bundle has an invalid bundle_version")
    return version


def _validate_candidate(candidate: dict[str, Any], previous_version: str | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None, int]:
    """Validate schema, metadata, legacy zones, optional sections, and their mapping."""
    _validate_json_value(candidate)
    _require(candidate.get("schema_version") == 1 and not isinstance(candidate.get("schema_version"), bool),
             "schema_version must equal integer 1")
    version = candidate.get("bundle_version")
    _require(isinstance(version, str) and _VERSION_RE.fullmatch(version) is not None,
             "bundle_version must match vN with a non-negative integer N")
    if previous_version is not None:
        current_match = _VERSION_RE.fullmatch(version)
        previous_match = _VERSION_RE.fullmatch(previous_version)
        _require(current_match is not None and previous_match is not None, "invalid bundle version")
        _require(int(current_match.group(1)) > int(previous_match.group(1)),
                 f"bundle_version {version} must be strictly greater than previous {previous_version}")
    _parse_utc(candidate.get("generated_at"), "generated_at")
    _validate_source(candidate.get("source"))
    zones = candidate.get("zones")
    _require(isinstance(zones, list) and bool(zones), "zones must be a non-empty array")
    zone_list: list[dict[str, Any]] = zones
    zone_ids: set[str] = set()
    for index, zone in enumerate(zones):
        _validate_zone(zone, index)
        _require(zone["id"] not in zone_ids, f"duplicate zone id: {zone['id']}")
        zone_ids.add(zone["id"])
    has_sections = "sections" in candidate
    _require(not has_sections or isinstance(candidate["sections"], list), "sections must be an array when present")
    raw_sections = candidate["sections"] if has_sections else None
    if isinstance(raw_sections, list):
        sections: list[dict[str, Any]] | None = raw_sections
    else:
        sections = None
    section_ids: set[str] = set()
    sections_by_key: dict[str, dict[str, Any]] = {}
    if sections is not None:
        for index, section in enumerate(sections):
            _validate_section(section, index)
            key = section["section_key"]
            _require(key not in section_ids, f"duplicate section_key: {key}")
            section_ids.add(key)
            sections_by_key[key] = section
        for zone in zones:
            section = sections_by_key.get(zone["id"])
            _require(section is not None,
                     f"legacy zone {zone['id']} has no matching section; cannot prove conservative projection")
            _require(section["road"] == zone["road"], f"legacy zone {zone['id']} road does not match its section")
            directions = section["directions"]
            _require(set(directions) == {"forward", "reverse"},
                     f"legacy zone {zone['id']} section must have both directions")
            for direction_name in ("forward", "reverse"):
                _require(_direction_is_publishable(directions[direction_name]),
                         f"legacy zone {zone['id']} rejected: {direction_name} is not certified, active, certificate-bound, and gate-clean")
            _require(zone["cert_len_m"] == directions["forward"]["cert_len_m"]
                     and zone["cert_len_m"] == directions["reverse"]["cert_len_m"],
                     f"legacy zone {zone['id']} certificate length must match both directions")
    section_count = len(section_ids)
    return zone_list, sections, section_count


def _canonical_bytes(value: Any) -> bytes:
    """Serialize JSON deterministically with Unicode preserved and a final newline."""
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def build_artifacts(candidate: dict[str, Any], previous_version: str | None = None) -> tuple[bytes, bytes, dict[str, Any]]:
    """Validate and return deterministic bundle bytes, manifest bytes, and summary."""
    zones, sections, section_count = _validate_candidate(candidate, previous_version)
    ordered_zones = sorted(zones, key=lambda zone: (zone["road"], zone["id"]))
    payload = dict(candidate)
    payload["zones"] = ordered_zones
    if sections is not None:
        payload["sections"] = sorted(sections, key=lambda section: (section["road"], section["section_key"]))
    bundle_bytes = _canonical_bytes(payload)
    bundle_sha256 = hashlib.sha256(bundle_bytes).hexdigest()
    directions: list[dict[str, Any]] = []
    status_counts: Counter[str] = Counter()
    if sections is not None:
        for section in sections:
            for direction in section["directions"].values():
                directions.append(direction)
                status_counts[direction["status"]] += 1
    manifest = {
        "manifest_version": 1,
        "schema_version": candidate["schema_version"],
        "bundle_version": candidate["bundle_version"],
        "generated_at": candidate["generated_at"],
        "bundle_sha256": bundle_sha256,
        "bundle_bytes": len(bundle_bytes),
        "counts": {
            "zones": len(ordered_zones),
            "sections": section_count,
            "directions": len(directions),
            "status_counts": {status: status_counts.get(status, 0) for status in sorted(_STATUSES)},
        },
        "sources": candidate["source"],
    }
    manifest_bytes = _canonical_bytes(manifest)
    holds = []
    for section in sections or []:
        directions_by_name = section["directions"]
        if set(directions_by_name) != {"forward", "reverse"} or not all(
            _direction_is_publishable(directions_by_name[name]) for name in ("forward", "reverse")
        ):
            holds.append(section["section_key"])
    summary = {
        "candidate_version": candidate["bundle_version"],
        "previous_version": previous_version,
        "zone_count": len(ordered_zones),
        "section_count": section_count,
        "direction_count": len(directions),
        "exclusions": len(holds),
        "holds": holds,
        "hold_count": len(holds),
        "bundle_sha256": bundle_sha256,
        "wrote": False,
    }
    return bundle_bytes, manifest_bytes, summary


def _write_temp(directory: Path, prefix: str, payload: bytes) -> Path:
    """Create and fsync a temporary file inside the destination directory."""
    descriptor, raw_path = tempfile.mkstemp(prefix=prefix, dir=directory)
    path = Path(raw_path)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def publish_files(output_dir: Path, bundle_bytes: bytes, manifest_bytes: bytes) -> None:
    """Atomically replace bundle/manifest as a rollback-protected pair."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    bundle_path = output_dir / "zones.json"
    manifest_path = output_dir / "manifest.json"
    backup_path = output_dir / "zones.json.bak"
    old_bundle = bundle_path.read_bytes() if bundle_path.exists() else None
    old_manifest = manifest_path.read_bytes() if manifest_path.exists() else None
    old_backup = backup_path.read_bytes() if backup_path.exists() else None
    temps: list[Path] = []
    changed_bundle = False
    changed_manifest = False
    backup_replaced = False
    try:
        # Prepare both complete payloads before touching either published file.
        bundle_temp = _write_temp(output_dir, ".zones.json.", bundle_bytes)
        temps.append(bundle_temp)
        manifest_temp = _write_temp(output_dir, ".manifest.json.", manifest_bytes)
        temps.append(manifest_temp)
        if old_bundle is not None:
            backup_temp = _write_temp(output_dir, ".zones.json.bak.", old_bundle)
            temps.append(backup_temp)
            os.replace(backup_temp, backup_path)
            temps.remove(backup_temp)
            backup_replaced = True
        os.replace(bundle_temp, bundle_path)
        temps.remove(bundle_temp)
        changed_bundle = True
        os.replace(manifest_temp, manifest_path)
        temps.remove(manifest_temp)
        changed_manifest = True
    except BaseException:
        # Restore the original visible pair and backup after any failed replacement.
        if changed_bundle:
            if old_bundle is None:
                bundle_path.unlink(missing_ok=True)
            else:
                restore = _write_temp(output_dir, ".zones.json.restore.", old_bundle)
                os.replace(restore, bundle_path)
        if changed_manifest:
            if old_manifest is None:
                manifest_path.unlink(missing_ok=True)
            else:
                restore = _write_temp(output_dir, ".manifest.json.restore.", old_manifest)
                os.replace(restore, manifest_path)
        if backup_replaced:
            if old_backup is None:
                backup_path.unlink(missing_ok=True)
            else:
                restore = _write_temp(output_dir, ".zones.json.bak.restore.", old_backup)
                os.replace(restore, backup_path)
        raise
    finally:
        for temporary in temps:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    """Run CLI validation and optional atomic publishing, emitting JSON summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path, help="candidate JSON path")
    parser.add_argument("--previous", type=Path, help="previous bundle for strict version check")
    parser.add_argument("--output-dir", type=Path, help="directory for zones.json and manifest.json")
    parser.add_argument("--write", action="store_true", help="atomically publish the validated pair")
    args = parser.parse_args(argv)
    try:
        if args.write and args.output_dir is None:
            raise ValidationError("--write requires --output-dir")
        candidate = load_candidate(args.candidate)
        previous_version = _previous_version(args.previous)
        bundle_bytes, manifest_bytes, summary = build_artifacts(candidate, previous_version)
        if args.write:
            assert args.output_dir is not None  # Checked before publishing.
            publish_files(args.output_dir, bundle_bytes, manifest_bytes)
            summary["wrote"] = True
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValidationError) as error:
        print(json.dumps({"error": str(error), "wrote": False}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
