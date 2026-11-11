"""PostgreSQL row-level security: the second, database-enforced layer of tenant isolation.

    python -m catalogpilot.rls            # print the SQL
    python -m catalogpilot.rls --apply    # apply it to CP_DATABASE_URL

Every table that carries ``tenant_id`` gets RLS enabled AND forced, with one policy:
a row is visible/writable only when its tenant equals ``app.tenant_id`` for the current
transaction. With no tenant set, zero rows match (fail closed).

STATUS: generated from the models and reviewed, but not yet executed against a real
PostgreSQL server (none was available where this was built). Run the tenant-isolation
tests against PostgreSQL before relying on it.

Known limitation: cross-tenant platform work (lease claiming, login lookup) currently sets
``app.system = '1'`` from the application role. The specification calls for narrowly scoped
SECURITY DEFINER functions owned by a separate role instead, so that a compromised
application session cannot switch the flag on. That hardening is still to do.
"""

from __future__ import annotations

import sys

from sqlalchemy import text

from . import models  # noqa: F401
from .db import Base, get_engine

POLICY = (
    "current_setting('app.system', true) = '1' "
    "OR tenant_id = nullif(current_setting('app.tenant_id', true), '')"
)


def tenant_tables() -> list[str]:
    return sorted(t.name for t in Base.metadata.sorted_tables if "tenant_id" in t.c and t.name != "tenants")


def statements() -> list[str]:
    out = []
    for table in tenant_tables():
        out += [
            f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY',
            f'ALTER TABLE "{table}" FORCE ROW LEVEL SECURITY',
            f'DROP POLICY IF EXISTS tenant_isolation ON "{table}"',
            f'CREATE POLICY tenant_isolation ON "{table}" USING ({POLICY}) WITH CHECK ({POLICY})',
        ]
    # The audit trail is append-only at the database level as well.
    out += ['REVOKE UPDATE, DELETE ON "audit_events" FROM PUBLIC']
    return out


def missing_policies() -> list[str]:
    """CI check (DB-10): tenant-owned tables without an enabled and forced policy."""
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        return []
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT c.relname FROM pg_class c WHERE c.relkind = 'r' AND c.relrowsecurity AND c.relforcerowsecurity"
        )).scalars().all()
    return [t for t in tenant_tables() if t not in set(rows)]


def main() -> int:
    if "--apply" not in sys.argv:
        print(";\n".join(statements()) + ";")
        return 0
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        print("CP_DATABASE_URL is not PostgreSQL; nothing to apply.")
        return 1
    with engine.begin() as conn:
        for stmt in statements():
            conn.execute(text(stmt))
    print(f"row-level security applied to {len(tenant_tables())} tables")
    return 0


if __name__ == "__main__":
    sys.exit(main())
