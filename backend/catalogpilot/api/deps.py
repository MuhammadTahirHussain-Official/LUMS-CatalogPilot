"""Request plumbing: database session, authentication, CSRF, tenant scope, rate limits,
idempotency and RFC 9457 problem responses (04_API_Design 2-3)."""

from __future__ import annotations

import functools
import hashlib
import json
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

from fastapi import Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import db as dbm
from ..config import get_settings
from ..identity import service as identity
from ..ids import as_utc, utcnow
from ..models import IdempotencyKey, User, UserSession
from ..security import audit
from ..security.authz import Forbidden, Principal

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def cookie_name() -> str:
    # The __Host- prefix requires Secure, so it is used whenever cookies are Secure (production).
    return "__Host-sid" if get_settings().cookie_secure else "cp_sid"


class Problem(Exception):
    def __init__(self, status: int, code: str, detail: str, extra: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None):
        self.status, self.code, self.detail, self.extra, self.headers = status, code, detail, extra or {}, headers


def problem_response(request: Request, status: int, code: str, detail: str,
                     extra: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> JSONResponse:
    body = {
        "type": f"https://docs.catalogpilot.example/errors/{code}", "title": code.replace("_", " ").capitalize(),
        "status": status, "code": code, "detail": detail,
        "request_id": getattr(request.state, "request_id", None), **(extra or {}),
    }
    return JSONResponse(body, status_code=status, headers=headers, media_type="application/problem+json")


def get_db() -> Iterator[Session]:
    session = dbm.session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


# ---------------------------------------------------------------- rate limiting

class RateLimiter:
    """Fixed-window counter per key. In-process: one node. A multi-node deployment swaps this
    for the Redis implementation behind the same ``hit`` call."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hits: dict[str, tuple[float, int]] = {}

    def hit(self, key: str, limit: int, window: float) -> float:
        """Returns 0 if allowed, else seconds until the window resets."""
        now = time.monotonic()
        with self._lock:
            if len(self._hits) > 50_000:
                self._hits = {k: v for k, v in self._hits.items() if now - v[0] < 3600}
            start, count = self._hits.get(key, (now, 0))
            if now - start >= window:
                start, count = now, 0
            if count >= limit:
                return window - (now - start)
            self._hits[key] = (start, count + 1)
            return 0.0

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


limiter = RateLimiter()


def rate_limit(request: Request, bucket: str, limit: int, window: float = 60.0, key: str | None = None) -> None:
    wait = limiter.hit(f"{bucket}:{key or client_ip(request)}", limit, window)
    if wait > 0:
        raise Problem(429, "rate_limited", "Too many requests. Try again shortly.",
                      headers={"Retry-After": str(int(wait) + 1)})


# ---------------------------------------------------------------- authentication

class Auth:
    def __init__(self, principal: Principal, session: UserSession | None, user: User | None):
        self.principal, self.session, self.user = principal, session, user
        self.actor = audit.SYSTEM


def _check_csrf(request: Request, session: UserSession) -> None:
    """Cookie-authenticated state changes need the synchronizer token and a trusted Origin
    (SEC-WEB-06). API keys are not sent automatically by browsers and are exempt."""
    if request.method in SAFE_METHODS:
        return
    origin = request.headers.get("origin")
    if origin and origin not in get_settings().allowed_origins and origin != str(request.base_url).rstrip("/"):
        raise Problem(403, "forbidden", "Cross-site request blocked.")
    if request.headers.get("sec-fetch-site") == "cross-site":
        raise Problem(403, "forbidden", "Cross-site request blocked.")
    token = request.headers.get("x-csrf-token", "")
    if not token or not _const_eq(token, session.csrf_token):
        raise Problem(403, "csrf_failed", "Missing or invalid CSRF token.")


def _const_eq(a: str, b: str) -> bool:
    from ..security.crypto import constant_time_equal

    return constant_time_equal(a, b)


def session_only(request: Request, db: Session = Depends(get_db)) -> tuple[UserSession, User]:
    """A valid session cookie, even if MFA is still pending (used by the MFA step itself)."""
    loaded = identity.load_session(db, request.cookies.get(cookie_name()))
    if loaded is None:
        raise Problem(401, "unauthenticated", "Sign in to continue.")
    _check_csrf(request, loaded[0])
    return loaded


def current_auth(request: Request, db: Session = Depends(get_db)) -> Auth:
    """Resolves the caller. The tenant scope itself is entered by ``@scoped`` on the endpoint:
    FastAPI runs dependencies and endpoints in different worker threads, and a context
    variable set here would not be visible there."""
    header = request.headers.get("authorization", "")
    session = user = None
    if header.lower().startswith("bearer "):
        rate_limit(request, "apikey", 600)
        principal = identity.principal_for_api_key(db, header[7:].strip())
        if principal is None:
            raise Problem(401, "unauthenticated", "Invalid or expired API key.")
    else:
        loaded = identity.load_session(db, request.cookies.get(cookie_name()))
        if loaded is None:
            raise Problem(401, "unauthenticated", "Sign in to continue.")
        session, user = loaded
        _check_csrf(request, session)
        if session.mfa_pending:
            raise Problem(403, "mfa_required", "Enter your verification code to continue.")
        principal = identity.principal_for_session(db, session, user)
        if principal is None:
            raise Problem(403, "forbidden", "You are not a member of an organization.")
    rate_limit(request, "principal", 1200, key=principal.actor_id)
    auth = Auth(principal, session, user)
    auth.actor = audit.Actor("api_key" if principal.kind == "api_key" else "user", principal.actor_id,
                             getattr(request.state, "request_id", None), client_ip(request))
    return auth


def scoped(fn):
    """Runs an endpoint inside its caller's tenant scope and commits there.

    Everything the endpoint does is confined to ``auth.principal.tenant_id`` by the ORM
    isolation layer (and by row-level security on PostgreSQL, because the transaction that
    authenticated the caller is ended first and a new one begins under the tenant)."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        auth: Auth = kwargs["auth"]
        db: Session = kwargs["db"]
        db.commit()
        token = audit.set_actor(auth.actor)
        try:
            with dbm.tenant_scope(auth.principal.tenant_id):
                result = fn(*args, **kwargs)
                db.commit()
                return result
        except Exception:
            db.rollback()
            raise
        finally:
            audit.reset_actor(token)

    return wrapper


def require(permission: str):
    def dependency(auth: Auth = Depends(current_auth)) -> Auth:
        if not auth.principal.can(permission):
            # Function-level authorization (SEC-AUZ-04). Uniform message; details stay server-side.
            raise Forbidden()
        return auth

    return dependency


# ---------------------------------------------------------------- idempotency

def idempotent(request: Request, db: Session, auth: Auth, body: Any) -> JSONResponse | None:
    """Returns the stored response for a replayed Idempotency-Key, or None to proceed.
    The same key with a different body is rejected (SEC-API-06)."""
    key = request.headers.get("idempotency-key")
    if not key:
        return None
    if len(key) > 200:
        raise Problem(422, "validation_failed", "Idempotency-Key is too long.")
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode() + request.url.path.encode()).hexdigest()
    row = db.execute(select(IdempotencyKey).where(IdempotencyKey.scope == "api", IdempotencyKey.key == key)).scalar_one_or_none()
    if row is not None and as_utc(row.expires_at) > utcnow():
        if row.request_hash != digest:
            raise Problem(422, "idempotency_key_reuse", "This Idempotency-Key was used with a different request.")
        if row.response is None:
            raise Problem(409, "conflict", "The original request is still being processed.")
        return JSONResponse(row.response, status_code=row.status_code or 200, headers={"Idempotent-Replay": "true"})
    if row is None:
        row = IdempotencyKey(tenant_id=auth.principal.tenant_id, scope="api", key=key)
        db.add(row)
    row.request_hash, row.response, row.status_code = digest, None, None
    row.created_at, row.expires_at = utcnow(), utcnow() + timedelta(hours=24)
    db.flush()
    request.state.idempotency_row = row
    return None


def remember(request: Request, payload: Any, status: int = 200) -> Any:
    row = getattr(request.state, "idempotency_row", None)
    if row is not None:
        row.response, row.status_code = payload, status
    return payload
