#!/usr/bin/env python3
"""Parse and archive the row-local BGTOLL speed-section inventory."""
from __future__ import annotations

import argparse
from datetime import datetime
from email.utils import parsedate_to_datetime
import hashlib
import html
from html.parser import HTMLParser
import json
import mimetypes
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable

SOURCE_URL = "https://www.bgtoll.bg/vaprosi-i-otgovori"
CERTIFIED_HEADING = "сертифицирани отсечки за средна скорост"
PENDING_HEADING = "предстои последваща метрологична проверка"
REPAIR_ANNOTATION = "/Пътен участък в ремонт/"
_CHAINAGE_RE = re.compile(r"км\s*(\d+)\s*\+\s*(\d{1,3})", re.IGNORECASE)
_REPAIR_RE = re.compile(re.escape(REPAIR_ANNOTATION), re.IGNORECASE)


class _InventoryHTMLParser(HTMLParser):
    """Collect headed four-column tables and text/link data from an HTML page."""

    def __init__(self) -> None:
        """Initialize the parser's heading, table, row, and cell state."""
        super().__init__(convert_charrefs=True)
        self.heading_parts: list[str] | None = None
        self.current_heading = ""
        self.table: dict[str, Any] | None = None
        self.row: dict[str, Any] | None = None
        self.cell: dict[str, Any] | None = None
        self.tables: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Record heading, table, row, cell, and anchor boundaries."""
        attributes = dict(attrs)
        lowered = tag.lower()
        if lowered in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.heading_parts = []
        elif lowered == "table" and self.table is None:
            self.table = {"heading": self.current_heading, "rows": []}
        elif lowered == "tr" and self.table is not None and self.row is None:
            self.row = {"cells": []}
        elif lowered in {"td", "th"} and self.row is not None and self.cell is None:
            self.cell = {"tag": lowered, "parts": [], "links": []}
        elif lowered == "a" and self.cell is not None:
            href = attributes.get("href")
            if href is not None:
                self.cell["links"].append(href)

    def handle_endtag(self, tag: str) -> None:
        """Close any active heading, table, row, or cell capture."""
        lowered = tag.lower()
        if lowered in {"h1", "h2", "h3", "h4", "h5", "h6"} and self.heading_parts is not None:
            self.current_heading = _clean_text("".join(self.heading_parts)).casefold()
            self.heading_parts = None
        elif lowered in {"td", "th"} and self.cell is not None and self.row is not None:
            self.cell["text"] = _clean_text("".join(self.cell["parts"]))
            self.row["cells"].append(self.cell)
            self.cell = None
        elif lowered == "tr" and self.row is not None and self.table is not None:
            self.table["rows"].append(self.row["cells"])
            self.row = None
        elif lowered == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None

    def handle_data(self, data: str) -> None:
        """Append visible text to the currently active heading or table cell."""
        if self.heading_parts is not None:
            self.heading_parts.append(data)
        if self.cell is not None:
            self.cell["parts"].append(data)


def _clean_text(value: str) -> str:
    """Decode HTML text and collapse layout whitespace without changing words."""
    return " ".join(html.unescape(value).replace("\xa0", " ").split())


def _normalise_match_label(value: str) -> str:
    """Remove only the published repair marker before comparing endpoint labels."""
    without_annotation = _REPAIR_RE.sub("", value)
    return _clean_text(without_annotation)


def _normalise_road(value: str) -> str:
    """Return a stable road identity token while retaining its display label."""
    return " ".join(_normalise_match_label(value).casefold().split())


def _chainage_metres(value: str) -> int | None:
    """Parse a Bulgarian kilometre-plus-metre post into an integer metre value."""
    match = _CHAINAGE_RE.search(value.replace("\xa0", " "))
    if match is None:
        return None
    kilometres, metres = (int(part) for part in match.groups())
    if metres > 999:
        return None
    return kilometres * 1000 + metres


def _stable_key(prefix: str, values: Iterable[Any]) -> str:
    """Build a deterministic, compact key from a canonical JSON tuple."""
    payload = json.dumps(list(values), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}-{digest}"


def _certificate_reference(links: list[str]) -> tuple[str | None, str | None, list[str]]:
    """Resolve only an unambiguous same-row fourth-cell BGTOLL certificate link."""
    if not links:
        return None, "missing_url", []
    if len(links) != 1:
        return None, "ambiguous_url", list(links)
    raw_url = links[0].strip()
    if not raw_url:
        return None, "invalid_url", [raw_url]
    resolved = urllib.parse.urljoin(SOURCE_URL, raw_url)
    parsed = urllib.parse.urlsplit(resolved)
    host = (parsed.hostname or "").casefold()
    if parsed.scheme.casefold() != "https" or host not in {"bgtoll.bg", "www.bgtoll.bg"} or not parsed.path:
        return None, "invalid_url", [raw_url]
    return urllib.parse.urlunsplit(parsed), None, [raw_url]


def _table_status(heading: str) -> str | None:
    """Map an official status heading to its canonical per-row status."""
    folded = _clean_text(heading).casefold()
    if CERTIFIED_HEADING in folded:
        return "CERTIFIED"
    if PENDING_HEADING in folded:
        return "PENDING_VERIFICATION"
    return None


def _inventory_tables(source: bytes | str) -> tuple[list[tuple[str, list[list[dict[str, Any]]]]], str]:
    """Extract the two official four-column status tables and the source text."""
    decoded = source.decode("utf-8-sig", errors="replace") if isinstance(source, bytes) else source
    parser = _InventoryHTMLParser()
    parser.feed(decoded)
    parser.close()
    selected: list[tuple[str, list[list[dict[str, Any]]]]] = []
    for table in parser.tables:
        status = _table_status(table["heading"])
        if status is None:
            continue
        rows = table["rows"]
        if not rows:
            raise ValueError(f"Official {status} table has no rows")
        header = rows[0]
        if len(header) != 4 or [cell["text"].casefold() for cell in header] != [
            "път", "начална точка", "крайна точка", "карта (изображение)"
        ]:
            raise ValueError(f"Official {status} table does not have the expected four-column header")
        data_rows = rows[1:]
        if not data_rows:
            raise ValueError(f"Official {status} table has no direction rows")
        if any(len(row) != 4 for row in data_rows):
            raise ValueError(f"Official {status} table contains a row that is not four columns")
        selected.append((status, data_rows))
    counts = Counter(status for status, _ in selected)
    if counts != Counter({"CERTIFIED": 1, "PENDING_VERIFICATION": 1}):
        raise ValueError(f"Expected one certified and one pending four-column table; found {dict(counts)}")
    return selected, decoded


def parse_inventory(source: bytes | str) -> dict[str, Any]:
    """Parse BGTOLL's two status tables into deterministic directional records.

    Raw normalized cell text and repair annotations remain available for audit. The
    repair annotation is removed only from the matching labels and section identity.
    The semantic hash covers row status, all four cells, and row-local certificate
    references, but excludes page-wrapper bytes and presentation markup.
    """
    tables, _ = _inventory_tables(source)
    raw_bytes = source if isinstance(source, bytes) else source.encode("utf-8")
    rows: list[dict[str, Any]] = []
    for status, table_rows in tables:
        for cells in table_rows:
            road_raw, start_raw, end_raw = (cell["text"] for cell in cells[:3])
            start_label = _normalise_match_label(start_raw)
            end_label = _normalise_match_label(end_raw)
            start_chainage = _chainage_metres(start_raw)
            end_chainage = _chainage_metres(end_raw)
            road_label = _normalise_match_label(road_raw)
            road_identity = _normalise_road(road_raw)
            start_identity = _normalise_match_label(start_label).casefold()
            end_identity = _normalise_match_label(end_label).casefold()
            endpoint_pair: tuple[Any, Any]
            if start_chainage is not None and end_chainage is not None:
                endpoint_pair = tuple(sorted((start_chainage, end_chainage)))
            else:
                endpoint_pair = tuple(sorted((start_identity, end_identity)))
            section_key = _stable_key("section", (road_identity, *endpoint_pair))
            direction_key = _stable_key("direction", (road_identity, start_chainage if start_chainage is not None else start_identity, end_chainage if end_chainage is not None else end_identity))
            cert_url, cert_url_issue, cert_url_candidates = _certificate_reference(cells[3]["links"])
            raw_row_text = " | ".join(cell["text"] for cell in cells)
            rows.append({
                "status": status,
                "road_raw": road_raw,
                "road_label": road_label,
                "start_raw": start_raw,
                "start_label": start_label,
                "start_annotations": _REPAIR_RE.findall(start_raw),
                "start_chainage_m": start_chainage,
                "end_raw": end_raw,
                "end_label": end_label,
                "end_annotations": _REPAIR_RE.findall(end_raw),
                "end_chainage_m": end_chainage,
                "raw_row_text": raw_row_text,
                "certificate_cell_raw": cells[3]["text"],
                "section_key": section_key,
                "direction_key": direction_key,
                "cert_url": cert_url,
                "cert_url_issue": cert_url_issue,
                "cert_url_candidates": cert_url_candidates,
            })
    if not rows:
        raise ValueError("Official inventory contains no direction rows")
    if len({row["direction_key"] for row in rows}) != len(rows):
        raise ValueError("Official inventory contains duplicate directional identities")

    semantic_rows = sorted(
        json.dumps(_semantic_row(row), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        for row in rows
    )
    semantic_payload = "[" + ",".join(semantic_rows) + "]"
    status_counts = dict(sorted(Counter(row["status"] for row in rows).items()))
    section_map: dict[str, dict[str, Any]] = {}
    for row in rows:
        section = section_map.setdefault(row["section_key"], {
            "section_key": row["section_key"], "road_label": row["road_label"], "direction_keys": []
        })
        section["direction_keys"].append(row["direction_key"])
    sections = [section_map[key] for key in sorted(section_map)]
    for section in sections:
        section["direction_keys"].sort()
    return {
        "rows": rows,
        "sections": sections,
        "status_counts": status_counts,
        "raw_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "semantic_sha256": hashlib.sha256(semantic_payload.encode("utf-8")).hexdigest(),
    }


def _semantic_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return the source-owned semantic tuple fields used for hashing and diffs."""
    return {
        "status": row["status"],
        "road": row["road_raw"],
        "start": row["start_raw"],
        "end": row["end_raw"],
        "certificate_cell": row["certificate_cell_raw"],
        "certificate_url": row["cert_url"],
        "certificate_issue": row["cert_url_issue"],
        "certificate_candidates": row["cert_url_candidates"],
    }


def _image_signature(data: bytes) -> str | None:
    """Recognize common raster image signatures without third-party libraries."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"BM"):
        return "image/bmp"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def fetch_certificates(
    rows: Iterable[dict[str, Any]],
    output_dir: Path,
    timeout: float = 15,
    max_bytes: int = 25 * 1024 * 1024,
    max_workers: int = 1,
    max_retries: int = 2,
    retry_delay: float = 0.25,
) -> list[dict[str, Any]]:
    """Fetch every row-bound certificate URL and retain all outcomes explicitly.

    Only successful, size-bounded responses whose declared MIME type and byte
    signature both identify an image are stored. Each input row yields one result;
    missing, invalid, dead, non-image, and oversized references are quarantined.
    """
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    row_list = list(rows)

    def fetch_one(row: dict[str, Any]) -> dict[str, Any]:
        """Fetch one certificate and return a durable success/quarantine record."""
        result: dict[str, Any] = {
            "direction_key": row.get("direction_key"),
            "cert_url": row.get("cert_url"),
            "http_status": None,
            "mime_type": None,
            "byte_count": 0,
            "sha256": None,
            "stored_path": None,
            "quarantined": True,
            "quarantine_reason": None,
        }
        url = row.get("cert_url")
        if not url:
            result["quarantine_reason"] = row.get("cert_url_issue") or "missing_url"
            return result
        request = urllib.request.Request(url, headers={"User-Agent": "avg-speed-zones-inventory/1.0"})
        payload = b""
        completed = False
        for attempt in range(max(0, int(max_retries)) + 1):
            try:
                with urllib.request.urlopen(request, timeout=timeout) as response:
                    result["http_status"] = response.status
                    result["mime_type"] = response.headers.get_content_type()
                    payload = response.read(max_bytes + 1)
                result.pop("retry_count", None)
                result["retry_count"] = attempt
                completed = True
                break
            except urllib.error.HTTPError as error:
                result["http_status"] = error.code
                result["mime_type"] = error.headers.get_content_type() if error.headers else None
                payload = error.read(max_bytes + 1)
                retry_after = error.headers.get("Retry-After") if error.headers else None
                error.close()
                retryable = error.code == 429 or 500 <= error.code < 600
                if retryable and attempt < max_retries:
                    delay = max(0.0, retry_delay)
                    if retry_after:
                        try:
                            delay = max(0.0, float(retry_after))
                        except ValueError:
                            try:
                                delay = max(0.0, (parsedate_to_datetime(retry_after) - datetime.now(parsedate_to_datetime(retry_after).tzinfo)).total_seconds())
                            except (TypeError, ValueError, OverflowError):
                                pass
                    time.sleep(min(delay, 30.0))
                    continue
                result["retry_count"] = attempt
                result["quarantine_reason"] = "http_error"
                completed = True
                break
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as error:
                result["retry_count"] = attempt
                result["quarantine_reason"] = "network_error"
                result["error"] = str(error)
                if attempt < max_retries:
                    time.sleep(min(max(0.0, retry_delay) * (2 ** attempt), 30.0))
                    continue
                completed = True
                break
        if not completed:
            result["quarantine_reason"] = "network_error"
            result["error"] = "request attempt did not produce a response"
        result["byte_count"] = len(payload)
        if payload:
            result["sha256"] = hashlib.sha256(payload).hexdigest()
        if result["http_status"] is not None and not 200 <= result["http_status"] < 300:
            result["quarantine_reason"] = result["quarantine_reason"] or "http_error"
        elif len(payload) > max_bytes:
            result["quarantine_reason"] = "too_large"
        elif result["http_status"] is None:
            result["quarantine_reason"] = result["quarantine_reason"] or "network_error"
        else:
            sniffed_mime = _image_signature(payload)
            declared_mime = result["mime_type"] or mimetypes.guess_type(urllib.parse.urlsplit(url).path)[0]
            if not declared_mime or not declared_mime.startswith("image/") or sniffed_mime is None:
                result["quarantine_reason"] = "non_image"
            elif declared_mime != sniffed_mime:
                result["quarantine_reason"] = "invalid_image"
            else:
                key = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(row.get("direction_key") or "direction"))
                suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif", "image/bmp": ".bmp", "image/webp": ".webp"}[sniffed_mime]
                path = destination / f"{key}{suffix}"
                try:
                    path.write_bytes(payload)
                except OSError as error:
                    result["quarantine_reason"] = "storage_error"
                    result["error"] = str(error)
                else:
                    result["stored_path"] = str(path)
                    result["quarantined"] = False
        return result

    with ThreadPoolExecutor(max_workers=max(1, int(max_workers))) as executor:
        return list(executor.map(fetch_one, row_list))


def main(argv: list[str] | None = None) -> int:
    """Parse an archived source, optionally fetch certificates, and emit a JSON report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path, help="archived official BGTOLL HTML")
    parser.add_argument("--certificates-dir", type=Path, help="fetch each bound certificate into this directory")
    parser.add_argument("--output", type=Path, help="write the complete inventory and fetch report to this JSON path")
    parser.add_argument("--timeout", type=float, default=15, help="per-certificate HTTP timeout in seconds")
    parser.add_argument("--max-retries", type=int, default=2, help="retries for 429, 5xx, and transient network errors")
    parser.add_argument("--retry-delay", type=float, default=0.25, help="base retry delay in seconds")
    args = parser.parse_args(argv)
    try:
        inventory = parse_inventory(args.source.read_bytes())
        report: dict[str, Any] = {
            "source_path": str(args.source),
            "row_count": len(inventory["rows"]),
            "section_count": len(inventory["sections"]),
            "status_counts": inventory["status_counts"],
            "raw_sha256": inventory["raw_sha256"],
            "semantic_sha256": inventory["semantic_sha256"],
            "rows": inventory["rows"],
            "sections": inventory["sections"],
            "certificates": [],
        }
        if args.certificates_dir is not None:
            report["certificates"] = fetch_certificates(
                inventory["rows"],
                output_dir=args.certificates_dir,
                timeout=args.timeout,
                max_retries=args.max_retries,
                retry_delay=args.retry_delay,
            )
        encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(encoded, encoding="utf-8")
        summary = {
            key: report[key]
            for key in ("source_path", "row_count", "section_count", "status_counts", "raw_sha256", "semantic_sha256")
        }
        if args.certificates_dir is not None:
            outcomes = report["certificates"]
            summary["certificate_url_count"] = sum(bool(row["cert_url"]) for row in inventory["rows"])
            summary["certificate_fetched_count"] = len(outcomes)
            summary["certificate_success_count"] = sum(not item["quarantined"] for item in outcomes)
            summary["certificate_quarantine_count"] = sum(item["quarantined"] for item in outcomes)
            summary["certificate_quarantine_reasons"] = dict(sorted(Counter(
                item["quarantine_reason"] for item in outcomes if item["quarantine_reason"]
            ).items()))
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, UnicodeError, ValueError, TypeError) as error:
        print(f"inventory failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
