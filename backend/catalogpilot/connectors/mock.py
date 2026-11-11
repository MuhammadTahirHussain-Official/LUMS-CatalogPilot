"""Mock platform: an in-process store with programmable faults.

Used for the free sandbox run (FR-34), local development and the chaos tests (TEST-09/11).
It emulates what real platforms do wrong: rate limits, 5xx bursts, slow responses, revoked
credentials, validation rejections, and the absence of idempotency-key support. It also
counts every create per source key so tests can prove no duplicate was ever written.
"""

from __future__ import annotations

import random
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .sdk import AuthError, CallContext, Capabilities, RejectedError, RetryableError, WriteResult


@dataclass
class MockStore:
    products: dict[str, dict[str, Any]] = field(default_factory=dict)  # external_id -> record
    by_idem: dict[str, str] = field(default_factory=dict)
    creates_per_source: Counter = field(default_factory=Counter)
    writes: int = 0
    seq: int = 0
    # fault injection
    fail_rate: float = 0.0  # probability of a 503 before the write
    fail_after_write_rate: float = 0.0  # write succeeds, response is lost (the nasty case)
    throttle_rate: float = 0.0
    latency: float = 0.0
    revoked: bool = False
    reject_skus: set[str] = field(default_factory=set)
    idempotency_supported: bool = True
    rng: random.Random = field(default_factory=lambda: random.Random(7))
    lock: threading.Lock = field(default_factory=threading.Lock)


_STORES: dict[str, MockStore] = {}
_GLOBAL = threading.Lock()


def store_for(channel_id: str) -> MockStore:
    with _GLOBAL:
        return _STORES.setdefault(channel_id, MockStore())


def reset() -> None:
    with _GLOBAL:
        _STORES.clear()


class MockConnector:
    platform = "mock"
    label = "Sandbox store (mock)"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            max_title_length=120, max_variants=50, supports_idempotency_key=True,
            requests_per_second=500.0, burst=1000, credential_kinds=("access_token",),
            credential_fields=("access_token",),
        )

    def _gate(self, ctx: CallContext) -> MockStore:
        store = store_for(ctx.channel_id)
        if store.latency:
            time.sleep(store.latency)
        if store.revoked or ctx.secret.get("access_token") == "revoked":
            raise AuthError("The store rejected our access. Reconnect the channel.", "Reconnect the channel.")
        with store.lock:
            if store.rng.random() < store.throttle_rate:
                raise RetryableError("The store is rate limiting requests.", retry_after=0.05, throttled=True)
            if store.rng.random() < store.fail_rate:
                raise RetryableError("The store returned a temporary error (503).")
        return store

    def test_connection(self, ctx: CallContext) -> list[str]:
        self._gate(ctx)
        if not ctx.secret.get("access_token"):
            raise AuthError("The access token is empty.")
        return ["read_products", "write_products"]

    def upsert_product(self, ctx: CallContext, payload: dict[str, Any], external_id: str | None,
                       idempotency_key: str) -> WriteResult:
        store = self._gate(ctx)
        for v in payload.get("variants", []):
            if v.get("sku") in store.reject_skus:
                raise RejectedError(f"The store rejected SKU '{v['sku']}': it is not allowed.",
                                    "Change the SKU or remove this variant.")
        with store.lock:
            created = False
            if store.idempotency_supported and idempotency_key in store.by_idem:
                external_id = store.by_idem[idempotency_key]
            elif external_id is None or external_id not in store.products:
                store.seq += 1
                external_id = f"mock_{store.seq}"
                created = True
                store.creates_per_source[payload["source_key"]] += 1
            store.products[external_id] = {**payload, "listed": True}
            store.by_idem[idempotency_key] = external_id
            store.writes += 1
            lose_response = store.rng.random() < store.fail_after_write_rate
        if lose_response:
            raise RetryableError("The connection dropped before the store answered.")
        return WriteResult(external_id, created)

    def find_by_marker(self, ctx: CallContext, source_key: str) -> str | None:
        store = self._gate(ctx)
        with store.lock:
            for external_id, record in store.products.items():
                if record.get("source_key") == source_key:
                    return external_id
        return None

    def get_product(self, ctx: CallContext, external_id: str) -> dict[str, Any] | None:
        store = self._gate(ctx)
        with store.lock:
            record = store.products.get(external_id)
            return dict(record) if record else None

    def delist(self, ctx: CallContext, external_id: str, idempotency_key: str) -> None:
        store = self._gate(ctx)
        with store.lock:
            if external_id in store.products:
                store.products[external_id]["listed"] = False

    def delete_product(self, ctx: CallContext, external_id: str, idempotency_key: str) -> None:
        store = self._gate(ctx)
        with store.lock:
            store.products.pop(external_id, None)  # deleting twice is fine (idempotent)

    def list_products(self, ctx: CallContext, cursor: str | None):
        store = self._gate(ctx)
        with store.lock:
            ids = sorted(store.products)
        start = int(cursor or 0)
        page = ids[start:start + 100]
        states = []
        for external_id in page:
            record = store.products[external_id]
            states.append({
                "external_key": external_id,
                "state": {
                    "title": {"en": record.get("title", "")},
                    "description_html": {"en": record.get("description_html", "")},
                    "brand": record.get("brand"), "tags": record.get("tags", []),
                    "variants": record.get("variants", []), "media": record.get("media", []),
                    "status": "active" if record.get("listed") else "draft",
                },
            })
        return states, (str(start + 100) if start + 100 < len(ids) else None)


CONNECTOR = MockConnector()
