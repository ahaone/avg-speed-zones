#!/usr/bin/env python3
"""Extract cautious certificate-length candidates with local Tesseract OCR only."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from decimal import Decimal, ROUND_HALF_UP
from typing import Any


_CALLOUT_RE = re.compile(
    r"(?<![\d.,])(?P<number>\d+(?:[.,]\d+)?)[ \t]*(?P<unit>km|км)(?![A-Za-zА-Яа-яЁё])\.?",
    re.IGNORECASE,
)
_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
_TESSERACT_TIMEOUT_SECONDS = 120


def extract_matches(text: str) -> list[dict[str, Any]]:
    """Return every number-plus-kilometre match and its integer metre value."""
    matches: list[dict[str, Any]] = []
    for found in _CALLOUT_RE.finditer(text):
        kilometres = Decimal(found.group("number").replace(",", "."))
        metres = int((kilometres * 1000).to_integral_value(rounding=ROUND_HALF_UP))
        matches.append({"raw": found.group(0), "value_m": metres})
    return matches


def analyze_text(text: str) -> dict[str, Any]:
    """Classify OCR callouts without accepting or binding a certificate length."""
    number_tokens = [match.group("number") for match in _CALLOUT_RE.finditer(text)]
    matches = extract_matches(text)
    values = {match["value_m"] for match in matches}
    candidate: int | None = None
    if not values:
        status = "UNREADABLE"
    elif len(values) > 1:
        status = "AMBIGUOUS"
    else:
        candidate = next(iter(values))
        if all("." not in token and "," not in token for token in number_tokens):
            status = "INTEGER_REVIEW"
        else:
            status = "SINGLE_CALLOUT"
    return {
        "matches": matches,
        "status": status,
        "candidate_cert_len_m": candidate,
    }


def tesseract_version(executable: str = "tesseract") -> str | None:
    """Return the first local Tesseract version line, or None if unavailable."""
    try:
        result = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.splitlines()[0] if result.stdout.splitlines() else None


def _run_ocr(image_path: Path, psm: int, executable: str = "tesseract") -> tuple[str, str | None]:
    """Run one local English Tesseract pass and return text plus an error, if any."""
    try:
        result = subprocess.run(
            [executable, str(image_path), "stdout", "--psm", str(psm), "-l", "eng"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TESSERACT_TIMEOUT_SECONDS,
            check=False,
        )
    except FileNotFoundError:
        return "", f"OCR executable not found: {executable}"
    except subprocess.TimeoutExpired:
        return "", f"Tesseract timed out after {_TESSERACT_TIMEOUT_SECONDS} seconds (PSM {psm})"
    except OSError as error:
        return "", f"cannot execute Tesseract: {error}"
    if result.returncode != 0:
        message = result.stderr.strip() or f"Tesseract exited with status {result.returncode}"
        return result.stdout, message
    return result.stdout, None


def read_image(
    image_path: Path,
    direction_key_claim: Any = None,
    engine_version: str | None = None,
    executable: str = "tesseract",
) -> dict[str, Any]:
    """Hash an image and return a local OCR candidate, never a confirmation."""
    path = Path(image_path)
    image_bytes = path.read_bytes()
    evidence: dict[str, Any] = {
        "image_path": str(image_path),
        "image_bytes": len(image_bytes),
        "image_sha256": hashlib.sha256(image_bytes).hexdigest(),
        "direction_key_claim": direction_key_claim,
        "engine_version": engine_version,
        "psm": 11,
        "raw_text": "",
        "matches": [],
        "status": "UNREADABLE",
        "candidate_cert_len_m": None,
        "human_confirmed": False,
        "release_eligible": False,
    }

    raw_text, error = _run_ocr(path, 11, executable)
    evidence["raw_text"] = raw_text
    if error is not None:
        evidence["error"] = error
        return evidence

    analysis = analyze_text(raw_text)
    if not analysis["matches"]:
        fallback_text, fallback_error = _run_ocr(path, 6, executable)
        evidence["psm"] = 6
        if fallback_error is not None:
            evidence["raw_text"] = fallback_text or raw_text
            evidence["error"] = fallback_error
            return evidence
        raw_text = fallback_text
        analysis = analyze_text(raw_text)

    evidence["raw_text"] = raw_text
    evidence.update(analysis)
    return evidence


def _load_manifest(path: Path) -> list[dict[str, Any]]:
    """Load manifest records and resolve relative image paths against the manifest."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read manifest {path}: {error}") from error
    if not isinstance(document, list):
        raise ValueError("manifest must be a JSON array of image records")

    records: list[dict[str, Any]] = []
    for index, record in enumerate(document):
        if not isinstance(record, dict):
            raise ValueError(f"manifest record {index} must be an object")
        image_value = record.get("image_path")
        if not isinstance(image_value, str) or not image_value.strip():
            raise ValueError(f"manifest record {index} requires a non-empty image_path")
        image_path = Path(image_value)
        if not image_path.is_absolute():
            image_path = path.parent / image_path
        records.append({
            "image_path": image_path,
            "direction_key_claim": record.get("direction_key"),
        })
    return records


def _collect_records(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Combine manifest, explicit image, and directory inputs without filename inference."""
    records: list[dict[str, Any]] = []
    if args.manifest is not None:
        records.extend(_load_manifest(args.manifest))
    records.extend({"image_path": path, "direction_key_claim": None} for path in args.image)
    for directory in args.dir:
        if not directory.is_dir():
            raise OSError(f"image directory does not exist or is not a directory: {directory}")
        images = sorted(
            (path for path in directory.rglob("*") if path.is_file() and path.suffix.lower() in _IMAGE_SUFFIXES),
            key=lambda path: path.as_posix(),
        )
        records.extend({"image_path": path, "direction_key_claim": None} for path in images)
    return records


def main(argv: list[str] | None = None) -> int:
    """Run the certificate evidence CLI and write JSON, returning nonzero on CLI/IO errors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, help="JSON array of image_path/direction_key records")
    parser.add_argument("--image", action="append", type=Path, default=[], help="image path; may be repeated")
    parser.add_argument("--dir", action="append", type=Path, default=[], help="recursively scan image files; may be repeated")
    parser.add_argument("--output", required=True, type=Path, help="JSON evidence output path, or - for stdout")
    args = parser.parse_args(argv)
    if args.manifest is None and not args.image and not args.dir:
        parser.error("at least one of --manifest, --image, or --dir is required")

    try:
        records = _collect_records(args)
        version = tesseract_version()
        images = [
            read_image(
                record["image_path"],
                direction_key_claim=record["direction_key_claim"],
                engine_version=version,
            )
            for record in records
        ]
        evidence = {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "engine": "tesseract",
            "engine_version": version,
            "images": images,
        }
        output_text = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
        if str(args.output) == "-":
            sys.stdout.write(output_text)
        else:
            args.output.write_text(output_text, encoding="utf-8")
    except (OSError, ValueError) as error:
        print(f"certificate_reader: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
