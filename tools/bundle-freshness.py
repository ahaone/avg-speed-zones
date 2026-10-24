#!/usr/bin/env python3
"""Record BGTOLL source freshness and fail closed on semantic inventory changes."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
from typing import Any

from bgtoll_inventory import _semantic_row, parse_inventory


def _read_json(path: Path) -> dict[str, Any]:
    """Load an existing freshness record, returning an empty state if absent/invalid."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _row_signature(row: dict[str, Any]) -> str:
    """Serialize the source-semantic fields into a stable row-set comparison key."""
    return json.dumps(_semantic_row(row), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _semantic_set(inventory: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Index semantic row tuples by canonical JSON for exact set comparison."""
    indexed: dict[str, dict[str, Any]] = {}
    for row in inventory["rows"]:
        signature = _row_signature(row)
        indexed[signature] = _semantic_row(row)
    return indexed


def _row_diff(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Return deterministic added and removed source tuples between inventories."""
    baseline_rows = _semantic_set(baseline)
    current_rows = _semantic_set(current)
    added = [current_rows[key] for key in sorted(current_rows.keys() - baseline_rows.keys())]
    removed = [baseline_rows[key] for key in sorted(baseline_rows.keys() - current_rows.keys())]
    return {"added": added, "removed": removed}


def _status_diff(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Compare official counts per status, using signed current-minus-baseline deltas."""
    all_statuses = sorted(set(baseline.get("status_counts", {})) | set(current.get("status_counts", {})))
    return {
        status: {
            "baseline": int(baseline.get("status_counts", {}).get(status, 0)),
            "current": int(current.get("status_counts", {}).get(status, 0)),
            "delta": int(current.get("status_counts", {}).get(status, 0)) - int(baseline.get("status_counts", {}).get(status, 0)),
        }
        for status in all_statuses
    }


def _timestamp(value: str | None) -> str:
    """Return the caller-supplied retrieval timestamp or a current UTC ISO timestamp."""
    if value:
        # Accept explicit ISO timestamps, but reject unparseable provenance values.
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        return value
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def evaluate_freshness(source_path: Path, baseline_path: Path, state_path: Path, retrieved_at: str | None = None) -> dict[str, Any]:
    """Evaluate one source capture against its reviewed semantic baseline and persist state.

    Wrapper-only raw byte changes are recorded but allowed. A semantic row/status/text/
    certificate difference or an unparseable inventory fails closed and cannot replace
    the prior last-success record.
    """
    source = Path(source_path).read_bytes()
    baseline_source = Path(baseline_path).read_bytes()
    previous = _read_json(Path(state_path))
    last_success = previous.get("last_success")
    attempted_at = _timestamp(retrieved_at)
    record: dict[str, Any] = {
        "retrieved_at": attempted_at,
        "source_path": str(Path(source_path)),
        "baseline_path": str(Path(baseline_path)),
        "raw_sha256": hashlib.sha256(source).hexdigest(),
        "baseline_raw_sha256": hashlib.sha256(baseline_source).hexdigest(),
        "semantic_sha256": None,
        "baseline_semantic_sha256": None,
        "row_count": 0,
        "baseline_row_count": 0,
        "status_counts": {},
        "baseline_status_counts": {},
        "status_diff": {},
        "row_diff": {"added": [], "removed": []},
        "inventory_ok": False,
        "errors": [],
        "last_success": last_success,
    }
    current_inventory: dict[str, Any] | None = None
    baseline_inventory: dict[str, Any] | None = None
    try:
        current_inventory = parse_inventory(source)
        record["semantic_sha256"] = current_inventory["semantic_sha256"]
        record["row_count"] = len(current_inventory["rows"])
        record["status_counts"] = current_inventory["status_counts"]
    except (UnicodeError, ValueError, TypeError) as error:
        record["errors"].append(f"source_inventory: {error}")
    try:
        baseline_inventory = parse_inventory(baseline_source)
        record["baseline_semantic_sha256"] = baseline_inventory["semantic_sha256"]
        record["baseline_row_count"] = len(baseline_inventory["rows"])
        record["baseline_status_counts"] = baseline_inventory["status_counts"]
    except (UnicodeError, ValueError, TypeError) as error:
        record["errors"].append(f"baseline_inventory: {error}")

    if current_inventory is not None and baseline_inventory is not None:
        record["row_diff"] = _row_diff(baseline_inventory, current_inventory)
        record["status_diff"] = _status_diff(baseline_inventory, current_inventory)
        if record["row_diff"]["added"] or record["row_diff"]["removed"]:
            record["errors"].append("semantic_source_change")
        else:
            record["inventory_ok"] = True
            record["last_success"] = {
                "retrieved_at": attempted_at,
                "raw_sha256": record["raw_sha256"],
                "semantic_sha256": record["semantic_sha256"],
                "row_count": record["row_count"],
                "status_counts": record["status_counts"],
            }
    else:
        if not record["errors"]:
            record["errors"].append("inventory_failed")

    target = Path(state_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


def main(argv: list[str] | None = None) -> int:
    """Parse CLI options, persist a freshness record, and return fail-closed status."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="captured official BGTOLL HTML")
    parser.add_argument("--baseline", required=True, type=Path, help="reviewed official HTML baseline")
    parser.add_argument("--state", required=True, type=Path, help="JSON freshness state record")
    parser.add_argument("--retrieved-at", help="ISO 8601 retrieval timestamp (defaults to current UTC)")
    args = parser.parse_args(argv)
    try:
        record = evaluate_freshness(args.source, args.baseline, args.state, args.retrieved_at)
    except (OSError, ValueError) as error:
        print(f"freshness evaluation failed: {error}", file=sys.stderr)
        return 2
    print(json.dumps(record, ensure_ascii=False, sort_keys=True))
    if not record["inventory_ok"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
