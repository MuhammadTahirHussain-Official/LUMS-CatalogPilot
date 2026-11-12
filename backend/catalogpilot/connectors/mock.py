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

from .sdk import AuthError, CallContext, Capabilities, CursorExpired, RejectedError, RetryableError, WriteResult


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
    page_size: int = 100
    expire_cursors: int = 0  # how many times to reject a page cursor (stores do expire them)
    seeded: bool = False
    list_calls: int = 0
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
    label = "Practice store"

    def capabilities(self) -> Capabilities:
        return Capabilities(
            max_title_length=120, max_variants=50, supports_idempotency_key=True, supports_import=True,
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
        if len(ctx.secret.get("access_token") or "") < 12:
            # Behaves like a real store refusing a bad key, so the "wrong key" path can be tried safely.
            raise AuthError("The practice store did not accept that key. Use any text of 12 or more characters.")
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
            store.list_calls += 1
            wanted = int((ctx.config or {}).get("demo_products") or 0)
            if wanted and not store.products and not store.seeded:
                seed_demo(store, wanted)  # the sandbox source store refills itself after a restart
            ids = sorted(store.products, key=lambda k: int(k.rsplit("_", 1)[-1]) if k.rsplit("_", 1)[-1].isdigit() else 0)
            if cursor and store.expire_cursors > 0:
                store.expire_cursors -= 1
                raise CursorExpired("The store no longer recognises this page marker.")
        try:
            start = int(cursor or 0)
        except ValueError:
            raise CursorExpired("The store no longer recognises this page marker.") from None
        page = ids[start:start + store.page_size]
        states = []
        for external_id in page:
            record = store.products.get(external_id)
            if record is None:
                continue  # deleted on the store while we were paging
            if not isinstance(record.get("title"), str) or not record["title"].strip():
                # One unreadable product must not sink the page: report it and carry on.
                states.append({"external_key": external_id, "error": "The product has no title on the store."})
                continue
            states.append({
                "external_key": external_id,
                "state": {
                    "title": {"en": record.get("title", "")},
                    "description_html": {"en": record.get("description_html", "")},
                    "brand": record.get("brand"), "product_type": record.get("product_type"),
                    "source_category": record.get("category", []), "tags": record.get("tags", []),
                    "variants": record.get("variants", []), "media": record.get("media", []),
                    "status": "active" if record.get("listed", True) else "draft",
                },
            })
        return states, (str(start + store.page_size) if start + store.page_size < len(ids) else None)


_DEMO = [
    ("Cotton T-Shirt", "Shirts", ["Apparel", "Shirts"], "cotton", 1999), ("Canvas Backpack", "Bags", ["Bags"], "canvas", 5400),
    ("Ceramic Coffee Mug", "Kitchen", ["Home", "Kitchen"], "ceramic", 1250), ("Wireless Headphones", "Audio", ["Electronics", "Audio"], "", 8900),
    ("Yoga Mat", "Fitness", ["Sports", "Fitness"], "rubber", 2900), ("Leather Wallet", "Accessories", ["Apparel", "Accessories"], "leather", 3500),
    ("Steel Water Bottle", "Kitchen", ["Home", "Kitchen"], "steel", 2200), ("Running Shoes", "Shoes", ["Apparel", "Shoes"], "", 7900),
    ("Desk Lamp", "Decor", ["Home", "Decor"], "aluminum", 4100), ("Wool Scarf", "Accessories", ["Apparel", "Accessories"], "wool", 2700),
]
_DEMO_COLORS = ["Black", "White", "Navy", "Olive", "Red", "Grey", "Teal", "Beige"]
_DEMO_PROMO = ["", "", "", " - FREE SHIPPING!!!", " *** BEST PRICE ***", " - HOT SALE"]


def seed_demo(store: MockStore, count: int) -> None:
    """Fills a sandbox store with a realistic, deliberately messy catalog: shouted titles,
    promotional text, pasted links, missing images, an unreadable product. Deterministic."""
    rng = random.Random(1234)
    for i in range(count):
        name, ptype, category, material, price = _DEMO[i % len(_DEMO)]
        color = _DEMO_COLORS[(i // len(_DEMO)) % len(_DEMO_COLORS)]
        title = f"Northwind {name} {color} {i + 1:04d}"
        style = rng.random()
        if style < 0.25:
            title = title.upper()
        elif style < 0.4:
            title = title.lower()
        title += rng.choice(_DEMO_PROMO)
        sku = f"NW-{i + 1:05d}"
        description = f"<p>{name} in {color.lower()}" + (f", made of {material}" if material else "") + ".</p>"
        if rng.random() < 0.15:
            description += "<p>Visit www.cheap-deals.example for more!</p>"
        if rng.random() < 0.05:
            description += "<script>track()</script>"
        sizes = ["S", "M", "L"] if ptype in ("Shirts", "Shoes") else [None]
        variants = [{
            "sku": f"{sku}-{size}" if size else sku, "gtin": None, "mpn": None, "currency": "USD",
            "price_minor": price + (i % 7) * 100, "compare_at_minor": None, "inventory": rng.randint(0, 60),
            "weight_grams": None, "option_values": {"Size": size} if size else {},
        } for size in sizes]
        store.seq += 1
        store.products[f"mock_{store.seq}"] = {
            "source_key": f"demo-{i}", "title": None if i % 401 == 400 else title, "description_html": description,
            "brand": "Northwind" if i % 9 else None, "product_type": ptype, "category": category,
            "tags": [ptype.lower(), color.lower()], "variants": variants, "listed": i % 13 != 0,
            "media": [] if i % 11 == 0 else [{"url": f"https://images.example.com/nw/{i + 1}.jpg", "alt": ""}],
        }
    store.seeded = True


CONNECTOR = MockConnector()
