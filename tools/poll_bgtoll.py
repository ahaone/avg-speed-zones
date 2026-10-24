#!/usr/bin/env python3
"""Poll official BGTOLL map and WIM sources with bounded, fail-closed state."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import html
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
from typing import Any, Callable
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

try:
    from . import candidate_builder, publish_bundle, zone_verifier
    from .bgtoll_inventory import parse_inventory
except ImportError:
    import candidate_builder
    import publish_bundle
    import zone_verifier
    from bgtoll_inventory import parse_inventory

_FRESHNESS_PATH = Path(__file__).with_name("bundle-freshness.py")
_FRESHNESS_SPEC = importlib.util.spec_from_file_location("poll_bgtoll_bundle_freshness", _FRESHNESS_PATH)
if _FRESHNESS_SPEC is None or _FRESHNESS_SPEC.loader is None:
    raise ImportError(f"cannot load Phase 1 freshness helper at {_FRESHNESS_PATH}")
_bundle_freshness_module = importlib.util.module_from_spec(_FRESHNESS_SPEC)
sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    _FRESHNESS_SPEC.loader.exec_module(_bundle_freshness_module)
finally:
    sys.path.pop(0)
bundle_freshness = _bundle_freshness_module

MAP_URL = "https://www.google.com/maps/d/u/0/kml?mid=1bydr93x-u18Oz3lm7ngmWhVTgsP2Eys&forcekml=1"
WIM_URL = "https://www.bgtoll.bg/vaprosi-i-otgovori/sredna-skorost/wim"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_MAP_BYTES = 4 * 1024 * 1024
ROLLING_WINDOW_SECONDS = 24 * 60 * 60
MAX_WIM_REQUESTS_PER_24H = 2
MIN_WIM_REQUEST_INTERVAL_SECONDS = 5 * 60
MAP_POLL_INTERVAL_SECONDS = 5 * 60
WIM_CHANGE_DEBOUNCE_SECONDS = 120
MAX_BACKOFF_SECONDS = 6 * 60 * 60
_BACKOFF_STEPS_SECONDS = (15 * 60, 30 * 60, 60 * 60, 2 * 60 * 60, 4 * 60 * 60, 6 * 60 * 60)
_POLL_LOCK = threading.Lock()


class PollError(ValueError):
    """Raised for malformed source content or invalid poller state."""


@dataclass(frozen=True)
class PollConfig:
    """Hold filesystem paths, source endpoints, and bounded cadence settings."""

    repo_root: Path
    state_dir: Path
    baseline_path: Path
    map_url: str = MAP_URL
    wim_url: str = WIM_URL
    source_file: Path | None = None
    map_file: Path | None = None
    km_points_path: Path | None = None
    kml_path: Path | None = None
    ria_path: Path | None = None
    certificate_evidence_path: Path | None = None
    publish_dir: Path | None = None
    max_stale_seconds: int = 1800
    timeout_seconds: float = 30.0
    max_source_bytes: int = MAX_SOURCE_BYTES
    max_map_bytes: int = MAX_MAP_BYTES
    max_wim_requests_per_24h: int = MAX_WIM_REQUESTS_PER_24H
    min_wim_request_interval_seconds: int = MIN_WIM_REQUEST_INTERVAL_SECONDS
    map_poll_interval_seconds: int = MAP_POLL_INTERVAL_SECONDS
    wim_max_age_seconds: int = ROLLING_WINDOW_SECONDS
    wim_change_debounce_seconds: int = WIM_CHANGE_DEBOUNCE_SECONDS

    @property
    def status_path(self) -> Path:
        """Return the durable heartbeat/status document path."""
        return self.state_dir / "poller-status.json"

    @property
    def freshness_path(self) -> Path:
        """Return the Phase 1 semantic freshness state path."""
        return self.state_dir / "bundle-freshness.json"



def _utc(value: datetime) -> str:
    """Serialize a timestamp in normalized second-precision UTC form."""
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_time(value: Any) -> datetime | None:
    """Parse a persisted timezone-aware ISO timestamp, returning None if invalid."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _atomic_write(path: Path, payload: bytes) -> None:
    """Atomically persist bytes in the target directory and fsync file and directory."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _canonical_json(value: Any) -> bytes:
    """Serialize JSON deterministically with Unicode preserved and finite values only."""
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")


def _load_status(path: Path) -> dict[str, Any]:
    """Load poller state, falling back to empty state while exposing parse failures."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        value = {}
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise PollError(f"STATE_INVALID: {error}") from error
    if not isinstance(value, dict):
        raise PollError("STATE_INVALID: status root must be an object")
    value.setdefault("schema_version", 1)
    value.setdefault("map", {})
    value.setdefault("wim", {})
    value.setdefault("last_success", None)
    value.setdefault("last_good_bundle", None)
    return value


def _store_snapshot(state_dir: Path, source_kind: str, payload: bytes, suffix: str) -> tuple[str, Path]:
    """Store one content-addressed immutable capture and return digest and path."""
    digest = hashlib.sha256(payload).hexdigest()
    path = state_dir / "snapshots" / source_kind / f"{digest}.{suffix}"
    if not path.exists():
        _atomic_write(path, payload)
    elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise OSError(f"content-addressed snapshot collision at {path}")
    return digest, path


def _local_name(tag: str) -> str:
    """Return an XML element's local name without its namespace URI."""
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _normalise_description(value: str) -> str:
    """Strip markup, decode entities, and collapse presentation whitespace."""
    return " ".join(re.sub(r"<[^>]*>", " ", html.unescape(value)).replace("\xa0", " ").split())


def _normalise_coordinates(text: str, context: str) -> list[list[float]]:
    """Parse KML coordinates into finite, range-checked longitude/latitude pairs."""
    values: list[list[float]] = []
    for index, token in enumerate(text.split()):
        components = token.split(",")
        if not 2 <= len(components) <= 3:
            raise PollError(f"{context}: malformed coordinate token {index}")
        try:
            longitude, latitude = float(components[0]), float(components[1])
            if len(components) == 3 and components[2] and not math.isfinite(float(components[2])):
                raise ValueError("non-finite altitude")
        except ValueError as error:
            raise PollError(f"{context}: non-numeric coordinate token {index}") from error
        if not math.isfinite(longitude) or not math.isfinite(latitude):
            raise PollError(f"{context}: non-finite coordinate token {index}")
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise PollError(f"{context}: coordinate token {index} is outside WGS84 bounds")
        values.append([0.0 if longitude == 0 else longitude, 0.0 if latitude == 0 else latitude])
    if not values:
        raise PollError(f"{context}: empty coordinates")
    return values


def normalize_map_document(kml: bytes | str) -> dict[str, Any]:
    """Parse official KML placemarks into canonical folder, text, and geometry records."""
    raw = kml.encode("utf-8") if isinstance(kml, str) else kml
    if not raw or len(raw) > MAX_MAP_BYTES:
        raise PollError(f"map KML is empty or exceeds {MAX_MAP_BYTES} bytes")
    try:
        root = ET.fromstring(raw)
    except (ET.ParseError, ValueError) as error:
        raise PollError(f"cannot parse map KML: {error}") from error
    parents = {child: parent for parent in root.iter() for child in parent}
    placemarks: list[dict[str, Any]] = []
    for index, placemark in enumerate(node for node in root.iter() if _local_name(node.tag) == "Placemark"):
        folder_names: list[str] = []
        parent = parents.get(placemark)
        while parent is not None:
            if _local_name(parent.tag) == "Folder":
                name = next((child.text for child in parent if _local_name(child.tag) == "name"), None)
                if name:
                    folder_names.append(" ".join(name.split()))
            parent = parents.get(parent)
        folder_path = list(reversed(folder_names))
        name_node = next((child for child in placemark if _local_name(child.tag) == "name"), None)
        description_node = next((child for child in placemark if _local_name(child.tag) == "description"), None)
        name = "" if name_node is None or name_node.text is None else " ".join(name_node.text.split())
        description = "" if description_node is None or description_node.text is None else _normalise_description(description_node.text)
        geometries: list[dict[str, Any]] = []
        for geometry in placemark.iter():
            kind = _local_name(geometry.tag)
            if kind not in {"Point", "LineString"}:
                continue
            coordinates_node = next((child for child in geometry.iter() if _local_name(child.tag) == "coordinates"), None)
            if coordinates_node is None or coordinates_node.text is None:
                raise PollError(f"Placemark {index} {kind} has no coordinates")
            coordinates = _normalise_coordinates(coordinates_node.text, f"Placemark {index} {kind}")
            if kind == "LineString" and len(coordinates) < 2:
                raise PollError(f"Placemark {index} LineString has fewer than two coordinates")
            geometries.append({"kind": kind, "coordinates": coordinates})
        if not geometries:
            raise PollError(f"Placemark {index} contains no supported geometry")
        placemarks.append({"folders": folder_path, "name": name, "description": description, "geometries": geometries})
    if not placemarks:
        raise PollError("map KML contains no placemarks")
    placemarks.sort(key=lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    counts = {
        "placemarks": len(placemarks),
        "lines": sum(item["kind"] == "LineString" for mark in placemarks for item in mark["geometries"]),
        "points": sum(item["kind"] == "Point" for mark in placemarks for item in mark["geometries"]),
        "folders": len({tuple(mark["folders"]) for mark in placemarks if mark["folders"]}),
    }
    return {"placemarks": placemarks, "counts": counts}


def normalized_map_digest(kml: bytes | str) -> str:
    """Return SHA-256 over normalized placemark records, not raw KML bytes."""
    parsed = normalize_map_document(kml)
    return hashlib.sha256(_canonical_json(parsed["placemarks"])).hexdigest()


def _save_status(config: PollConfig, status: dict[str, Any]) -> None:
    """Atomically persist the complete poller status document."""
    encoded = json.dumps(status, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    _atomic_write(config.status_path, encoded.encode("utf-8"))


def _status_result(status: dict[str, Any], now: datetime, config: PollConfig) -> dict[str, Any]:
    """Build the one-line JSON health report from persistent status fields."""
    success = status.get("last_success") if isinstance(status.get("last_success"), dict) else None
    success_at = _parse_time(success.get("retrieved_at")) if success else None
    age = None if success_at is None else max(0, int((now - success_at).total_seconds()))
    if success is not None and success_at is None:
        success["invalid_retrieved_at"] = True
    health = "STALE" if age is None or age > config.max_stale_seconds else (
        "DEGRADED" if status.get("state") in {"SOURCE_BLOCKED", "FAILED", "HELD"} else "OK"
    )
    status["last_success_age_seconds"] = age
    status["health"] = health
    status["stale_after_seconds"] = config.max_stale_seconds
    _save_status(config, status)
    return {
        "state": status.get("state", "UNKNOWN"), "health": health,
        "last_attempt": status.get("last_attempt"), "last_success": success,
        "last_success_age_seconds": age, "source_sha256": status.get("source_sha256"),
        "counts": status.get("counts", {}), "current_tier": status.get("current_tier"),
        "current_backoff": status.get("current_backoff"), "skip_reason": status.get("skip_reason"),
        "source_change_pending": status.get("source_change_pending", False),
        "map_changed": status.get("map_changed", False), "wim_requested": status.get("wim_requested", False),
        "source_changed": status.get("source_changed", False), "diff": status.get("diff", {"added": [], "removed": []}),
        "holds": status.get("holds", []), "alerts": status.get("alerts", []),
        "published": status.get("published_this_attempt", False), "exit_code": status.get("exit_code", 0),
    }


def _response_header(headers: Any, name: str) -> str | None:
    """Return one stripped response header or None when missing."""
    value = headers.get(name) if hasattr(headers, "get") else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _blocked_response(headers: Any, body: bytes, status_code: int) -> bool:
    """Recognize rate limits, Cloudflare metadata, or explicit challenge-page text."""
    prefix = body[:8192].lower()
    return status_code in {403, 429} or "cloudflare" in (_response_header(headers, "Server") or "").casefold() or (
        _response_header(headers, "CF-Ray") is not None or b"attention required" in prefix or b"you have been blocked" in prefix
    )


def _fetch(url: str, opener: Callable[..., Any], timeout: float, limit: int) -> tuple[bytes | None, dict[str, Any], str | None]:
    """Make one bounded GET, returning body, response metadata, and transport error."""
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/vnd.google-earth.kml+xml,application/vnd.google-earth.kmz,application/xml,text/html,*/*;q=0.8",
        "Accept-Language": "bg-BG,bg;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept-Encoding": "identity",
        "Upgrade-Insecure-Requests": "1",
    })
    try:
        with opener(request, timeout=timeout) as response:
            status_code = getattr(response, "status", None) or response.getcode()
            headers = response.headers
            body = response.read(limit + 1)
        if len(body) > limit:
            raise PollError(f"source response exceeds {limit} bytes")
        return body, {"http_status": status_code, "cf_ray": _response_header(headers, "CF-Ray"),
                      "server": _response_header(headers, "Server"), "content_type": _response_header(headers, "Content-Type"),
                      "attention_required": b"attention required" in body[:8192].lower() or b"you have been blocked" in body[:8192].lower()}, None
    except urllib.error.HTTPError as error:
        try:
            body = error.read(min(limit, 8192))
        except OSError:
            body = b""
        metadata = {"http_status": error.code, "cf_ray": _response_header(error.headers, "CF-Ray"),
                    "server": _response_header(error.headers, "Server"), "retry_after": _response_header(error.headers, "Retry-After"),
                    "attention_required": b"attention required" in body[:8192].lower()}
        error.close()
        return body, metadata, None
    except (urllib.error.URLError, TimeoutError, OSError, PollError, ValueError) as error:
        return None, {"http_status": None}, str(error)


def _retry_after(value: str | None, now: datetime) -> int | None:
    """Parse Retry-After seconds or an HTTP date into a non-negative delay."""
    if not value:
        return None
    try:
        return max(0, int(float(value)))
    except ValueError:
        try:
            target = parsedate_to_datetime(value)
            if target.tzinfo is None:
                target = target.replace(tzinfo=timezone.utc)
            return max(0, int((target - now).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            return None


def _rolling_times(values: Any, now: datetime) -> list[str]:
    """Filter request timestamps to the preceding rolling 24 hours."""
    if not isinstance(values, list):
        return []
    cutoff = now - timedelta(seconds=ROLLING_WINDOW_SECONDS)
    return [_utc(parsed) for value in values if (parsed := _parse_time(value)) is not None and cutoff < parsed <= now]


def _wim_is_due(status: dict[str, Any], map_digest: str, now: datetime, config: PollConfig) -> tuple[bool, str | None]:
    """Return whether a daily WIM refresh or stable-map-change refresh is due."""
    wim = status.get("wim", {})
    last_good = wim.get("last_good") if isinstance(wim, dict) else None
    retrieved = _parse_time(last_good.get("retrieved_at")) if isinstance(last_good, dict) else None
    if retrieved is None or (now - retrieved).total_seconds() >= config.wim_max_age_seconds:
        return True, "DAILY_REFRESH"
    map_state = status.get("map", {})
    if isinstance(map_state, dict) and map_state.get("pending_wim_digest") == map_digest:
        pending_since = _parse_time(map_state.get("pending_wim_since"))
        if pending_since is not None and (now - pending_since).total_seconds() >= config.wim_change_debounce_seconds:
            return True, "DEBOUNCED_MAP_CHANGE"
    return False, None


def _current_version(path: Path) -> str:
    """Read an existing validated schema-1 bundle version, defaulting to v0."""
    if not path.exists():
        return "v0"
    current = publish_bundle._load_json(path)
    version = current.get("bundle_version") if isinstance(current, dict) else None
    if not isinstance(version, str) or re.fullmatch(r"v(0|[1-9][0-9]*)", version) is None:
        raise ValueError("existing last-known-good bundle has no valid version")
    return version


def _candidate_pipeline(
    config: PollConfig, source_path: Path, kml_path: Path, now: datetime, *, publish: bool,
    source_changed: bool,
) -> tuple[list[str], dict[str, Any] | None, dict[str, Any] | None, bool]:
    """Run verification, candidate building, publisher validation, and gated publishing."""
    holds: list[str] = []
    if config.km_points_path is None or not config.km_points_path.is_file():
        holds.append("KM_POINTS_INPUT_MISSING")
    if not kml_path.is_file():
        holds.append("KML_INPUT_MISSING")
    if holds:
        return holds, None, None, False
    verification_path = config.state_dir / "verification.json"
    candidate_path = config.state_dir / "candidate.json"
    try:
        verification = zone_verifier.build_report(source_path, config.km_points_path, config.ria_path)
        _atomic_write(verification_path, json.dumps(verification, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")
        kml_bytes = kml_path.read_bytes()
        certificate = None
        certificate_hash = None
        if config.certificate_evidence_path is not None:
            certificate_bytes = config.certificate_evidence_path.read_bytes()
            certificate = json.loads(certificate_bytes.decode("utf-8"))
            certificate_hash = hashlib.sha256(certificate_bytes).hexdigest()
        output_dir = config.publish_dir or config.repo_root
        previous_version = _current_version(output_dir / "zones.json")
        next_version = f"v{int(previous_version[1:]) + 1}"
        candidate = candidate_builder.build_candidate(
            verification, kml_bytes, certificate, generated_at=_utc(now), bundle_version=next_version,
            verification_sha256=hashlib.sha256(verification_path.read_bytes()).hexdigest(),
            kml_sha256=hashlib.sha256(kml_bytes).hexdigest(), certificate_evidence_sha256=certificate_hash,
        )
        candidate["source"]["paths"].update({"verification": str(verification_path), "kml": str(kml_path),
                                              "certificate_evidence": str(config.certificate_evidence_path or ""),
                                              "bgtoll_snapshot": str(source_path)})
        _atomic_write(candidate_path, candidate_builder.canonical_json(candidate).encode("utf-8"))
        bundle_bytes, manifest_bytes, summary = publish_bundle.build_artifacts(candidate, previous_version=previous_version)
        overview = candidate.get("summary", {})
        if (overview.get("row_count"), overview.get("section_count"), overview.get("direction_count")) != (82, 41, 82):
            holds.append("CANDIDATE_COUNTS_INCOMPLETE")
        if overview.get("geometry_inventory", {}).get("reconciliation_status") != "PASS":
            holds.append("GEOMETRY_RECONCILIATION_NOT_PASS")
        if summary.get("hold_count") != 0:
            holds.append("PUBLISHER_REPORTED_HELD_SECTIONS")
        if summary.get("zone_count", 0) <= 0:
            holds.append("NO_PUBLISHABLE_ZONES")
        if source_changed:
            holds.append("WIM_SEMANTIC_CHANGE_REQUIRES_REVIEW")
        if holds:
            return holds, candidate, summary, False
        if not publish:
            holds.append("EXPLICIT_PUBLISH_FLAG_REQUIRED")
            return holds, candidate, summary, False
        if source_changed:
            holds.append("SOURCE_CHANGE_REQUIRES_REVIEW")
            return holds, candidate, summary, False
        publish_bundle.publish_files(output_dir, bundle_bytes, manifest_bytes)
        return [], candidate, summary, True
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, OverflowError,
            publish_bundle.ValidationError, candidate_builder.CandidateError) as error:
        return [f"PIPELINE_VALIDATION_FAILED: {error}"], None, None, False


def _failure_backoff(wim: dict[str, Any], metadata: dict[str, Any], now: datetime) -> str:
    """Advance bounded failure backoff, respecting Retry-After as a minimum delay."""
    strikes = int(wim.get("block_strikes", 0)) + 1
    wim["block_strikes"] = strikes
    step = _BACKOFF_STEPS_SECONDS[min(strikes - 1, len(_BACKOFF_STEPS_SECONDS) - 1)]
    retry_after = _retry_after(metadata.get("retry_after"), now)
    delay = min(max(step, retry_after or 0), MAX_BACKOFF_SECONDS)
    wim["blocked_until"] = _utc(now + timedelta(seconds=delay))
    return wim["blocked_until"]


def _finish(
    status: dict[str, Any], config: PollConfig, now: datetime, *, state: str, holds: list[str],
    alerts: list[dict[str, Any]], exit_code: int, tier: str | None, map_changed: bool,
    wim_requested: bool, source_changed: bool = False, diff: dict[str, Any] | None = None,
    source_hash: str | None = None, counts: dict[str, Any] | None = None,
    publisher_summary: dict[str, Any] | None = None, published: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Persist current tick results and return its structured status report."""
    status.update({"state": state, "holds": holds, "alerts": alerts, "exit_code": exit_code,
                   "current_tier": tier, "current_backoff": status.get("wim", {}).get("blocked_until"),
                   "map_changed": map_changed, "wim_requested": wim_requested, "source_changed": source_changed,
                   "source_change_pending": status.get("source_change_pending", False),
                   "diff": diff or {"added": [], "removed": []}, "source_sha256": source_hash or status.get("source_sha256"),
                   "counts": counts or status.get("counts", {}), "publisher_summary": publisher_summary,
                   "published_this_attempt": published, "last_attempt": _utc(now)})
    result = _status_result(status, now, config)
    return exit_code, result


def _run_poll_unlocked(
    config: PollConfig, *, opener: Callable[..., Any], now: datetime, publish: bool,
) -> tuple[int, dict[str, Any]]:
    """Perform one finite poll tick after acquiring the process-wide single-flight mutex."""
    config.state_dir.mkdir(parents=True, exist_ok=True)
    try:
        status = _load_status(config.status_path)
    except PollError as error:
        failed_status = {"wim": {}, "map": {}, "state": "FAILED", "holds": [str(error)], "alerts": [],
                         "exit_code": 1, "current_tier": None, "current_backoff": None}
        result = _status_result(failed_status, now, config)
        result["exit_code"] = 1
        return 1, result
    status["map"] = status.get("map") if isinstance(status.get("map"), dict) else {}
    status["wim"] = status.get("wim") if isinstance(status.get("wim"), dict) else {}
    map_state, wim_state = status["map"], status["wim"]
    wim_state.setdefault("request_times", [])
    wim_state.setdefault("block_strikes", 0)
    wim_state.setdefault("blocked_until", None)
    status.pop("skip_reason", None)
    status["source_change_pending"] = bool(status.get("source_change_pending", False))
    backoff = _parse_time(wim_state.get("blocked_until"))
    if backoff is not None and now < backoff:
        status["current_tier"] = "NONE"
        status["skip_reason"] = "BACKOFF_ACTIVE"
        return _finish(status, config, now, state="SOURCE_BLOCKED", holds=["WIM_BACKOFF_ACTIVE"], alerts=[],
                       exit_code=3, tier="NONE", map_changed=False, wim_requested=False)
    try:
        if config.map_file is not None:
            map_payload = config.map_file.read_bytes()
            map_metadata: dict[str, Any] = {"http_status": 200, "offline_source_file": str(config.map_file)}
        else:
            last_map_request = _parse_time(map_state.get("last_request_at"))
            if last_map_request is not None and (now - last_map_request).total_seconds() < max(MAP_POLL_INTERVAL_SECONDS, config.map_poll_interval_seconds):
                status["skip_reason"] = "MAP_MINIMUM_INTERVAL"
                return _finish(status, config, now, state="UNCHANGED", holds=["MAP_MINIMUM_INTERVAL"], alerts=[],
                               exit_code=0, tier="NONE", map_changed=False, wim_requested=False)
            map_state["last_request_at"] = _utc(now)
            map_payload, map_metadata, map_error = _fetch(config.map_url, opener, config.timeout_seconds, config.max_map_bytes)
            map_state["source_metadata"] = map_metadata
            if map_error is not None:
                raise PollError(f"MAP_FETCH_FAILED: {map_error}")
            if map_metadata.get("http_status") != 200:
                if _blocked_response(None, map_payload or b"", int(map_metadata.get("http_status") or 0)):
                    map_state["last_attempt"] = _utc(now)
                    map_state["source_metadata"] = map_metadata
                    return _finish(status, config, now, state="SOURCE_BLOCKED", holds=["MAP_SOURCE_BLOCKED"],
                                   alerts=[{"code": "SOURCE_BLOCKED", "at": _utc(now), "detail": "MAP_SOURCE_BLOCKED"}],
                                   exit_code=0, tier="MAP", map_changed=False, wim_requested=False)
                raise PollError(f"MAP_HTTP_{map_metadata.get('http_status')}")
        if not map_payload or len(map_payload) > config.max_map_bytes:
            raise PollError("MAP_EMPTY_OR_OVERSIZED_RESPONSE")
        normalized = normalize_map_document(map_payload)
        map_digest = hashlib.sha256(_canonical_json(normalized["placemarks"])).hexdigest()
        raw_map_hash = hashlib.sha256(map_payload).hexdigest()
        old_good = map_state.get("last_good")
        old_digest = old_good.get("semantic_sha256") if isinstance(old_good, dict) else None
        map_changed = old_digest is not None and old_digest != map_digest
        previous_capture = Path(old_good["snapshot_path"]) if isinstance(old_good, dict) and isinstance(old_good.get("snapshot_path"), str) else None
        if old_digest is None or map_changed or previous_capture is None or not previous_capture.is_file():
            _, map_snapshot = _store_snapshot(config.state_dir, "map", map_payload, "kml")
        else:
            map_snapshot = previous_capture
        if old_digest is None or map_changed:
            map_state["pending_wim_digest"] = map_digest
            map_state["pending_wim_since"] = _utc(now)
        map_state["last_good"] = {"retrieved_at": _utc(now), "snapshot_path": str(map_snapshot),
                                   "raw_sha256": raw_map_hash, "semantic_sha256": map_digest,
                                   "counts": normalized["counts"]}
        map_state["last_attempt"] = _utc(now)
        map_state["source_metadata"] = map_metadata
    except (OSError, UnicodeError, PollError, ValueError, TypeError) as error:
        map_state["last_attempt"] = _utc(now)
        map_state["last_error"] = str(error)
        status["map_changed"] = False
        return _finish(status, config, now, state="FAILED", holds=[f"MAP_SOURCE_INVALID: {error}"], alerts=[],
                       exit_code=1, tier="MAP", map_changed=False, wim_requested=False)

    wim = wim_state
    due, due_reason = _wim_is_due(status, map_digest, now, config)
    holds: list[str] = []
    alerts: list[dict[str, Any]] = []
    wim_requested = False
    wim_snapshot: Path | None = None
    inventory: dict[str, Any] | None = None
    freshness: dict[str, Any] | None = None
    source_hash: str | None = None
    source_changed = False
    if due:
        request_times = _rolling_times(wim.get("request_times"), now)
        wim["request_times"] = request_times
        if len(request_times) >= min(MAX_WIM_REQUESTS_PER_24H, max(0, config.max_wim_requests_per_24h)):
            holds.append("WIM_DAILY_REQUEST_CAP")
        else:
            last_wim_request = _parse_time(wim.get("last_request_at"))
            if last_wim_request is not None and (now - last_wim_request).total_seconds() < max(MIN_WIM_REQUEST_INTERVAL_SECONDS, config.min_wim_request_interval_seconds):
                holds.append("WIM_MINIMUM_REQUEST_INTERVAL")
            else:
                wim["last_request_at"] = _utc(now)
                wim["request_times"] = request_times + [_utc(now)]
                if config.source_file is not None:
                    try:
                        wim_payload = config.source_file.read_bytes()
                    except OSError as error:
                        wim_payload, fetch_error = None, str(error)
                    else:
                        fetch_error = None
                    metadata = {"http_status": 200 if wim_payload is not None else None, "offline_source_file": str(config.source_file)}
                else:
                    wim["last_request_at"] = _utc(now)
                    wim_payload, metadata, fetch_error = _fetch(config.wim_url, opener, config.timeout_seconds, config.max_source_bytes)
                    wim["request_times"] = request_times + [_utc(now)]
                wim_requested = True
                wim["last_attempt"] = _utc(now)
                wim["source_metadata"] = metadata
                if fetch_error is not None:
                    blocked_until = _failure_backoff(wim, metadata, now)
                    wim["blocked_until"] = blocked_until
                    return _finish(status, config, now, state="FAILED", holds=[f"WIM_FETCH_FAILED: {fetch_error}"],
                                   alerts=[{"code": "WIM_FETCH_FAILED", "at": _utc(now)}], exit_code=1,
                                   tier="WIM", map_changed=map_changed, wim_requested=True)
                if metadata.get("http_status") in {403, 429} or (wim_payload is not None and _blocked_response(None, wim_payload, int(metadata.get("http_status") or 0))):
                    reason = "CLOUDFLARE_BLOCKED" if metadata.get("http_status") == 403 or metadata.get("attention_required") else "HTTP_429_RATE_LIMITED"
                    blocked_until = _failure_backoff(wim, metadata, now)
                    pending = status.get("source_change_pending", False)
                    return _finish(status, config, now, state="SOURCE_BLOCKED", holds=[reason],
                                   alerts=[{"code": "SOURCE_BLOCKED", "at": _utc(now), "detail": reason}],
                                   exit_code=0, tier="WIM", map_changed=map_changed, wim_requested=True,
                                   source_changed=pending)
                try:
                    if metadata.get("http_status") != 200 or wim_payload is None or len(wim_payload) > config.max_source_bytes:
                        raise PollError(f"WIM_HTTP_{metadata.get('http_status')}_OR_OVERSIZED")
                    inventory = parse_inventory(wim_payload)
                    source_hash, wim_snapshot = _store_snapshot(config.state_dir, "wim", wim_payload, "html")
                    freshness = bundle_freshness.evaluate_freshness(
                        wim_snapshot, config.baseline_path, config.freshness_path, retrieved_at=_utc(now)
                    )
                    source_changed = bool(freshness.get("row_diff", {}).get("added") or freshness.get("row_diff", {}).get("removed"))
                    if not freshness.get("inventory_ok") and not source_changed:
                        raise PollError("Phase 1 freshness helper rejected source")
                except (OSError, UnicodeError, PollError, ValueError, TypeError) as error:
                    wim["last_error"] = str(error)
                    blocked_until = _failure_backoff(wim, metadata, now)
                    wim["blocked_until"] = blocked_until
                    return _finish(status, config, now, state="FAILED", holds=[f"MALFORMED_SOURCE: {error}"],
                                   alerts=[{"code": "MALFORMED_SOURCE", "at": _utc(now), "detail": str(error)}],
                                   exit_code=1, tier="WIM", map_changed=map_changed, wim_requested=True)
                if source_changed:
                    status["source_change_pending"] = True
                    alerts.append({"code": "SEMANTIC_SOURCE_CHANGE", "at": _utc(now),
                                   "added_count": len(freshness["row_diff"]["added"]),
                                   "removed_count": len(freshness["row_diff"]["removed"]),
                                   "row_diff": freshness["row_diff"], "status_diff": freshness.get("status_diff", {})})
                    holds.append("WIM_SEMANTIC_CHANGE_REQUIRES_REVIEW")
                else:
                    status["last_success"] = freshness.get("last_success")
                    status["source_change_pending"] = False
                wim["last_good"] = {"retrieved_at": _utc(now), "snapshot_path": str(wim_snapshot),
                                     "raw_sha256": source_hash, "semantic_sha256": inventory["semantic_sha256"],
                                     "row_count": len(inventory["rows"]), "status_counts": inventory["status_counts"]}
                wim["block_strikes"] = 0
                wim["blocked_until"] = None
                wim.pop("last_error", None)
                if map_state.get("pending_wim_digest") == map_digest:
                    map_state["pending_wim_digest"] = None
                    map_state["pending_wim_since"] = None
    elif map_state.get("pending_wim_digest") == map_digest and due_reason is None:
        holds.append("MAP_CHANGE_DEBOUNCE_PENDING")

    candidate: dict[str, Any] | None = None
    publisher_summary: dict[str, Any] | None = None
    published = False
    if wim_snapshot is not None and inventory is not None:
        kml_path = config.kml_path or map_snapshot
        candidate_holds, candidate, publisher_summary, published = _candidate_pipeline(
            config, wim_snapshot, kml_path, now,
            publish=publish and not source_changed and not status.get("source_change_pending", False),
            source_changed=source_changed or status.get("source_change_pending", False))
        holds.extend(candidate_holds)
        if not published:
            holds.append("LAST_KNOWN_GOOD_RETAINED")
        if published and candidate is not None and publisher_summary is not None:
            output = config.publish_dir or config.repo_root
            status["last_good_bundle"] = {"path": str(output / "zones.json"),
                                           "manifest_path": str(output / "manifest.json"),
                                           "bundle_version": candidate.get("bundle_version"),
                                           "sha256": publisher_summary.get("bundle_sha256"), "published_at": _utc(now)}
    else:
        if map_changed:
            holds.append("MAP_CHANGE_REQUIRES_WIM_RECONCILIATION")
        if not wim_requested and due_reason is None and not holds:
            pass
    state = "PUBLISHED" if published else "HELD" if holds or source_changed or status.get("source_change_pending", False) or map_changed else "UNCHANGED"
    counts = {"map": map_state.get("last_good", {}).get("counts", {}),
              "wim": ({"rows": len(inventory["rows"]), "sections": len(inventory["sections"]),
                       "status_counts": inventory["status_counts"]} if inventory is not None
                      else wim.get("last_good", {}).get("status_counts", {}))}
    status["source_metadata"] = {"map": map_state.get("source_metadata"), "wim": wim.get("source_metadata")}
    return _finish(status, config, now, state=state, holds=holds, alerts=alerts, exit_code=0,
                   tier="WIM" if wim_requested else "MAP", map_changed=map_changed,
                   wim_requested=wim_requested, source_changed=source_changed,
                   diff=freshness.get("row_diff") if freshness else None,
                   source_hash=source_hash or map_state.get("last_good", {}).get("raw_sha256"),
                   counts=counts, publisher_summary=publisher_summary, published=published)


def run_poll(
    config: PollConfig, *, opener: Callable[..., Any] | None = None,
    now: datetime | None = None, publish: bool = False,
) -> tuple[int, dict[str, Any]]:
    """Run one single-flight two-tier tick and return exit code with status report."""
    current_time = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if not _POLL_LOCK.acquire(blocking=False):
        return 3, {"state": "SKIPPED", "health": "UNKNOWN", "holds": ["SINGLE_FLIGHT_ACTIVE"],
                   "skip_reason": "SINGLE_FLIGHT_ACTIVE", "published": False, "exit_code": 3}
    try:
        return _run_poll_unlocked(config, opener=opener or urllib.request.urlopen, now=current_time, publish=publish)
    finally:
        _POLL_LOCK.release()


def _config_from_args(args: argparse.Namespace) -> PollConfig:
    """Resolve CLI and environment path configuration into an immutable settings object."""
    root = Path(args.repo_root).resolve()
    state = Path(args.state_dir or os.environ.get("AVG_SPEED_STATE_DIR", "/var/lib/avg-speed-zones")).resolve()
    if state.exists() and not state.is_dir():
        raise ValueError("state_dir must be a directory")
    if not math.isfinite(args.timeout_seconds) or args.timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive finite number")
    if min(args.max_source_bytes, args.max_map_bytes, args.stale_after_seconds) <= 0:
        raise ValueError("size and stale-age limits must be positive")
    if min(args.max_wim_requests_per_24h, args.min_wim_request_interval_seconds,
           args.map_poll_interval_seconds, args.wim_max_age_seconds,
           args.wim_change_debounce_seconds) < 0:
        raise ValueError("poll cadence limits must not be negative")
    baseline = Path(args.baseline or os.environ.get("AVG_SPEED_BASELINE", root / "data/bgtoll-vaprosi-i-otgovori.html")).resolve()

    def optional_path(value: str | None, env_key: str) -> Path | None:
        """Resolve one optional file path from a CLI argument or environment variable."""
        selected = value or os.environ.get(env_key)
        return Path(selected).resolve() if selected else None

    return PollConfig(
        repo_root=root, state_dir=state, baseline_path=baseline,
        map_url=args.map_url or os.environ.get("AVG_SPEED_MAP_URL", MAP_URL),
        wim_url=args.source_url or os.environ.get("AVG_SPEED_SOURCE_URL", WIM_URL),
        source_file=optional_path(args.source_file, "AVG_SPEED_SOURCE_FILE"),
        map_file=optional_path(args.map_file, "AVG_SPEED_MAP_FILE"),
        km_points_path=optional_path(args.km_points, "AVG_SPEED_KM_POINTS"),
        kml_path=optional_path(args.kml, "AVG_SPEED_KML"),
        ria_path=optional_path(args.ria_road_links, "AVG_SPEED_RIA_ROAD_LINKS"),
        certificate_evidence_path=optional_path(args.certificate_evidence, "AVG_SPEED_CERTIFICATE_EVIDENCE"),
        publish_dir=optional_path(args.publish_dir, "AVG_SPEED_PUBLISH_DIR"),
        max_stale_seconds=args.stale_after_seconds, timeout_seconds=args.timeout_seconds,
        max_source_bytes=args.max_source_bytes, max_map_bytes=args.max_map_bytes,
        max_wim_requests_per_24h=args.max_wim_requests_per_24h,
        min_wim_request_interval_seconds=args.min_wim_request_interval_seconds,
        map_poll_interval_seconds=args.map_poll_interval_seconds,
        wim_max_age_seconds=args.wim_max_age_seconds,
        wim_change_debounce_seconds=args.wim_change_debounce_seconds,
    )


def main(argv: list[str] | None = None) -> int:
    """Run one poll/status command and emit exactly one JSON line to stdout."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=Path(__file__).resolve().parents[1])
    parser.add_argument("--state-dir")
    parser.add_argument("--baseline")
    parser.add_argument("--map-url")
    parser.add_argument("--source-url")
    parser.add_argument("--source-file", help="offline WIM fixture file (no WIM HTTP)")
    parser.add_argument("--map-file", help="offline KML fixture file (no map HTTP)")
    parser.add_argument("--km-points")
    parser.add_argument("--kml", help="optional candidate KML; otherwise uses latest map snapshot")
    parser.add_argument("--ria-road-links")
    parser.add_argument("--certificate-evidence")
    parser.add_argument("--publish-dir")
    parser.add_argument("--stale-after-seconds", type=int, default=1800)
    parser.add_argument("--timeout-seconds", type=float, default=30)
    parser.add_argument("--max-source-bytes", type=int, default=MAX_SOURCE_BYTES)
    parser.add_argument("--max-map-bytes", type=int, default=MAX_MAP_BYTES)
    parser.add_argument("--max-wim-requests-per-24h", type=int, default=MAX_WIM_REQUESTS_PER_24H)
    parser.add_argument("--min-wim-request-interval-seconds", type=int, default=MIN_WIM_REQUEST_INTERVAL_SECONDS)
    parser.add_argument("--map-poll-interval-seconds", type=int, default=MAP_POLL_INTERVAL_SECONDS)
    parser.add_argument("--wim-max-age-seconds", type=int, default=ROLLING_WINDOW_SECONDS)
    parser.add_argument("--wim-change-debounce-seconds", type=int, default=WIM_CHANGE_DEBOUNCE_SECONDS)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)
    try:
        config = _config_from_args(args)
        state = _load_status(config.status_path)
        if args.status:
            now = datetime.now(timezone.utc)
            result = _status_result(state, now, config)
            print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
            return 0
        _, result = run_poll(config, publish=args.publish)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return int(result.get("exit_code", 0))
    except (OSError, ValueError, TypeError) as error:
        print(json.dumps({"state": "FAILED", "health": "STALE", "error": str(error), "published": False},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":")))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
