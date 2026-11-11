"""Engine guarantees: no duplicates, no lost items, no zombie writes, isolation between channels."""

import random
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from catalogpilot import db
from catalogpilot.catalog import service as cs
from catalogpilot.catalog.service import Change
from catalogpilot.config import get_settings
from catalogpilot.connectors import mock
from catalogpilot.engine import jobs, worker
from catalogpilot.ids import utcnow
from catalogpilot.models import (
    Channel,
    ChannelListing,
    Credential,
    CredentialUsage,
    Job,
    JobItem,
    Membership,
    User,
)
from catalogpilot.security.authz import Forbidden, Principal
from catalogpilot.vault import broker


class Crash(BaseException):
    """Simulates a handler process dying: not an Exception, so nothing in the engine catches it."""


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
    principal = Principal(ta, uid, "owner", step_up_fresh=True)
    return ta, ca, principal


def add_products(ta, ca, n, prefix="P"):
    with db.tenant_scope(ta), db.session_scope() as s:
        changes = [
            Change(f"{prefix}{i}", {
                "title": {"en": f"{prefix} product {i}"}, "status": "active",
                "variants": [{"sku": f"{prefix}-{i}", "price_minor": 1000 + i, "currency": "USD",
                              "option_values": {}, "inventory": 5}],
            }) for i in range(n)
        ]
        cs.apply_changes(s, cs.get_catalog(s, ca), changes, "upload", None)


def add_channel(ta, uid, name="Sandbox", token="tok_canary_SECRET_1234567890"):
    with db.tenant_scope(ta), db.session_scope() as s:
        ch = Channel(tenant_id=ta, platform="mock", name=name, role="destination")
        s.add(ch)
        s.flush()
        broker.store_credential(s, ch, "access_token", {"access_token": token}, uid)
        return ch.id


def start_job(principal, ca, channel_ids, **options):
    with db.tenant_scope(principal.tenant_id), db.session_scope() as s:
        job = jobs.create_publish_job(s, principal, ca, channel_ids, options)
        jobs.approve(s, principal, job)
        return job.id


def drain(max_rounds=400, **kw):
    for _ in range(max_rounds):
        if worker.run_once(limit=25, **kw) == 0:
            with db.system_scope(), db.session_scope() as s:
                waiting = s.execute(select(JobItem.id).where(
                    JobItem.state.in_(("QUEUED", "RETRY_WAIT", "PUSHING")))).first()
            if not waiting:
                return
            fast_forward()
    raise AssertionError("engine did not drain")


def fast_forward(seconds=3600):
    """Jump time for waiting items and breakers instead of sleeping."""
    past = utcnow() - timedelta(seconds=seconds)
    with db.system_scope(), db.session_scope() as s:
        s.execute(update(JobItem).where(JobItem.state.in_(("QUEUED", "RETRY_WAIT"))).values(not_before=past))
        s.execute(update(JobItem).where(JobItem.state == "PUSHING").values(lease_until=past))
        from catalogpilot.models import BreakerState
        s.execute(update(BreakerState).values(blocked_until=None, opened_at=past, tokens=1000.0))


def states(ta, job_id):
    with db.tenant_scope(ta), db.session_scope() as s:
        job = jobs.get_job(s, job_id)
        counts = {}
        for per in jobs.item_counts(s, job_id).values():
            for k, v in per.items():
                counts[k] = counts.get(k, 0) + v
        return job.state, counts


def test_publish_happy_path_and_unchanged_skip(shop):
    ta, ca, principal = shop
    add_products(ta, ca, 30)
    ch = add_channel(ta, principal.user_id)
    job_id = start_job(principal, ca, [ch])
    drain()
    assert states(ta, job_id) == ("completed", {"PUSHED": 30})
    store = mock.store_for(ch)
    assert len(store.products) == 30 and set(store.creates_per_source.values()) == {1}
    # Publishing again without changes makes no API calls at all.
    writes = store.writes
    with db.tenant_scope(ta), db.session_scope() as s:
        job2 = jobs.create_publish_job(s, principal, ca, [ch])
        assert job2.preview["writes"] == 0 and job2.preview["channels"][ch]["unchanged"] == 30
    assert store.writes == writes


def test_chaos_crashes_and_lost_responses_never_duplicate(shop):
    """TEST-09/11 in miniature: handlers die after the destination write but before recording
    it, responses get lost, the platform throws 503s and throttles. Every item must still be
    written exactly once."""
    ta, ca, principal = shop
    n = 400
    add_products(ta, ca, n)
    ch = add_channel(ta, principal.user_id)
    store = mock.store_for(ch)
    store.fail_rate, store.fail_after_write_rate, store.throttle_rate = 0.08, 0.08, 0.05
    job_id = start_job(principal, ca, [ch])
    rng = random.Random(42)
    crashes = 0

    def crash_hook(stage, item):
        if rng.random() < 0.07:
            raise Crash()

    for _ in range(3000):
        try:
            claimed = worker.run_once(limit=25, crash_hook=crash_hook)
        except Crash:
            crashes += 1
            fast_forward()  # the dead handler's leases expire; a peer takes over
            continue
        if claimed == 0:
            if states(ta, job_id)[0] != "running":
                break
            fast_forward()
    state, counts = states(ta, job_id)
    assert crashes > 5, "the test must actually exercise takeovers"
    assert state == "completed" and counts == {"PUSHED": n}
    assert len(store.products) == n
    assert set(store.creates_per_source.values()) == {1}, "a takeover produced a duplicate"
    with db.tenant_scope(ta), db.session_scope() as s:
        live = s.execute(select(ChannelListing).where(ChannelListing.state == "live")).scalars().all()
        assert len(live) == n and len({listing.destination_external_id for listing in live}) == n


def test_no_duplicates_without_platform_idempotency(shop):
    ta, ca, principal = shop
    add_products(ta, ca, 120)
    ch = add_channel(ta, principal.user_id)
    store = mock.store_for(ch)
    store.idempotency_supported = False
    store.fail_after_write_rate = 0.15
    job_id = start_job(principal, ca, [ch])
    # MockConnector advertises idempotency keys; turn that off so the engine uses the marker lookup.
    from catalogpilot.connectors import get_connector
    from catalogpilot.connectors.sdk import Capabilities
    connector = get_connector("mock")
    original = connector.capabilities
    connector.capabilities = lambda: Capabilities(max_title_length=120, max_variants=50, requests_per_second=500.0,
                                                  burst=1000, credential_kinds=("access_token",),
                                                  credential_fields=("access_token",))
    try:
        drain()
    finally:
        connector.capabilities = original
    assert states(ta, job_id) == ("completed", {"PUSHED": 120})
    assert set(store.creates_per_source.values()) == {1} and len(store.products) == 120


def test_zombie_handler_is_fenced_out(shop):
    ta, ca, principal = shop
    add_products(ta, ca, 1)
    ch = add_channel(ta, principal.user_id)
    job_id = start_job(principal, ca, [ch])
    zombie = worker._claim("zombie", 1)[0]
    fast_forward()  # zombie stalls; its lease expires
    new_owner = worker._claim("healthy", 1)[0]
    assert new_owner.token > zombie.token
    with db.tenant_scope(ta), db.session_scope() as s:
        item = s.execute(select(JobItem)).scalar_one()
        # The zombie can neither get the credential nor change state.
        with pytest.raises(broker.LeaseDenied):
            broker.lease_for_item(s, item, "zombie", zombie.token, "write")
        with pytest.raises(worker.StaleLease):
            worker._guarded(s, zombie, state="PUSHED")
    worker._execute(zombie, "zombie")  # a full zombie run is a no-op
    assert mock.store_for(ch).writes == 0
    worker._execute(new_owner, "healthy")
    assert states(ta, job_id) == ("completed", {"PUSHED": 1})
    with db.tenant_scope(ta), db.session_scope() as s:
        denied = s.execute(select(CredentialUsage).where(CredentialUsage.result.like("denied%"))).scalars().all()
        assert denied, "denied credential requests must be recorded"


def test_one_failing_channel_does_not_block_the_other(shop):
    ta, ca, principal = shop
    add_products(ta, ca, 40)
    good = add_channel(ta, principal.user_id, "Good")
    bad = add_channel(ta, principal.user_id, "Bad")
    mock.store_for(bad).fail_rate = 1.0
    job_id = start_job(principal, ca, [good, bad])
    for _ in range(12):
        worker.run_once(limit=100)
    with db.tenant_scope(ta), db.session_scope() as s:
        counts = jobs.item_counts(s, job_id)
        assert counts[good] == {"PUSHED": 40}
        assert "PUSHED" not in counts[bad] and "FAILED" not in counts[bad]  # waiting, not failed
        channel = s.execute(select(Channel).where(Channel.id == bad)).scalar_one()
        assert channel.health == "degraded"  # breaker opened
    mock.store_for(bad).fail_rate = 0.0  # the platform recovers
    drain()
    assert states(ta, job_id) == ("completed", {"PUSHED": 80})


def test_retries_exhausted_goes_to_dead_letter_then_manual_retry(shop):
    ta, ca, principal = shop
    add_products(ta, ca, 3)
    ch = add_channel(ta, principal.user_id)
    mock.store_for(ch).fail_rate = 1.0
    job_id = start_job(principal, ca, [ch])
    drain()
    state, counts = states(ta, job_id)
    assert state == "failed" and counts == {"DEAD_LETTER": 3}
    mock.store_for(ch).fail_rate = 0.0
    with db.tenant_scope(ta), db.session_scope() as s:
        assert jobs.retry_failed(s, jobs.get_job(s, job_id)) == 3
    drain()
    assert states(ta, job_id) == ("completed", {"PUSHED": 3})


def test_platform_rejection_fails_one_item_only(shop):
    ta, ca, principal = shop
    add_products(ta, ca, 5)
    ch = add_channel(ta, principal.user_id)
    mock.store_for(ch).reject_skus = {"P-2"}
    job_id = start_job(principal, ca, [ch])
    drain()
    state, counts = states(ta, job_id)
    assert state == "partially_completed" and counts == {"PUSHED": 4, "FAILED": 1}
    with db.tenant_scope(ta), db.session_scope() as s:
        failed = s.execute(select(JobItem).where(JobItem.state == "FAILED")).scalar_one()
        assert "P-2" in failed.last_error["message"] and failed.last_error["suggestion"]


def test_revoked_credential_pauses_channel_without_failing_items(shop):
    ta, ca, principal = shop
    add_products(ta, ca, 4)
    ch = add_channel(ta, principal.user_id)
    mock.store_for(ch).revoked = True
    job_id = start_job(principal, ca, [ch])
    for _ in range(5):
        worker.run_once()
        fast_forward()
    state, counts = states(ta, job_id)
    assert state == "running" and set(counts) <= {"QUEUED", "PUSHING"}
    with db.tenant_scope(ta), db.session_scope() as s:
        channel = s.execute(select(Channel).where(Channel.id == ch)).scalar_one()
        assert channel.health == "reauth_required"
        mock.store_for(ch).revoked = False
        broker.store_credential(s, channel, "access_token", {"access_token": "tok_new_value_ABCDEFGH"}, principal.user_id)
    drain()
    assert states(ta, job_id) == ("completed", {"PUSHED": 4})


def test_poison_item_is_quarantined(shop):
    ta, ca, principal = shop
    add_products(ta, ca, 3)
    ch = add_channel(ta, principal.user_id)
    job_id = start_job(principal, ca, [ch])
    with db.tenant_scope(ta), db.session_scope() as s:
        poison = s.execute(select(JobItem).order_by(JobItem.id).limit(1)).scalar_one().id

    def crash_hook(stage, item):
        if item.id == poison:
            raise Crash()

    for _ in range(40):
        try:
            if worker.run_once(crash_hook=crash_hook) == 0 and states(ta, job_id)[0] != "running":
                break
        except Crash:
            pass
        fast_forward()
    state, counts = states(ta, job_id)
    assert counts == {"PUSHED": 2, "DEAD_LETTER": 1} and state == "partially_completed"


def test_rollback_deletes_created_and_restores_modified(shop):
    ta, ca, principal = shop
    add_products(ta, ca, 6)
    ch = add_channel(ta, principal.user_id)
    first = start_job(principal, ca, [ch])
    drain()
    store = mock.store_for(ch)
    before = {k: v["title"] for k, v in store.products.items()}
    # Second job: change three titles and add two products.
    with db.tenant_scope(ta), db.session_scope() as s:
        cat = cs.get_catalog(s, ca)
        cs.apply_changes(s, cat, [Change(f"P{i}", {"title": {"en": f"Renamed {i}"}}, partial=True) for i in range(3)],
                         "bulk_edit", None)
    add_products(ta, ca, 2, prefix="N")
    second = start_job(principal, ca, [ch])
    drain()
    assert len(store.products) == 8
    with db.tenant_scope(ta), db.session_scope() as s:
        rb = jobs.create_rollback_job(s, principal, jobs.get_job(s, second))
        assert rb.preview["delete"] == 2 and rb.preview["restore"] == 3 and rb.preview["cannot_rollback"] == []
        jobs.approve(s, principal, rb)
        rb_id = rb.id
    drain()
    assert states(ta, rb_id)[0] == "rolled_back"
    assert {k: v["title"] for k, v in store.products.items()} == before
    assert states(ta, second)[1] == {"ROLLED_BACK": 5, "SKIPPED": 3}
    # The first job is untouched and can still be rolled back on its own.
    assert states(ta, first)[1] == {"PUSHED": 6}


def test_high_risk_needs_step_up_and_validation_blocks(shop):
    ta, ca, principal = shop
    add_products(ta, ca, 2)
    ch = add_channel(ta, principal.user_id)
    start_job(principal, ca, [ch])
    drain()
    with db.tenant_scope(ta), db.session_scope() as s:
        cat = cs.get_catalog(s, ca)
        p0 = s.execute(select(cs.Product).where(cs.Product.external_key == "P0")).scalar_one()
        variant = {**p0.variants[0], "price_minor": 1}
        cs.apply_changes(s, cat, [
            Change("P0", {"variants": [variant]}, partial=True),
            Change("P1", {"title": {"en": "x" * 500}}, partial=True),
        ], "bulk_edit", None)
        job = jobs.create_publish_job(s, principal, ca, [ch])
        items = {i.product_id: i for i in s.execute(select(JobItem).where(JobItem.job_id == job.id)).scalars()}
        assert items[p0.id].risk == "high"
        assert any(d["field"].endswith("price_minor") for d in items[p0.id].diff)
        blocked = next(i for i in items.values() if i.product_id != p0.id)
        assert blocked.state == "FAILED" and blocked.issues[0]["code"] == "too_long"
        weak = Principal(ta, principal.user_id, "owner", step_up_fresh=False)
        with pytest.raises(Forbidden) as err:
            jobs.approve(s, weak, job)
        assert err.value.code == "step_up_required"


def test_credential_never_stored_or_logged_in_plaintext(shop):
    ta, ca, principal = shop
    canary = "tok_canary_SECRET_1234567890"
    add_products(ta, ca, 3)
    ch = add_channel(ta, principal.user_id, token=canary)
    mock.store_for(ch).fail_rate = 0.5
    start_job(principal, ca, [ch])
    drain()
    path = get_settings().data_dir / "catalogpilot.db"
    blob = b"".join(p.read_bytes() for p in path.parent.glob("catalogpilot.db*"))
    assert canary.encode() not in blob, "plaintext credential found in the database files"
    with db.tenant_scope(ta), db.session_scope() as s:
        cred = s.execute(select(Credential)).scalar_one()
        assert broker.metadata(cred)["hint"] == "…7890" and "ciphertext" not in broker.metadata(cred)
        channel = s.execute(select(Channel)).scalar_one()
        broker.disconnect(s, channel)
        assert cred.ciphertext == b"" and cred.wrapped_data_key == b"" and cred.shredded_at


def test_refuses_passwords_and_one_job_per_channel(shop):
    ta, ca, principal = shop
    add_products(ta, ca, 2)
    with db.tenant_scope(ta), db.session_scope() as s:
        ch = Channel(tenant_id=ta, platform="mock", name="X", role="destination")
        s.add(ch)
        s.flush()
        with pytest.raises(broker.VaultError) as err:
            broker.store_credential(s, ch, "access_token", {"password": "hunter2hunter2"}, principal.user_id)
        assert err.value.code == "credential_type_refused"
        with pytest.raises(broker.VaultError):
            broker.store_credential(s, ch, "access_token", {"access_token": "revoked"}, principal.user_id)
    ch = add_channel(ta, principal.user_id)
    start_job(principal, ca, [ch])
    with db.tenant_scope(ta), db.session_scope() as s:
        second = jobs.create_publish_job(s, principal, ca, [ch])
        with pytest.raises(jobs.JobError) as err:
            jobs.approve(s, principal, second)
        assert err.value.code == "job_already_running"
        assert s.execute(select(Job)).scalars().all()
