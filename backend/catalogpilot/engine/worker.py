"""Publish handler: lease-based, fenced, idempotent execution of job items (HA-01..14).

Any number of handlers (threads or processes) may run this loop against the same database.
Guarantees, and where each comes from:

  no lost work       item state lives in the database; a lease that stops being renewed
                     expires and any peer reclaims the item (``_claim``).
  no duplicate write every destination write carries a deterministic idempotency key, and
                     the id mapping / platform marker turns a repeated create into an update.
  no zombie write    each claim takes a new fencing token. The broker refuses a credential
                     for a stale token, and every state change is ``WHERE token = mine``.
  bounded blast      per-channel circuit breaker and token bucket; per-tenant share of each
                     claim batch; poison items are quarantined after repeated crashes.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import threading
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, case, or_, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..connectors import get_connector
from ..connectors.sdk import AuthError, CallContext, ConnectorError, RejectedError, RetryableError
from ..db import session_scope, system_scope, tenant_scope
from ..ids import as_utc, utcnow
from ..models import (
    BreakerState,
    Channel,
    ChannelListing,
    FencingCounter,
    IdMapping,
    Job,
    JobItem,
    ProductSnapshot,
    Tenant,
)
from ..security import audit
from ..vault import broker
from . import jobs, transform

log = logging.getLogger("catalogpilot.worker")
RUNNABLE_JOB_STATES = ("running", "rolling_back")


@dataclass
class Claim:
    item_id: str
    tenant_id: str
    token: int


class StaleLease(Exception):
    """Our fencing token is no longer current: another handler owns the item. Stop quietly."""


def next_fencing_token(db: Session) -> int:
    """Monotonic across all handlers. The UPDATE takes a row lock, so two transactions can
    never read the same value."""
    updated = db.execute(update(FencingCounter).where(FencingCounter.id == 1)
                         .values(value=FencingCounter.value + 1)).rowcount
    if not updated:
        db.add(FencingCounter(id=1, value=1))
        db.flush()
    return db.execute(select(FencingCounter.value).where(FencingCounter.id == 1)).scalar_one()


def idempotency_key(tenant_id: str, channel_id: str, source_key: str, payload_hash: str | None, op: str) -> str:
    raw = "|".join((tenant_id, channel_id, source_key, payload_hash or "-", op))
    return hashlib.sha256(raw.encode()).hexdigest()


def backoff_seconds(attempt: int, rng: random.Random | None = None) -> float:
    """Exponential back-off with full jitter (REL-02)."""
    settings = get_settings()
    ceiling = min(settings.retry_cap_seconds, settings.retry_base_seconds * (2 ** max(attempt - 1, 0)))
    return (rng or random).uniform(0, ceiling)


# ---------------------------------------------------------------- claiming

def _claim(worker_id: str, limit: int) -> list[Claim]:
    """Take leases on runnable items. Expired leases are reclaimed here: that is the takeover."""
    settings = get_settings()
    now = utcnow()
    claims: list[Claim] = []
    with system_scope(), session_scope() as db:
        candidates = db.execute(
            select(JobItem.id, JobItem.tenant_id, JobItem.state, JobItem.lease_fencing_token)
            .join(Job, Job.id == JobItem.job_id)
            .where(
                Job.state.in_(RUNNABLE_JOB_STATES),
                or_(
                    and_(JobItem.state.in_(("QUEUED", "RETRY_WAIT")), JobItem.not_before <= now),
                    and_(JobItem.state == "PUSHING", JobItem.lease_until < now),
                ),
            )
            .order_by(JobItem.not_before)
            .limit(limit * 4)
        ).all()
        # Bulkhead: when several tenants are waiting, none may take more than its share of a batch.
        tenants = {c.tenant_id for c in candidates}
        per_tenant_cap = limit if len(tenants) <= 1 else max(1, int(limit * settings.tenant_pool_share))
        taken: dict[str, int] = {}
        for cand in candidates:
            if len(claims) >= limit:
                break
            if taken.get(cand.tenant_id, 0) >= per_tenant_cap:
                continue
            token = next_fencing_token(db)
            reclaim = cand.state == "PUSHING"
            guard = [JobItem.id == cand.id, JobItem.state == cand.state]
            if reclaim:
                guard += [JobItem.lease_until < now, JobItem.lease_fencing_token == cand.lease_fencing_token]
            values: dict[str, Any] = dict(
                state="PUSHING", lease_owner=worker_id, lease_fencing_token=token,
                lease_until=now + timedelta(seconds=settings.lease_seconds), updated_at=now, started_at=None,
            )
            if reclaim:
                # The previous owner died or hung. Only an item it had actually started counts
                # as a crash; items that were merely waiting in its batch are innocent. An item
                # that keeps killing handlers is poison (HA-08).
                values["crash_count"] = case(
                    (JobItem.started_at.is_not(None), JobItem.crash_count + 1), else_=JobItem.crash_count)
            if db.execute(update(JobItem).where(*guard).values(**values)).rowcount == 1:
                claims.append(Claim(cand.id, cand.tenant_id, token))
                taken[cand.tenant_id] = taken.get(cand.tenant_id, 0) + 1
    return claims


def _guarded(db: Session, claim: Claim, **values: Any) -> None:
    """Every state change by a handler is conditional on still holding the lease (HA-06)."""
    values.setdefault("updated_at", utcnow())
    rows = db.execute(
        update(JobItem).where(JobItem.id == claim.item_id, JobItem.lease_fencing_token == claim.token,
                              JobItem.state == "PUSHING").values(**values)
    ).rowcount
    if rows != 1:
        raise StaleLease(claim.item_id)


def heartbeat(db: Session, claim: Claim) -> None:
    _guarded(db, claim, lease_until=utcnow() + timedelta(seconds=get_settings().lease_seconds))


def _release(db: Session, claim: Claim, delay: float, state: str = "QUEUED", **extra: Any) -> None:
    """Give the item back without failing it: paused, throttled or waiting on a dependency (HA-14, HA-28)."""
    _guarded(db, claim, state=state, lease_owner=None, lease_until=None,
             not_before=utcnow() + timedelta(seconds=delay), **extra)


# ---------------------------------------------------------------- breaker and rate limit

def _breaker(db: Session, channel: Channel) -> BreakerState:
    state = db.execute(select(BreakerState).where(BreakerState.channel_id == channel.id)).scalar_one_or_none()
    if state is None:
        caps = get_connector(channel.platform).capabilities()
        state = BreakerState(tenant_id=channel.tenant_id, channel_id=channel.id, recent=[],
                             tokens=float(caps.burst), rate_per_sec=caps.requests_per_second, refilled_at=utcnow())
        db.add(state)
        db.flush()
    return state


def _admit(db: Session, channel: Channel, now: datetime) -> float:
    """Returns 0 if the call may proceed, otherwise the number of seconds to wait."""
    settings = get_settings()
    state = _breaker(db, channel)
    if state.blocked_until and as_utc(state.blocked_until) > now:
        return (as_utc(state.blocked_until) - now).total_seconds()
    if state.state == "open":
        waited = (now - as_utc(state.opened_at)).total_seconds()
        if waited < settings.breaker_probe_seconds:
            return settings.breaker_probe_seconds - waited
        state.state = "half_open"  # let one probe through
    caps = get_connector(channel.platform).capabilities()
    elapsed = max(0.0, (now - as_utc(state.refilled_at or now)).total_seconds())
    state.tokens = min(float(caps.burst), state.tokens + elapsed * state.rate_per_sec)
    state.refilled_at = now
    if state.tokens < 1.0:
        return (1.0 - state.tokens) / max(state.rate_per_sec, 0.01)
    state.tokens -= 1.0
    return 0.0


def _record_outcome(db: Session, channel: Channel, ok: bool, throttled: bool = False,
                    retry_after: float | None = None) -> None:
    settings = get_settings()
    state = _breaker(db, channel)
    caps = get_connector(channel.platform).capabilities()
    if throttled:
        # The platform told us to slow down: halve the rate and honour Retry-After (REL-05).
        state.rate_per_sec = max(caps.requests_per_second / 16, state.rate_per_sec / 2)
        state.blocked_until = utcnow() + timedelta(seconds=retry_after or 1.0)
        return  # throttling is not a failure for breaker purposes
    recent = (list(state.recent or []) + [0 if ok else 1])[-settings.breaker_window:]
    state.recent = recent
    if ok:
        state.rate_per_sec = min(caps.requests_per_second, state.rate_per_sec * 1.05 + 0.01)  # recover gently
        if state.state == "half_open":
            state.state, state.recent, state.opened_at = "closed", [], None
            if channel.health == "degraded":
                channel.health = "ok"
    elif state.state == "half_open" or (
        len(recent) >= settings.breaker_window and sum(recent) / len(recent) >= settings.breaker_error_ratio
    ):
        if state.state != "open":
            audit.record(db, channel.tenant_id, "channel.breaker_opened", f"channel:{channel.id}", {})
            jobs.emit(db, channel.tenant_id, "channel.paused", {"channel_id": channel.id, "reason": "error rate"})
        state.state, state.opened_at = "open", utcnow()
        channel.health = "degraded"


# ---------------------------------------------------------------- execution

def _fingerprint(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)[-3:]
    raw = type(exc).__name__ + "|" + "|".join(f"{f.name}:{f.lineno}" for f in frames)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _execute(claim: Claim, worker_id: str, crash_hook=None) -> None:
    settings = get_settings()
    with tenant_scope(claim.tenant_id), session_scope() as db:
        item = db.execute(select(JobItem).where(JobItem.id == claim.item_id)).scalar_one()
        if item.lease_fencing_token != claim.token or item.state != "PUSHING":
            return
        job = db.execute(select(Job).where(Job.id == item.job_id)).scalar_one()
        channel = db.execute(select(Channel).where(Channel.id == item.channel_id)).scalar_one()
        tenant = db.get(Tenant, claim.tenant_id)
        try:
            if item.crash_count >= settings.poison_threshold:
                _guarded(db, claim, state="DEAD_LETTER", lease_owner=None, lease_until=None, last_error={
                    "code": "poison", "message": "This item stopped the publisher several times and was set aside.",
                    "suggestion": "Contact support with the job id; other items were not affected."})
                jobs.finalize_if_done(db, job)
                return
            if job.state not in RUNNABLE_JOB_STATES or (tenant.frozen and item.operation != "import"):
                _release(db, claim, 30)  # an emergency freeze stops writes to stores; reading is still safe
                return
            if channel.health in ("paused", "reauth_required"):
                _release(db, claim, 60)  # paused, not failed: resumes when the seller reconnects
                return
            if item.operation == "import":
                from . import importer

                importer.run(db, claim, item, job, channel, worker_id, crash_hook)
                return
            wait = _admit(db, channel, utcnow())
            if wait > 0:
                _release(db, claim, min(wait, 60))
                return
            connector = get_connector(channel.platform)
            if item.operation in ("upsert", "restore") and item.payload is not None:
                issues = transform.validate(item.payload, connector.capabilities())
                if transform.has_errors(issues):
                    first = next(i for i in issues if i["severity"] == "error")
                    _guarded(db, claim, state="FAILED", lease_owner=None, lease_until=None, issues=issues,
                             last_error={"code": "validation", "message": first["message"],
                                         "suggestion": first["suggestion"]})
                    jobs.finalize_if_done(db, job)
                    return
            op_kind = "delete" if item.operation in ("delete", "delist") else "write"
            ctx = broker.lease_for_item(db, item, worker_id, claim.token, op_kind)
            _guarded(db, claim, started_at=utcnow())
            db.commit()  # usage row and bucket debit are durable before the external call
            result = _call_destination(db, connector, ctx, item, crash_hook)
            _commit_success(db, claim, item, job, channel, result)
            _record_outcome(db, channel, ok=True)
            jobs.finalize_if_done(db, job)
        except StaleLease:
            db.rollback()
            log.info("stale lease on %s; another handler owns it", claim.item_id)
        except broker.LeaseDenied as exc:
            db.rollback()
            log.warning("credential lease denied for %s: %s", claim.item_id, exc)
        except AuthError as exc:
            db.rollback()
            channel.health, channel.paused_reason = "reauth_required", exc.message[:300]
            jobs.emit(db, claim.tenant_id, "channel.reauth_required", {"channel_id": channel.id})
            audit.record(db, claim.tenant_id, "channel.reauth_required", f"channel:{channel.id}", {})
            _try(db, lambda: _release(db, claim, 60))
        except RetryableError as exc:
            db.rollback()
            _record_outcome(db, channel, ok=False, throttled=exc.throttled, retry_after=exc.retry_after)
            _try(db, lambda e=exc: _retry_or_dead(db, claim, item, job, e.message, e.retry_after, e.throttled))
        except RejectedError as exc:
            db.rollback()
            _record_outcome(db, channel, ok=True)  # the platform answered; the item is the problem
            _try(db, lambda e=exc: _fail(db, claim, item, job, e))
        except ConnectorError as exc:
            db.rollback()
            _try(db, lambda e=exc: _fail(db, claim, item, job, e))
        except Exception as exc:  # a bug or an unexpected response: never take the handler down
            db.rollback()
            log.exception("unexpected error on item %s", claim.item_id)
            fp = _fingerprint(exc)
            same = item.error_fingerprint == fp
            crashes = (item.crash_count + 1) if same else 1

            def record():
                if crashes >= settings.poison_threshold:
                    _guarded(db, claim, state="DEAD_LETTER", lease_owner=None, lease_until=None,
                             crash_count=crashes, error_fingerprint=fp,
                             last_error={"code": "internal", "message": "An internal error stopped this item.",
                                         "suggestion": "Retry later or contact support with the job id."})
                    jobs.finalize_if_done(db, job)
                else:
                    _release(db, claim, backoff_seconds(crashes), state="RETRY_WAIT", crash_count=crashes,
                             error_fingerprint=fp, attempt=item.attempt + 1,
                             last_error={"code": "internal", "message": "Temporary internal error; retrying."})
            _try(db, record)


def _try(db: Session, fn) -> None:
    try:
        fn()
        db.commit()
    except StaleLease:
        db.rollback()


def _call_destination(db: Session, connector, ctx: CallContext, item: JobItem, crash_hook):
    """One destination operation. Order: resolve existing object -> write -> (caller records)."""
    source_key = item.product_id
    mapping = db.execute(select(IdMapping).where(IdMapping.channel_id == item.channel_id,
                                                 IdMapping.source_key == source_key)).scalar_one_or_none()
    external_id = mapping.destination_external_id if mapping else item.destination_ref
    key = idempotency_key(item.tenant_id, item.channel_id, source_key, item.payload_hash, item.operation)
    caps = connector.capabilities()
    if item.operation in ("upsert", "restore"):
        if external_id is None and not caps.supports_idempotency_key:
            # A previous owner may have created the object and died before recording it.
            external_id = connector.find_by_marker(ctx, source_key)
        prior_existed = external_id is not None
        result = connector.upsert_product(ctx, item.payload, external_id, key)
        if crash_hook:
            crash_hook("after_write", item)  # test seam: die after the write, before recording it
        return {"external_id": result.external_id, "created": result.created and not prior_existed}
    if external_id is None:
        return {"external_id": None, "created": False}  # nothing there: already gone, which is the goal
    if item.operation == "delist":
        connector.delist(ctx, external_id, key)
    else:
        connector.delete_product(ctx, external_id, key)
    return {"external_id": external_id, "created": False}


def _commit_success(db: Session, claim: Claim, item: JobItem, job: Job, channel: Channel, result: dict[str, Any]) -> None:
    now = utcnow()
    final = {"upsert": "PUSHED", "restore": "ROLLED_BACK", "delete": "ROLLED_BACK", "delist": "DELISTED"}[item.operation]
    if job.kind != "rollback" and item.operation == "delete":
        final = "DELISTED"
    # Guard first: if we lost the lease, none of the bookkeeping below may be written.
    _guarded(db, claim, state=final, lease_owner=None, lease_until=None, destination_ref=result["external_id"],
             last_error=None)
    listing = db.execute(select(ChannelListing).where(
        ChannelListing.product_id == item.product_id, ChannelListing.channel_id == item.channel_id)).scalar_one_or_none()
    if item.operation == "upsert":
        mapping = db.execute(select(IdMapping).where(
            IdMapping.channel_id == item.channel_id, IdMapping.source_key == item.product_id)).scalar_one_or_none()
        if mapping is None:
            db.add(IdMapping(tenant_id=item.tenant_id, channel_id=item.channel_id, source_key=item.product_id,
                             destination_external_id=result["external_id"], first_job_id=job.id))
        else:
            mapping.destination_external_id = result["external_id"]
        # Snapshot what was live before this job touched it, once per job (rollback source).
        has_snapshot = db.execute(select(ProductSnapshot.id).where(
            ProductSnapshot.job_id == job.id, ProductSnapshot.product_id == item.product_id,
            ProductSnapshot.channel_id == item.channel_id)).first()
        if not has_snapshot:
            was_live = listing is not None and listing.destination_external_id is not None \
                and listing.state in ("live", "pending")
            db.add(ProductSnapshot(
                tenant_id=item.tenant_id, product_id=item.product_id, channel_id=item.channel_id, job_id=job.id,
                # No listing before this job means the object is ours even when the platform
                # answered "updated" (an idempotent replay after a takeover), so rollback deletes it.
                existed=was_live,
                payload=listing.last_published_payload if was_live else None,
            ))
    if listing is None:
        listing = ChannelListing(tenant_id=item.tenant_id, product_id=item.product_id, channel_id=item.channel_id)
        db.add(listing)
    listing.last_error = None
    listing.last_synced_at = now
    if item.operation in ("upsert", "restore"):
        listing.destination_external_id = result["external_id"]
        listing.state = "live"
        listing.last_published_hash = item.payload_hash
        listing.last_published_payload = item.payload
    elif item.operation == "delist":
        listing.state = "delisted"
    else:
        listing.state = "delisted"
        listing.destination_external_id = None
        listing.last_published_hash = None
        listing.last_published_payload = None
        db.execute(IdMapping.__table__.delete().where(
            IdMapping.tenant_id == item.tenant_id, IdMapping.channel_id == item.channel_id,
            IdMapping.source_key == item.product_id))
    if job.kind == "rollback" and job.parent_job_id:
        db.execute(update(JobItem).where(
            JobItem.job_id == job.parent_job_id, JobItem.product_id == item.product_id,
            JobItem.channel_id == item.channel_id, JobItem.state.in_(("PUSHED", "SYNCED")),
        ).values(state="ROLLED_BACK", updated_at=now))
    db.flush()


def _retry_or_dead(db: Session, claim: Claim, item: JobItem, job: Job, message: str,
                   retry_after: float | None, throttled: bool) -> None:
    settings = get_settings()
    # Being throttled is the platform pacing us, not the item failing: it does not use up attempts.
    attempt = item.attempt if throttled else item.attempt + 1
    if attempt >= settings.retry_max_attempts:
        _guarded(db, claim, state="DEAD_LETTER", lease_owner=None, lease_until=None, attempt=attempt, last_error={
            "code": "retries_exhausted", "message": f"Gave up after {attempt} attempts: {message}",
            "suggestion": "Check the channel's status, then retry the failed items."})
        jobs.finalize_if_done(db, job)
        return
    delay = max(retry_after or 0.0, backoff_seconds(max(attempt, 1)))
    _release(db, claim, delay, state="RETRY_WAIT", attempt=attempt,
             last_error={"code": "temporary", "message": message})


def _fail(db: Session, claim: Claim, item: JobItem, job: Job, exc: ConnectorError) -> None:
    _guarded(db, claim, state="FAILED", lease_owner=None, lease_until=None, last_error={
        "code": "rejected_by_platform", "message": exc.message, "suggestion": exc.suggestion})
    listing = db.execute(select(ChannelListing).where(
        ChannelListing.product_id == item.product_id, ChannelListing.channel_id == item.channel_id)).scalar_one_or_none()
    if listing is None:
        listing = ChannelListing(tenant_id=item.tenant_id, product_id=item.product_id, channel_id=item.channel_id)
        db.add(listing)
    if not listing.destination_external_id:
        listing.state = "rejected"
    listing.last_error = {"message": exc.message, "suggestion": exc.suggestion}
    jobs.emit(db, item.tenant_id, "listing.rejected", {"product_id": item.product_id, "channel_id": item.channel_id})
    jobs.finalize_if_done(db, job)


# ---------------------------------------------------------------- loop

def run_once(worker_id: str | None = None, limit: int = 20, crash_hook=None) -> int:
    """Claim and execute one batch. Returns how many items were claimed."""
    worker_id = worker_id or default_worker_id()
    claims = _claim(worker_id, limit)
    for claim in claims:
        _execute(claim, worker_id, crash_hook)
    return len(claims)


def sweep_jobs() -> None:
    """Supervisor duty: close jobs whose last item finished while nobody was looking."""
    with system_scope(), session_scope() as db:
        pending = db.execute(select(Job.id, Job.tenant_id).where(Job.state.in_(RUNNABLE_JOB_STATES))).all()
    for job_id, tenant_id in pending:
        with tenant_scope(tenant_id), session_scope() as db:
            jobs.finalize_if_done(db, jobs.get_job(db, job_id))


def default_worker_id() -> str:
    return f"publish-{os.getpid()}-{threading.get_ident()}"


class WorkerThread(threading.Thread):
    """In-process handler for single-node installs. Production runs the same loop in its own
    deployment (``python -m catalogpilot.worker``) with at least two replicas."""

    def __init__(self, interval: float = 0.5):
        super().__init__(name="cp-publish-handler", daemon=True)
        self.interval = interval
        self._halt = threading.Event()  # (Thread already owns the name _stop)

    def run(self) -> None:
        ticks = 0
        while not self._halt.is_set():
            try:
                worked = run_once()
                ticks += 1
                if ticks % 20 == 0:
                    sweep_jobs()
            except Exception:
                log.exception("handler loop error")
                worked = 0
            if not worked:
                self._halt.wait(self.interval)

    def stop(self) -> None:  # graceful drain: finish the current batch, then exit (HA-09)
        self._halt.set()
