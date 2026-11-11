"""Database engine, sessions and tenant isolation.

Isolation is enforced twice (principle P7):

1. Here, in the ORM. Every SELECT on a model that carries ``tenant_id`` gets a tenant
   filter injected automatically, and every flush verifies that written rows belong to
   the active tenant. Forgetting a WHERE clause in a route therefore cannot leak data.
2. In PostgreSQL, by forced row-level security (see ``catalogpilot/rls.py``).

If no tenant is active, tenant-owned tables return zero rows (fail closed) unless the
caller explicitly enters ``system_scope()`` for cross-tenant platform work.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker, with_loader_criteria

from .config import get_settings

_tenant: ContextVar[str | None] = ContextVar("cp_tenant", default=None)
_system: ContextVar[bool] = ContextVar("cp_system", default=False)

NO_TENANT = "00000000-0000-0000-0000-000000000000"


class Base(DeclarativeBase):
    pass


class TenantOwned:
    """Marker mixin: the model has a ``tenant_id`` column and is isolated per tenant."""

    __tenant_owned__ = True


class TenantIsolationError(RuntimeError):
    pass


def current_tenant() -> str | None:
    return _tenant.get()


@contextmanager
def tenant_scope(tenant_id: str) -> Iterator[None]:
    token = _tenant.set(tenant_id)
    sys_token = _system.set(False)
    try:
        yield
    finally:
        _system.reset(sys_token)
        _tenant.reset(token)


@contextmanager
def system_scope() -> Iterator[None]:
    """Cross-tenant access for platform jobs (lease claiming, retention, login lookup)."""
    token = _system.set(True)
    try:
        yield
    finally:
        _system.reset(token)


def _make_engine(url: str) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - driver hook
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=20)


_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine, _factory
    if _engine is None:
        _engine = _make_engine(get_settings().db_url)
        _factory = sessionmaker(_engine, expire_on_commit=False, autoflush=False)
        _install_isolation(_factory)
    return _engine


def reset_engine() -> None:
    """Used by tests to point at a fresh database."""
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None


def session_factory() -> sessionmaker[Session]:
    get_engine()
    assert _factory is not None
    return _factory


@contextmanager
def session_scope() -> Iterator[Session]:
    session = session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _install_isolation(factory: sessionmaker[Session]) -> None:
    @event.listens_for(factory, "do_orm_execute")
    def _filter_by_tenant(state):
        if _system.get() or not (state.is_select or state.is_update or state.is_delete):
            return
        tenant = _tenant.get() or NO_TENANT
        state.statement = state.statement.options(
            with_loader_criteria(
                TenantOwned,
                lambda cls: cls.tenant_id == tenant,
                include_aliases=True,
                track_closure_variables=True,
            )
        )

    @event.listens_for(factory, "before_flush")
    def _guard_writes(session, _ctx, _instances):
        if _system.get():
            return
        tenant = _tenant.get()
        for obj in list(session.new) + list(session.dirty) + list(session.deleted):
            if not getattr(obj, "__tenant_owned__", False):
                continue
            if tenant is None:
                raise TenantIsolationError(f"write to {type(obj).__name__} without a tenant context")
            if obj.tenant_id is None:
                obj.tenant_id = tenant
            elif obj.tenant_id != tenant:
                raise TenantIsolationError(
                    f"cross-tenant write to {type(obj).__name__} blocked"
                )

    @event.listens_for(factory, "after_begin")
    def _set_pg_tenant(session, _tx, connection):
        # Second layer: PostgreSQL RLS reads app.tenant_id (SET LOCAL is transaction-scoped,
        # so it is safe behind a transaction-mode pooler).
        if connection.dialect.name != "postgresql":
            return
        tenant = "" if _system.get() else (_tenant.get() or "")
        connection.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
        connection.execute(
            text("SELECT set_config('app.system', :s, true)"), {"s": "1" if _system.get() else ""}
        )


def create_all() -> None:
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(get_engine())
