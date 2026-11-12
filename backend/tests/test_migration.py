"""Store-to-store migration under the worst cases: 1,500 products, handler crashes, the
server going away mid-import, outages, throttling, expired page markers, a revoked key,
hostile source data. The promise under test: nothing lost, nothing duplicated, and the
seller always ends up with a clear account of what happened."""

import random
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update

from catalogpilot import db
from catalogpilot.ai import service as ai_service
from catalogpilot.catalog import service as cs
from catalogpilot.catalog.normalize import normalize_state
from catalogpilot.connectors import mock
from catalogpilot.engine import importer, jobs, worker
from catalogpilot.ids import utcnow
from catalogpilot.models import BreakerState, CatalogVersion, Channel, JobItem, Membership, Product, User
from catalogpilot.security.authz import Principal
from catalogpilot.vault import broker

N = 1500
UNREADABLE = len([i for i in range(N) if i % 401 == 400])  # the sandbox seeds these without a title
INACTIVE = len([i for i in range(N) if i % 13 == 0 and i % 401 != 400])


class Crash(BaseException):
    """A handler process dying: not an Exception, so nothing in the engine can catch it."""


@pytest.fixture()
def shop(tenants):
    mock.reset()
    ta, ca = tenants["a"]
    with db.system_scope(), db.session_scope() as s:
        user = User(email="owner@example.test")
        s.add(user)
        s.flush()
        s.add(Membership(tenant_id=ta, user_id=user.id, role="owner"))
        uid = user.id
    return ta, ca, Principal(ta, uid, "owner", step_up_fresh=True)


def channel(ta, uid, name, role, products=0):
    with db.tenant_scope(ta), db.session_scope() as s:
        ch = Channel(tenant_id=ta, platform="mock", name=name, role=role,
                     config={"demo_products": products} if products else {})
        s.add(ch)
        s.flush()
        broker.store_credential(s, ch, "access_token", {"access_token": f"tok_{name}_0123456789abcdef"}, uid)
        return ch.id


def start_import(principal, ca, source):
    with db.tenant_scope(principal.tenant_id), db.session_scope() as s:
        return importer.create_import_job(s, principal, source, ca).id


def fast_forward():
    past = utcnow() - timedelta(hours=1)
    with db.system_scope(), db.session_scope() as s:
        s.execute(update(JobItem).where(JobItem.state.in_(("QUEUED", "RETRY_WAIT"))).values(not_before=past))
        s.execute(update(JobItem).where(JobItem.state == "PUSHING").values(lease_until=past))
        s.execute(update(BreakerState).values(blocked_until=None, opened_at=past, tokens=1000.0))


def job_view(ta, job_id):
    with db.tenant_scope(ta), db.session_scope() as s:
        job = jobs.get_job(s, job_id)
        item = s.execute(select(JobItem).where(JobItem.job_id == job_id)).scalars().first()
        return job.state, dict(job.preview or {}), item.state, dict(item.last_error or {})


def run_until_done(ta, job_id, crash_hook=None, rounds=4000):
    crashes = 0
    for _ in range(rounds):
        try:
            claimed = worker.run_once(limit=25, crash_hook=crash_hook)
        except Crash:
            crashes += 1
            fast_forward()
            continue
        if job_view(ta, job_id)[0] not in ("running", "rolling_back"):
            return crashes
        if claimed == 0:
            fast_forward()
    raise AssertionError("did not finish")


def product_count(ta, **where):
    with db.tenant_scope(ta), db.session_scope() as s:
        stmt = select(func.count()).select_from(Product)
        for key, value in where.items():
            stmt = stmt.where(getattr(Product, key) == value)
        return s.execute(stmt).scalar_one()


def test_import_1500_products_survives_crashes_outages_and_expired_cursors(shop):
    ta, ca, principal = shop
    source = channel(ta, principal.user_id, "source", "source", N)
    store = mock.store_for(source)
    store.fail_rate, store.throttle_rate, store.expire_cursors = 0.15, 0.1, 2
    job_id = start_import(principal, ca, source)
    rng = random.Random(3)

    def crash_hook(stage, item):
        if stage == "after_page" and rng.random() < 0.3:
            raise Crash()

    crashes = run_until_done(ta, job_id, crash_hook)
    state, report, item_state, _ = job_view(ta, job_id)
    assert crashes >= 3, "the test must actually kill handlers mid-import"
    assert state == "completed" and item_state == "PUSHED" and report["finished"] is True
    assert product_count(ta) == N - UNREADABLE, "a product was lost or duplicated"
    assert report["seen"] == N and report["skipped"] == UNREADABLE and report["restarts"] == 2
    assert report["created"] == N - UNREADABLE and report["updated"] == 0
    assert len(report["problems"]) == UNREADABLE and "no title" in report["problems"][0]["reason"]
    assert report["quality"]["missing_images"] > 0 and report["quality"]["missing_brand"] > 0
    with db.tenant_scope(ta), db.session_scope() as s:
        keys = s.execute(select(Product.external_key)).scalars().all()
        assert len(keys) == len(set(keys))
        hostile = s.execute(select(Product).where(func.cast(Product.description_html, db.String).like("%track()%"))).first()
        assert hostile is None, "a script from the source store survived sanitization"

    # Running the same import again changes nothing and creates no new catalog version.
    with db.tenant_scope(ta), db.session_scope() as s:
        versions_before = s.execute(select(func.count()).select_from(CatalogVersion)).scalar_one()
    store.fail_rate = store.throttle_rate = 0.0
    again = start_import(principal, ca, source)
    run_until_done(ta, again)
    _, report2, _, _ = job_view(ta, again)
    assert report2["created"] == 0 and report2["updated"] == 0 and report2["unchanged"] == N - UNREADABLE
    assert product_count(ta) == N - UNREADABLE
    with db.tenant_scope(ta), db.session_scope() as s:
        assert s.execute(select(func.count()).select_from(CatalogVersion)).scalar_one() == versions_before


def test_server_goes_offline_mid_import_and_resumes_from_checkpoint(shop):
    """The whole process disappears after a few pages (crash, deploy, power cut). When a
    handler comes back it continues from the saved page instead of starting over."""
    ta, ca, principal = shop
    source = channel(ta, principal.user_id, "source", "source", N)
    store = mock.store_for(source)
    job_id = start_import(principal, ca, source)
    pages_done = {"n": 0}

    def die_after_four_pages(stage, item):
        pages_done["n"] += 1
        if pages_done["n"] == 4:
            raise Crash()

    with pytest.raises(Crash):
        worker.run_once(worker_id="before-outage", crash_hook=die_after_four_pages)
    state, report, item_state, _ = job_view(ta, job_id)
    assert state == "running" and item_state == "PUSHING" and report["pages"] == 4
    assert product_count(ta) == 400  # four pages are durable
    calls_before = store.list_calls
    assert worker.run_once(worker_id="after-restart") == 0  # the dead handler's lease is still valid
    fast_forward()  # ... until it expires
    run_until_done(ta, job_id)
    state, report, _, _ = job_view(ta, job_id)
    assert state == "completed" and product_count(ta) == N - UNREADABLE
    assert store.list_calls - calls_before == N // 100 - 4, "the import restarted instead of resuming"


def test_source_store_down_for_good_keeps_partial_import_and_retry_continues(shop):
    ta, ca, principal = shop
    source = channel(ta, principal.user_id, "source", "source", 600)
    store = mock.store_for(source)
    job_id = start_import(principal, ca, source)
    stop = {"at": 3}

    def outage(stage, item):
        stop["at"] -= 1
        if stop["at"] == 0:
            store.fail_rate = 1.0  # the store goes down after three pages

    run_until_done(ta, job_id, outage)
    state, report, item_state, error = job_view(ta, job_id)
    assert state == "failed" and item_state == "FAILED"
    assert "Nothing imported so far was lost" in error["message"] and error["suggestion"]
    assert "Traceback" not in str(error) and "503" not in error["message"]
    imported = product_count(ta)
    assert imported == 300 and report["pages"] == 3
    store.fail_rate = 0.0  # the store comes back; the seller presses Retry
    calls = store.list_calls
    with db.tenant_scope(ta), db.session_scope() as s:
        assert jobs.retry_failed(s, jobs.get_job(s, job_id)) == 1
    run_until_done(ta, job_id)
    assert job_view(ta, job_id)[0] == "completed" and product_count(ta) == 600 - 1
    assert store.list_calls - calls == 3, "retry must continue from the checkpoint"


def test_revoked_key_pauses_import_and_reconnect_resumes(shop):
    ta, ca, principal = shop
    source = channel(ta, principal.user_id, "source", "source", 500)
    store = mock.store_for(source)
    job_id = start_import(principal, ca, source)
    seen = {"n": 0}

    def revoke(stage, item):
        seen["n"] += 1
        if seen["n"] == 2:
            store.revoked = True

    for _ in range(6):
        worker.run_once(crash_hook=revoke)
        fast_forward()
    state, report, item_state, _ = job_view(ta, job_id)
    assert state == "running" and item_state in ("QUEUED", "PUSHING") and report["pages"] == 2  # waiting, not failed
    with db.tenant_scope(ta), db.session_scope() as s:
        ch = s.execute(select(Channel).where(Channel.id == source)).scalar_one()
        assert ch.health == "reauth_required"
        store.revoked = False
        broker.store_credential(s, ch, "access_token", {"access_token": "tok_new_0123456789abcdef"}, principal.user_id)
    run_until_done(ta, job_id)
    assert job_view(ta, job_id)[0] == "completed" and product_count(ta) == 500 - 1


def test_pause_resume_cancel_and_single_import_per_store(shop):
    ta, ca, principal = shop
    source = channel(ta, principal.user_id, "source", "source", 2500)
    job_id = start_import(principal, ca, source)
    with db.tenant_scope(ta), db.session_scope() as s:
        with pytest.raises(jobs.JobError) as err:
            importer.create_import_job(s, principal, source, ca)
        assert err.value.code == "job_already_running"
    worker.run_once()  # one turn = at most ten pages, then it yields
    assert product_count(ta) == importer.PAGES_PER_TURN * 100 - 2
    with db.tenant_scope(ta), db.session_scope() as s:
        jobs.pause(s, jobs.get_job(s, job_id))
    for _ in range(3):
        worker.run_once()
        fast_forward()
    assert product_count(ta) == importer.PAGES_PER_TURN * 100 - 2  # paused means paused
    with db.tenant_scope(ta), db.session_scope() as s:
        jobs.resume(s, jobs.get_job(s, job_id))
    worker.run_once()
    with db.tenant_scope(ta), db.session_scope() as s:
        jobs.cancel(s, jobs.get_job(s, job_id))
    kept = product_count(ta)
    for _ in range(3):
        worker.run_once()
        fast_forward()
    assert job_view(ta, job_id)[0] == "cancelled" and product_count(ta) == kept == 2000 - 4  # what was imported stays
    with db.tenant_scope(ta), db.session_scope() as s:
        dest = Channel(tenant_id=ta, platform="csv_feed", name="feed", role="destination")
        s.add(dest)
        s.flush()
        with pytest.raises(jobs.JobError) as err:
            importer.create_import_job(s, principal, dest.id, ca)
        assert err.value.code == "import_not_supported"


def test_full_migration_import_enhance_publish_1500_with_crashes(shop):
    """Journey J1 end to end: read 1,500 products from one store, improve them with the
    seller's preferences, publish to another store while handlers keep dying."""
    ta, ca, principal = shop
    source = channel(ta, principal.user_id, "source", "source", N)
    dest = channel(ta, principal.user_id, "destination", "destination")
    run_until_done(ta, start_import(principal, ca, source))
    with db.tenant_scope(ta), db.session_scope() as s:
        summary = ai_service.enhance(s, cs.get_catalog(s, ca), principal.user_id, ["title", "description"],
                                     preferences={"title_style": "sentence", "brand_first": True, "bogus": "x"})
        assert summary["proposals"] > 500 and summary["discarded"] == 0
        # Restyled titles were not covered by the benchmark, so none is applied without review.
        from catalogpilot.models import AIProposal
        titles = s.execute(select(AIProposal).where(AIProposal.task == "title")).scalars().all()
        assert titles and all(p.status == "pending" for p in titles)
        assert all("FREE SHIPPING" not in p.proposed_value and "BEST PRICE" not in p.proposed_value for p in titles)
        ai_service.decide(s, ta, principal.user_id, [p.id for p in titles[:1000]], "approve")
        ai_service.decide(s, ta, principal.user_id, [p.id for p in titles[1000:]], "approve")
    store = mock.store_for(dest)
    store.fail_rate, store.fail_after_write_rate, store.throttle_rate = 0.05, 0.05, 0.03
    with db.tenant_scope(ta), db.session_scope() as s:
        job = jobs.create_publish_job(s, principal, ca, [dest])
        assert job.preview["writes"] == N - UNREADABLE - INACTIVE and job.preview["blocked"] == 0
        jobs.approve(s, principal, job)
        job_id = job.id
    rng = random.Random(9)

    def crash_hook(stage, item):
        if stage == "after_write" and rng.random() < 0.02:
            raise Crash()

    crashes = run_until_done(ta, job_id, crash_hook, rounds=8000)
    with db.tenant_scope(ta), db.session_scope() as s:
        job = jobs.get_job(s, job_id)
        counts = {}
        for per in jobs.item_counts(s, job_id).values():
            for k, v in per.items():
                counts[k] = counts.get(k, 0) + v
    expected = N - UNREADABLE - INACTIVE
    assert crashes >= 5 and job.state == "completed" and counts == {"PUSHED": expected}
    assert len(store.products) == expected and set(store.creates_per_source.values()) == {1}
    sample = next(iter(store.products.values()))
    assert sample["title"].startswith("Northwind ") and "SHIPPING" not in sample["title"]
    assert all("<script" not in p["description_html"] for p in store.products.values())


@pytest.mark.parametrize("raw,reason", [
    ("not a dict", "not a product"),
    ({"title": ""}, "no title"),
    ({"title": "x", "variants": "nope"}, "unreadable"),
    ({"title": "x", "variants": [{"price_minor": -5}]}, "out of range"),
    ({"title": "x", "variants": [{"price_minor": float("nan")}]}, "not a number"),
    ({"title": "x", "variants": [{"price_minor": "19.99"}]}, "not a number"),
    ({"title": "x", "variants": [{"price_minor": 5, "currency": "dollars"}]}, "currency"),
    ({"title": "x", "variants": [{"price_minor": 10**20}]}, "out of range"),
])
def test_hostile_or_broken_source_data_is_rejected_per_product(raw, reason):
    with pytest.raises(ValueError, match=reason):
        normalize_state(raw, "en", "USD")


def test_source_data_is_sanitized_and_bounded():
    state, notes = normalize_state({
        "title": "  Shirt‮\x00  ", "description_html": "<p onclick='x()'>Hi</p><script>steal()</script><img src=x onerror=y>",
        "tags": ["a", "a", "  ", 5, "b" * 500], "status": "published-ish", "handle": "../../etc/passwd",
        "media": [{"url": "javascript:alert(1)"}, {"url": "https://img.example.com/a.jpg", "alt": "<b>x</b>"}, "ftp://x/y.jpg"],
        "variants": [{"sku": "S" * 900, "price_minor": 100, "inventory": -3, "option_values": {"Size": "M", "": "x"}}] * 2500,
        "seo": {"title": "t", "description": 7, "evil": "x"},
    }, "en", "USD")
    assert state["title"] == {"en": "Shirt"} and state["status"] == "draft" and state["handle"] == "etc-passwd"
    assert "script" not in state["description_html"]["en"] and "onclick" not in state["description_html"]["en"]
    assert [m["url"] for m in state["media"]] == ["https://img.example.com/a.jpg"]
    assert state["tags"] == ["a", "5", "b" * 120] and set(state["seo"]) <= {"title", "description"}
    assert len(state["variants"]) == 2000 and len(state["variants"][0]["sku"]) == 200
    assert state["variants"][0]["inventory"] == 0 and state["variants"][0]["option_values"] == {"Size": "M"}
    assert any("2000" in n for n in notes) and "cost_minor" in state["variants"][0]
