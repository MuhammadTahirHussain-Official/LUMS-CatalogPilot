"""Accounts, sessions, MFA, step-up, API keys and membership (FR-01..06, SEC-IAM, SEC-SES)."""

from __future__ import annotations

import re
import secrets
from datetime import timedelta
from typing import Any

import pyotp
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import system_scope, tenant_scope
from ..ids import as_utc, utcnow
from ..models import ApiKey, Invitation, Membership, OrgPolicy, RecoveryCode, Tenant, User, UserSession
from ..security import audit, crypto, passwords
from ..security.authz import ROLE_PERMISSIONS, Principal, can_grant
from . import mailer

EMAIL = re.compile(r"^[^@\s]{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9-]{1,63})+$")
API_KEY_PREFIX = "cpk_live_"
GENERIC_LOGIN_ERROR = "Email or password is incorrect."


class AuthError(Exception):
    def __init__(self, code: str, detail: str, status: int = 401, extra: dict[str, Any] | None = None):
        self.code, self.detail, self.status, self.extra = code, detail, status, extra or {}


def normalize_email(email: str) -> str:
    email = (email or "").strip().lower()
    if len(email) > 254 or not EMAIL.match(email):
        raise AuthError("validation_failed", "Enter a valid email address.", 422)
    return email


# ---------------------------------------------------------------- registration

def register(db: Session, email: str, password: str, display_name: str, org_name: str) -> dict[str, Any]:
    """Creates the user, an organization and an Owner membership. The response is the same
    whether or not the address was already registered (SEC-IAM-10)."""
    email = normalize_email(email)
    passwords.check_policy(password, email)
    org_name = (org_name or "").strip()[:200] or "My organization"
    uniform = {"status": "verification_sent"}
    with system_scope():
        existing = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        if existing is not None:
            passwords.hash_password(password)  # equalize timing with the new-account path
            mailer.send(email, "Sign-in attempt", "Someone tried to register with your address. "
                        "If it was you, sign in or reset your password.")
            return uniform
        token = crypto.random_token()
        user = User(email=email, password_hash=passwords.hash_password(password),
                    display_name=(display_name or "").strip()[:200] or None,
                    verify_token_hash=crypto.keyed_hash("email-verify", token),
                    verify_expires_at=utcnow() + timedelta(hours=24))
        tenant = Tenant(name=org_name)
        db.add_all([user, tenant])
        db.flush()
        db.add(Membership(tenant_id=tenant.id, user_id=user.id, role="owner"))
        db.add(OrgPolicy(tenant_id=tenant.id))
        audit.record(db, tenant.id, "user.registered", f"user:{user.id}", {}, audit.Actor("user", user.id))
        db.flush()
    mailer.send(email, "Verify your email", f"Use this code to verify your address (valid 24 hours):\n\n{token}\n")
    if not get_settings().is_production:
        uniform = {**uniform, "dev_verification_token": token}  # development convenience only
    return uniform


def verify_email(db: Session, token: str) -> None:
    with system_scope():
        user = db.execute(select(User).where(
            User.verify_token_hash == crypto.keyed_hash("email-verify", token or ""))).scalar_one_or_none()
        if user is None or user.verify_expires_at is None or as_utc(user.verify_expires_at) < utcnow():
            raise AuthError("invalid_token", "This verification link is invalid or has expired.", 400)
        user.email_verified = True
        user.verify_token_hash = None  # single use
        user.verify_expires_at = None
        db.flush()


# ---------------------------------------------------------------- sessions

def _new_session(db: Session, user: User, tenant_id: str | None, ip: str | None, ua: str | None,
                 mfa_pending: bool) -> tuple[str, UserSession]:
    settings = get_settings()
    raw = crypto.random_token(32)
    now = utcnow()
    session = UserSession(
        id_hash=crypto.keyed_hash("session", raw), user_id=user.id, tenant_id=tenant_id,
        csrf_token=secrets.token_urlsafe(32), mfa_pending=mfa_pending, created_at=now, last_seen_at=now,
        expires_at=now + timedelta(hours=settings.session_absolute_hours), ip=(ip or "")[:64] or None,
        user_agent=(ua or "")[:400] or None,
        step_up_at=None if mfa_pending else now,  # a fresh full login counts as recent authentication
    )
    db.add(session)
    db.flush()
    return raw, session


def login(db: Session, email: str, password: str, ip: str | None, ua: str | None) -> dict[str, Any]:
    settings = get_settings()
    try:
        email = normalize_email(email)
    except AuthError:
        passwords.verify_password(None, password or "")
        raise AuthError("unauthenticated", GENERIC_LOGIN_ERROR) from None
    with system_scope():
        user = db.execute(select(User).where(User.email == email)).scalar_one_or_none()
        now = utcnow()
        locked = user is not None and user.locked_until is not None and as_utc(user.locked_until) > now
        ok = passwords.verify_password(user.password_hash if user else None, password or "")
        if user is None or user.disabled_at is not None or locked or not ok:
            if user is not None and not locked and not ok:
                user.failed_logins += 1
                if user.failed_logins >= settings.login_max_failures:
                    # Temporary lock: an attacker cannot lock the owner out indefinitely.
                    user.locked_until = now + timedelta(minutes=settings.login_lock_minutes)
                    user.failed_logins = 0
                db.commit()
            raise AuthError("unauthenticated", GENERIC_LOGIN_ERROR)
        if not user.email_verified:
            raise AuthError("email_not_verified", "Verify your email address before signing in.", 403)
        user.failed_logins = 0
        user.locked_until = None
        user.last_login_at = now
        if passwords.needs_rehash(user.password_hash):
            user.password_hash = passwords.hash_password(password)
        membership = db.execute(select(Membership).where(Membership.user_id == user.id)
                                .order_by(Membership.created_at)).scalars().first()
        tenant_id = membership.tenant_id if membership else None
        raw, session = _new_session(db, user, tenant_id, ip, ua, mfa_pending=user.totp_enabled)
        if tenant_id:
            audit.record(db, tenant_id, "user.login", f"user:{user.id}", {"mfa_pending": user.totp_enabled},
                         audit.Actor("user", user.id, ip=ip))
        return {"session_id": raw, "csrf_token": session.csrf_token, "mfa_required": user.totp_enabled}


def load_session(db: Session, raw: str | None, touch: bool = True) -> tuple[UserSession, User] | None:
    if not raw:
        return None
    settings = get_settings()
    with system_scope():
        session = db.get(UserSession, crypto.keyed_hash("session", raw))
        if session is None or session.revoked_at is not None:
            return None
        now = utcnow()
        idle = settings.session_idle_minutes
        if session.tenant_id:
            policy = db.get(OrgPolicy, session.tenant_id)
            if policy:
                idle = min(idle, policy.session_idle_minutes)
        if as_utc(session.expires_at) <= now or as_utc(session.last_seen_at) + timedelta(minutes=idle) <= now:
            session.revoked_at = now
            db.commit()
            return None
        user = db.get(User, session.user_id)
        if user is None or user.disabled_at is not None:
            return None
        if touch and (now - as_utc(session.last_seen_at)).total_seconds() > 60:
            session.last_seen_at = now
        return session, user


def principal_for_session(db: Session, session: UserSession, user: User) -> Principal | None:
    if session.mfa_pending or session.tenant_id is None:
        return None
    with system_scope():
        membership = db.get(Membership, (session.tenant_id, user.id))
        tenant = db.get(Tenant, session.tenant_id)
    if membership is None or tenant is None or tenant.status != "active":
        return None  # removed members lose access at once (SEC-SES-04)
    fresh = session.step_up_at is not None and \
        utcnow() - as_utc(session.step_up_at) <= timedelta(minutes=get_settings().step_up_minutes)
    return Principal(tenant.id, user.id, membership.role, step_up_fresh=fresh)


def logout(db: Session, session: UserSession) -> None:
    session.revoked_at = utcnow()
    db.flush()


def list_sessions(db: Session, user_id: str, current: UserSession) -> list[dict[str, Any]]:
    with system_scope():
        rows = db.execute(select(UserSession).where(UserSession.user_id == user_id, UserSession.revoked_at.is_(None))
                          .order_by(UserSession.last_seen_at.desc())).scalars().all()
    now = utcnow()
    return [{
        "id": s.id_hash.hex()[:16], "current": s.id_hash == current.id_hash, "ip": s.ip, "user_agent": s.user_agent,
        "created_at": s.created_at.isoformat(), "last_seen_at": s.last_seen_at.isoformat(),
    } for s in rows if as_utc(s.expires_at) > now]


def revoke_sessions(db: Session, user_id: str, short_id: str | None = None, keep: UserSession | None = None) -> int:
    with system_scope():
        rows = db.execute(select(UserSession).where(UserSession.user_id == user_id,
                                                    UserSession.revoked_at.is_(None))).scalars().all()
        count = 0
        for s in rows:
            if keep is not None and s.id_hash == keep.id_hash:
                continue
            if short_id is None or s.id_hash.hex()[:16] == short_id:
                s.revoked_at = utcnow()
                count += 1
        db.flush()
    return count


# ---------------------------------------------------------------- MFA and step-up

def _totp_key() -> bytes:
    return crypto.key_provider().platform_key("totp-secrets")


def _totp_secret(user: User) -> str:
    return crypto.open_sealed(_totp_key(), user.totp_secret_enc, user.id.encode()).decode()


def _check_totp(db: Session, user: User, code: str) -> bool:
    """Accepts the current or adjacent window; each code works once (SEC-IAM-07)."""
    if not user.totp_secret_enc or not re.fullmatch(r"\d{6}", code or ""):
        return False
    totp = pyotp.TOTP(_totp_secret(user))
    now = utcnow()
    counter_now = int(now.timestamp()) // 30
    for offset in (0, -1, 1):
        counter = counter_now + offset
        if crypto.constant_time_equal(totp.at(counter * 30), code):
            if user.totp_last_counter is not None and counter <= user.totp_last_counter:
                return False  # replay
            user.totp_last_counter = counter
            db.flush()
            return True
    return False


def _use_recovery_code(db: Session, user: User, code: str) -> bool:
    digest = crypto.keyed_hash("recovery-code", (code or "").strip().lower())
    with system_scope():
        row = db.execute(select(RecoveryCode).where(RecoveryCode.user_id == user.id, RecoveryCode.code_hash == digest,
                                                    RecoveryCode.used_at.is_(None))).scalar_one_or_none()
        if row is None:
            return False
        row.used_at = utcnow()
        db.flush()
    mailer.send(user.email, "A recovery code was used", "A recovery code was just used to sign in to your account.")
    return True


def mfa_setup(db: Session, user: User) -> dict[str, str]:
    if user.totp_enabled:
        raise AuthError("conflict", "Two-step verification is already on.", 409)
    secret = pyotp.random_base32()
    user.totp_secret_enc = crypto.seal(_totp_key(), secret.encode(), user.id.encode())
    db.flush()
    return {"secret": secret, "otpauth_url": pyotp.TOTP(secret).provisioning_uri(user.email, issuer_name="CatalogPilot")}


def mfa_enable(db: Session, user: User, code: str) -> list[str]:
    if user.totp_enabled:
        raise AuthError("conflict", "Two-step verification is already on.", 409)
    if not _check_totp(db, user, code):
        raise AuthError("invalid_code", "That code is not correct.", 400)
    user.totp_enabled = True
    codes = [f"{secrets.token_hex(4)}-{secrets.token_hex(4)}" for _ in range(10)]
    with system_scope():
        db.execute(RecoveryCode.__table__.delete().where(RecoveryCode.user_id == user.id))
        for c in codes:
            db.add(RecoveryCode(user_id=user.id, code_hash=crypto.keyed_hash("recovery-code", c)))
        db.flush()
    return codes  # shown once


def mfa_verify(db: Session, session: UserSession, user: User, code: str) -> None:
    if not session.mfa_pending:
        return
    if not (_check_totp(db, user, code) or _use_recovery_code(db, user, code)):
        user.failed_logins += 1
        if user.failed_logins >= get_settings().login_max_failures:
            session.revoked_at = utcnow()
            user.failed_logins = 0
        db.commit()
        raise AuthError("invalid_code", "That code is not correct.", 400)
    user.failed_logins = 0
    session.mfa_pending = False
    session.step_up_at = utcnow()
    db.flush()


def step_up(db: Session, session: UserSession, user: User, password: str | None, code: str | None) -> None:
    """Re-authentication for sensitive actions (CRED-02, SEC-IAM-12). With MFA on, a code is
    required; a password alone is not enough."""
    ok = _check_totp(db, user, code or "") if user.totp_enabled else \
        bool(password) and passwords.verify_password(user.password_hash, password or "")
    if not ok:
        raise AuthError("invalid_credentials", "Could not confirm it is you.", 403)
    session.step_up_at = utcnow()
    db.flush()


def change_password(db: Session, session: UserSession, user: User, current: str, new: str) -> None:
    if not passwords.verify_password(user.password_hash, current or ""):
        raise AuthError("invalid_credentials", "Your current password is not correct.", 403)
    passwords.check_policy(new, user.email)
    user.password_hash = passwords.hash_password(new)
    revoke_sessions(db, user.id, keep=session)  # every other session ends (SEC-SES-04)
    mailer.send(user.email, "Your password was changed", "If this was not you, contact support immediately.")


# ---------------------------------------------------------------- API keys

def create_api_key(db: Session, principal: Principal, name: str, scopes: list[str], days: int = 365) -> dict[str, Any]:
    allowed = ROLE_PERMISSIONS[principal.role]
    bad = [s for s in scopes if s not in allowed]
    if not scopes or bad:
        raise AuthError("validation_failed", f"Unknown or disallowed scopes: {', '.join(bad) or 'none given'}.", 422)
    public = secrets.token_hex(6)
    secret = secrets.token_urlsafe(32)
    key = ApiKey(tenant_id=principal.tenant_id, name=(name or "")[:120], prefix=f"{API_KEY_PREFIX}{public}",
                 secret_hash=crypto.keyed_hash("api-key", secret), scopes=sorted(set(scopes)),
                 created_by=principal.user_id, expires_at=utcnow() + timedelta(days=max(1, min(days, 365))))
    db.add(key)
    audit.record(db, principal.tenant_id, "api_key.created", f"api_key:{key.prefix}", {"scopes": key.scopes})
    db.flush()
    return {"id": key.id, "prefix": key.prefix, "key": f"{key.prefix}.{secret}", "scopes": key.scopes,
            "expires_at": key.expires_at.isoformat()}


def principal_for_api_key(db: Session, presented: str) -> Principal | None:
    prefix, _, secret = (presented or "").partition(".")
    if not prefix.startswith(API_KEY_PREFIX) or not secret:
        return None
    with system_scope():
        key = db.execute(select(ApiKey).where(ApiKey.prefix == prefix)).scalar_one_or_none()
        digest = crypto.keyed_hash("api-key", secret)
        if key is None:
            crypto.constant_time_equal(digest, digest)
            return None
        if not crypto.constant_time_equal(key.secret_hash, digest) or key.revoked_at is not None \
                or as_utc(key.expires_at) <= utcnow():
            return None
        membership = db.get(Membership, (key.tenant_id, key.created_by))
        tenant = db.get(Tenant, key.tenant_id)
        if membership is None or tenant is None or tenant.status != "active":
            return None  # a key dies with its creator's membership (SEC-AUZ-07)
        if key.last_used_at is None or (utcnow() - as_utc(key.last_used_at)).total_seconds() > 300:
            key.last_used_at = utcnow()
            db.flush()  # written here, inside the platform scope that looked the key up
        return Principal(key.tenant_id, key.created_by, membership.role, kind="api_key",
                         actor_id=key.prefix, scopes=set(key.scopes))


# ---------------------------------------------------------------- members

def list_members(db: Session) -> list[dict[str, Any]]:
    rows = db.execute(select(Membership)).scalars().all()
    with system_scope():
        users = {u.id: u for u in db.execute(select(User).where(User.id.in_([m.user_id for m in rows]))).scalars()}
    return [{"user_id": m.user_id, "email": users[m.user_id].email, "name": users[m.user_id].display_name,
             "role": m.role, "mfa": users[m.user_id].totp_enabled} for m in rows]


def invite(db: Session, principal: Principal, email: str, role: str) -> dict[str, Any]:
    email = normalize_email(email)
    if not can_grant(principal.role, role):
        raise AuthError("forbidden", "You cannot grant a role higher than your own.", 403)
    token = crypto.random_token()
    inv = Invitation(tenant_id=principal.tenant_id, email=email, role=role, invited_by=principal.user_id,
                     token_hash=crypto.keyed_hash("invite", token), expires_at=utcnow() + timedelta(days=7))
    db.add(inv)
    audit.record(db, principal.tenant_id, "member.invited", f"invitation:{inv.id}", {"role": role})
    db.flush()
    mailer.send(email, "You have been invited to CatalogPilot", f"Invitation code (valid 7 days):\n\n{token}\n")
    out = {"id": inv.id, "email": email, "role": role}
    if not get_settings().is_production:
        out["dev_invitation_token"] = token
    return out


def accept_invitation(db: Session, user: User, token: str) -> str:
    """The invitation is single-use, expiring and bound to the invited address (SEC-IAM-16)."""
    with system_scope():
        inv = db.execute(select(Invitation).where(
            Invitation.token_hash == crypto.keyed_hash("invite", token or ""))).scalar_one_or_none()
        if inv is None or inv.used_at is not None or as_utc(inv.expires_at) < utcnow() or inv.email != user.email:
            raise AuthError("invalid_token", "This invitation is invalid, expired or for another address.", 400)
        inv.used_at = utcnow()
        if db.get(Membership, (inv.tenant_id, user.id)) is None:
            db.add(Membership(tenant_id=inv.tenant_id, user_id=user.id, role=inv.role))
        audit.record(db, inv.tenant_id, "member.joined", f"user:{user.id}", {"role": inv.role},
                     audit.Actor("user", user.id))
        db.flush()
        return inv.tenant_id


def _owners(db: Session) -> int:
    return db.execute(select(func.count()).select_from(Membership).where(Membership.role == "owner")).scalar_one()


def set_role(db: Session, principal: Principal, user_id: str, role: str) -> None:
    member = db.execute(select(Membership).where(Membership.user_id == user_id)).scalar_one_or_none()
    if member is None:
        raise AuthError("not_found", "Member not found.", 404)
    if not can_grant(principal.role, role) or not can_grant(principal.role, member.role):
        raise AuthError("forbidden", "You cannot change a role above your own.", 403)
    if member.role == "owner" and role != "owner" and _owners(db) <= 1:
        raise AuthError("conflict", "An organization must keep at least one owner.", 409)
    member.role = role
    audit.record(db, principal.tenant_id, "member.role_changed", f"user:{user_id}", {"role": role})


def remove_member(db: Session, principal: Principal, user_id: str) -> None:
    member = db.execute(select(Membership).where(Membership.user_id == user_id)).scalar_one_or_none()
    if member is None:
        raise AuthError("not_found", "Member not found.", 404)
    if not can_grant(principal.role, member.role):
        raise AuthError("forbidden", "You cannot remove someone with a higher role.", 403)
    if member.role == "owner" and _owners(db) <= 1:
        raise AuthError("conflict", "The last owner cannot be removed.", 409)
    tenant_id = member.tenant_id
    db.delete(member)
    with system_scope():
        # Their sessions in this organization end now; channels they connected keep working
        # because credentials belong to the organization (CRED-18).
        db.execute(update(UserSession).where(UserSession.user_id == user_id, UserSession.tenant_id == tenant_id,
                                             UserSession.revoked_at.is_(None)).values(revoked_at=utcnow()))
        db.execute(update(ApiKey).where(ApiKey.created_by == user_id, ApiKey.tenant_id == tenant_id,
                                        ApiKey.revoked_at.is_(None)).values(revoked_at=utcnow()))
    audit.record(db, tenant_id, "member.removed", f"user:{user_id}", {})


def switch_tenant(db: Session, session: UserSession, user: User, tenant_id: str) -> None:
    with system_scope(), tenant_scope(tenant_id):
        pass
    with system_scope():
        if db.get(Membership, (tenant_id, user.id)) is None:
            raise AuthError("not_found", "Organization not found.", 404)
    session.tenant_id = tenant_id
    db.flush()
