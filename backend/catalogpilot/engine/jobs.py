"""Job lifecycle: preview (dry run), approval, control, retry and rollback.

A job never writes to a destination until it is approved; the payload each item will send
is frozen at preview time, so what was reviewed is exactly what is executed (FR-33, FR-35).
"""

from __future__ import annotations

import math
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..catalog import service as catalog_service
from ..connectors import get_connector
from ..ids import utcnow
from ..models import (
    Catalog,
    Channel,
    ChannelListing,
    ChannelOverride,
    Job,
    JobItem,
    Membership,
    OrgPolicy,
    OutboxEvent,
    Product,
    ProductSnapshot,
    Tenant,
)
from ..security import audit
from ..security.authz import Forbidden, Principal
from . import transform

ACTIVE_ITEM_STATES = ("APPROVED", "QUEUED", "PUSHING", "RETRY_WAIT")
ACTIVE_JOB_STATES = ("running", "paused", "rolling_back")
SANDBOX_SIZE = 10


class JobError(Exception):
    def __init__(self, code: str, detail: str, status: int = 409, extra: dict[str, Any] | None = None):
        self.code, self.detail, self.status, self.extra = code, detail, status, extra or {}


def emit(db: Session, tenant_id: str, topic: str, payload: dict[str, Any]) -> None:
    """Transactional outbox: the event exists if and only if the state change commits (REL-08)."""
    db.add(OutboxEvent(tenant_id=tenant_id, topic=topic, payload=payload))


def get_job(db: Session, job_id: str) -> Job:
    job = db.execute(select(Job).where(Job.id == job_id)).scalar_one_or_none()
    if job is None:
        raise catalog_service.NotFound("job not found")
    return job


def item_counts(db: Session, job_id: str) -> dict[str, dict[str, int]]:
    rows = db.execute(
        select(JobItem.channel_id, JobItem.state, func.count()).where(JobItem.job_id == job_id)
        .group_by(JobItem.channel_id, JobItem.state)
    ).all()
    out: dict[str, dict[str, int]] = {}
    for channel_id, state, n in rows:
        out.setdefault(channel_id or "-", {})[state] = n
    return out


def _channels(db: Session, channel_ids: list[str]) -> list[Channel]:
    if not channel_ids or len(set(channel_ids)) != len(channel_ids) or len(channel_ids) > 25:
        raise JobError("validation_failed", "Choose between 1 and 25 distinct channels.", 422)
    channels = db.execute(select(Channel).where(Channel.id.in_(channel_ids))).scalars().all()
    if len(channels) != len(channel_ids):
        raise catalog_service.NotFound("channel not found")
    for ch in channels:
        if ch.role == "source":
            raise JobError("validation_failed", f"'{ch.name}' is connected as a source only.", 422)
        if not ch.credential_id and get_connector(ch.platform).capabilities().credential_kinds:
            raise JobError("channel_not_connected", f"'{ch.name}' has no credential yet. Connect it first.")
    return channels


def create_publish_job(db: Session, principal: Principal, catalog_id: str, channel_ids: list[str],
                       options: dict[str, Any] | None = None) -> Job:
    """Builds the dry run: one item per product per channel with its frozen payload, the
    field-level diff against what is live, validation issues and a risk class."""
    options = options or {}
    existing_mode = options.get("existing", "update")
    if existing_mode not in ("update", "skip"):
        raise JobError("validation_failed", "options.existing must be 'update' or 'skip'.", 422)
    catalog: Catalog = catalog_service.get_catalog(db, catalog_id)
    channels = _channels(db, channel_ids)
    sandbox = bool(options.get("sandbox"))
    job = Job(
        tenant_id=principal.tenant_id, kind="publish", state="previewing", catalog_id=catalog.id,
        channel_ids=channel_ids, options={"existing": existing_mode, "sandbox": sandbox,
                                          "catalog_version": catalog.head_version},
        started_by=principal.user_id,
    )
    db.add(job)
    db.flush()

    stmt = select(Product).where(Product.catalog_id == catalog.id, Product.status == "active").order_by(Product.id)
    product_ids = options.get("product_ids")
    if product_ids:
        stmt = stmt.where(Product.id.in_(list(product_ids)[:1000]))
    if sandbox:
        stmt = stmt.limit(SANDBOX_SIZE)
    products = db.execute(stmt).scalars().all()
    if not products:
        raise JobError("nothing_to_publish", "No active products match. Set products to 'active' first.", 422)

    summary: dict[str, Any] = {"channels": {}, "high_risk": 0, "blocked": 0, "warnings": 0}
    for channel in channels:
        caps = get_connector(channel.platform).capabilities()
        stats = {"name": channel.name, "platform": channel.platform, "create": 0, "update": 0, "unchanged": 0,
                 "blocked": 0, "skipped_existing": 0}
        listings = {
            row.product_id: row for row in db.execute(
                select(ChannelListing).where(ChannelListing.channel_id == channel.id)
            ).scalars()
        }
        overrides: dict[str, dict[str, Any]] = {}
        for ov in db.execute(select(ChannelOverride).where(ChannelOverride.channel_id == channel.id)).scalars():
            overrides.setdefault(ov.product_id, {})[ov.field_path] = ov.value
        for product in products:
            payload = transform.build_payload(product, overrides.get(product.id, {}), catalog.default_locale)
            digest = transform.payload_hash(payload)
            listing = listings.get(product.id)
            live = listing is not None and listing.state in ("live", "pending") and listing.destination_external_id
            issues = transform.validate(payload, caps)
            changes = transform.diff(listing.last_published_payload if live else None, payload)
            item = JobItem(
                tenant_id=job.tenant_id, job_id=job.id, product_id=product.id, channel_id=channel.id,
                operation="upsert", payload=payload, payload_hash=digest, issues=issues, diff=changes,
                risk=transform.classify_risk("upsert", changes), state="VALIDATED",
            )
            if transform.has_errors(issues):
                item.state = "FAILED"
                item.last_error = {"code": "validation", "message": "Blocked by validation errors.",
                                   "suggestion": next(i["suggestion"] for i in issues if i["severity"] == "error")}
                stats["blocked"] += 1
            elif live and listing.last_published_hash == digest:
                item.state = "SKIPPED"  # unchanged since the last publish: no API call (PER-06)
                stats["unchanged"] += 1
            elif live and existing_mode == "skip":
                item.state = "SKIPPED"
                stats["skipped_existing"] += 1
            else:
                stats["update" if live else "create"] += 1
                if item.risk == "high":
                    summary["high_risk"] += 1
            summary["warnings"] += sum(1 for i in issues if i["severity"] == "warning")
            db.add(item)
        summary["blocked"] += stats["blocked"]
        writes = stats["create"] + stats["update"]
        stats["estimated_api_calls"] = writes
        stats["estimated_seconds"] = math.ceil(writes / max(caps.requests_per_second, 0.1))
        summary["channels"][channel.id] = stats
    summary["writes"] = sum(c["create"] + c["update"] for c in summary["channels"].values())
    summary["estimated_seconds"] = max((c["estimated_seconds"] for c in summary["channels"].values()), default=0)
    job.preview = summary
    job.state = "awaiting_approval"
    audit.record(db, job.tenant_id, "job.previewed", f"job:{job.id}", {"writes": summary["writes"],
                                                                        "blocked": summary["blocked"]})
    emit(db, job.tenant_id, "job.preview_ready", {"job_id": job.id})
    db.flush()
    return job


def _four_eyes_needed(db: Session, job: Job) -> bool:
    """PUB-14: bulk deletes/delists above the threshold need a second approver."""
    destructive = db.execute(
        select(func.count()).select_from(JobItem).where(
            JobItem.job_id == job.id, JobItem.operation.in_(("delist", "delete")), JobItem.state == "VALIDATED")
    ).scalar_one()
    if not destructive:
        return False
    # A one-person organization has nobody to give a second approval; the rule would only
    # lock them out of their own rollback. Step-up authentication still applies.
    approvers = db.execute(select(func.count()).select_from(Membership).where(
        Membership.role.in_(("owner", "admin", "publisher")))).scalar_one()
    if approvers < 2:
        return False
    policy =db.execute(select(OrgPolicy)).scalar_one_or_none()
    threshold = policy.bulk_four_eyes_threshold if policy else 50
    percent = policy.bulk_four_eyes_percent if policy else 10
    if destructive >= threshold:
        return True
    for channel_id in job.channel_ids:
        live = db.execute(select(func.count()).select_from(ChannelListing).where(
            ChannelListing.channel_id == channel_id, ChannelListing.state == "live")).scalar_one()
        on_channel = db.execute(select(func.count()).select_from(JobItem).where(
            JobItem.job_id == job.id, JobItem.channel_id == channel_id,
            JobItem.operation.in_(("delist", "delete")), JobItem.state == "VALIDATED")).scalar_one()
        if live and on_channel * 100 >= live * percent and on_channel > 1:
            return True
    return False


def approve(db: Session, principal: Principal, job: Job, exclude_item_ids: list[str] | None = None) -> Job:
    if job.state != "awaiting_approval":
        raise JobError("conflict", f"This job is {job.state.replace('_', ' ')} and cannot be approved.")
    tenant = db.get(Tenant, job.tenant_id)
    if tenant.frozen:
        raise JobError("tenant_frozen", "Publishing is frozen for this organization.", 423)
    if exclude_item_ids:
        db.execute(update(JobItem).where(JobItem.job_id == job.id, JobItem.id.in_(exclude_item_ids[:1000]),
                                         JobItem.state == "VALIDATED").values(state="SKIPPED"))
    high = db.execute(select(func.count()).select_from(JobItem).where(
        JobItem.job_id == job.id, JobItem.state == "VALIDATED", JobItem.risk == "high")).scalar_one()
    if high:
        principal.require_step_up()  # SEC-AUZ-09
    # FR-40: one active write job per destination.
    busy = db.execute(
        select(Job.id, JobItem.channel_id).join(JobItem, JobItem.job_id == Job.id)
        .where(Job.state.in_(ACTIVE_JOB_STATES), Job.id != job.id, JobItem.channel_id.in_(job.channel_ids),
               JobItem.state.in_(ACTIVE_ITEM_STATES)).limit(1)
    ).first()
    if busy:
        raise JobError("job_already_running", "Another job is still writing to one of these channels.",
                       extra={"job_id": busy[0]})
    if _four_eyes_needed(db, job):
        if job.approved_by is None:
            job.approved_by = principal.user_id
            job.needs_second_approver = True
            audit.record(db, job.tenant_id, "job.first_approval", f"job:{job.id}", {})
            db.flush()
            return job
        if job.approved_by == principal.user_id:
            raise Forbidden("second_approver_required", "A different person must give the second approval.")
        job.second_approver = principal.user_id
    else:
        job.approved_by = principal.user_id
    job.needs_second_approver = False
    queued = db.execute(update(JobItem).where(JobItem.job_id == job.id, JobItem.state == "VALIDATED")
                        .values(state="QUEUED", not_before=utcnow(), updated_at=utcnow())).rowcount
    job.state = "running" if job.kind != "rollback" else "rolling_back"
    audit.record(db, job.tenant_id, "job.approved", f"job:{job.id}", {"queued": queued, "high_risk": high})
    emit(db, job.tenant_id, "job.started", {"job_id": job.id})
    db.flush()
    if queued == 0:
        finalize_if_done(db, job)
    return job


def pause(db: Session, job: Job) -> Job:
    if job.state not in ("running", "rolling_back"):
        raise JobError("conflict", "Only a running job can be paused.")
    job.options = {**job.options, "paused_from": job.state}
    job.state = "paused"
    audit.record(db, job.tenant_id, "job.paused", f"job:{job.id}", {})
    emit(db, job.tenant_id, "job.paused", {"job_id": job.id})
    return job


def resume(db: Session, job: Job) -> Job:
    if job.state != "paused":
        raise JobError("conflict", "Only a paused job can be resumed.")
    job.state = job.options.get("paused_from", "running")
    audit.record(db, job.tenant_id, "job.resumed", f"job:{job.id}", {})
    emit(db, job.tenant_id, "job.resumed", {"job_id": job.id})
    return job


def cancel(db: Session, job: Job) -> Job:
    if job.state in ("completed", "partially_completed", "failed", "cancelled", "rolled_back"):
        raise JobError("conflict", "This job has already finished.")
    # Items a handler holds right now (PUSHING) finish their single write; everything else stops.
    db.execute(update(JobItem).where(JobItem.job_id == job.id,
                                     JobItem.state.in_(("VALIDATED", "APPROVED", "QUEUED", "RETRY_WAIT")))
               .values(state="CANCELLED", updated_at=utcnow()))
    job.state = "cancelled"
    job.finished_at = utcnow()
    audit.record(db, job.tenant_id, "job.cancelled", f"job:{job.id}", {})
    emit(db, job.tenant_id, "job.cancelled", {"job_id": job.id})
    return job


def retry_failed(db: Session, job: Job, item_ids: list[str] | None = None) -> int:
    if job.state in ("awaiting_approval", "previewing", "draft", "cancelled"):
        raise JobError("conflict", "This job has not run, so there is nothing to retry.")
    stmt = update(JobItem).where(
        JobItem.job_id == job.id, JobItem.state.in_(("FAILED", "DEAD_LETTER")),
        JobItem.payload.is_not(None),
    )
    if item_ids:
        stmt = stmt.where(JobItem.id.in_(item_ids[:1000]))
    # Validation-blocked items are re-queued too: the engine re-validates before writing.
    count = db.execute(stmt.values(state="QUEUED", attempt=0, crash_count=0, error_fingerprint=None,
                                   not_before=utcnow(), updated_at=utcnow())).rowcount
    if count:
        job.state = "rolling_back" if job.kind == "rollback" else "running"
        job.finished_at = None
        audit.record(db, job.tenant_id, "job.retried", f"job:{job.id}", {"items": count})
    return count


def finalize_if_done(db: Session, job: Job) -> bool:
    if job.state not in ("running", "rolling_back"):
        return False
    counts: dict[str, int] = {}
    for per_channel in item_counts(db, job.id).values():
        for state, n in per_channel.items():
            counts[state] = counts.get(state, 0) + n
    if any(counts.get(s) for s in ACTIVE_ITEM_STATES):
        return False
    ok = counts.get("PUSHED", 0) + counts.get("SYNCED", 0) + counts.get("ROLLED_BACK", 0) + counts.get("DELISTED", 0)
    bad = counts.get("FAILED", 0) + counts.get("DEAD_LETTER", 0)
    if job.kind == "rollback":
        job.state = "rolled_back" if not bad else "partially_completed"
    elif bad and ok:
        job.state = "partially_completed"
    elif bad:
        job.state = "failed"
    else:
        job.state = "completed"
    job.finished_at = utcnow()
    audit.record(db, job.tenant_id, f"job.{job.state}", f"job:{job.id}", counts)
    emit(db, job.tenant_id, f"job.{job.state}", {"job_id": job.id, "counts": counts})
    db.flush()
    return True


def create_rollback_job(db: Session, principal: Principal, job: Job, channel_ids: list[str] | None = None) -> Job:
    """FR-49: remove what the job created, restore what it modified, and say up front what
    cannot be rolled back. The rollback is itself a previewed, approved job."""
    if job.kind != "publish" or job.state in ("previewing", "awaiting_approval", "draft"):
        raise JobError("conflict", "Only a publish job that has run can be rolled back.")
    if job.state in ("running", "paused"):
        raise JobError("conflict", "Pause or cancel the job before rolling it back.")
    targets = channel_ids or job.channel_ids
    if not set(targets) <= set(job.channel_ids):
        raise JobError("validation_failed", "Those channels are not part of this job.", 422)
    existing = db.execute(select(Job).where(Job.parent_job_id == job.id, Job.kind == "rollback",
                                            Job.state.in_(("awaiting_approval", "rolling_back", "paused")))).first()
    if existing:
        raise JobError("conflict", "A rollback of this job is already in progress.")
    rollback = Job(
        tenant_id=job.tenant_id, kind="rollback", state="previewing", catalog_id=job.catalog_id,
        channel_ids=list(targets), parent_job_id=job.id, started_by=principal.user_id, options={},
    )
    db.add(rollback)
    db.flush()
    snapshots = {
        (s.product_id, s.channel_id): s for s in db.execute(
            select(ProductSnapshot).where(ProductSnapshot.job_id == job.id)).scalars()
    }
    summary = {"delete": 0, "restore": 0, "cannot_rollback": []}
    items = db.execute(select(JobItem).where(
        JobItem.job_id == job.id, JobItem.state.in_(("PUSHED", "SYNCED")), JobItem.channel_id.in_(targets)
    )).scalars().all()
    for item in items:
        snap = snapshots.get((item.product_id, item.channel_id))
        if snap is None:
            summary["cannot_rollback"].append({"product_id": item.product_id, "channel_id": item.channel_id,
                                               "reason": "no snapshot was recorded"})
            continue
        if not snap.existed:
            operation, payload = "delete", None
            summary["delete"] += 1
        elif snap.payload is None:
            summary["cannot_rollback"].append({
                "product_id": item.product_id, "channel_id": item.channel_id,
                "reason": "the listing existed before this job but its earlier content is unknown"})
            continue
        else:
            operation, payload = "restore", snap.payload
            summary["restore"] += 1
        db.add(JobItem(
            tenant_id=job.tenant_id, job_id=rollback.id, product_id=item.product_id, channel_id=item.channel_id,
            operation=operation, payload=payload, payload_hash=transform.payload_hash(payload) if payload else None,
            risk="high", state="VALIDATED", destination_ref=item.destination_ref,
            diff=[{"field": "*", "before": "published by job", "after": "removed" if operation == "delete" else "previous version"}],
        ))
    if not summary["delete"] and not summary["restore"]:
        raise JobError("nothing_to_roll_back", "Nothing this job published can be rolled back.", 422,
                       {"cannot_rollback": summary["cannot_rollback"][:100]})
    summary["cannot_rollback"] = summary["cannot_rollback"][:500]
    rollback.preview = summary
    rollback.state = "awaiting_approval"
    audit.record(db, job.tenant_id, "job.rollback_previewed", f"job:{rollback.id}",
                 {"of": job.id, "delete": summary["delete"], "restore": summary["restore"]})
    db.flush()
    return rollback
