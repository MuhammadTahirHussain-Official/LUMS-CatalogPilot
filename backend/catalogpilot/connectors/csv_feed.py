"""CSV / feed destination (Wave 1). "Publishing" maintains a feed file per channel that the
seller downloads or points another system at. Useful before any native connector is approved."""

from __future__ import annotations

import csv
import io
import json
import threading
from typing import Any

from ..catalog.cleaning import format_money
from ..datahub import storage
from ..datahub.service import neutralize
from .sdk import CallContext, Capabilities, UnsupportedOperation, WriteResult

_LOCK = threading.Lock()


def _key(ctx: CallContext) -> str:
    return f"{ctx.tenant_id}/feeds/{ctx.channel_id}.json"


def _load(ctx: CallContext) -> dict[str, dict[str, Any]]:
    if not storage.exists("exports", _key(ctx)):
        return {}
    return json.loads(storage.object_path("exports", _key(ctx)).read_text(encoding="utf-8"))


def _save(ctx: CallContext, data: dict[str, dict[str, Any]]) -> None:
    storage.put("exports", _key(ctx), json.dumps(data).encode("utf-8"))


def render_feed(tenant_id: str, channel_id: str) -> bytes:
    """Google-Merchant-style feed, one row per variant, formula-neutralized."""
    ctx = CallContext(tenant_id, channel_id, None, {}, {})
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["id", "item_group_id", "title", "description", "brand", "price", "gtin", "mpn",
                     "availability", "image_link", "product_type"])
    for source_key, p in sorted(_load(ctx).items()):
        if not p.get("listed", True):
            continue
        image = next((m["url"] for m in p.get("media", []) if m.get("url")), "")
        for i, v in enumerate(p.get("variants") or [{}]):
            price = format_money(v.get("price_minor"), v.get("currency") or "USD")
            writer.writerow(neutralize([
                v.get("sku") or f"{source_key}-{i}", source_key, p.get("title", ""), p.get("description_html", ""),
                p.get("brand") or "", f"{price} {v.get('currency') or ''}".strip(), v.get("gtin") or "",
                v.get("mpn") or "", "in_stock" if (v.get("inventory") or 0) > 0 else "out_of_stock", image,
                " > ".join(p.get("category") or []),
            ]))
    return buf.getvalue().encode("utf-8-sig")


class CsvFeedConnector:
    platform = "csv_feed"
    label = "CSV / product feed"

    def capabilities(self) -> Capabilities:
        return Capabilities(max_title_length=150, max_variants=2000, max_options=10, supports_idempotency_key=True,
                            requests_per_second=200.0, burst=500, supports_read_back=True)  # no credential needed

    def test_connection(self, ctx: CallContext) -> list[str]:
        return ["feed:write"]

    def upsert_product(self, ctx, payload, external_id, idempotency_key) -> WriteResult:
        with _LOCK:
            data = _load(ctx)
            created = payload["source_key"] not in data
            data[payload["source_key"]] = {**payload, "listed": True}
            _save(ctx, data)
        return WriteResult(payload["source_key"], created)

    def find_by_marker(self, ctx, source_key):
        return source_key if source_key in _load(ctx) else None

    def get_product(self, ctx, external_id):
        return _load(ctx).get(external_id)

    def delist(self, ctx, external_id, idempotency_key) -> None:
        with _LOCK:
            data = _load(ctx)
            if external_id in data:
                data[external_id]["listed"] = False
                _save(ctx, data)

    def delete_product(self, ctx, external_id, idempotency_key) -> None:
        with _LOCK:
            data = _load(ctx)
            if data.pop(external_id, None) is not None:
                _save(ctx, data)

    def list_products(self, ctx, cursor):
        raise UnsupportedOperation("A feed is a destination only; it cannot be imported from.")


CONNECTOR = CsvFeedConnector()
