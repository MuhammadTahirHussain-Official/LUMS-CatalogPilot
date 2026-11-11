"""Data-only parsers for tabular product files (HUB-05, SEC-UPL-08/09).

Every parser yields plain ``dict[str, str]`` rows and enforces row, column and cell limits.
Nothing is ever evaluated: spreadsheet formulas are read as their cached value or text,
XML is parsed with DTDs and external entities disabled.
"""

from __future__ import annotations

import codecs
import csv
import io
import json
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from charset_normalizer import from_bytes
from defusedxml import ElementTree as SafeET

from ..config import Settings, get_settings

DELIMITERS = [",", ";", "\t", "|"]


class ParseError(Exception):
    pass


@dataclass
class ParseMeta:
    kind: str
    encoding: str | None = None
    delimiter: str | None = None
    columns: list[str] = field(default_factory=list)
    sheet: str | None = None
    warnings: list[str] = field(default_factory=list)


def detect_encoding(path: Path) -> str:
    with path.open("rb") as fh:
        head = fh.read(256 * 1024)
    if head.startswith(codecs.BOM_UTF8):
        return "utf-8-sig"
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return "utf-16"
    try:
        head.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError as exc:
        # A multi-byte character cut by the sample boundary is still UTF-8.
        if exc.start >= len(head) - 4:
            return "utf-8"
    best = from_bytes(head).best()
    return best.encoding if best and best.encoding else "cp1252"


def sniff_delimiter(sample: str) -> str:
    """Pick the delimiter that splits the first lines into the most consistent column count."""
    best, best_score = ",", -1.0
    for delim in DELIMITERS:
        try:
            rows = list(csv.reader(io.StringIO(sample), delimiter=delim))[:30]
        except csv.Error:
            continue
        widths = [len(r) for r in rows if r]
        if not widths or max(widths) < 2:
            continue
        common, freq = Counter(widths).most_common(1)[0]
        score = (freq / len(widths)) * common
        if score > best_score:
            best, best_score = delim, score
    return best


def _dedupe_headers(raw: list[Any]) -> list[str]:
    seen: Counter[str] = Counter()
    out = []
    for i, cell in enumerate(raw):
        name = str(cell).strip() if cell is not None else ""
        name = name.lstrip("﻿") or f"column_{i + 1}"
        seen[name] += 1
        out.append(name if seen[name] == 1 else f"{name}_{seen[name]}")
    return out


def _cell(value: Any, settings: Settings) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # Excel stores 12 as 12.0; keep SKUs and barcodes intact
    text = str(value)
    if len(text.encode("utf-8", errors="ignore")) > settings.max_cell_bytes:
        raise ParseError(f"a cell is larger than {settings.max_cell_bytes // 1024} KB")
    return text


def _rows_from_table(header: list[Any], body: Iterator[list[Any]], meta: ParseMeta, settings: Settings):
    columns = _dedupe_headers(header)
    if len(columns) > settings.max_columns:
        raise ParseError(f"more than {settings.max_columns} columns")
    if not any(c and not c.startswith("column_") for c in columns):
        raise ParseError("the first row has no column names")
    meta.columns = columns
    count = 0
    for raw in body:
        if raw is None or all(c is None or str(c).strip() == "" for c in raw):
            continue  # blank line
        count += 1
        if count > settings.max_rows:
            raise ParseError(f"more than {settings.max_rows} rows")
        if len(raw) > len(columns):
            extra = [c for c in raw[len(columns):] if c not in (None, "")]
            if extra and "ragged" not in meta.warnings:
                meta.warnings.append("ragged")
        yield {col: _cell(raw[i] if i < len(raw) else None, settings) for i, col in enumerate(columns)}


def _iter_delimited(path: Path, meta: ParseMeta, settings: Settings):
    meta.encoding = detect_encoding(path)
    csv.field_size_limit(max(settings.max_cell_bytes * 4, 131072))
    with path.open("r", encoding=meta.encoding, errors="replace", newline="") as fh:
        sample = fh.read(64 * 1024)
        fh.seek(0)
        meta.delimiter = "\t" if meta.kind == "tsv" else sniff_delimiter(sample)
        reader = csv.reader(fh, delimiter=meta.delimiter)
        try:
            header = next(reader)
        except StopIteration:
            raise ParseError("the file is empty") from None
        except csv.Error as exc:
            raise ParseError(f"malformed file: {exc}") from exc
        try:
            yield from _rows_from_table(header, reader, meta, settings)
        except csv.Error as exc:
            raise ParseError(f"malformed row near line {reader.line_num}: {exc}") from exc


def _iter_xlsx(path: Path, meta: ParseMeta, settings: Settings):
    from openpyxl import load_workbook

    try:
        # read_only streams rows; data_only returns cached values, so formulas are never evaluated.
        wb = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    except Exception as exc:
        raise ParseError(f"cannot open workbook: {exc}") from exc
    try:
        sheet = next((ws for ws in wb.worksheets if ws.sheet_state == "visible"), None)
        if sheet is None:
            raise ParseError("the workbook has no visible sheet")
        meta.sheet = sheet.title
        if len(wb.worksheets) > 1:
            meta.warnings.append(f"only the first sheet ('{sheet.title}') is imported")
        rows = sheet.iter_rows(values_only=True)
        header = None
        for candidate in rows:
            if candidate and any(c not in (None, "") for c in candidate):
                header = list(candidate)
                break
        if header is None:
            raise ParseError("the sheet is empty")
        while header and header[-1] in (None, ""):
            header.pop()
        yield from _rows_from_table(header, (list(r) for r in rows), meta, settings)
    finally:
        wb.close()


def _flatten(obj: Any, prefix: str = "", out: dict[str, str] | None = None, depth: int = 0) -> dict[str, str]:
    out = {} if out is None else out
    if depth > 6:
        out[prefix or "value"] = json.dumps(obj, ensure_ascii=False)[:2000]
    elif isinstance(obj, dict):
        for key, value in obj.items():
            _flatten(value, f"{prefix}.{key}" if prefix else str(key), out, depth + 1)
    elif isinstance(obj, list):
        if all(not isinstance(v, (dict, list)) for v in obj):
            out[prefix] = "|".join("" if v is None else str(v) for v in obj)
        else:
            for i, value in enumerate(obj[:50]):
                _flatten(value, f"{prefix}[{i}]", out, depth + 1)
    else:
        out[prefix or "value"] = "" if obj is None else str(obj)
    return out


def _records_to_rows(records: Iterator[Any], meta: ParseMeta, settings: Settings):
    columns: dict[str, None] = {}
    count = 0
    for record in records:
        if not isinstance(record, dict):
            raise ParseError("every record must be an object")
        count += 1
        if count > settings.max_rows:
            raise ParseError(f"more than {settings.max_rows} rows")
        flat = _flatten(record)
        for value in flat.values():
            if len(value) > settings.max_cell_bytes:
                raise ParseError(f"a value is larger than {settings.max_cell_bytes // 1024} KB")
        for key in flat:
            columns.setdefault(key, None)
        if len(columns) > settings.max_columns:
            raise ParseError(f"more than {settings.max_columns} columns")
        meta.columns = list(columns)
        yield flat


def _iter_json(path: Path, meta: ParseMeta, settings: Settings):
    if path.stat().st_size > 200 * 1024 * 1024:
        raise ParseError("JSON files above 200 MB must be sent as NDJSON")
    try:
        data = json.loads(path.read_text(encoding=detect_encoding(path)))
    except (json.JSONDecodeError, RecursionError, UnicodeDecodeError) as exc:
        raise ParseError(f"invalid JSON: {exc}") from exc
    if isinstance(data, dict):
        lists = [v for v in data.values() if isinstance(v, list) and v and isinstance(v[0], dict)]
        if not lists:
            raise ParseError("JSON must be a list of products or an object containing one")
        data = max(lists, key=len)
    if not isinstance(data, list):
        raise ParseError("JSON must be a list of products")
    yield from _records_to_rows(iter(data), meta, settings)


def _iter_ndjson(path: Path, meta: ParseMeta, settings: Settings):
    def records():
        with path.open("r", encoding=detect_encoding(path), errors="replace") as fh:
            for number, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ParseError(f"invalid JSON on line {number}: {exc.msg}") from exc

    yield from _records_to_rows(records(), meta, settings)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _iter_xml(path: Path, meta: ParseMeta, settings: Settings):
    try:
        # defusedxml forbids DTDs, external entities and entity expansion (XXE, billion laughs).
        root = SafeET.parse(str(path), forbid_dtd=True).getroot()
    except Exception as exc:
        raise ParseError(f"invalid or unsafe XML: {type(exc).__name__}") from exc
    # Find the repeated record element: the most common grandchild/child tag with children.
    best: tuple[int, list] = (0, [])
    for parent in [root, *list(root)]:
        groups: dict[str, list] = {}
        for child in parent:
            if len(child):
                groups.setdefault(_local(child.tag), []).append(child)
        for members in groups.values():
            if len(members) > best[0]:
                best = (len(members), members)
    if not best[1]:
        raise ParseError("no repeated product element found in the XML")

    def records():
        for element in best[1]:
            record: dict[str, Any] = {}
            for child in element:
                key = _local(child.tag)
                value = (child.text or "").strip() if not len(child) else {
                    _local(g.tag): (g.text or "").strip() for g in child
                }
                if key in record:  # repeated tags (e.g. additional_image_link) become a list
                    existing = record[key]
                    record[key] = [*existing, value] if isinstance(existing, list) else [existing, value]
                else:
                    record[key] = value
            yield record

    yield from _records_to_rows(records(), meta, settings)


_PARSERS = {"csv": _iter_delimited, "tsv": _iter_delimited, "xlsx": _iter_xlsx, "json": _iter_json,
            "ndjson": _iter_ndjson, "xml": _iter_xml}


def iter_rows(path: Path, kind: str, settings: Settings | None = None) -> tuple[ParseMeta, Iterator[dict[str, str]]]:
    if kind not in _PARSERS:
        raise ParseError(f"'{kind}' files cannot be imported as product data")
    meta = ParseMeta(kind=kind)
    return meta, _PARSERS[kind](path, meta, settings or get_settings())


def preview(path: Path, kind: str, limit: int = 20) -> tuple[ParseMeta, list[dict[str, str]]]:
    meta, rows = iter_rows(path, kind)
    sample = []
    for row in rows:
        sample.append(row)
        if len(sample) >= limit:
            break
    return meta, sample
