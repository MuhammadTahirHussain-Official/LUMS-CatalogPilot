"""WooCommerce connector (REST API v3, consumer key pair over HTTPS).

STATUS: written against the documented API; not yet verified against a live store. It must
pass the contract tests against a sandbox store before it is enabled for customers (INT-09).
Scope today: simple products and variable products with variations. Product identity on the
store is tracked with the meta field ``_catalogpilot_source_key``.
"""

from __future__ import annotations

from typing import Any

from ..catalog.cleaning import format_money
from . import http
from .sdk import CallContext, Capabilities, UnsupportedOperation, WriteResult

MARKER = "_catalogpilot_source_key"


def _auth(ctx: CallContext) -> tuple[str, str]:
    return ctx.secret["consumer_key"], ctx.secret["consumer_secret"]


def _url(ctx: CallContext, path: str) -> str:
    return f"{ctx.base_url}/wp-json/wc/v3/{path}"


def _price(v: dict[str, Any], field: str) -> str:
    return format_money(v.get(field), v.get("currency") or "USD")


def _body(payload: dict[str, Any]) -> dict[str, Any]:
    variants = payload.get("variants") or []
    variable = len(variants) > 1
    first = variants[0] if variants else {}
    body: dict[str, Any] = {
        "name": payload["title"],
        "description": payload.get("description_html") or "",
        "status": "publish" if payload.get("status") == "active" else "draft",
        "type": "variable" if variable else "simple",
        "tags": [{"name": t} for t in (payload.get("tags") or [])[:100]],
        "images": [{"src": m["url"], "alt": m.get("alt") or ""} for m in (payload.get("media") or [])[:50]],
        "meta_data": [{"key": MARKER, "value": payload["source_key"]}],
    }
    if payload.get("handle"):
        body["slug"] = payload["handle"]
    if variable:
        names: dict[str, list[str]] = {}
        for v in variants:
            for name, value in (v.get("option_values") or {}).items():
                names.setdefault(name, [])
                if value not in names[name]:
                    names[name].append(value)
        body["attributes"] = [{"name": n, "options": o, "variation": True, "visible": True} for n, o in names.items()]
    else:
        body.update(_variant_fields(first))
    return body


def _variant_fields(v: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if v.get("sku"):
        out["sku"] = v["sku"]
    # Woo: regular_price is the list price; sale_price is what the customer pays when lower.
    if v.get("compare_at_minor") and v.get("price_minor") is not None and v["compare_at_minor"] > v["price_minor"]:
        out["regular_price"], out["sale_price"] = _price(v, "compare_at_minor"), _price(v, "price_minor")
    elif v.get("price_minor") is not None:
        out["regular_price"], out["sale_price"] = _price(v, "price_minor"), ""
    if v.get("inventory") is not None:
        out["manage_stock"], out["stock_quantity"] = True, v["inventory"]
    if v.get("weight_grams") is not None:
        out["weight"] = f"{v['weight_grams'] / 1000:.3f}"  # Woo's unit is a store setting; kg is its default
    return out


class WooCommerceConnector:
    platform = "woocommerce"
    label = "WooCommerce"

    def capabilities(self) -> Capabilities:
        return Capabilities(max_title_length=200, max_variants=100, max_options=10, supports_idempotency_key=False,
                            requests_per_second=2.0, burst=10, credential_kinds=("api_key_pair",),
                            credential_fields=("consumer_key", "consumer_secret"), needs_base_url=True)

    def test_connection(self, ctx: CallContext) -> list[str]:
        http.request("GET", _url(ctx, "products"), params={"per_page": 1}, auth=_auth(ctx))
        return ["read_write"]

    def find_by_marker(self, ctx: CallContext, source_key: str) -> str | None:
        # Woo cannot filter by meta over REST; SKU is the reliable lookup, checked against the marker.
        return None

    def _find_by_sku(self, ctx: CallContext, payload: dict[str, Any]) -> str | None:
        variants = payload.get("variants") or []
        if len(variants) != 1 or not variants[0].get("sku"):
            return None
        found = http.request("GET", _url(ctx, "products"), params={"sku": variants[0]["sku"]}, auth=_auth(ctx))
        for product in found if isinstance(found, list) else []:
            if any(m.get("key") == MARKER and m.get("value") == payload["source_key"] for m in product.get("meta_data", [])):
                return str(product["id"])
        return None

    def upsert_product(self, ctx, payload, external_id, idempotency_key) -> WriteResult:
        external_id = external_id or self._find_by_sku(ctx, payload)
        body = _body(payload)
        if external_id:
            product = http.request("PUT", _url(ctx, f"products/{external_id}"), json=body, auth=_auth(ctx))
            created = False
        else:
            product = http.request("POST", _url(ctx, "products"), json=body, auth=_auth(ctx))
            created = True
        product_id = str(product["id"])
        variants = payload.get("variants") or []
        if len(variants) > 1:
            self._sync_variations(ctx, product_id, variants)
        return WriteResult(product_id, created, product.get("permalink"))

    def _sync_variations(self, ctx: CallContext, product_id: str, variants: list[dict[str, Any]]) -> None:
        existing = http.request("GET", _url(ctx, f"products/{product_id}/variations"), params={"per_page": 100},
                                auth=_auth(ctx))
        by_sku = {v.get("sku"): v["id"] for v in existing if v.get("sku")}
        wanted = {v.get("sku") for v in variants if v.get("sku")}
        batch: dict[str, list[Any]] = {"create": [], "update": [], "delete": []}
        for v in variants:
            row = {**_variant_fields(v), "attributes": [{"name": n, "option": o}
                                                         for n, o in (v.get("option_values") or {}).items()]}
            if v.get("sku") in by_sku:
                batch["update"].append({"id": by_sku[v["sku"]], **row})
            else:
                batch["create"].append(row)
        batch["delete"] = [vid for sku, vid in by_sku.items() if sku not in wanted]
        http.request("POST", _url(ctx, f"products/{product_id}/variations/batch"), json=batch, auth=_auth(ctx))

    def get_product(self, ctx, external_id):
        return http.request("GET", _url(ctx, f"products/{external_id}"), auth=_auth(ctx))

    def delist(self, ctx, external_id, idempotency_key) -> None:
        http.request("PUT", _url(ctx, f"products/{external_id}"), json={"status": "draft"}, auth=_auth(ctx))

    def delete_product(self, ctx, external_id, idempotency_key) -> None:
        from .sdk import RejectedError

        try:  # to the trash, not force-deleted: the seller can still restore it in WordPress
            http.request("DELETE", _url(ctx, f"products/{external_id}"), auth=_auth(ctx))
        except RejectedError as exc:
            if "404" not in exc.message and "invalid_id" not in exc.message:
                raise  # already gone is success (idempotent)

    def list_products(self, ctx, cursor):
        raise UnsupportedOperation("Importing from WooCommerce is not available yet; export a CSV from WooCommerce and upload it.")


CONNECTOR = WooCommerceConnector()
