"""Catalog, Data Hub, channel, job and AI endpoints (04_API_Design 4.2-4.4)."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import db as dbm
from ..ai import confidence
from ..ai import service as ai_service
from ..ai import taxonomy
from ..catalog import service as catalog_service
from ..catalog.cleaning import clean_text, sanitize_html
from ..catalog.service import Change
from ..connectors import csv_feed, get_connector, platforms
from ..datahub import service as hub
from ..datahub import storage
from ..engine import importer
from ..engine import jobs as job_service
from ..engine import transform
from ..identity import service as identity
from ..models import (
    AIProposal,
    Catalog,
    CatalogVersion,
    Channel,
    ChannelListing,
    ChannelOverride,
    Credential,
    CredentialUsage,
    Job,
    JobItem,
    MappingTemplate,
    Product,
    ScanResult,
    Tenant,
    Upload,
    UploadFile,
)
from ..security import audit
from ..security.authz import (
    P_AUDIT_READ,
    P_CATALOG_READ,
    P_CATALOG_WRITE,
    P_CHANNELS_ADMIN,
    P_CHANNELS_READ,
    P_COST,
    P_JOBS_READ,
    P_JOBS_WRITE,
    P_PUBLISH,
    P_REVIEW,
    P_UPLOAD,
)
from ..vault import broker
from .deps import Auth, Problem, get_db, idempotent, rate_limit, remember, require, scoped

router = APIRouter()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _page(limit: int) -> int:
    return max(1, min(limit, 200))


# ================================================================ catalogs & products

class CatalogIn(Strict):
    name: str = Field(min_length=1, max_length=200)
    default_currency: str = Field(default="USD", pattern="^[A-Z]{3}$")
    default_locale: str = Field(default="en", pattern="^[a-zA-Z]{2,3}(-[a-zA-Z0-9]{2,8})?$")


class VariantIn(Strict):
    sku: str | None = Field(default=None, max_length=200)
    gtin: str | None = Field(default=None, max_length=20)
    mpn: str | None = Field(default=None, max_length=120)
    price_minor: int | None = Field(default=None, ge=0, le=10**13)
    compare_at_minor: int | None = Field(default=None, ge=0, le=10**13)
    cost_minor: int | None = Field(default=None, ge=0, le=10**13)
    currency: str = Field(pattern="^[A-Z]{3}$")
    inventory: int | None = Field(default=None, ge=0, le=10**9)
    weight_grams: float | None = Field(default=None, ge=0, le=10**7)
    option_values: dict[str, str] = Field(default_factory=dict)


class ProductPatch(Strict):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description_html: str | None = Field(default=None, max_length=65_000)
    brand: str | None = Field(default=None, max_length=200)
    product_type: str | None = Field(default=None, max_length=200)
    tags: list[str] | None = Field(default=None, max_length=250)
    status: str | None = Field(default=None, pattern="^(draft|active|archived)$")
    variants: list[VariantIn] | None = Field(default=None, max_length=2000)


class BulkIn(Strict):
    product_ids: list[str] = Field(min_length=1, max_length=1000)
    set_status: str | None = Field(default=None, pattern="^(draft|active|archived)$")
    add_tags: list[str] = Field(default_factory=list, max_length=50)
    remove_tags: list[str] = Field(default_factory=list, max_length=50)


def _catalog_view(c: Catalog, products: int | None = None) -> dict[str, Any]:
    out = {"id": c.id, "name": c.name, "head_version": c.head_version, "default_currency": c.default_currency,
           "default_locale": c.default_locale, "created_at": c.created_at.isoformat()}
    if products is not None:
        out["products"] = products
    return out


@router.get("/catalogs")
@scoped
def list_catalogs(db: Session = Depends(get_db), auth: Auth = Depends(require(P_CATALOG_READ))) -> dict[str, Any]:
    counts = dict(db.execute(select(Product.catalog_id, func.count()).where(Product.status != "archived")
                             .group_by(Product.catalog_id)).all())
    rows = db.execute(select(Catalog).order_by(Catalog.created_at)).scalars().all()
    return {"data": [_catalog_view(c, counts.get(c.id, 0)) for c in rows]}


@router.post("/catalogs", status_code=201)
@scoped
def create_catalog(body: CatalogIn, db: Session = Depends(get_db), auth: Auth = Depends(require(P_CATALOG_WRITE))) -> dict[str, Any]:
    if db.execute(select(func.count()).select_from(Catalog)).scalar_one() >= 50:
        raise Problem(429, "quota_exceeded", "You have reached the limit of 50 catalogs.")
    catalog = Catalog(tenant_id=auth.principal.tenant_id, name=clean_text(body.name, 200),
                      default_currency=body.default_currency, default_locale=body.default_locale)
    db.add(catalog)
    db.flush()
    audit.record(db, catalog.tenant_id, "catalog.created", f"catalog:{catalog.id}", {"name": catalog.name})
    return _catalog_view(catalog, 0)


@router.get("/catalogs/{catalog_id}/products")
@scoped
def list_products(catalog_id: str, limit: int = 50, cursor: str | None = None, q: str | None = None,
                  status: str | None = None, db: Session = Depends(get_db),
                  auth: Auth = Depends(require(P_CATALOG_READ))) -> dict[str, Any]:
    catalog = catalog_service.get_catalog(db, catalog_id)
    stmt = select(Product).where(Product.catalog_id == catalog.id).order_by(Product.id).limit(_page(limit) + 1)
    if cursor:
        stmt = stmt.where(Product.id > cursor[:36])
    if status in ("draft", "active", "archived"):
        stmt = stmt.where(Product.status == status)
    else:
        stmt = stmt.where(Product.status != "archived")
    if q:
        needle = "%" + re.sub(r"[%_\\]", "", q[:100]) + "%"
        stmt = stmt.where(or_(Product.external_key.ilike(needle), Product.handle.ilike(needle),
                              func.cast(Product.title, dbm.String).ilike(needle)))
    rows = db.execute(stmt).scalars().all()
    more = len(rows) > _page(limit)
    rows = rows[:_page(limit)]
    include_cost = auth.principal.can(P_COST)
    return {"data": [catalog_service.product_view(p, include_cost) for p in rows],
            "next_cursor": rows[-1].id if more else None}


@router.get("/products/{product_id}")
@scoped
def get_product(product_id: str, response: Response, db: Session = Depends(get_db),
                auth: Auth = Depends(require(P_CATALOG_READ))) -> dict[str, Any]:
    p = catalog_service.get_product(db, product_id)
    response.headers["ETag"] = f'"{p.version}"'
    return catalog_service.product_view(p, auth.principal.can(P_COST))


@router.patch("/products/{product_id}")
@scoped
def patch_product(product_id: str, body: ProductPatch, request: Request, response: Response,
                  db: Session = Depends(get_db), auth: Auth = Depends(require(P_CATALOG_WRITE))) -> dict[str, Any]:
    p = catalog_service.get_product(db, product_id)
    catalog = catalog_service.get_catalog(db, p.catalog_id)
    if_match = request.headers.get("if-match")
    if not if_match:
        raise Problem(428, "precondition_required", "Send If-Match with the product version you edited.")
    try:
        expected = int(if_match.strip('"W/ '))
    except ValueError:
        raise Problem(400, "bad_request", "If-Match must be the product version.") from None
    fields = body.model_dump(exclude_unset=True)
    state: dict[str, Any] = {}
    locale = catalog.default_locale
    if "title" in fields:
        state["title"] = {**p.title, locale: clean_text(fields["title"], 500)}
    if "description_html" in fields:
        state["description_html"] = {**p.description_html, locale: sanitize_html(fields["description_html"] or "")}
    for key in ("brand", "product_type"):
        if key in fields:
            state[key] = clean_text(fields[key], 200) or None
    if "tags" in fields:
        state["tags"] = list(dict.fromkeys(clean_text(t, 120) for t in fields["tags"] if clean_text(t, 120)))
    if "status" in fields:
        state["status"] = fields["status"]
    if "variants" in fields:
        can_cost = auth.principal.can(P_COST)
        old_cost = {v.get("sku"): v.get("cost_minor") for v in p.variants}
        variants = []
        for v in fields["variants"]:
            # Property-level authorization: without the cost permission the field can be neither
            # read nor written; the stored value is kept (SEC-AUZ-05, no mass assignment).
            if not can_cost:
                v["cost_minor"] = old_cost.get(v.get("sku"))
            variants.append(v)
        skus = [v["sku"] for v in variants if v.get("sku")]
        if len(skus) != len(set(skus)):
            raise Problem(422, "validation_failed", "Each variant needs a distinct SKU.")
        state["variants"] = variants
    if not state:
        raise Problem(422, "validation_failed", "Nothing to change.")
    try:
        catalog_service.apply_changes(db, catalog, [Change(p.external_key, state, origin="seller_edit",
                                                           ref=f"user:{auth.principal.actor_id}", partial=True,
                                                           expected_version=expected)], "bulk_edit", auth.principal.user_id)
    except catalog_service.VersionConflict:
        raise Problem(412, "precondition_failed", "This product was changed by someone else. Reload and try again.") from None
    response.headers["ETag"] = f'"{p.version}"'
    return catalog_service.product_view(p, auth.principal.can(P_COST))


@router.post("/catalogs/{catalog_id}/products:bulk")
@scoped
def bulk_edit(catalog_id: str, body: BulkIn, db: Session = Depends(get_db),
              auth: Auth = Depends(require(P_CATALOG_WRITE))) -> dict[str, Any]:
    catalog = catalog_service.get_catalog(db, catalog_id)
    products = db.execute(select(Product).where(Product.catalog_id == catalog.id,
                                                Product.id.in_(body.product_ids))).scalars().all()
    add = [clean_text(t, 120) for t in body.add_tags if clean_text(t, 120)]
    drop = {t.lower() for t in body.remove_tags}
    changes = []
    for p in products:
        state: dict[str, Any] = {}
        if body.set_status:
            state["status"] = body.set_status
        if add or drop:
            state["tags"] = list(dict.fromkeys([t for t in p.tags if t.lower() not in drop] + add))
        if state:
            changes.append(Change(p.external_key, state, origin="seller_edit", partial=True))
    version, results = catalog_service.apply_changes(db, catalog, changes, "bulk_edit", auth.principal.user_id)
    counts: dict[str, int] = {}
    for r in results:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    return {"matched": len(products), "results": counts, "catalog_version": version.version if version else None}


@router.get("/catalogs/{catalog_id}/versions")
@scoped
def list_versions(catalog_id: str, db: Session = Depends(get_db), auth: Auth = Depends(require(P_CATALOG_READ))) -> dict[str, Any]:
    catalog = catalog_service.get_catalog(db, catalog_id)
    rows = db.execute(select(CatalogVersion.version, CatalogVersion.source, CatalogVersion.summary,
                             CatalogVersion.created_by, CatalogVersion.created_at)
                      .where(CatalogVersion.catalog_id == catalog.id).order_by(CatalogVersion.version.desc()).limit(200)).all()
    return {"head_version": catalog.head_version,
            "data": [{"version": v, "source": s, "summary": m, "created_by": by, "created_at": at.isoformat()}
                     for v, s, m, by, at in rows]}


@router.get("/catalogs/{catalog_id}/versions/{version}/diff")
@scoped
def version_diff(catalog_id: str, version: int, limit: int = 50, offset: int = 0, db: Session = Depends(get_db),
                 auth: Auth = Depends(require(P_CATALOG_READ))) -> dict[str, Any]:
    catalog = catalog_service.get_catalog(db, catalog_id)
    row = db.execute(select(CatalogVersion).where(CatalogVersion.catalog_id == catalog.id,
                                                  CatalogVersion.version == version)).scalar_one_or_none()
    if row is None:
        raise Problem(404, "not_found", "Version not found.")
    entries = row.diff[max(0, offset): max(0, offset) + _page(limit)]
    hide_cost = not auth.principal.can(P_COST)
    out = []
    for e in entries:
        fields = []
        before, after = e["before"] or {}, e["after"] or {}
        for key in catalog_service.STATE_FIELDS:
            if before.get(key) != after.get(key):
                b, a = before.get(key), after.get(key)
                if key == "variants" and hide_cost:
                    b = [{k: v for k, v in x.items() if k != "cost_minor"} for x in (b or [])]
                    a = [{k: v for k, v in x.items() if k != "cost_minor"} for x in (a or [])]
                    if a == b:
                        continue
                fields.append({"field": key, "before": b, "after": a})
        out.append({"product_id": e["product_id"], "external_key": e["external_key"], "op": e["op"], "fields": fields})
    return {"version": version, "summary": row.summary, "total": len(row.diff), "data": out}


@router.post("/catalogs/{catalog_id}/versions/{version}/restore")
@scoped
def restore_version(catalog_id: str, version: int, db: Session = Depends(get_db),
                    auth: Auth = Depends(require(P_CATALOG_WRITE))) -> dict[str, Any]:
    catalog = catalog_service.get_catalog(db, catalog_id)
    new = catalog_service.restore_version(db, catalog, version, auth.principal.user_id)
    return {"restored_to": version, "new_version": new.version if new else catalog.head_version,
            "changed": bool(new), "summary": new.summary if new else {}}


@router.post("/catalogs/{catalog_id}/exports")
@scoped
def export_catalog(catalog_id: str, format: str = "csv", db: Session = Depends(get_db),
                   auth: Auth = Depends(require(P_CATALOG_READ))) -> Response:
    catalog = catalog_service.get_catalog(db, catalog_id)
    data, mime, name = hub.export_catalog(db, catalog, format, auth.principal.can(P_COST))
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return Response(data, media_type=mime, headers={"Content-Disposition": f'attachment; filename="{safe}"',
                                                    "X-Content-Type-Options": "nosniff"})


@router.get("/products/{product_id}/readiness")
@scoped
def readiness(product_id: str, db: Session = Depends(get_db), auth: Auth = Depends(require(P_CATALOG_READ))) -> dict[str, Any]:
    """HUB-16: 'ready for Shopify, 3 issues for Amazon' for one product across every channel."""
    p = catalog_service.get_product(db, product_id)
    catalog = catalog_service.get_catalog(db, p.catalog_id)
    out = []
    for channel in db.execute(select(Channel).where(Channel.role != "source")).scalars():
        overrides = {o.field_path: o.value for o in db.execute(select(ChannelOverride).where(
            ChannelOverride.product_id == p.id, ChannelOverride.channel_id == channel.id)).scalars()}
        issues = transform.validate(transform.build_payload(p, overrides, catalog.default_locale),
                                    get_connector(channel.platform).capabilities())
        out.append({"channel_id": channel.id, "channel": channel.name, "platform": channel.platform,
                    "ready": not transform.has_errors(issues), "issues": issues})
    return {"data": out}


class OverrideIn(Strict):
    value: Any


@router.put("/products/{product_id}/overrides/{channel_id}/{field_path:path}")
@scoped
def put_override(product_id: str, channel_id: str, field_path: str, body: OverrideIn, db: Session = Depends(get_db),
                 auth: Auth = Depends(require(P_CATALOG_WRITE))) -> dict[str, Any]:
    p = catalog_service.get_product(db, product_id)
    channel = _channel(db, channel_id)
    if field_path not in transform.OVERRIDABLE and not transform._VARIANT_PATH.match(field_path):
        raise Problem(422, "validation_failed", "That field cannot be overridden per channel.")
    value = body.value
    if field_path == "title":
        value = clean_text(value, 500) if isinstance(value, str) else None
        if not value:
            raise Problem(422, "validation_failed", "The title override cannot be empty.")
    elif field_path == "description_html":
        value = sanitize_html(value) if isinstance(value, str) else None
    elif transform._VARIANT_PATH.match(field_path) and (not isinstance(value, int) or isinstance(value, bool) or value < 0):
        raise Problem(422, "validation_failed", "Price and stock overrides must be non-negative whole numbers.")
    row = db.execute(select(ChannelOverride).where(
        ChannelOverride.product_id == p.id, ChannelOverride.channel_id == channel.id,
        ChannelOverride.field_path == field_path)).scalar_one_or_none()
    if row is None:
        row = ChannelOverride(tenant_id=p.tenant_id, product_id=p.id, channel_id=channel.id, field_path=field_path)
        db.add(row)
    row.value, row.author = value, auth.principal.user_id
    audit.record(db, p.tenant_id, "override.set", f"product:{p.id}", {"channel_id": channel.id, "field": field_path})
    return {"field_path": field_path, "value": value}


@router.delete("/products/{product_id}/overrides/{channel_id}/{field_path:path}")
@scoped
def delete_override(product_id: str, channel_id: str, field_path: str, db: Session = Depends(get_db),
                    auth: Auth = Depends(require(P_CATALOG_WRITE))) -> dict[str, str]:
    row = db.execute(select(ChannelOverride).where(
        ChannelOverride.product_id == product_id, ChannelOverride.channel_id == channel_id,
        ChannelOverride.field_path == field_path)).scalar_one_or_none()
    if row is not None:
        db.delete(row)
    return {"status": "removed"}


# ================================================================ uploads

class FileIn(Strict):
    name: str = Field(min_length=1, max_length=400)
    size: int = Field(gt=0)
    sha256: str = Field(pattern="^[0-9a-fA-F]{64}$")


class UploadIn(Strict):
    catalog_id: str
    files: list[FileIn] = Field(min_length=1, max_length=100)


class MappingIn(Strict):
    mapping: dict[str, Any] | None = None
    template_id: str | None = None
    save_as: str | None = Field(default=None, max_length=120)


def _file_view(f: UploadFile, scans: list[ScanResult] | None = None) -> dict[str, Any]:
    out = {"id": f.id, "name": f.original_name, "size": f.size_bytes, "bytes_received": f.bytes_received,
           "state": f.state, "detected_type": f.detected_type, "import_result": f.import_result,
           "has_mapping": bool(f.mapping),
           "rejected": (f.parse_meta or {}).get("rejected"),
           "media": [{"name": m["name"], "status": m["status"], "reason": m.get("reason", "")}
                     for m in (f.parse_meta or {}).get("media", [])][:500]}
    if scans is not None:
        out["scans"] = [{"engine": s.engine, "verdict": s.verdict, "detail": s.detail} for s in scans]
    return out


def _process_in_background(tenant_id: str, file_id: str) -> None:
    """Scan and release outside the request (PER-01). Safe to repeat: process_file is idempotent."""
    with dbm.tenant_scope(tenant_id), dbm.session_scope() as db:
        row = db.execute(select(UploadFile).where(UploadFile.id == file_id)).scalar_one_or_none()
        if row is not None:
            try:
                hub.process_file(db, row)
            except hub.UploadError:
                db.commit()  # scanner unavailable: the file stays held; state was already saved


@router.post("/uploads", status_code=201)
@scoped
def create_upload(body: UploadIn, request: Request, db: Session = Depends(get_db),
                  auth: Auth = Depends(require(P_UPLOAD))):
    replay = idempotent(request, db, auth, body.model_dump())
    if replay is not None:
        return replay
    tenant = db.get(Tenant, auth.principal.tenant_id)
    upload, files = hub.create_upload(db, tenant.id, tenant.plan, auth.principal.user_id, body.catalog_id,
                                      [f.model_dump() for f in body.files])
    return remember(request, {"id": upload.id, "state": upload.state, "files": [
        {"id": f.id, "name": f.original_name, "size": f.size_bytes,
         "upload_url": f"/v1/uploads/{upload.id}/files/{f.id}"} for f in files]}, 201)


@router.head("/uploads/{upload_id}/files/{file_id}")
@scoped
def upload_offset(upload_id: str, file_id: str, response: Response, db: Session = Depends(get_db),
                  auth: Auth = Depends(require(P_UPLOAD))) -> Response:
    row = hub.get_file(db, upload_id, file_id)
    return Response(status_code=204, headers={"Upload-Offset": str(row.bytes_received),
                                              "Upload-Length": str(row.size_bytes), "Cache-Control": "no-store"})


@router.patch("/uploads/{upload_id}/files/{file_id}")
async def upload_chunk(upload_id: str, file_id: str, request: Request, background: BackgroundTasks,
                       db: Session = Depends(get_db), auth: Auth = Depends(require(P_UPLOAD))) -> Response:
    """One chunk at ``Upload-Offset``. The body is read with a hard cap so a client cannot
    make the server buffer more than one chunk."""
    max_chunk = 16 * 1024 * 1024
    try:
        offset = int(request.headers.get("upload-offset", ""))
    except ValueError:
        raise Problem(400, "bad_request", "Upload-Offset header is required.") from None
    declared = request.headers.get("content-length")
    if declared and int(declared) > max_chunk:
        raise Problem(413, "payload_too_large", "Send chunks of at most 16 MB.")
    data = bytearray()
    async for part in request.stream():
        data.extend(part)
        if len(data) > max_chunk:
            raise Problem(413, "payload_too_large", "Send chunks of at most 16 MB.")
    checksum = request.headers.get("upload-checksum", "")
    checksum = checksum.split(" ", 1)[1].strip() if checksum.lower().startswith("sha256 ") else None

    def work() -> tuple[int, bool]:
        db.commit()
        with dbm.tenant_scope(auth.principal.tenant_id):
            row = hub.get_file(db, upload_id, file_id)
            done = hub.receive_chunk(db, row, offset, bytes(data), checksum)
            db.commit()
            return row.bytes_received, done

    received, done = await asyncio.to_thread(work)
    if done:
        background.add_task(_process_in_background, auth.principal.tenant_id, file_id)
    return Response(status_code=204, headers={"Upload-Offset": str(received), "Upload-Complete": "1" if done else "0"})


@router.get("/uploads")
@scoped
def list_uploads(db: Session = Depends(get_db), auth: Auth = Depends(require(P_UPLOAD))) -> dict[str, Any]:
    uploads = db.execute(select(Upload).order_by(Upload.created_at.desc()).limit(50)).scalars().all()
    files: dict[str, list[UploadFile]] = {}
    if uploads:
        for f in db.execute(select(UploadFile).where(UploadFile.upload_id.in_([u.id for u in uploads]))).scalars():
            files.setdefault(f.upload_id, []).append(f)
    return {"data": [{"id": u.id, "state": u.state, "catalog_id": u.catalog_id, "created_at": u.created_at.isoformat(),
                      "files": [_file_view(f) for f in files.get(u.id, [])]} for u in uploads]}


@router.get("/uploads/{upload_id}")
@scoped
def get_upload(upload_id: str, db: Session = Depends(get_db), auth: Auth = Depends(require(P_UPLOAD))) -> dict[str, Any]:
    upload = db.execute(select(Upload).where(Upload.id == upload_id)).scalar_one_or_none()
    if upload is None:
        raise Problem(404, "not_found", "Upload not found.")
    files = db.execute(select(UploadFile).where(UploadFile.upload_id == upload.id)).scalars().all()
    scans: dict[str, list[ScanResult]] = {}
    for s in db.execute(select(ScanResult).where(ScanResult.file_id.in_([f.id for f in files]))).scalars():
        scans.setdefault(s.file_id, []).append(s)
    return {"id": upload.id, "state": upload.state, "catalog_id": upload.catalog_id,
            "files": [_file_view(f, scans.get(f.id, [])) for f in files]}


@router.post("/uploads/{upload_id}/files/{file_id}/rescan", status_code=202)
@scoped
def rescan(upload_id: str, file_id: str, background: BackgroundTasks, db: Session = Depends(get_db),
           auth: Auth = Depends(require(P_UPLOAD))) -> dict[str, str]:
    row = hub.get_file(db, upload_id, file_id)
    if row.state not in ("received", "scanning"):
        raise Problem(409, "conflict", "Only a file that is waiting for scanning can be re-queued.")
    background.add_task(_process_in_background, auth.principal.tenant_id, file_id)
    return {"status": "queued"}


@router.get("/uploads/{upload_id}/files/{file_id}/preview")
@scoped
def preview(upload_id: str, file_id: str, db: Session = Depends(get_db), auth: Auth = Depends(require(P_UPLOAD))) -> dict[str, Any]:
    row = hub.get_file(db, upload_id, file_id)
    out = hub.preview_file(db, row)
    out["mapping"] = row.mapping
    out["templates"] = [{"id": t.id, "name": t.name} for t in db.execute(select(MappingTemplate)).scalars()]
    return out


@router.put("/uploads/{upload_id}/files/{file_id}/mapping")
@scoped
def put_mapping(upload_id: str, file_id: str, body: MappingIn, db: Session = Depends(get_db),
                auth: Auth = Depends(require(P_UPLOAD))) -> dict[str, Any]:
    row = hub.get_file(db, upload_id, file_id)
    return {"mapping": hub.set_mapping(db, row, body.mapping, body.template_id, body.save_as)}


@router.post("/uploads/{upload_id}/files/{file_id}/import")
@scoped
def run_import(upload_id: str, file_id: str, db: Session = Depends(get_db), auth: Auth = Depends(require(P_UPLOAD))) -> dict[str, Any]:
    row = hub.get_file(db, upload_id, file_id)
    return hub.run_import(db, row, auth.principal.user_id)


@router.get("/uploads/{upload_id}/files/{file_id}/errors.csv")
@scoped
def error_file(upload_id: str, file_id: str, db: Session = Depends(get_db), auth: Auth = Depends(require(P_UPLOAD))) -> Response:
    row = hub.get_file(db, upload_id, file_id)
    key = f"{row.tenant_id}/errors/{row.id}.csv"
    if not storage.exists("exports", key):
        raise Problem(404, "not_found", "This import had no row errors.")
    return Response(storage.object_path("exports", key).read_bytes(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="import-errors.csv"',
                             "X-Content-Type-Options": "nosniff"})


# ================================================================ channels & credentials

class ChannelIn(Strict):
    platform: str = Field(max_length=32)
    name: str = Field(min_length=1, max_length=200)
    base_url: str | None = Field(default=None, max_length=500)
    role: str = Field(default="destination", pattern="^(source|destination|both)$")


class CredentialIn(Strict):
    kind: str = Field(max_length=24)
    secret: dict[str, str]


def _channel(db: Session, channel_id: str) -> Channel:
    channel = db.execute(select(Channel).where(Channel.id == channel_id)).scalar_one_or_none()
    if channel is None:
        raise Problem(404, "not_found", "Channel not found.")
    return channel


def _channel_view(db: Session, c: Channel) -> dict[str, Any]:
    caps = get_connector(c.platform).capabilities()
    live = db.execute(select(func.count()).select_from(ChannelListing).where(
        ChannelListing.channel_id == c.id, ChannelListing.state == "live")).scalar_one()
    return {"id": c.id, "platform": c.platform, "name": c.name, "role": c.role, "base_url": c.base_url,
            "health": c.health, "paused_reason": c.paused_reason, "connected": bool(c.credential_id) or not caps.credential_kinds,
            "needs_credential": bool(caps.credential_kinds), "live_listings": live,
            "can_import": caps.supports_import and c.role in ("source", "both"),
            "created_at": c.created_at.isoformat()}  # no secret field exists in this schema


@router.get("/platforms")
def list_platforms() -> dict[str, Any]:
    out = []
    for name in platforms():
        connector = get_connector(name)
        caps = connector.capabilities()
        out.append({"platform": name, "label": connector.label, "credential_kinds": list(caps.credential_kinds),
                    "credential_fields": list(caps.credential_fields), "needs_base_url": caps.needs_base_url,
                    "limits": {"max_title_length": caps.max_title_length, "max_variants": caps.max_variants,
                               "max_options": caps.max_options, "requires_image": caps.requires_image},
                    "can_import": caps.supports_import, "beta": name not in ("mock", "csv_feed")})
    return {"data": out}


@router.get("/channels")
@scoped
def list_channels(db: Session = Depends(get_db), auth: Auth = Depends(require(P_CHANNELS_READ))) -> dict[str, Any]:
    rows = db.execute(select(Channel).order_by(Channel.created_at)).scalars().all()
    return {"data": [_channel_view(db, c) for c in rows]}


@router.post("/channels", status_code=201)
@scoped
def create_channel(body: ChannelIn, db: Session = Depends(get_db), auth: Auth = Depends(require(P_CHANNELS_ADMIN))) -> dict[str, Any]:
    if body.platform not in platforms():
        raise Problem(422, "validation_failed", "Unknown platform.")
    if db.execute(select(func.count()).select_from(Channel)).scalar_one() >= 50:
        raise Problem(429, "quota_exceeded", "You have reached the limit of 50 channels.")
    caps = get_connector(body.platform).capabilities()
    try:
        base_url = broker.validate_channel_url(body.base_url, caps.needs_base_url)
    except broker.VaultError as exc:
        raise Problem(exc.status, exc.code, exc.detail) from exc
    role = body.role
    if role != "destination" and not caps.supports_import:
        raise Problem(422, "validation_failed", "Products cannot be imported from this kind of channel.")
    channel = Channel(tenant_id=auth.principal.tenant_id, platform=body.platform, name=clean_text(body.name, 200),
                      base_url=base_url, created_by=auth.principal.user_id, role=role,
                      # A sandbox store used as a source comes pre-filled with a messy demo catalog.
                      config={"demo_products": 1200} if body.platform == "mock" and role != "destination" else {},
                      health="unknown" if caps.credential_kinds else "ok")
    db.add(channel)
    db.flush()
    audit.record(db, channel.tenant_id, "channel.created", f"channel:{channel.id}", {"platform": body.platform})
    return _channel_view(db, channel)


@router.put("/channels/{channel_id}/credential")
@scoped
def put_credential(channel_id: str, body: CredentialIn, request: Request, db: Session = Depends(get_db),
                   auth: Auth = Depends(require(P_CHANNELS_ADMIN))) -> dict[str, Any]:
    """Write-only. The secret is accepted here and nowhere else, and never comes back (CRED-01)."""
    auth.principal.require_step_up()
    rate_limit(request, "credential", 20, 600, key=auth.principal.user_id)
    channel = _channel(db, channel_id)
    try:
        cred = broker.store_credential(db, channel, body.kind, body.secret, auth.principal.user_id)
    except broker.VaultError as exc:
        raise Problem(exc.status, exc.code, exc.detail) from exc
    return broker.metadata(cred)


@router.get("/channels/{channel_id}/credential")
@scoped
def get_credential(channel_id: str, db: Session = Depends(get_db), auth: Auth = Depends(require(P_CHANNELS_ADMIN))) -> dict[str, Any]:
    channel = _channel(db, channel_id)
    cred = db.execute(select(Credential).where(Credential.id == channel.credential_id)).scalar_one_or_none() \
        if channel.credential_id else None
    if cred is None:
        raise Problem(404, "not_found", "This channel has no credential.")
    return broker.metadata(cred)


@router.get("/channels/{channel_id}/credential/usage")
@scoped
def credential_usage(channel_id: str, db: Session = Depends(get_db), auth: Auth = Depends(require(P_AUDIT_READ))) -> dict[str, Any]:
    channel = _channel(db, channel_id)
    rows = db.execute(select(CredentialUsage).where(CredentialUsage.credential_id == (channel.credential_id or ""))
                      .order_by(CredentialUsage.at.desc()).limit(200)).scalars().all()
    return {"data": [{"at": u.at.isoformat(), "task_id": u.task_id, "handler": u.handler_id,
                      "operation": u.operation, "result": u.result} for u in rows]}


@router.delete("/channels/{channel_id}")
@scoped
def disconnect_channel(channel_id: str, db: Session = Depends(get_db), auth: Auth = Depends(require(P_CHANNELS_ADMIN))) -> dict[str, str]:
    auth.principal.require_step_up()
    broker.disconnect(db, _channel(db, channel_id))
    return {"status": "disconnected"}


@router.post("/channels/{channel_id}/pause")
@scoped
def pause_channel(channel_id: str, db: Session = Depends(get_db), auth: Auth = Depends(require(P_PUBLISH))) -> dict[str, Any]:
    channel = _channel(db, channel_id)
    channel.health, channel.paused_reason = "paused", "paused by a user"
    audit.record(db, channel.tenant_id, "channel.paused", f"channel:{channel.id}", {})
    return _channel_view(db, channel)


@router.post("/channels/{channel_id}/resume")
@scoped
def resume_channel(channel_id: str, db: Session = Depends(get_db), auth: Auth = Depends(require(P_PUBLISH))) -> dict[str, Any]:
    channel = _channel(db, channel_id)
    caps = get_connector(channel.platform).capabilities()
    if caps.credential_kinds and not channel.credential_id:
        raise Problem(409, "channel_not_connected", "Connect the channel before resuming it.")
    if channel.health == "reauth_required":
        raise Problem(409, "reauth_required", "Reconnect the channel: the store rejected the saved access.")
    channel.health, channel.paused_reason = "ok", None
    audit.record(db, channel.tenant_id, "channel.resumed", f"channel:{channel.id}", {})
    return _channel_view(db, channel)


@router.get("/channels/{channel_id}/listings")
@scoped
def listings(channel_id: str, limit: int = 100, state: str | None = None, db: Session = Depends(get_db),
             auth: Auth = Depends(require(P_CHANNELS_READ))) -> dict[str, Any]:
    channel = _channel(db, channel_id)
    stmt = select(ChannelListing, Product.title, Product.external_key).join(Product, Product.id == ChannelListing.product_id) \
        .where(ChannelListing.channel_id == channel.id).limit(_page(limit))
    if state:
        stmt = stmt.where(ChannelListing.state == state)
    return {"data": [{"product_id": listing.product_id, "title": next(iter(title.values()), ""), "key": key,
                      "state": listing.state, "external_id": listing.destination_external_id,
                      "last_error": listing.last_error,
                      "last_synced_at": listing.last_synced_at.isoformat() if listing.last_synced_at else None}
                     for listing, title, key in db.execute(stmt).all()]}


@router.get("/channels/{channel_id}/feed.csv")
@scoped
def feed(channel_id: str, db: Session = Depends(get_db), auth: Auth = Depends(require(P_CHANNELS_READ))) -> Response:
    channel = _channel(db, channel_id)
    if channel.platform != "csv_feed":
        raise Problem(404, "not_found", "This channel is not a feed.")
    return Response(csv_feed.render_feed(channel.tenant_id, channel.id), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="feed.csv"', "X-Content-Type-Options": "nosniff"})


# ================================================================ jobs

class JobIn(Strict):
    kind: str = Field(pattern="^(publish|import)$")
    catalog_id: str
    channel_ids: list[str] = Field(min_length=1, max_length=25)
    options: dict[str, Any] = Field(default_factory=dict)


class ApproveIn(Strict):
    exclude_item_ids: list[str] = Field(default_factory=list, max_length=1000)


class RetryIn(Strict):
    item_ids: list[str] | None = Field(default=None, max_length=1000)


class RollbackIn(Strict):
    channel_ids: list[str] | None = Field(default=None, max_length=25)


def _job_view(db: Session, job: Job) -> dict[str, Any]:
    counts = job_service.item_counts(db, job.id)
    total: dict[str, int] = {}
    for per in counts.values():
        for k, v in per.items():
            total[k] = total.get(k, 0) + v
    return {"id": job.id, "kind": job.kind, "state": job.state, "catalog_id": job.catalog_id,
            "channel_ids": job.channel_ids, "options": {k: v for k, v in job.options.items() if k != "paused_from"},
            "preview": job.preview, "counts": total, "counts_by_channel": counts,
            "needs_second_approver": job.needs_second_approver, "approved_by": job.approved_by,
            "parent_job_id": job.parent_job_id, "started_by": job.started_by,
            "created_at": job.created_at.isoformat(),
            "finished_at": job.finished_at.isoformat() if job.finished_at else None}


def _job_call(fn, *args):
    try:
        return fn(*args)
    except job_service.JobError as exc:
        raise Problem(exc.status, exc.code, exc.detail, exc.extra) from exc


@router.post("/jobs", status_code=202)
@scoped
def create_job(body: JobIn, request: Request, db: Session = Depends(get_db), auth: Auth = Depends(require(P_JOBS_WRITE))):
    replay = idempotent(request, db, auth, body.model_dump())
    if replay is not None:
        return replay
    allowed = {"existing", "sandbox", "product_ids"}
    if set(body.options) - allowed:
        raise Problem(422, "validation_failed", f"Unknown options: {', '.join(sorted(set(body.options) - allowed))}.")
    if body.kind == "import":
        if len(body.channel_ids) != 1 or body.options:
            raise Problem(422, "validation_failed", "An import reads from exactly one store.")
        job = _job_call(importer.create_import_job, db, auth.principal, body.channel_ids[0], body.catalog_id)
    else:
        job = _job_call(job_service.create_publish_job, db, auth.principal, body.catalog_id, body.channel_ids, body.options)
    return remember(request, _job_view(db, job), 202)


@router.get("/jobs")
@scoped
def list_jobs(limit: int = 50, db: Session = Depends(get_db), auth: Auth = Depends(require(P_JOBS_READ))) -> dict[str, Any]:
    rows = db.execute(select(Job).order_by(Job.created_at.desc()).limit(_page(limit))).scalars().all()
    return {"data": [_job_view(db, j) for j in rows]}


@router.get("/jobs/{job_id}")
@scoped
def get_job(job_id: str, db: Session = Depends(get_db), auth: Auth = Depends(require(P_JOBS_READ))) -> dict[str, Any]:
    return _job_view(db, job_service.get_job(db, job_id))


@router.get("/jobs/{job_id}/items")
@scoped
def job_items(job_id: str, limit: int = 100, cursor: str | None = None, state: str | None = None,
              channel_id: str | None = None, risk: str | None = None, db: Session = Depends(get_db),
              auth: Auth = Depends(require(P_JOBS_READ))) -> dict[str, Any]:
    job = job_service.get_job(db, job_id)
    stmt = select(JobItem, Product.title, Product.external_key).outerjoin(Product, Product.id == JobItem.product_id) \
        .where(JobItem.job_id == job.id).order_by(JobItem.id).limit(_page(limit) + 1)
    if cursor:
        stmt = stmt.where(JobItem.id > cursor[:36])
    if state:
        stmt = stmt.where(JobItem.state.in_([s for s in state.split(",")][:10]))
    if channel_id:
        stmt = stmt.where(JobItem.channel_id == channel_id)
    if risk in ("low", "medium", "high"):
        stmt = stmt.where(JobItem.risk == risk)
    rows = db.execute(stmt).all()
    more = len(rows) > _page(limit)
    rows = rows[:_page(limit)]
    return {"data": [{
        "id": item.id, "product_id": item.product_id, "title": next(iter((title or {}).values()), ""), "key": key,
        "channel_id": item.channel_id, "operation": item.operation, "state": item.state, "risk": item.risk,
        "attempt": item.attempt, "diff": item.diff[:50], "issues": item.issues, "last_error": item.last_error,
        "destination_ref": item.destination_ref,
        # HA-28: an item being taken over or waiting on a destination is "paused", not "failed".
        "waiting": item.state in ("RETRY_WAIT", "QUEUED") and item.attempt > 0,
    } for item, title, key in rows], "next_cursor": rows[-1][0].id if more else None}


@router.post("/jobs/{job_id}/approve")
@scoped
def approve_job(job_id: str, body: ApproveIn, db: Session = Depends(get_db), auth: Auth = Depends(require(P_PUBLISH))) -> dict[str, Any]:
    job = job_service.get_job(db, job_id)
    _job_call(job_service.approve, db, auth.principal, job, body.exclude_item_ids)
    return _job_view(db, job)


@router.post("/jobs/{job_id}/control/{action}")
@scoped
def control_job(job_id: str, action: str, db: Session = Depends(get_db), auth: Auth = Depends(require(P_PUBLISH))) -> dict[str, Any]:
    job = job_service.get_job(db, job_id)
    actions = {"pause": job_service.pause, "resume": job_service.resume, "cancel": job_service.cancel}
    if action not in actions:
        raise Problem(404, "not_found", "Unknown action.")
    if action == "resume" and db.get(Tenant, job.tenant_id).frozen:
        raise Problem(423, "tenant_frozen", "Publishing is frozen for this organization.")
    _job_call(actions[action], db, job)
    return _job_view(db, job)


@router.post("/jobs/{job_id}/items/retry")
@scoped
def retry_items(job_id: str, body: RetryIn, db: Session = Depends(get_db), auth: Auth = Depends(require(P_PUBLISH))) -> dict[str, Any]:
    job = job_service.get_job(db, job_id)
    return {"requeued": _job_call(job_service.retry_failed, db, job, body.item_ids), "job": _job_view(db, job)}


@router.post("/jobs/{job_id}/rollbacks", status_code=202)
@scoped
def rollback_job(job_id: str, body: RollbackIn, db: Session = Depends(get_db), auth: Auth = Depends(require(P_PUBLISH))) -> dict[str, Any]:
    job = job_service.get_job(db, job_id)
    rollback = _job_call(job_service.create_rollback_job, db, auth.principal, job, body.channel_ids)
    return _job_view(db, rollback)


@router.get("/events/stream")
async def stream(request: Request, job_id: str, auth: Auth = Depends(require(P_JOBS_READ))) -> StreamingResponse:
    """Server-sent events with job progress (FR-36). Authorization was checked for this
    subscription; the job is re-read under the caller's tenant on every tick, and any
    replica can serve a reconnect because all state comes from the database."""
    tenant_id = auth.principal.tenant_id

    def snapshot() -> dict[str, Any] | None:
        with dbm.tenant_scope(tenant_id), dbm.session_scope() as db:
            job = db.execute(select(Job).where(Job.id == job_id)).scalar_one_or_none()
            return _job_view(db, job) if job else None

    async def events():
        last = None
        for tick in range(3600):
            if await request.is_disconnected():
                return
            view = await asyncio.to_thread(snapshot)
            if view is None:
                yield "event: error\ndata: {\"code\":\"not_found\"}\n\n"
                return
            payload = json.dumps({"job_id": view["id"], "state": view["state"], "counts": view["counts"],
                                  "counts_by_channel": view["counts_by_channel"]}, sort_keys=True)
            if payload != last:
                last = payload
                yield f"id: {tick}\nevent: job.progress\ndata: {payload}\n\n"
            elif tick % 15 == 0:
                yield ": keep-alive\n\n"
            if view["state"] in ("completed", "partially_completed", "failed", "cancelled", "rolled_back"):
                return
            await asyncio.sleep(1.0)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


# ================================================================ AI & review

class EnhanceIn(Strict):
    tasks: list[str] | None = Field(default=None, max_length=10)
    product_ids: list[str] | None = Field(default=None, max_length=1000)
    preferences: dict[str, Any] | None = None


class DecideIn(Strict):
    proposal_ids: list[str] = Field(min_length=1, max_length=1000)
    decision: str = Field(pattern="^(approve|reject|edit)$")
    value: Any = None
    reason: str | None = Field(default=None, max_length=400)


def _review_call(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ai_service.ReviewError as exc:
        raise Problem(exc.status, exc.code, exc.detail) from exc


@router.post("/catalogs/{catalog_id}/enhance")
@scoped
def enhance(catalog_id: str, body: EnhanceIn, request: Request, db: Session = Depends(get_db),
            auth: Auth = Depends(require(P_CATALOG_WRITE))) -> dict[str, Any]:
    rate_limit(request, "enhance", 30, 600, key=auth.principal.tenant_id)
    catalog = catalog_service.get_catalog(db, catalog_id)
    return _review_call(ai_service.enhance, db, catalog, auth.principal.user_id, body.tasks, body.product_ids,
                        preferences=body.preferences)


@router.get("/proposals")
@scoped
def list_proposals(status: str = "pending", task: str | None = None, risk: str | None = None,
                   min_confidence: float | None = None, limit: int = 100, cursor: str | None = None,
                   db: Session = Depends(get_db), auth: Auth = Depends(require(P_CATALOG_READ))) -> dict[str, Any]:
    stmt = select(AIProposal, Product.external_key).join(Product, Product.id == AIProposal.product_id) \
        .where(AIProposal.status == status).order_by(AIProposal.id).limit(_page(limit) + 1)
    if task:
        stmt = stmt.where(AIProposal.task == task)
    if risk:
        stmt = stmt.where(AIProposal.risk == risk)
    if min_confidence is not None:
        stmt = stmt.where(AIProposal.confidence >= min_confidence)
    if cursor:
        stmt = stmt.where(AIProposal.id > cursor[:36])
    rows = db.execute(stmt).all()
    more = len(rows) > _page(limit)
    rows = rows[:_page(limit)]
    return {"data": [{**ai_service.proposal_view(p), "product_key": key} for p, key in rows],
            "next_cursor": rows[-1][0].id if more else None, "pending": ai_service.pending_counts(db)}


@router.post("/proposals:decide")
@scoped
def decide(body: DecideIn, db: Session = Depends(get_db), auth: Auth = Depends(require(P_REVIEW))) -> dict[str, Any]:
    return _review_call(ai_service.decide, db, auth.principal.tenant_id, auth.principal.user_id, body.proposal_ids,
                        body.decision, body.value, body.reason, auth.principal.step_up_fresh)


@router.get("/ai/status")
def ai_status() -> dict[str, Any]:
    cal = confidence.load()
    from ..ai import gateway
    from ..config import get_settings

    return {"provider": get_settings().ai_provider, "paused": gateway.killed(), "calibration": cal.meta,
            "auto_approve_thresholds": cal.tau}


@router.get("/taxonomy")
def taxonomy_search(q: str = "") -> dict[str, Any]:
    if q:
        return {"data": [{"id": n.id, "path": n.path, "score": round(s, 3)} for n, s in taxonomy.retrieve(q[:200], 10)]}
    return {"data": [{"id": n.id, "path": n.path} for n in taxonomy.nodes()]}


_ = identity  # imported for route modules that share this namespace
