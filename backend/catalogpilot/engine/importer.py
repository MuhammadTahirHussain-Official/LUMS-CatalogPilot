"""Importing a catalog from a connected store (FR-12): paginated, checkpointed, resumable.

One import is one job item whose payload is the checkpoint: the page cursor plus running
totals. Each page is read, normalized, upserted into the catalog and the checkpoint advanced
in a single database transaction, under the same lease + fencing-token rules as publishing.

What that buys, case by case:
  server crash / deploy / power loss   the lease expires, any handler resumes at the saved cursor
  the same page processed twice        products are upserted by a stable key: second pass = "unchanged"
  the store times out or throttles     back-off and retry the same page; the cursor does not move
  the store rejects the saved cursor   restart from page one (bounded); upserts make that harmless
  the store's key is revoked           the channel asks to be reconnected; the import waits, not fails
  one unreadable product               recorded in the report; the rest of the page still imports
  a product deleted mid-import         simply absent from later pages
  the seller pauses or cancels         checked between pages
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..catalog import service as catalog_service
from ..catalog.normalize import normalize_state
from ..catalog.service import Change
from ..config import get_settings
from ..connectors import get_connector
from ..connectors.sdk import AuthError, ConnectorError, CursorExpired, RetryableError
from ..ids import utcnow
from ..models import Channel, Job, JobItem
from ..security import audit
from ..security.authz import Principal
from ..vault import broker
from . import jobs

log = logging.getLogger("catalogpilot.import")
PAGES_PER_TURN = 10  # then yield, so one large import cannot monopolise a handler
MAX_RESTARTS = 3
MAX_REPORTED = 100
_NOTE_KEYS = {"no images": "missing_images", "no brand": "missing_brand", "no category": "missing_category",
              "the product has no price": "missing_price"}


def _fresh_checkpoint() -> dict[str, Any]:
    return {"cursor": None, "pages": 0, "seen": 0, "created": 0, "updated": 0, "unchanged": 0, "skipped": 0,
            "restarts": 0, "problems": [], "quality": {}, "finished": False}


def source_key(channel: Channel, external_id: str) -> str:
    """Stable identity of a source product inside the catalog. It is derived from the store,
    not from our channel row, so reconnecting the same store never duplicates its products."""
    host = (urlsplit(channel.base_url).hostname or "") if channel.base_url else ""
    return f"{channel.platform}:{host or channel.id[:8]}:{external_id}"[:300]


def create_import_job(db: Session, principal: Principal, channel_id: str, catalog_id: str) -> Job:
    catalog = catalog_service.get_catalog(db, catalog_id)
    channel = db.execute(select(Channel).where(Channel.id == channel_id)).scalar_one_or_none()
    if channel is None:
        raise catalog_service.NotFound("channel not found")
    caps = get_connector(channel.platform).capabilities()
    if not caps.supports_import:
        raise jobs.JobError("import_not_supported", "Products cannot be imported from this kind of channel.", 422)
    if channel.role == "destination":
        # The seller connected this store to publish to. Reading from it needs their say-so.
        raise jobs.JobError("import_not_allowed", f"'{channel.name}' is set up for publishing only.", 422)
    if caps.credential_kinds and not channel.credential_id:
        raise jobs.JobError("channel_not_connected", f"'{channel.name}' is not connected yet.")
    running = db.execute(
        select(Job.id).join(JobItem, JobItem.job_id == Job.id).where(
            Job.kind == "import", Job.state.in_(("running", "paused")), JobItem.channel_id == channel.id)
    ).first()
    if running:
        raise jobs.JobError("job_already_running", "An import from this store is already in progress.",
                            extra={"job_id": running[0]})
    job = Job(tenant_id=principal.tenant_id, kind="import", state="running", catalog_id=catalog.id,
              channel_ids=[channel.id], options={}, started_by=principal.user_id, approved_by=principal.user_id,
              preview={"source": channel.name, **_fresh_checkpoint()})
    db.add(job)
    db.flush()
    db.add(JobItem(tenant_id=job.tenant_id, job_id=job.id, channel_id=channel.id, operation="import",
                   state="QUEUED", risk="low", payload=_fresh_checkpoint()))
    audit.record(db, job.tenant_id, "import.started", f"job:{job.id}", {"channel_id": channel.id})
    jobs.emit(db, job.tenant_id, "job.started", {"job_id": job.id})
    db.flush()
    return job


def _apply_page(db: Session, job: Job, channel: Channel, entries: list[dict[str, Any]], cp: dict[str, Any]) -> None:
    catalog = catalog_service.get_catalog(db, job.catalog_id)
    changes: list[Change] = []
    problems = list(cp["problems"])
    quality = dict(cp["quality"])

    def problem(external_id: str, reason: str) -> None:
        cp["skipped"] += 1
        if len(problems) < MAX_REPORTED:
            problems.append({"id": str(external_id)[:80], "reason": reason[:200]})

    seen_in_page: set[str] = set()
    for entry in entries:
        cp["seen"] += 1
        external_id = str(entry.get("external_key") or "")
        if not external_id:
            problem("?", "the store returned a product without an id")
            continue
        if external_id in seen_in_page:
            continue  # the store listed the same product twice on one page
        seen_in_page.add(external_id)
        if entry.get("error"):
            problem(external_id, str(entry["error"]))
            continue
        try:
            state, notes = normalize_state(entry.get("state"), catalog.default_locale, catalog.default_currency)
        except ValueError as exc:
            problem(external_id, str(exc))
            continue
        for note in notes:
            bucket = _NOTE_KEYS.get(note)
            if bucket:
                quality[bucket] = quality.get(bucket, 0) + 1
        changes.append(Change(source_key(channel, external_id), state, origin="import", ref=f"job:{job.id}",
                              source_ref={"platform": channel.platform, "external_id": external_id,
                                          "channel_id": channel.id}))
    if changes:
        _version, results = catalog_service.apply_changes(db, catalog, changes, "import", job.started_by)
        for result in results:
            if result.outcome in ("created", "updated", "unchanged"):
                cp[result.outcome] += 1
            else:
                problem(result.external_key.rsplit(":", 1)[-1], result.error or "could not be saved")
    cp["problems"], cp["quality"] = problems, quality


def run(db: Session, claim, item: JobItem, job: Job, channel: Channel, worker_id: str, crash_hook=None) -> None:
    """Executes up to PAGES_PER_TURN pages. Called by the handler with the item leased."""
    from . import worker  # the handler owns leases, fencing, back-off and the breaker

    settings = get_settings()
    connector = get_connector(channel.platform)
    cp = {**_fresh_checkpoint(), **(item.payload or {})}
    attempt = item.attempt
    try:
        for _turn in range(PAGES_PER_TURN):
            db.refresh(job)
            if job.state != "running":
                worker._release(db, claim, 30)  # paused or cancelled between pages
                db.commit()
                return
            wait = worker._admit(db, channel, utcnow())
            if wait > 0:
                worker._release(db, claim, min(wait, 60))
                db.commit()
                return
            ctx = broker.lease_for_item(db, item, worker_id, claim.token, "read")
            worker._guarded(db, claim, started_at=utcnow())
            db.commit()
            try:
                entries, next_cursor = connector.list_products(ctx, cp["cursor"])
            except CursorExpired:
                cp["restarts"] += 1
                if cp["restarts"] > MAX_RESTARTS:
                    raise ConnectorError("The store kept rejecting our place in its product list.",
                                         "Try the import again in a few minutes.") from None
                # Start over from page one. Everything already imported is matched by key, so
                # the second pass reports it as unchanged instead of duplicating it.
                cp.update(cursor=None, pages=0, seen=0, unchanged=0)
                worker._guarded(db, claim, payload=dict(cp), started_at=None,
                                lease_until=utcnow() + timedelta(seconds=settings.lease_seconds))
                db.commit()
                continue
            if not isinstance(entries, list) or len(entries) > 1000:
                raise ConnectorError("The store returned a page we could not read.")
            _apply_page(db, job, channel, entries, cp)
            cp["cursor"] = next_cursor
            cp["pages"] += 1
            cp["finished"] = next_cursor is None
            job.preview = {"source": channel.name, **{k: v for k, v in cp.items() if k != "cursor"}}
            attempt = 0
            if cp["finished"]:
                worker._guarded(db, claim, state="PUSHED", lease_owner=None, lease_until=None, payload=dict(cp),
                                attempt=0, crash_count=0, last_error=None)
                worker._record_outcome(db, channel, ok=True)
                audit.record(db, job.tenant_id, "import.finished", f"job:{job.id}",
                             {k: cp[k] for k in ("seen", "created", "updated", "unchanged", "skipped")})
                jobs.finalize_if_done(db, job)
                db.commit()
                return
            # Page data and checkpoint commit together: a crash after this line resumes at the
            # next page; a crash before it redoes this page, which is harmless.
            worker._guarded(db, claim, payload=dict(cp), attempt=0, crash_count=0, started_at=None, last_error=None,
                            lease_until=utcnow() + timedelta(seconds=settings.lease_seconds))
            worker._record_outcome(db, channel, ok=True)
            db.commit()
            if crash_hook:
                crash_hook("after_page", item)
        worker._release(db, claim, 0)  # more pages remain: yield, then continue
        db.commit()
    except worker.StaleLease:
        db.rollback()
        log.info("import %s: lease lost, another handler continues", job.id)
    except broker.LeaseDenied as exc:
        db.rollback()
        log.warning("import %s: credential lease denied: %s", job.id, exc)
    except AuthError as exc:
        db.rollback()
        channel.health, channel.paused_reason = "reauth_required", exc.message[:300]
        jobs.emit(db, job.tenant_id, "channel.reauth_required", {"channel_id": channel.id})
        audit.record(db, job.tenant_id, "channel.reauth_required", f"channel:{channel.id}", {})
        worker._try(db, lambda: worker._release(db, claim, 60))
    except RetryableError as exc:
        db.rollback()
        worker._record_outcome(db, channel, ok=False, throttled=exc.throttled, retry_after=exc.retry_after)
        attempt = attempt if exc.throttled else attempt + 1
        if attempt >= settings.retry_max_attempts:
            message = f"The store stopped answering after {cp['seen']} products. Nothing imported so far was lost."
            worker._try(db, lambda: _stop(db, claim, job, cp, message, "Press Retry to continue from where it stopped."))
        else:
            delay = max(exc.retry_after or 0.0, worker.backoff_seconds(max(attempt, 1)))
            waiting = {"code": "temporary", "message": exc.message}
            worker._try(db, lambda: worker._release(db, claim, delay, state="RETRY_WAIT", attempt=attempt,
                                                    payload=dict(cp), last_error=waiting))
    except ConnectorError as exc:
        db.rollback()
        worker._try(db, lambda e=exc: _stop(db, claim, job, cp, e.message, e.suggestion or "Press Retry to continue."))


def _stop(db: Session, claim, job: Job, cp: dict[str, Any], message: str, suggestion: str) -> None:
    from . import worker

    worker._guarded(db, claim, state="FAILED", lease_owner=None, lease_until=None, payload=dict(cp),
                    last_error={"code": "import_stopped", "message": message, "suggestion": suggestion})
    job.preview = {**(job.preview or {}), **{k: v for k, v in cp.items() if k != "cursor"}}
    jobs.finalize_if_done(db, job)
