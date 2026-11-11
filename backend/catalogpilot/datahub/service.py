"""Upload sessions, resumable chunks, scan-and-release, import and export (HUB-01..20)."""

from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
import zipfile
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..catalog import service as catalog_service
from ..catalog.cleaning import format_money
from ..catalog.service import Change
from ..config import get_settings
from ..ids import uuid7
from ..models import Catalog, MappingTemplate, Product, ScanResult, Upload, UploadFile
from ..security import audit
from . import mapping as mapping_mod
from . import parse, scan, storage

PLAN_STORAGE_BYTES = {"free": 2 * 1024**3, "pro": 50 * 1024**3, "scale": 1024**4}
DATA_TYPES = {"csv", "tsv", "xlsx", "json", "ndjson", "xml"}
FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")


class UploadError(Exception):
    def __init__(self, code: str, detail: str, status: int = 422, extra: dict[str, Any] | None = None):
        self.code, self.detail, self.status, self.extra = code, detail, status, extra or {}


def sanitize_filename(name: str) -> str:
    """Original names are metadata only; storage keys are always server-generated (SEC-UPL-13)."""
    name = unicodedata.normalize("NFC", name).replace("\\", "/").rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f‪-‮⁦-⁩]", "", name).strip(" .")
    return name[:255] or "file"


def create_upload(db: Session, tenant_id: str, plan: str, user_id: str, catalog_id: str,
                  files: list[dict[str, Any]]) -> tuple[Upload, list[UploadFile]]:
    settings = get_settings()
    catalog_service.get_catalog(db, catalog_id)
    if not files or len(files) > 100:
        raise UploadError("validation_failed", "An upload needs between 1 and 100 files.")
    total = 0
    for f in files:
        size = int(f["size"])
        if size <= 0:
            raise UploadError("validation_failed", f"'{f['name']}' is empty.")
        if size > settings.max_file_bytes:
            raise UploadError("payload_too_large", f"'{f['name']}' is larger than the plan allows.", 413)
        if not re.fullmatch(r"[0-9a-fA-F]{64}", str(f["sha256"])):
            raise UploadError("validation_failed", "sha256 must be 64 hex characters.")
        total += size
    quota = PLAN_STORAGE_BYTES.get(plan, PLAN_STORAGE_BYTES["free"])
    if storage.tenant_usage_bytes(tenant_id) + total > quota:
        raise UploadError("quota_exceeded", "This upload would exceed your storage quota.", 429)
    upload = Upload(tenant_id=tenant_id, catalog_id=catalog_id, created_by=user_id, total_bytes=total)
    db.add(upload)
    db.flush()
    rows = []
    for f in files:
        row = UploadFile(
            tenant_id=tenant_id, upload_id=upload.id, original_name=sanitize_filename(str(f["name"])),
            object_key=f"{tenant_id}/{upload.id}/{uuid7()}", size_bytes=int(f["size"]),
            sha256=str(f["sha256"]).lower(),
        )
        db.add(row)
        rows.append(row)
    audit.record(db, tenant_id, "upload.created", f"upload:{upload.id}", {"files": len(rows), "bytes": total})
    db.flush()
    return upload, rows


def get_file(db: Session, upload_id: str, file_id: str) -> UploadFile:
    row = db.execute(
        select(UploadFile).where(UploadFile.id == file_id, UploadFile.upload_id == upload_id)
    ).scalar_one_or_none()
    if row is None:
        raise catalog_service.NotFound("file not found")
    return row


def receive_chunk(db: Session, row: UploadFile, offset: int, data: bytes, checksum: str | None) -> bool:
    """Appends one chunk at the durable offset. Returns True when the file is complete.

    The offset lives in the database, so after a dropped connection or a server restart the
    client asks for it (HEAD) and continues; nothing is restarted (HUB-02).
    """
    if row.state != "receiving":
        raise UploadError("conflict", "This file has already been received.", 409)
    if offset != row.bytes_received:
        raise UploadError("offset_mismatch", "Resume from the stored offset.", 409, {"offset": row.bytes_received})
    if not data:
        raise UploadError("validation_failed", "Empty chunk.")
    if offset + len(data) > row.size_bytes:
        raise UploadError("payload_too_large", "More bytes than the declared file size.", 413)
    if checksum and hashlib.sha256(data).hexdigest() != checksum.lower():
        raise UploadError("checksum_mismatch", "Chunk checksum does not match; resend this chunk.", 460)
    storage.write_at("quarantine", row.object_key, offset, data)
    row.bytes_received = offset + len(data)
    if row.bytes_received < row.size_bytes:
        db.flush()
        return False
    digest = hashlib.sha256()
    with storage.object_path("quarantine", row.object_key).open("rb") as fh:
        while block := fh.read(1 << 20):
            digest.update(block)
    if digest.hexdigest() != row.sha256:
        row.bytes_received = 0
        storage.delete("quarantine", row.object_key)
        db.flush()
        raise UploadError("checksum_mismatch", "The file hash does not match what was declared; upload it again.", 460)
    row.state = "received"
    db.flush()
    return True


def process_file(db: Session, row: UploadFile) -> None:
    """Scan, then release to the clean bucket or quarantine. Idempotent: safe to re-run after a crash."""
    if row.state not in ("received", "scanning"):
        return
    row.state = "scanning"
    db.flush()
    path = storage.object_path("quarantine", row.object_key)
    detected, verdicts = scan.scan_file(path, row.original_name)
    for v in verdicts:
        db.add(ScanResult(tenant_id=row.tenant_id, file_id=row.id, engine=v.engine, verdict=v.verdict, detail=v.detail))
    row.detected_type = detected
    bad = [v for v in verdicts if not v.clean]
    if any(v.verdict == "error" and v.engine == "clamd" for v in bad):
        # Scanner unavailable: hold the file, do not reject it and do not release it (fail closed).
        row.state = "received"
        db.flush()
        raise UploadError("dependency_unavailable", "The malware scanner is unavailable; the file is held.", 503)
    if bad:
        row.state = "quarantined"
        row.parse_meta = {"rejected": [{"engine": v.engine, "verdict": v.verdict, "detail": v.detail} for v in bad]}
        audit.record(db, row.tenant_id, "upload.quarantined", f"file:{row.id}",
                     {"sha256": row.sha256, "reasons": [f"{v.engine}: {v.detail}" for v in bad]})
    else:
        meta: dict[str, Any] = {}
        if detected == "zip":
            meta["media"] = _extract_media(row)
        else:
            storage.copy("quarantine", row.object_key, "clean", row.object_key)
        row.bucket = "clean"
        row.state = "clean"
        row.parse_meta = meta
        audit.record(db, row.tenant_id, "upload.clean", f"file:{row.id}", {"sha256": row.sha256, "type": detected})
    _refresh_upload_state(db, row.upload_id)
    db.flush()


def _extract_media(row: UploadFile) -> list[dict[str, Any]]:
    """Unpack a media ZIP entry by entry. Each image is type-checked, scanned and re-encoded
    from pixels (content disarm) before it reaches the clean bucket; other entries are skipped."""
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = scan.MAX_IMAGE_PIXELS
    settings = get_settings()
    out: list[dict[str, Any]] = []
    signature = scan.SignatureEngine()
    tmp_root = settings.data_dir / "tmp" / row.id
    tmp_root.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(storage.object_path("quarantine", row.object_key)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                name = sanitize_filename(PurePosixPath(info.filename.replace("\\", "/")).name)
                entry = {"name": name, "status": "skipped", "reason": ""}
                out.append(entry)
                if info.file_size > 50 * 1024 * 1024:
                    entry["reason"] = "larger than 50 MB"
                    continue
                tmp = tmp_root / uuid7()
                written = 0
                with zf.open(info) as src, tmp.open("wb") as dst:
                    while block := src.read(1 << 16):
                        written += len(block)
                        if written > info.file_size or written > 50 * 1024 * 1024:
                            break  # header lied about the size: stop (zip bomb)
                        dst.write(block)
                try:
                    if written != info.file_size:
                        entry["reason"] = "size does not match archive header"
                        continue
                    kind = scan.detect_type(tmp)
                    if kind not in scan.IMAGE_TYPES:
                        entry["reason"] = "not an accepted image type"
                        continue
                    checks = [signature.scan(tmp, kind), scan.check_image(tmp, kind)]
                    if not all(v.clean for v in checks):
                        entry.update(status="rejected", reason="; ".join(v.detail for v in checks if not v.clean))
                        continue
                    try:
                        with Image.open(tmp) as img:
                            img.load()
                            if img.width > scan.MAX_IMAGE_DIM or img.height > scan.MAX_IMAGE_DIM:
                                raise ValueError("dimensions above limit")
                            mode = "RGBA" if img.mode in ("RGBA", "LA", "P") else "RGB"
                            clean = img.convert(mode)  # new image from pixels: metadata is not carried
                            buf = io.BytesIO()
                            if mode == "RGBA":
                                clean.save(buf, "PNG", optimize=True)
                                ext = "png"
                            else:
                                clean.save(buf, "JPEG", quality=90)
                                ext = "jpg"
                            width, height = clean.size
                    except Exception as exc:
                        entry.update(status="rejected", reason=f"cannot decode image ({type(exc).__name__})")
                        continue
                    data = buf.getvalue()
                    digest = hashlib.sha256(data).hexdigest()
                    key = f"{row.tenant_id}/media/{digest}.{ext}"  # per-tenant dedup only (HUB-19)
                    if not storage.exists("clean", key):
                        storage.put("clean", key, data)
                    entry.update(status="stored", key=key, sha256=digest, width=width, height=height,
                                 stem=PurePosixPath(name).stem.lower())
                finally:
                    tmp.unlink(missing_ok=True)
    finally:
        tmp_root.rmdir() if tmp_root.exists() and not any(tmp_root.iterdir()) else None
    return out


def _refresh_upload_state(db: Session, upload_id: str) -> None:
    upload = db.execute(select(Upload).where(Upload.id == upload_id)).scalar_one()
    states = db.execute(select(UploadFile.state).where(UploadFile.upload_id == upload_id)).scalars().all()
    if upload.state in ("imported", "parsing"):
        return
    if any(s == "quarantined" for s in states) and all(s in ("quarantined", "clean") for s in states):
        upload.state = "quarantined" if all(s == "quarantined" for s in states) else "clean"
    elif all(s == "clean" for s in states):
        upload.state = "clean"
    elif any(s in ("received", "scanning") for s in states):
        upload.state = "scanning"


def _require_clean_data(row: UploadFile) -> None:
    # HUB-04: nothing is parsed or shown before it has passed scanning.
    if row.state != "clean" or row.bucket != "clean":
        raise UploadError("conflict", "This file has not passed security checks yet.", 409)
    if row.detected_type not in DATA_TYPES:
        raise UploadError("validation_failed", "This file is not a product data file.")


def preview_file(db: Session, row: UploadFile) -> dict[str, Any]:
    _require_clean_data(row)
    try:
        meta, sample = parse.preview(storage.object_path("clean", row.object_key), row.detected_type)
    except parse.ParseError as exc:
        raise UploadError("validation_failed", str(exc)) from exc
    suggestion = mapping_mod.suggest_mapping(meta.columns, sample)
    return {
        "encoding": meta.encoding, "delimiter": meta.delimiter, "sheet": meta.sheet, "columns": meta.columns,
        "warnings": meta.warnings, "rows": sample, "suggested_mapping": suggestion,
        "target_fields": mapping_mod.TARGET_FIELDS, "key_fields": list(mapping_mod.KEY_FIELDS),
    }


def set_mapping(db: Session, row: UploadFile, mapping: dict[str, Any] | None, template_id: str | None,
                save_as: str | None) -> dict[str, Any]:
    _require_clean_data(row)
    if template_id:
        template = db.execute(select(MappingTemplate).where(MappingTemplate.id == template_id)).scalar_one_or_none()
        if template is None:
            raise catalog_service.NotFound("mapping template not found")
        mapping = template.mapping
    if not mapping:
        raise UploadError("validation_failed", "Provide a mapping or a template id.")
    try:
        meta, _ = parse.preview(storage.object_path("clean", row.object_key), row.detected_type, limit=1)
    except parse.ParseError as exc:
        raise UploadError("validation_failed", str(exc)) from exc
    problems = mapping_mod.validate_mapping(mapping, meta.columns)
    if problems:
        raise UploadError("validation_failed", "The column mapping is not valid.", extra={"errors": problems})
    row.mapping = mapping
    if save_as:
        existing = db.execute(select(MappingTemplate).where(MappingTemplate.name == save_as)).scalar_one_or_none()
        if existing:
            existing.mapping = mapping
        else:
            db.add(MappingTemplate(tenant_id=row.tenant_id, name=save_as[:120], mapping=mapping))
    db.flush()
    return mapping


def run_import(db: Session, row: UploadFile, user_id: str) -> dict[str, Any]:
    _require_clean_data(row)
    if not row.mapping:
        raise UploadError("conflict", "Set the column mapping before importing.", 409)
    upload = db.execute(select(Upload).where(Upload.id == row.upload_id)).scalar_one()
    catalog = catalog_service.get_catalog(db, upload.catalog_id)
    try:
        _meta, rows = parse.iter_rows(storage.object_path("clean", row.object_key), row.detected_type)
        records, issues, total = mapping_mod.build_records(rows, row.mapping, catalog.default_locale, catalog.default_currency)
    except parse.ParseError as exc:
        raise UploadError("validation_failed", str(exc)) from exc

    # HUB-07: attach images from media ZIPs in the same upload by file-name stem = SKU or handle.
    media_by_stem: dict[str, list[dict[str, Any]]] = {}
    unmatched: set[str] = set()
    siblings = db.execute(select(UploadFile).where(UploadFile.upload_id == row.upload_id)).scalars().all()
    for sibling in siblings:
        for m in (sibling.parse_meta or {}).get("media", []):
            if m.get("status") == "stored":
                stem = re.sub(r"[-_ ]\d{1,2}$", "", m["stem"])  # "sku-2.jpg" -> second image of "sku"
                media_by_stem.setdefault(stem, []).append(m)
                unmatched.add(m["name"])
    for record in records:
        stems = {(v.get("sku") or "").lower() for v in record.state["variants"]} | {
            (record.state.get("handle") or "").lower(), record.external_key.lower()}
        for stem in sorted(s for s in stems if s and s in media_by_stem):
            for m in sorted(media_by_stem[stem], key=lambda x: x["name"]):
                record.state["media"].append({
                    "url": None, "stored_key": m["key"], "sha256": m["sha256"], "alt": {},
                    "position": len(record.state["media"]), "width": m["width"], "height": m["height"],
                })
                unmatched.discard(m["name"])

    changes = [
        Change(r.external_key, r.state, origin="upload", ref=f"file:{row.id}",
               source_ref={"upload_file_id": row.id, "rows": r.rows[:50]})
        for r in records
    ]
    version, results = catalog_service.apply_changes(db, catalog, changes, "upload", user_id)
    counts = {"created": 0, "updated": 0, "unchanged": 0, "error": 0}
    row_results: list[dict[str, Any]] = []
    by_key = {r.external_key: r for r in records}
    for result in results:
        counts[result.outcome if result.outcome in counts else "error"] += 1
        if result.outcome == "error":
            for number in by_key[result.external_key].rows:
                issues.append(mapping_mod.RowIssue(number, "error", result.error or "could not be saved"))
    error_rows = {i.row for i in issues if i.level == "error"}
    summary = {
        "total_rows": total, "products": counts, "row_errors": len(error_rows),
        "warnings": sum(1 for i in issues if i.level == "warning"),
        "catalog_version": version.version if version else catalog.head_version,
        "unmatched_media": sorted(unmatched)[:200],
        "issues": [{"row": i.row, "level": i.level, "message": i.message} for i in sorted(issues, key=lambda i: i.row)[:500]],
    }
    row.import_result = summary
    upload.state = "imported"
    if error_rows:
        buf = io.StringIO()
        csv.writer(buf).writerows(neutralize(r) for r in mapping_mod.iter_error_rows(issues))
        storage.put("exports", f"{row.tenant_id}/errors/{row.id}.csv", buf.getvalue().encode("utf-8-sig"))
    audit.record(db, row.tenant_id, "upload.imported", f"file:{row.id}",
                 {"rows": total, **counts, "row_errors": len(error_rows)})
    db.flush()
    _ = row_results
    return summary


# ---------------------------------------------------------------- export (HUB-18)

def neutralize_cell(value: Any) -> Any:
    """Formula-injection neutralization (SEC-UPL-10): text that a spreadsheet would execute
    is prefixed with an apostrophe so it is displayed, not evaluated."""
    if isinstance(value, str) and value.startswith(FORMULA_PREFIX):
        return "'" + value
    return value


def neutralize(row: list[Any]) -> list[Any]:
    return [neutralize_cell(c) for c in row]


EXPORT_COLUMNS = [
    "handle", "title", "description", "brand", "product_type", "category", "tags", "status", "sku", "gtin",
    "price", "compare_at_price", "currency", "inventory", "weight_grams", "options", "image_urls",
    "seo_title", "seo_description",
]


def export_rows(db: Session, catalog: Catalog, include_cost: bool):
    header = EXPORT_COLUMNS + (["cost"] if include_cost else [])
    yield header
    locale = catalog.default_locale
    last_id = ""
    while True:
        batch = db.execute(
            select(Product).where(Product.catalog_id == catalog.id, Product.status != "archived", Product.id > last_id)
            .order_by(Product.id).limit(500)
        ).scalars().all()
        if not batch:
            return
        for p in batch:
            last_id = p.id
            title = p.title.get(locale) or next(iter(p.title.values()), "")
            desc = p.description_html.get(locale) or next(iter(p.description_html.values()), "")
            images = "|".join(m["url"] for m in p.media if m.get("url"))
            for i, v in enumerate(p.variants or [{}]):
                currency = v.get("currency") or catalog.default_currency
                line = [
                    p.handle or "", title if i == 0 else "", desc if i == 0 else "", p.brand or "" if i == 0 else "",
                    p.product_type or "" if i == 0 else "", " > ".join(p.source_category) if i == 0 else "",
                    ", ".join(p.tags) if i == 0 else "", p.status, v.get("sku") or "", v.get("gtin") or "",
                    format_money(v.get("price_minor"), currency), format_money(v.get("compare_at_minor"), currency),
                    currency, "" if v.get("inventory") is None else v["inventory"],
                    "" if v.get("weight_grams") is None else v["weight_grams"],
                    "; ".join(f"{k}: {val}" for k, val in (v.get("option_values") or {}).items()),
                    images if i == 0 else "", (p.seo or {}).get("title", "") if i == 0 else "",
                    (p.seo or {}).get("description", "") if i == 0 else "",
                ]
                if include_cost:
                    line.append(format_money(v.get("cost_minor"), currency))
                # Numeric columns are produced by us and are never negative; neutralize text only.
                yield neutralize(line)


def export_catalog(db: Session, catalog: Catalog, fmt: str, include_cost: bool) -> tuple[bytes, str, str]:
    rows = export_rows(db, catalog, include_cost)
    if fmt == "csv":
        buf = io.StringIO()
        csv.writer(buf).writerows(rows)
        data, mime = buf.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8"
    elif fmt == "xlsx":
        from openpyxl import Workbook

        wb = Workbook(write_only=True)
        ws = wb.create_sheet("Products")
        for line in rows:
            ws.append(line)
        out = io.BytesIO()
        wb.save(out)
        data, mime = out.getvalue(), "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    else:
        raise UploadError("validation_failed", "Export format must be csv or xlsx.")
    count = db.execute(select(func.count()).select_from(Product).where(Product.catalog_id == catalog.id)).scalar_one()
    audit.record(db, catalog.tenant_id, "catalog.exported", f"catalog:{catalog.id}", {"format": fmt, "products": count})
    return data, mime, f"{catalog.name[:60] or 'catalog'}.{fmt}"
