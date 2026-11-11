"""Identity, organization and policy endpoints (04_API_Design 4.1)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import service as ai_service
from ..config import get_settings
from ..db import system_scope
from ..identity import service as identity
from ..models import ApiKey, Membership, Tenant
from ..security import audit
from ..security.authz import P_AUDIT_READ, P_MEMBERS, P_ORG_OWNER, P_POLICY, ROLE_PERMISSIONS
from .deps import Auth, Problem, cookie_name, current_auth, get_db, rate_limit, require, scoped, session_only

router = APIRouter()


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_max_length=10_000)


class RegisterIn(Strict):
    email: str
    password: str = Field(max_length=256)
    display_name: str = Field(default="", max_length=200)
    organization: str = Field(default="", max_length=200)


class LoginIn(Strict):
    email: str
    password: str = Field(max_length=256)


class CodeIn(Strict):
    code: str = Field(max_length=64)


class TokenIn(Strict):
    token: str = Field(max_length=200)


class StepUpIn(Strict):
    password: str | None = Field(default=None, max_length=256)
    code: str | None = Field(default=None, max_length=16)


class PasswordIn(Strict):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


def _set_cookie(response: Response, raw: str) -> None:
    settings = get_settings()
    response.set_cookie(
        cookie_name(), raw, httponly=True, secure=settings.cookie_secure, samesite="lax", path="/",
        max_age=settings.session_absolute_hours * 3600,
    )


@router.post("/auth/register", status_code=202)
def register(body: RegisterIn, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
    rate_limit(request, "register", 10, 3600)
    return identity.register(db, body.email, body.password, body.display_name, body.organization)


@router.post("/auth/verify-email")
def verify_email(body: TokenIn, request: Request, db: Session = Depends(get_db)) -> dict[str, str]:
    rate_limit(request, "verify", 20, 600)
    identity.verify_email(db, body.token)
    return {"status": "verified"}


@router.post("/auth/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, Any]:
    # Per IP and per account, so neither a single host nor a distributed attack on one
    # account gets unlimited guesses (SEC-IAM-09).
    rate_limit(request, "login-ip", 30, 300)
    rate_limit(request, "login-acct", 10, 300, key=body.email.strip().lower()[:254])
    result = identity.login(db, body.email, body.password, request.client.host if request.client else None,
                            request.headers.get("user-agent"))
    _set_cookie(response, result["session_id"])
    return {"csrf_token": result["csrf_token"], "mfa_required": result["mfa_required"]}


@router.post("/auth/mfa/verify")
def mfa_verify(body: CodeIn, request: Request, db: Session = Depends(get_db),
               loaded=Depends(session_only)) -> dict[str, str]:
    rate_limit(request, "mfa", 10, 300)
    session, user = loaded
    identity.mfa_verify(db, session, user, body.code)
    return {"status": "ok"}


@router.post("/auth/logout")
def logout(response: Response, db: Session = Depends(get_db), loaded=Depends(session_only)) -> dict[str, str]:
    identity.logout(db, loaded[0])
    response.delete_cookie(cookie_name(), path="/")
    return {"status": "signed_out"}


@router.get("/me")
def me(db: Session = Depends(get_db), loaded=Depends(session_only)) -> dict[str, Any]:
    session, user = loaded
    principal = identity.principal_for_session(db, session, user)
    with system_scope():
        memberships = db.execute(select(Membership, Tenant).join(Tenant, Tenant.id == Membership.tenant_id)
                                 .where(Membership.user_id == user.id)).all()
    return {
        "user": {"id": user.id, "email": user.email, "name": user.display_name, "mfa_enabled": user.totp_enabled},
        "csrf_token": session.csrf_token, "mfa_pending": session.mfa_pending,
        "organization_id": session.tenant_id,
        "role": principal.role if principal else None,
        "permissions": sorted(principal.permissions) if principal else [],
        "step_up_fresh": principal.step_up_fresh if principal else False,
        "organizations": [{"id": t.id, "name": t.name, "role": m.role, "frozen": t.frozen} for m, t in memberships],
    }


@router.post("/auth/step-up")
def step_up(body: StepUpIn, request: Request, db: Session = Depends(get_db), auth: Auth = Depends(current_auth)) -> dict[str, Any]:
    if auth.session is None:
        raise Problem(403, "forbidden", "Step-up needs an interactive session.")
    rate_limit(request, "stepup", 10, 300, key=auth.principal.user_id)
    identity.step_up(db, auth.session, auth.user, body.password, body.code)
    return {"status": "ok", "valid_minutes": get_settings().step_up_minutes}


@router.post("/auth/password")
def change_password(body: PasswordIn, db: Session = Depends(get_db), auth: Auth = Depends(current_auth)) -> dict[str, str]:
    if auth.session is None:
        raise Problem(403, "forbidden", "Use an interactive session to change your password.")
    identity.change_password(db, auth.session, auth.user, body.current_password, body.new_password)
    return {"status": "changed"}


@router.post("/auth/mfa/setup")
def mfa_setup(db: Session = Depends(get_db), auth: Auth = Depends(current_auth)) -> dict[str, str]:
    if auth.session is None:
        raise Problem(403, "forbidden", "Use an interactive session.")
    auth.principal.require_step_up()
    return identity.mfa_setup(db, auth.user)


@router.post("/auth/mfa/enable")
def mfa_enable(body: CodeIn, db: Session = Depends(get_db), auth: Auth = Depends(current_auth)) -> dict[str, Any]:
    if auth.session is None:
        raise Problem(403, "forbidden", "Use an interactive session.")
    return {"recovery_codes": identity.mfa_enable(db, auth.user, body.code)}


@router.get("/me/sessions")
def sessions(db: Session = Depends(get_db), auth: Auth = Depends(current_auth)) -> dict[str, Any]:
    if auth.session is None:
        raise Problem(403, "forbidden", "Use an interactive session.")
    return {"data": identity.list_sessions(db, auth.user.id, auth.session)}


@router.delete("/me/sessions/{short_id}")
def revoke_session(short_id: str, db: Session = Depends(get_db), auth: Auth = Depends(current_auth)) -> dict[str, int]:
    if auth.session is None:
        raise Problem(403, "forbidden", "Use an interactive session.")
    keep = auth.session if short_id == "others" else None
    return {"revoked": identity.revoke_sessions(db, auth.user.id, None if short_id == "others" else short_id, keep)}


@router.post("/invitations/accept")
def accept_invitation(body: TokenIn, db: Session = Depends(get_db), loaded=Depends(session_only)) -> dict[str, str]:
    session, user = loaded
    tenant_id = identity.accept_invitation(db, user, body.token)
    session.tenant_id = tenant_id
    return {"organization_id": tenant_id}


# ---------------------------------------------------------------- organization

class InviteIn(Strict):
    email: str
    role: str


class RoleIn(Strict):
    role: str


class PolicyIn(Strict):
    mfa_required: bool | None = None
    session_idle_minutes: int | None = Field(default=None, ge=5, le=30)
    external_ai_allowed: bool | None = None
    ai_enabled: bool | None = None
    ai_mode: str | None = Field(default=None, pattern="^(manual|assisted|automatic)$")
    ai_field_allowlist: list[str] | None = None
    auto_approve_threshold: float | None = Field(default=None, ge=0.5, le=1.0)
    bulk_four_eyes_threshold: int | None = Field(default=None, ge=1, le=100_000)
    bulk_four_eyes_percent: int | None = Field(default=None, ge=1, le=100)


class ApiKeyIn(Strict):
    name: str = Field(default="", max_length=120)
    scopes: list[str]
    days: int = Field(default=365, ge=1, le=365)


def _policy_view(policy) -> dict[str, Any]:
    return {c: getattr(policy, c) for c in (
        "mfa_required", "session_idle_minutes", "external_ai_allowed", "ai_enabled", "ai_mode",
        "ai_field_allowlist", "auto_approve_threshold", "bulk_four_eyes_threshold", "bulk_four_eyes_percent")}


@router.get("/org/members")
@scoped
def members(db: Session = Depends(get_db), auth: Auth = Depends(require(P_MEMBERS))) -> dict[str, Any]:
    return {"data": identity.list_members(db), "roles": sorted(ROLE_PERMISSIONS)}


@router.post("/org/invitations", status_code=201)
@scoped
def invite(body: InviteIn, db: Session = Depends(get_db), auth: Auth = Depends(require(P_MEMBERS))) -> dict[str, Any]:
    return identity.invite(db, auth.principal, body.email, body.role)


@router.put("/org/members/{user_id}/role")
@scoped
def set_role(user_id: str, body: RoleIn, db: Session = Depends(get_db), auth: Auth = Depends(require(P_MEMBERS))) -> dict[str, str]:
    auth.principal.require_step_up()  # role changes are high-risk (SEC-AUZ-09)
    identity.set_role(db, auth.principal, user_id, body.role)
    return {"status": "updated"}


@router.delete("/org/members/{user_id}")
@scoped
def remove_member(user_id: str, db: Session = Depends(get_db), auth: Auth = Depends(require(P_MEMBERS))) -> dict[str, str]:
    auth.principal.require_step_up()
    identity.remove_member(db, auth.principal, user_id)
    return {"status": "removed"}


@router.get("/org/policies")
@scoped
def get_policies(db: Session = Depends(get_db), auth: Auth = Depends(current_auth)) -> dict[str, Any]:
    return _policy_view(ai_service.get_policy(db, auth.principal.tenant_id))


@router.put("/org/policies")
@scoped
def put_policies(body: PolicyIn, db: Session = Depends(get_db), auth: Auth = Depends(require(P_POLICY))) -> dict[str, Any]:
    auth.principal.require_step_up()
    policy = ai_service.get_policy(db, auth.principal.tenant_id)
    changes = body.model_dump(exclude_none=True)
    if "ai_field_allowlist" in changes:
        bad = set(changes["ai_field_allowlist"]) - set(ai_service.POLICY_FIELD.values())
        if bad:
            raise Problem(422, "validation_failed", f"Unknown AI fields: {', '.join(sorted(bad))}.")
    for key, value in changes.items():
        setattr(policy, key, value)
    audit.record(db, auth.principal.tenant_id, "policy.changed", "org_policy", changes)
    return _policy_view(policy)


@router.post("/org/freeze")
@scoped
def freeze(db: Session = Depends(get_db), auth: Auth = Depends(require(P_ORG_OWNER))) -> dict[str, bool]:
    """Emergency stop: all publishing for this organization halts at once (SEC-IR-07).
    Freezing needs no step-up, so it is always one click away; unfreezing does."""
    tenant = db.get(Tenant, auth.principal.tenant_id)
    tenant.frozen = True
    audit.record(db, tenant.id, "org.frozen", f"tenant:{tenant.id}", {})
    return {"frozen": True}


@router.post("/org/unfreeze")
@scoped
def unfreeze(db: Session = Depends(get_db), auth: Auth = Depends(require(P_ORG_OWNER))) -> dict[str, bool]:
    auth.principal.require_step_up()
    tenant = db.get(Tenant, auth.principal.tenant_id)
    tenant.frozen = False
    audit.record(db, tenant.id, "org.unfrozen", f"tenant:{tenant.id}", {})
    return {"frozen": False}


@router.get("/org/api-keys")
@scoped
def list_api_keys(db: Session = Depends(get_db), auth: Auth = Depends(require(P_POLICY))) -> dict[str, Any]:
    keys = db.execute(select(ApiKey).order_by(ApiKey.created_at.desc())).scalars().all()
    return {"data": [{"id": k.id, "name": k.name, "prefix": k.prefix, "scopes": k.scopes,
                      "expires_at": k.expires_at.isoformat(), "revoked": k.revoked_at is not None,
                      "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None} for k in keys]}


@router.post("/org/api-keys", status_code=201)
@scoped
def create_api_key(body: ApiKeyIn, db: Session = Depends(get_db), auth: Auth = Depends(require(P_POLICY))) -> dict[str, Any]:
    auth.principal.require_step_up()
    return identity.create_api_key(db, auth.principal, body.name, body.scopes, body.days)


@router.delete("/org/api-keys/{key_id}")
@scoped
def revoke_api_key(key_id: str, db: Session = Depends(get_db), auth: Auth = Depends(require(P_POLICY))) -> dict[str, str]:
    from ..ids import utcnow

    key = db.execute(select(ApiKey).where(ApiKey.id == key_id)).scalar_one_or_none()
    if key is None:
        raise Problem(404, "not_found", "API key not found.")
    key.revoked_at = key.revoked_at or utcnow()
    audit.record(db, auth.principal.tenant_id, "api_key.revoked", f"api_key:{key.prefix}", {})
    return {"status": "revoked"}


# ---------------------------------------------------------------- audit

@router.get("/audit-events")
@scoped
def audit_events(limit: int = 100, before: int | None = None, action: str | None = None,
                 db: Session = Depends(get_db), auth: Auth = Depends(require(P_AUDIT_READ))) -> dict[str, Any]:
    from ..models import AuditEvent

    stmt = select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(max(1, min(limit, 200)))
    if before:
        stmt = stmt.where(AuditEvent.seq < before)
    if action:
        stmt = stmt.where(AuditEvent.action.like(action[:60].replace("%", "") + "%"))
    rows = db.execute(stmt).scalars().all()
    return {"data": [{"seq": e.seq, "at": e.at.isoformat(), "actor_type": e.actor_type, "actor_id": e.actor_id,
                      "action": e.action, "resource": e.resource, "detail": e.detail, "request_id": e.request_id,
                      "source_ip": e.source_ip} for e in rows],
            "next_before": rows[-1].seq if len(rows) == limit else None}


@router.get("/audit-events/verify")
@scoped
def audit_verify(db: Session = Depends(get_db), auth: Auth = Depends(require(P_AUDIT_READ))) -> dict[str, Any]:
    report = audit.verify_chain(db, auth.principal.tenant_id)
    if report.ok:
        audit.seal_anchor(db, auth.principal.tenant_id)
    return {"ok": report.ok, "events_checked": report.checked, "broken_at_seq": report.broken_at_seq,
            "reason": report.reason}
