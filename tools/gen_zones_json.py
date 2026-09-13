#!/usr/bin/env python3
"""Generate deterministic zones.json v1 from the supplied Google My Maps KML.

Usage:
  python3 tools/gen_zones_json.py zones-source.kml app/src/main/assets/zones.json

The generator preserves KML ordering, emits only LineString enforcement zones,
and computes geometry length using haversine distance. Certified length and
category limits are parsed from each zone description and remain authoritative.
"""
from __future__ import annotations

import argparse
import html
import json
import math
import re
import unicodedata
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

NS = {"k": "http://www.opengis.net/kml/2.2"}
KML_URL = "https://www.google.com/maps/d/u/0/kml?mid=1bydr93x-u18Oz3lm7ngmWhVTgsP2Eys&forcekml=1"

# Canonical Latin category codes emitted in limit_by_category. Cyrillic look-alikes
# in the source KML (А/А2/В/В1/С/СЕ) are transliterated to these codes; the D family
# (D, D1, D1E, DE) is already Latin in the source and must be preserved verbatim.
CANONICAL_CATEGORIES = frozenset({
    "A", "A2", "B", "B1", "BE", "C", "C1", "C1E", "CE", "D", "D1", "D1E", "DE",
})

# Cyrillic code letters that have an identical-looking Latin counterpart. Only the
# letters used in the KML's category codes and road designators are mapped.
_CYRILLIC_CODE_LATIN = str.maketrans({
    "А": "A", "В": "B", "С": "C", "Е": "E", "І": "I",
    "а": "a", "в": "b", "с": "c", "е": "e", "і": "i",
})


def haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Return the great-circle distance in metres for (longitude, latitude)."""
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    dlon, dlat = lon2 - lon1, lat2 - lat1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


def bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Return initial bearing in degrees clockwise from true north."""
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    value = math.atan2(math.sin(lon2 - lon1) * math.cos(lat2), math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(lon2 - lon1))
    return round((math.degrees(value) + 360) % 360, 3)


def slug(value: str) -> str:
    """Create a stable Latin identifier from a Bulgarian zone name."""
    translit = str.maketrans({
        "А":"A", "Б":"B", "В":"V", "Г":"G", "Д":"D", "Е":"E", "Ж":"Zh", "З":"Z", "И":"I", "Й":"Y", "К":"K", "Л":"L", "М":"M", "Н":"N", "О":"O", "П":"P", "Р":"R", "С":"S", "Т":"T", "У":"U", "Ф":"F", "Х":"H", "Ц":"Ts", "Ч":"Ch", "Ш":"Sh", "Щ":"Sht", "Ъ":"A", "Ь":"Y", "Ю":"Yu", "Я":"Ya",
        "а":"a", "б":"b", "в":"v", "г":"g", "д":"d", "е":"e", "ж":"zh", "з":"z", "и":"i", "й":"y", "к":"k", "л":"l", "м":"m", "н":"n", "о":"o", "п":"p", "р":"r", "с":"s", "т":"t", "у":"u", "ф":"f", "х":"h", "ц":"ts", "ч":"ch", "ш":"sh", "щ":"sht", "ъ":"a", "ь":"y", "ю":"yu", "я":"ya",
    })
    value = unicodedata.normalize("NFKD", value.translate(translit)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", value).strip("-") or "zone"


def clean_description(element: ET.Element | None) -> str:
    """Extract readable text from an HTML-bearing KML description."""
    if element is None:
        return ""
    return html.unescape(" ".join("".join(element.itertext()).split()))


def canon_category(token: str) -> str:
    """Transliterate a raw Cyrillic/Latin category token to its canonical code.

    Returns "" when the token has no canonical counterpart, so callers can fail closed.
    """
    cleaned = token.translate(_CYRILLIC_CODE_LATIN).strip().upper()
    return cleaned if cleaned in CANONICAL_CATEGORIES else ""


def parse_limits(description: str, zone_name: str) -> dict[str, int | None]:
    """Parse per-category speed limits from a single zone description.

    Only the zone's own ``Категория ... - <value>`` lines are authoritative. A value of
    ``забранено`` (forbidden) is emitted as ``None`` — never substituted with a number.
    There are no global defaults: any category token that cannot be transliterated to a
    canonical code, or any zone that declares no limits at all, raises ``ValueError``
    naming the zone instead of guessing.
    """
    result: dict[str, int | None] = {}
    # Match the Bulgarian category lines: "Категория А,А2,В - 90 km/h" / "... - забранено".
    # The category list is a comma / "и" separated set of code tokens; requiring the full
    # word "Категория" (not the preamble word "категории") keeps prose out of the match.
    pattern = re.compile(
        r"Категория\s+"
        r"(?P<cats>[А-ЯA-Z0-9]+(?:\s*[,;/]\s*[А-ЯA-Z0-9]+|\s+и\s+[А-ЯA-Z0-9]+)*)"
        r"\s*[-–:]\s*(?P<value>забранено|\d{1,3})(?:\s*km/h)?",
        re.IGNORECASE,
    )
    for match in pattern.finditer(description):
        raw_value = match.group("value").strip().lower()
        value: int | None = None if raw_value == "забранено" else int(raw_value)
        for raw_token in re.split(r"\s*[,;/]\s*|\s+и\s+", match.group("cats")):
            token = raw_token.strip()
            if not token:
                continue
            code = canon_category(token)
            if not code:
                raise ValueError(
                    f"zone {zone_name!r}: unrecognised category token {token!r} in {match.group(0)!r}"
                )
            result[code] = value
    if not result:
        raise ValueError(f"zone {zone_name!r}: no category limits could be parsed from its description")
    return dict(sorted(result.items(), key=lambda item: item[0]))


def parse_road(description: str) -> str:
    """Extract a road designator such as A-1, I-5, II-55 or E-79 from a description.

    The ``II`` (second-class) prefix is matched before ``I`` so ``II-55`` is not truncated.
    """
    match = re.search(r"\b(II|I|A|E)[ -]?(\d{1,3})\b", description, re.IGNORECASE)
    if not match:
        return ""
    return f"{match.group(1).upper()}-{match.group(2)}"


def parse_certified_length(description: str, geometry_m: float) -> int:
    """Extract certified metres, using geometry only as a strict fallback."""
    match = re.search(r"(?:Дължина(?: на участъка)?|length)\s*(?:в метри)?\s*[:：]?\s*(\d+(?:[.,]\d+)?)\s*m?", description, re.IGNORECASE)
    return round(float(match.group(1).replace(",", "."))) if match else round(geometry_m)


def parse_kml(source: Path) -> tuple[list[dict], int, int]:
    """Parse KML into v1 zone records and return zone/point counts."""
    root = ET.parse(source).getroot()
    placemarks = root.findall(".//k:Placemark", NS)
    zones, points = [], 0
    seen: dict[str, int] = {}
    for placemark in placemarks:
        point = placemark.find("k:Point/k:coordinates", NS)
        line = placemark.find("k:LineString/k:coordinates", NS)
        if point is not None:
            points += 1
        if line is None or not (line.text or "").strip():
            continue
        name = (placemark.findtext("k:name", "", NS) or "").strip()
        coords = [tuple(map(float, token.split(",")[:2])) for token in line.text.split()]
        geometry_m = sum(haversine_m(coords[i], coords[i + 1]) for i in range(len(coords) - 1))
        base = slug(name)
        seen[base] = seen.get(base, 0) + 1
        zone_id = base if seen[base] == 1 else f"{base}-{seen[base]}"
        description = clean_description(placemark.find("k:description", NS))
        road = parse_road(description)
        if not road:
            raise ValueError(f"zone {name!r} ({zone_id}): no road designator could be parsed from its description")
        zones.append({
            "id": zone_id,
            "name_bg": name,
            "road": road,
            "cert_len_m": parse_certified_length(description, geometry_m),
            "polyline": [[round(lat, 7), round(lon, 7)] for lon, lat in coords],
            "start_heading": bearing_deg(coords[0], coords[1]),
            "limit_by_category": parse_limits(description, name),
        })
    return zones, len(placemarks), points


def source_mtime_utc(source: Path) -> str:
    """Derive a deterministic generated_at from the source file's mtime (UTC, seconds)."""
    stamp = datetime.fromtimestamp(source.stat().st_mtime, tz=timezone.utc)
    return stamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def resolve_generated_at(source: Path, override: str | None) -> str:
    """Return the explicit --generated-at value, else the source mtime-derived stamp."""
    if override is not None:
        return override
    return source_mtime_utc(source)


def main() -> None:
    """Parse the requested source and write stable, pretty-printed JSON."""
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument(
        "--generated-at",
        default=None,
        help="Override generated_at (UTC ISO-8601 seconds). Defaults to the source file mtime.",
    )
    args = parser.parse_args()
    zones, placemarks, points = parse_kml(args.source)
    payload = {
        "schema_version": 1,
        "bundle_version": "v1",
        "generated_at": resolve_generated_at(args.source, args.generated_at),
        "source": {"url": KML_URL, "placemarks": placemarks, "line_zones": len(zones), "camera_points": points},
        "zones": zones,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"placemarks={placemarks} line_zones={len(zones)} camera_points={points} output={args.output}")


if __name__ == "__main__":
    main()
