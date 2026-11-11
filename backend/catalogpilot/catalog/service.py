"""Hosted catalog: upserts, versions with diffs, restore (HUB-09/10/12, FR-16).

Every mutation goes through ``apply_changes`` so that it always produces a catalog version
with a per-product before/after diff. Products are never hard-deleted: "delete" archives,
and restoring an older version archives products that did not exist yet (principle P3).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ids import utcnow
from ..models import Catalog, CatalogVersion, Product
from ..security import audit
from .cleaning import slugify

STATE_FIELDS = (
    "title", "description_html", "brand", "product_type", "source_category", "mapped_taxonomy_id",
    "tags", "status", "handle", "seo", "attributes", "media", "variants", "platform_extras",
)


class VersionConflict(Exception):
    """Optimistic concurrency failure: the product changed since the caller read it."""


class NotFound(Exception):
    pass


def product_state(p: Product) -> dict[str, Any]:
    return {f: json.loads(json.dumps(getattr(p, f))) for f in STATE_FIELDS}


def content_hash(state: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(state, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def product_view(p: Product, include_cost: bool) -> dict[str, Any]:
    """API representation. Cost price is a restricted property (SEC-AUZ-05)."""
    state = product_state(p)
    if not include_cost:
        for v in state["variants"]:
            v.pop("cost_minor", None)
    return {
        "id": p.id, "catalog_id": p.catalog_id, "version": p.version, "external_key": p.external_key,
        **state, "provenance": p.provenance, "updated_at": p.updated_at.isoformat(),
    }


@dataclass
class Change:
    external_key: str
    state: dict[str, Any] | None  # None = archive
    origin: str = "seller_edit"  # upload | import | rule | ai | seller_edit | restore
    ref: str | None = None
    expected_version: int | None = None
    partial: bool = False  # True = only the fields present in ``state`` are changed
    source_ref: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChangeResult:
    external_key: str
    product_id: str | None
    outcome: str  # created | updated | unchanged | archived | error
    error: str | None = None


def _default_state() -> dict[str, Any]:
    return {
        "title": {}, "description_html": {}, "brand": None, "product_type": None, "source_category": [],
        "mapped_taxonomy_id": None, "tags": [], "status": "draft", "handle": None, "seo": {},
        "attributes": [], "media": [], "variants": [], "platform_extras": {},
    }


def _unique_handle(db: Session, catalog_id: str, wanted: str, product_id: str | None, taken: set[str]) -> str:
    base = wanted or "product"
    candidate, n = base, 1
    while True:
        clash = candidate in taken or db.execute(
            select(Product.id).where(
                Product.catalog_id == catalog_id, Product.handle == candidate, Product.id != (product_id or "")
            )
        ).first()
        if not clash:
            taken.add(candidate)
            return candidate
        n += 1
        candidate = f"{base}-{n}"


def apply_changes(
    db: Session,
    catalog: Catalog,
    changes: list[Change],
    source: str,
    user_id: str | None,
) -> tuple[CatalogVersion | None, list[ChangeResult]]:
    results: list[ChangeResult] = []
    diff: list[dict[str, Any]] = []
    counts = {"created": 0, "updated": 0, "unchanged": 0, "archived": 0, "error": 0}
    keys = [c.external_key for c in changes]
    existing: dict[str, Product] = {}
    for start in range(0, len(keys), 500):
        chunk = keys[start:start + 500]
        for p in db.execute(
            select(Product).where(Product.catalog_id == catalog.id, Product.external_key.in_(chunk))
        ).scalars():
            existing[p.external_key] = p
    taken_handles: set[str] = set()
    now = utcnow()

    for change in changes:
        p = existing.get(change.external_key)
        was_new = p is None
        savepoint = db.begin_nested()  # a failing record rolls back alone
        try:
            if change.expected_version is not None and p is not None and p.version != change.expected_version:
                raise VersionConflict(f"expected version {change.expected_version}, found {p.version}")
            if change.state is None:
                if p is None or p.status == "archived":
                    results.append(ChangeResult(change.external_key, p.id if p else None, "unchanged"))
                    counts["unchanged"] += 1
                    savepoint.commit()
                    continue
                before = product_state(p)
                p.status = "archived"
                after = product_state(p)
                outcome = "archived"
            else:
                before = product_state(p) if p else None
                base = before if (change.partial and before) else _default_state()
                after = {**base, **{k: v for k, v in change.state.items() if k in STATE_FIELDS}}
                if not after.get("handle"):
                    title = next(iter(after["title"].values()), "") if after["title"] else ""
                    after["handle"] = slugify(title) or slugify(change.external_key)
                if p is None or after["handle"] != before["handle"]:
                    after["handle"] = _unique_handle(db, catalog.id, after["handle"], p.id if p else None, taken_handles)
                if before is not None and content_hash(after) == content_hash(before):
                    results.append(ChangeResult(change.external_key, p.id, "unchanged"))
                    counts["unchanged"] += 1
                    savepoint.commit()
                    continue
                if p is None:
                    p = Product(
                        tenant_id=catalog.tenant_id, catalog_id=catalog.id, external_key=change.external_key,
                        source_ref=change.source_ref, original=after, version=0, provenance={},
                    )
                    db.add(p)
                    existing[change.external_key] = p
                    outcome = "created"
                else:
                    outcome = "updated"
                changed = [f for f in STATE_FIELDS if before is None or after[f] != before[f]]
                for f in changed:
                    setattr(p, f, after[f])
                prov = dict(p.provenance or {})
                stamp = {"origin": change.origin, "ref": change.ref, "at": now.isoformat()}
                for f in changed:
                    prov[f] = stamp
                p.provenance = prov
            p.version = (p.version or 0) + 1
            p.content_hash = content_hash(after)
            p.updated_at = now
            db.flush()
            savepoint.commit()
            diff.append({
                "product_id": p.id, "external_key": p.external_key, "op": outcome,
                "before": before, "after": after,
            })
            counts[outcome] += 1
            results.append(ChangeResult(change.external_key, p.id, outcome))
        except VersionConflict:
            savepoint.rollback()
            raise
        except Exception as exc:  # one bad record never aborts the batch (FR-37)
            savepoint.rollback()
            if was_new:
                existing.pop(change.external_key, None)
            counts["error"] += 1
            results.append(ChangeResult(change.external_key, None if was_new else p.id, "error", str(exc)[:300]))

    if not diff:
        return None, results
    catalog.head_version += 1
    version = CatalogVersion(
        catalog_id=catalog.id, version=catalog.head_version, tenant_id=catalog.tenant_id,
        parent=catalog.head_version - 1 or None, source=source, summary=counts, diff=diff, created_by=user_id,
    )
    db.add(version)
    audit.record(db, catalog.tenant_id, f"catalog.{source}", f"catalog:{catalog.id}",
                 {"version": version.version, **counts})
    db.flush()
    return version, results


def restore_version(db: Session, catalog: Catalog, target: int, user_id: str | None) -> CatalogVersion | None:
    """Rebuilds the catalog as it was at ``target`` by unwinding later diffs, as a NEW version."""
    if target < 0 or target > catalog.head_version:
        raise NotFound("version does not exist")
    later = db.execute(
        select(CatalogVersion)
        .where(CatalogVersion.catalog_id == catalog.id, CatalogVersion.version > target)
        .order_by(CatalogVersion.version.desc())
    ).scalars().all()
    wanted: dict[str, dict[str, Any] | None] = {}
    for version in later:  # newest first: the oldest 'before' wins
        for entry in version.diff:
            wanted[entry["external_key"]] = entry["before"]
    changes = [Change(key, state, origin="restore", ref=f"version:{target}") for key, state in wanted.items()]
    version, _ = apply_changes(db, catalog, changes, "restore", user_id)
    return version


def get_catalog(db: Session, catalog_id: str) -> Catalog:
    catalog = db.get(Catalog, catalog_id)
    # db.get can serve from the identity map; re-check the tenant explicitly.
    from ..db import current_tenant

    if catalog is None or catalog.tenant_id != current_tenant():
        raise NotFound("catalog not found")
    return catalog


def get_product(db: Session, product_id: str) -> Product:
    p = db.execute(select(Product).where(Product.id == product_id)).scalar_one_or_none()
    if p is None:
        raise NotFound("product not found")
    return p
