"""Append-only, hash-chained audit trail (SEC-AUD-06/07/08).

Each tenant has its own chain: hash = SHA-256(prev_hash || canonical_json(event)). Editing or
deleting any earlier row breaks every later hash, which ``verify_chain`` detects. Anchors record
the chain head so that truncating the tail of the log is detectable too.
"""

from __future__ import annotations

import hashlib
import json
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ids import as_utc, utcnow
from ..models import AuditAnchor, AuditEvent

GENESIS = "0" * 64


@dataclass
class Actor:
    type: str  # user | api_key | system | operator
    id: str
    request_id: str | None = None
    ip: str | None = None


SYSTEM = Actor("system", "engine")
_actor: ContextVar[Actor] = ContextVar("cp_actor", default=SYSTEM)


def set_actor(actor: Actor):
    return _actor.set(actor)


def reset_actor(token) -> None:
    _actor.reset(token)


def current_actor() -> Actor:
    return _actor.get()


def _canonical(body: dict[str, Any]) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":"), default=str).encode()


def _body(ev: AuditEvent) -> dict[str, Any]:
    return {
        "tenant_id": ev.tenant_id,
        "at": as_utc(ev.at).isoformat(timespec="microseconds"),
        "actor_type": ev.actor_type,
        "actor_id": ev.actor_id,
        "action": ev.action,
        "resource": ev.resource,
        "detail": ev.detail,
        "request_id": ev.request_id,
        "source_ip": ev.source_ip,
    }


def _digest(prev_hash: str, ev: AuditEvent) -> str:
    return hashlib.sha256(prev_hash.encode() + _canonical(_body(ev))).hexdigest()


def record(
    db: Session,
    tenant_id: str,
    action: str,
    resource: str,
    detail: dict[str, Any] | None = None,
    actor: Actor | None = None,
) -> AuditEvent:
    """Appends within the caller's transaction, so an action and its audit row commit together."""
    actor = actor or current_actor()
    db.flush()
    # Pending (unflushed-then-flushed) events in this transaction are visible to this query.
    stmt = (
        select(AuditEvent)
        .where(AuditEvent.tenant_id == tenant_id)
        .order_by(AuditEvent.seq.desc())
        .limit(1)
    )
    if db.get_bind().dialect.name == "postgresql":
        stmt = stmt.with_for_update()  # serialize appends per tenant chain
    last = db.execute(stmt).scalar_one_or_none()
    ev = AuditEvent(
        tenant_id=tenant_id,
        at=utcnow(),
        actor_type=actor.type,
        actor_id=actor.id,
        action=action,
        resource=resource,
        detail=_scrub(detail or {}),
        request_id=actor.request_id,
        source_ip=actor.ip,
        prev_hash=last.hash if last else GENESIS,
        hash="",
    )
    ev.hash = _digest(ev.prev_hash, ev)
    db.add(ev)
    db.flush()
    return ev


_SECRET_KEYS = ("secret", "password", "token", "key", "authorization", "cookie", "credential")


def _scrub(value: Any, key: str = "") -> Any:
    """Defense in depth: audit detail must never carry secret material (SEC-LOG-02)."""
    if isinstance(value, dict):
        return {k: _scrub(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, key) for v in value]
    lowered = key.lower()
    if isinstance(value, str) and any(s in lowered for s in _SECRET_KEYS) and not lowered.endswith(("_id", "_hint")):
        return "[redacted]"
    return value


@dataclass
class ChainReport:
    ok: bool
    checked: int
    broken_at_seq: int | None = None
    reason: str | None = None


def verify_chain(db: Session, tenant_id: str) -> ChainReport:
    prev = GENESIS
    count = 0
    last_seq = 0
    rows = db.execute(
        select(AuditEvent).where(AuditEvent.tenant_id == tenant_id).order_by(AuditEvent.seq)
    ).scalars()
    for ev in rows:
        if ev.prev_hash != prev:
            return ChainReport(False, count, ev.seq, "previous-hash mismatch (row removed or reordered)")
        if _digest(prev, ev) != ev.hash:
            return ChainReport(False, count, ev.seq, "content hash mismatch (row modified)")
        prev = ev.hash
        last_seq = ev.seq
        count += 1
    anchor = db.execute(
        select(AuditAnchor).where(AuditAnchor.tenant_id == tenant_id).order_by(AuditAnchor.id.desc()).limit(1)
    ).scalar_one_or_none()
    if anchor is not None:
        if anchor.up_to_seq > last_seq:
            return ChainReport(False, count, anchor.up_to_seq, "log truncated below the last anchor")
        at_anchor = db.execute(
            select(AuditEvent.hash).where(
                AuditEvent.tenant_id == tenant_id, AuditEvent.seq == anchor.up_to_seq
            )
        ).scalar_one_or_none()
        if at_anchor != anchor.chain_head:
            return ChainReport(False, count, anchor.up_to_seq, "chain head differs from anchor")
    return ChainReport(True, count)


def seal_anchor(db: Session, tenant_id: str) -> AuditAnchor | None:
    last = db.execute(
        select(AuditEvent).where(AuditEvent.tenant_id == tenant_id).order_by(AuditEvent.seq.desc()).limit(1)
    ).scalar_one_or_none()
    if last is None:
        return None
    anchor = AuditAnchor(tenant_id=tenant_id, up_to_seq=last.seq, chain_head=last.hash)
    db.add(anchor)
    db.flush()
    return anchor
