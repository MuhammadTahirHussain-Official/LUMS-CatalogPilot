import os
import tempfile
from pathlib import Path

import pytest

_TMP = tempfile.mkdtemp(prefix="cp-test-")
os.environ["CP_ENV"] = "test"
os.environ["CP_DATA_DIR"] = _TMP


@pytest.fixture()
def fresh(tmp_path, monkeypatch):
    """A clean database and data directory for each test."""
    from catalogpilot import config, db
    from catalogpilot.security import crypto

    monkeypatch.setenv("CP_DATA_DIR", str(tmp_path))
    config.get_settings.cache_clear()
    db.reset_engine()
    crypto.reset_key_provider()
    db.create_all()
    yield Path(tmp_path)
    db.reset_engine()
    config.get_settings.cache_clear()
    crypto.reset_key_provider()


@pytest.fixture()
def tenants(fresh):
    from catalogpilot import db
    from catalogpilot.models import Catalog, Tenant

    out = {}
    with db.system_scope(), db.session_scope() as s:
        for name in ("a", "b"):
            t = Tenant(name=f"Tenant {name}")
            s.add(t)
            s.flush()
            c = Catalog(tenant_id=t.id, name=f"Catalog {name}")
            s.add(c)
            s.flush()
            out[name] = (t.id, c.id)
    return out
