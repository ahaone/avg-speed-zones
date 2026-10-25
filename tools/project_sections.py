#!/usr/bin/env python3
"""Project candidate sections into app-compatible zones for the v5 advisory feed.

The app's list and tracker are still zone-based. The section catalogue is additive
metadata. This tool keeps the sections and adds one zone per physical section so the
v5 app can display all 41 sections without touching the legacy feed.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

CATEGORY_MAP = {
    "А": ["A"],
    "А2": ["A2"],
    "В": ["B"],
    "В1": ["B1"],
    "BE": ["BE"],
    "C и CE": ["C", "CE"],
    "C1": ["C1"],
    "C1E": ["C1E"],
    "D": ["D"],
    "D1": ["D1"],
    "D1E": ["D1E"],
    "DE": ["DE"],
}


def bearing(a, b):
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def project_limits(raw: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for source, targets in CATEGORY_MAP.items():
        if source not in raw:
            continue
        value = raw[source]
        if value is None:
            continue
        normalized = int(value)
        for target in targets:
            out[target] = normalized
    return out


def project(candidate: dict) -> dict:
    zones = []
    skipped = []
    for section in candidate.get("sections", []):
        geometry = section.get("geometry") or {}
        polyline = geometry.get("normalized_polyline") or []
        if len(polyline) < 2:
            skipped.append(section.get("section_key"))
            continue
        forward = (section.get("directions") or {}).get("forward") or {}
        span = section.get("span_m") or forward.get("span_m") or geometry.get("geometry_len_m")
        if not isinstance(span, (int, float)) or span <= 0:
            skipped.append(section.get("section_key"))
            continue
        name = section.get("name_bg") or forward.get("name_bg") or section.get("section_key")
        limits = project_limits(geometry.get("limit_by_category") or {})
        if not limits:
            skipped.append(section.get("section_key"))
            continue
        zones.append({
            "id": section["section_key"],
            "section_key": section["section_key"],
            "name_bg": name,
            "road": section["road"],
            "cert_len_m": int(round(span)),
            "start_heading": round(bearing(polyline[0], polyline[1]), 3),
            "polyline": polyline,
            "limit_by_category": limits,
        })
    out = dict(candidate)
    out["zones"] = zones
    out["advisory_projection"] = True
    out["projection_skipped"] = skipped
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    data = json.loads(args.candidate.read_text(encoding="utf-8"))
    projected = project(data)
    args.output.write_text(json.dumps(projected, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"zones": len(projected["zones"]), "sections": len(projected.get("sections", []))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
