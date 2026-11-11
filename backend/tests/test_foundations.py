import pytest
from sqlalchemy import select, update

from catalogpilot import db
from catalogpilot.catalog import cleaning
from catalogpilot.catalog import service as cs
from catalogpilot.catalog.service import Change
from catalogpilot.models import AuditEvent, Catalog, Product
from catalogpilot.security import audit, crypto, passwords


def _state(title, price=1000):
    return {"title": {"en": title}, "variants": [{"sku": title[:3], "price_minor": price, "currency": "USD"}]}


def test_tenant_isolation_reads_and_writes(tenants):
    (ta, ca), (tb, _cb) = tenants["a"], tenants["b"]
    with db.tenant_scope(ta), db.session_scope() as s:
        cs.apply_changes(s, cs.get_catalog(s, ca), [Change("k1", _state("Alpha"))], "upload", None)
    with db.tenant_scope(tb), db.session_scope() as s:
        assert s.execute(select(Product)).scalars().all() == []
        with pytest.raises(cs.NotFound):
            cs.get_catalog(s, ca)
        # a bulk update issued by another tenant must touch nothing
        s.execute(update(Product).values(brand="hacked"))
    with db.tenant_scope(ta), db.session_scope() as s:
        p = s.execute(select(Product)).scalar_one()
        assert p.brand is None
    # no tenant at all -> zero rows (fail closed)
    with db.session_scope() as s:
        assert s.execute(select(Product)).scalars().all() == []
    # cross-tenant write is blocked
    with pytest.raises(db.TenantIsolationError):
        with db.tenant_scope(tb), db.session_scope() as s:
            s.add(Catalog(tenant_id=ta, name="x"))


def test_versions_diff_and_restore(tenants):
    ta, ca = tenants["a"]
    with db.tenant_scope(ta), db.session_scope() as s:
        cat = cs.get_catalog(s, ca)
        v1, r1 = cs.apply_changes(s, cat, [Change("k1", _state("Alpha")), Change("k2", _state("Beta"))], "upload", None)
        assert [r.outcome for r in r1] == ["created", "created"] and v1.version == 1
        v2, r2 = cs.apply_changes(
            s, cat, [Change("k1", _state("Alpha 2")), Change("k2", _state("Beta")), Change("k3", _state("Gamma"))],
            "upload", None)
        assert [r.outcome for r in r2] == ["updated", "unchanged", "created"] and v2.version == 2
        v3 = cs.restore_version(s, cat, 1, None)
        assert v3.version == 3
        prods = {p.external_key: p for p in s.execute(select(Product)).scalars()}
        assert prods["k1"].title == {"en": "Alpha"}
        assert prods["k3"].status == "archived"
        assert prods["k1"].original["title"] == {"en": "Alpha"}  # FR-16
        # the same title twice gets a unique handle
        cs.apply_changes(s, cat, [Change("k9", _state("Alpha"))], "upload", None)
        handles = [p.handle for p in s.execute(select(Product)).scalars()]
        assert len(handles) == len(set(handles))


def test_optimistic_concurrency(tenants):
    ta, ca = tenants["a"]
    with db.tenant_scope(ta), db.session_scope() as s:
        cat = cs.get_catalog(s, ca)
        cs.apply_changes(s, cat, [Change("k1", _state("Alpha"))], "upload", None)
        with pytest.raises(cs.VersionConflict):
            cs.apply_changes(s, cat, [Change("k1", {"brand": "X"}, partial=True, expected_version=7)], "bulk_edit", None)


def test_audit_chain_detects_tampering(tenants):
    ta, _ = tenants["a"]
    with db.tenant_scope(ta), db.session_scope() as s:
        for i in range(5):
            audit.record(s, ta, "test.event", f"thing:{i}", {"n": i, "api_secret": "s3cr3t"})
        audit.seal_anchor(s, ta)
    with db.tenant_scope(ta), db.session_scope() as s:
        assert audit.verify_chain(s, ta).ok
        ev = s.execute(select(AuditEvent).where(AuditEvent.resource == "thing:2")).scalar_one()
        assert ev.detail["api_secret"] == "[redacted]"
        ev.detail = {"n": 99}
    with db.tenant_scope(ta), db.session_scope() as s:
        report = audit.verify_chain(s, ta)
        assert not report.ok and "modified" in report.reason
    with db.tenant_scope(ta), db.session_scope() as s:
        last = s.execute(select(AuditEvent).order_by(AuditEvent.seq.desc()).limit(1)).scalar_one()
        s.delete(last)
    with db.tenant_scope(ta), db.session_scope() as s:
        assert not audit.verify_chain(s, ta).ok


def test_envelope_encryption_binds_context(fresh):
    ct, wrapped = crypto.envelope_encrypt("t1", "c1", b"shpat_secret")
    assert b"shpat_secret" not in ct
    assert crypto.envelope_decrypt("t1", "c1", ct, wrapped) == b"shpat_secret"
    for tenant, obj in (("t2", "c1"), ("t1", "c2")):
        with pytest.raises(crypto.DecryptionError):
            crypto.envelope_decrypt(tenant, obj, ct, wrapped)


def test_passwords(fresh):
    with pytest.raises(passwords.WeakPassword):
        passwords.check_policy("short")
    with pytest.raises(passwords.WeakPassword):
        passwords.check_policy("alice-is-here-99", "alice@example.com")
    passwords.check_policy("correct horse battery staple")
    h = passwords.hash_password("correct horse battery staple")
    assert passwords.verify_password(h, "correct horse battery staple")
    assert not passwords.verify_password(h, "wrong")
    assert not passwords.verify_password(None, "anything")


@pytest.mark.parametrize("raw,cur,expected", [
    ("1,299.50", "USD", (129950, "USD")), ("1.299,50", "EUR", (129950, "EUR")), ("$19", "EUR", (1900, "USD")),
    ("19.99 EUR", "USD", (1999, "EUR")), ("12,5", "USD", (1250, "USD")), ("1,299", "USD", (129900, "USD")),
    ("500", "JPY", (500, "JPY")), (19.99, "USD", (1999, "USD")), ("0.005", "USD", (1, "USD")), ("", "USD", None),
])
def test_parse_money(raw, cur, expected):
    assert cleaning.parse_money(raw, cur) == expected


def test_money_rejects_bad_values():
    for bad in ("abc", "-5", "NaN", "1e400"):
        with pytest.raises(cleaning.MoneyError):
            cleaning.parse_money(bad, "USD")


def test_cleaning():
    assert cleaning.gtin_valid("4006381333931") and not cleaning.gtin_valid("4006381333932")
    assert cleaning.normalize_gtin("4006381333931.0") == "4006381333931"
    assert cleaning.normalize_gtin("4.006381333931E+12") == "4006381333931"
    assert cleaning.clean_text("  a‮b \n c\x00 ") == "ab c"
    html = cleaning.sanitize_html('<p onclick="x()">Hi<script>alert(1)</script><a href="javascript:x">l</a></p>')
    assert "script" not in html and "onclick" not in html and "javascript" not in html
    assert cleaning.parse_weight_grams("1.5 kg") == 1500.0
