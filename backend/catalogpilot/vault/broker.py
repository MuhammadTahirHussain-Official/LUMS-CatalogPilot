"""Credential Vault and Broker (CRED-01..19, SEC-VLT-02).

This module is the only place in the codebase that decrypts third-party credentials.
There is deliberately no "get secret" call: plaintext is released only

  * to validate a credential at connect time (never returned to the caller), or
  * as a lease bound to one job item that is currently PUSHING under the caller's
    fencing token, for one tenant, one channel and one operation.

Every release is written to ``credential_usage``. Deployed as a separate service in
production (own identity, own network segment); the contract is the same.
"""

from __future__ import annotations

import json
import re
from datetime import timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..connectors import get_connector
from ..connectors.sdk import AuthError, CallContext, ConnectorError
from ..ids import as_utc, utcnow, uuid7
from ..models import Channel, Credential, CredentialUsage, JobItem
from ..security import audit, crypto
from ..security.netguard import UnsafeUrl, check_public_https_url

# Things a seller might paste by mistake. We refuse them and explain why (CRED-03).
_FORBIDDEN_FIELDS = {"password", "login_password", "account_password", "otp", "mfa_secret", "totp_secret",
                     "backup_codes", "cookie", "session", "card_number", "cvv"}


class VaultError(Exception):
    def __init__(self, code: str, detail: str, status: int = 422):
        self.code, self.detail, self.status = code, detail, status


class LeaseDenied(Exception):
    pass


def _hint(secret: dict[str, Any]) -> str:
    longest = max((str(v) for v in secret.values()), key=len, default="")
    return "…" + longest[-4:] if len(longest) >= 12 else "…"


def validate_channel_url(base_url: str | None, required: bool) -> str | None:
    if not base_url:
        if required:
            raise VaultError("validation_failed", "This platform needs your store URL.")
        return None
    try:
        return check_public_https_url(base_url)
    except UnsafeUrl as exc:
        raise VaultError("validation_failed", str(exc)) from exc


def store_credential(db: Session, channel: Channel, kind: str, secret: dict[str, Any], user_id: str) -> Credential:
    """Write-only intake: validate against the platform, encrypt, return metadata only."""
    connector = get_connector(channel.platform)
    caps = connector.capabilities()
    if kind not in caps.credential_kinds:
        raise VaultError("validation_failed", f"{connector.label} accepts: {', '.join(caps.credential_kinds)}.")
    bad = {k for k in secret if k.lower() in _FORBIDDEN_FIELDS}
    if bad:
        raise VaultError(
            "credential_type_refused",
            "We never accept account passwords, MFA codes, cookies or card data. "
            "Create an API key or app token for this integration instead.",
        )
    unknown = set(secret) - set(caps.credential_fields)
    missing = [f for f in caps.credential_fields if not str(secret.get(f, "")).strip()]
    if unknown or missing:
        raise VaultError("validation_failed", f"Expected exactly these fields: {', '.join(caps.credential_fields)}.")
    for value in secret.values():
        if not isinstance(value, str) or len(value) > 8192 or re.search(r"[\x00-\x1f]", value):
            raise VaultError("validation_failed", "Credential values must be single-line text.")
    base_url = validate_channel_url(channel.base_url, caps.needs_base_url)

    ctx = CallContext(channel.tenant_id, channel.id, base_url, channel.config or {}, dict(secret), task_id="validate")
    try:
        scopes = connector.test_connection(ctx)  # CRED-04: must work before it is saved
    except AuthError as exc:
        raise VaultError("credential_rejected", exc.message) from exc
    except ConnectorError as exc:
        raise VaultError("dependency_unavailable", f"Could not reach the platform to verify: {exc.message}", 503) from exc

    if channel.credential_id:
        _shred(db, channel.credential_id, "replaced")
    cred_id = uuid7()
    ciphertext, wrapped = crypto.envelope_encrypt(channel.tenant_id, cred_id, json.dumps(secret).encode())
    cred = Credential(
        id=cred_id, tenant_id=channel.tenant_id, platform=channel.platform, kind=kind, ciphertext=ciphertext,
        wrapped_data_key=wrapped, hint=_hint(secret), scopes=scopes, created_by=user_id,
        rotate_due_at=utcnow() + timedelta(days=180),
    )
    db.add(cred)
    channel.credential_id = cred_id
    channel.health = "ok"
    channel.paused_reason = None
    audit.record(db, channel.tenant_id, "credential.stored", f"channel:{channel.id}",
                 {"credential_id": cred_id, "kind": kind, "scopes": scopes})
    db.flush()
    return cred


def metadata(cred: Credential) -> dict[str, Any]:
    """The only view of a credential that any API may return (CRED-11)."""
    return {
        "id": cred.id, "platform": cred.platform, "kind": cred.kind, "hint": cred.hint, "scopes": cred.scopes,
        "created_at": cred.created_at.isoformat(), "created_by": cred.created_by,
        "last_used_at": cred.last_used_at.isoformat() if cred.last_used_at else None,
        "expires_at": cred.expires_at.isoformat() if cred.expires_at else None,
        "rotate_due_at": cred.rotate_due_at.isoformat() if cred.rotate_due_at else None,
        "revoked": cred.revoked_at is not None,
    }


def _usage(db: Session, cred: Credential, task_id: str, handler_id: str, operation: str, result: str) -> None:
    db.add(CredentialUsage(tenant_id=cred.tenant_id, credential_id=cred.id, task_id=task_id,
                           handler_id=handler_id, operation=operation, result=result))


def lease_for_item(db: Session, item: JobItem, handler_id: str, fencing_token: int, operation: str) -> CallContext:
    """Release a credential for exactly one leased job item."""
    current = db.execute(
        select(JobItem.state, JobItem.lease_fencing_token, JobItem.lease_owner, JobItem.lease_until)
        .where(JobItem.id == item.id)
    ).one_or_none()
    channel = db.execute(select(Channel).where(Channel.id == item.channel_id)).scalar_one_or_none()
    cred = None
    if channel is not None and channel.credential_id:
        cred = db.execute(select(Credential).where(Credential.id == channel.credential_id)).scalar_one_or_none()

    def deny(reason: str):
        if cred is not None:
            _usage(db, cred, item.id, handler_id, operation, f"denied:{reason}")
            db.flush()
        raise LeaseDenied(reason)

    if operation not in ("read", "write", "delete"):
        deny("bad_operation")
    if current is None or channel is None:
        deny("unknown_task")
    state, token, owner, until = current
    if state != "PUSHING" or token != fencing_token or owner != handler_id:
        deny("stale_lease")  # a zombie handler cannot obtain the credential
    if until is None or as_utc(until) <= utcnow():
        deny("lease_expired")
    if cred is None and not get_connector(channel.platform).capabilities().credential_kinds:
        # Destinations such as a feed file need no secret; the lease checks above still apply.
        return CallContext(channel.tenant_id, channel.id, channel.base_url, channel.config or {}, {},
                           task_id=item.id, fencing_token=fencing_token)
    if cred is None or cred.revoked_at is not None or cred.shredded_at is not None:
        raise AuthError("This channel has no usable credential. Reconnect it.", "Reconnect the channel.")
    try:
        secret = json.loads(crypto.envelope_decrypt(cred.tenant_id, cred.id, cred.ciphertext, cred.wrapped_data_key))
    except crypto.DecryptionError:
        deny("decrypt_failed")
    cred.last_used_at = utcnow()
    _usage(db, cred, item.id, handler_id, operation, "granted")
    db.flush()
    _ = get_settings().credential_lease_seconds  # lease lifetime is bounded by the item lease itself
    return CallContext(channel.tenant_id, channel.id, channel.base_url, channel.config or {}, secret,
                       task_id=item.id, fencing_token=fencing_token)


def _shred(db: Session, credential_id: str, reason: str) -> None:
    cred = db.execute(select(Credential).where(Credential.id == credential_id)).scalar_one_or_none()
    if cred is None or cred.shredded_at is not None:
        return
    # Crypto-shred: without the wrapped data key the ciphertext (including any copy in a
    # backup) is unrecoverable. Both columns are overwritten.
    cred.wrapped_data_key = b""
    cred.ciphertext = b""
    cred.revoked_at = cred.revoked_at or utcnow()
    cred.shredded_at = utcnow()
    audit.record(db, cred.tenant_id, "credential.shredded", f"credential:{cred.id}", {"reason": reason})


def disconnect(db: Session, channel: Channel) -> None:
    """CRED-12: revoke, shred, and pause the channel so in-flight items stop."""
    if channel.credential_id:
        _shred(db, channel.credential_id, "disconnect")
    channel.credential_id = None
    channel.health = "paused"
    channel.paused_reason = "disconnected"
    audit.record(db, channel.tenant_id, "channel.disconnected", f"channel:{channel.id}", {})
    db.flush()


def host_of(url: str | None) -> str:
    return urlsplit(url).hostname or "" if url else ""
