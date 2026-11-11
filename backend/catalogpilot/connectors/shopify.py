"""Shopify connector (Admin GraphQL API, custom-app access token).

STATUS: written against the documented API; not yet verified against a live store. It must
pass the contract tests against a development store before it is enabled for customers
(INT-09). The API version is pinned (INT-10). Product identity on the store is tracked with
a tag ``cp-<source key>`` so a create repeated after a crash becomes an update.
"""

from __future__ import annotations

from typing import Any

from ..catalog.cleaning import format_money
from . import http
from .sdk import CallContext, Capabilities, RejectedError, RetryableError, UnsupportedOperation, WriteResult

API_VERSION = "2025-01"


def _gql(ctx: CallContext, query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    body = http.request(
        "POST", f"{ctx.base_url}/admin/api/{API_VERSION}/graphql.json",
        headers={"X-Shopify-Access-Token": ctx.secret["access_token"], "Content-Type": "application/json"},
        json={"query": query, "variables": variables or {}},
    )
    errors = body.get("errors")
    if errors:
        codes = {str((e.get("extensions") or {}).get("code")) for e in errors if isinstance(e, dict)}
        if "THROTTLED" in codes:  # cost-based limit: wait for the bucket to refill
            raise RetryableError("Shopify is rate limiting requests.", retry_after=2.0, throttled=True)
        raise RejectedError(f"Shopify rejected the request: {str(errors)[:300]}")
    return body.get("data") or {}


def _marker(source_key: str) -> str:
    return f"cp-{source_key}"


def _input(payload: dict[str, Any], external_id: str | None) -> dict[str, Any]:
    variants = payload.get("variants") or []
    option_names: list[str] = []
    for v in variants:
        for name in (v.get("option_values") or {}):
            if name not in option_names:
                option_names.append(name)
    option_names = option_names or ["Title"]
    product: dict[str, Any] = {
        "title": payload["title"],
        "descriptionHtml": payload.get("description_html") or "",
        "vendor": payload.get("brand") or "",
        "productType": payload.get("product_type") or "",
        "status": "ACTIVE" if payload.get("status") == "active" else "DRAFT",
        "tags": [*(payload.get("tags") or [])[:249], _marker(payload["source_key"])],
        "productOptions": [
            {"name": name, "values": [{"name": val} for val in dict.fromkeys(
                (v.get("option_values") or {}).get(name, "Default Title") for v in variants)]}
            for name in option_names
        ],
        "variants": [
            {
                "optionValues": [{"optionName": name, "name": (v.get("option_values") or {}).get(name, "Default Title")}
                                 for name in option_names],
                "price": format_money(v.get("price_minor"), v.get("currency") or "USD"),
                **({"compareAtPrice": format_money(v["compare_at_minor"], v.get("currency") or "USD")}
                   if v.get("compare_at_minor") is not None else {}),
                **({"sku": v["sku"]} if v.get("sku") else {}),
                **({"barcode": v["gtin"]} if v.get("gtin") else {}),
            }
            for v in variants
        ],
    }
    if payload.get("handle"):
        product["handle"] = payload["handle"]
    if payload.get("seo"):
        product["seo"] = {k: payload["seo"][k] for k in ("title", "description") if payload["seo"].get(k)}
    if payload.get("media"):
        product["files"] = [{"originalSource": m["url"], "alt": m.get("alt") or "", "contentType": "IMAGE"}
                            for m in payload["media"][:250]]
    if external_id:
        product["id"] = external_id
    return product


_SET = """
mutation Set($input: ProductSetInput!) {
  productSet(synchronous: true, input: $input) {
    product { id onlineStoreUrl }
    userErrors { field message code }
  }
}"""


class ShopifyConnector:
    platform = "shopify"
    label = "Shopify"

    def capabilities(self) -> Capabilities:
        return Capabilities(max_title_length=255, max_variants=2048, max_options=3, max_images=250, max_tags=250,
                            supports_idempotency_key=False, requests_per_second=2.0, burst=20,
                            credential_kinds=("access_token",), credential_fields=("access_token",),
                            needs_base_url=True)

    def test_connection(self, ctx: CallContext) -> list[str]:
        data = _gql(ctx, "{ shop { name } currentAppInstallation { accessScopes { handle } } }")
        scopes = [s["handle"] for s in ((data.get("currentAppInstallation") or {}).get("accessScopes") or [])]
        if "write_products" not in scopes:
            raise RejectedError("The token cannot write products.",
                                "Give the custom app the write_products scope and try again.")
        return scopes

    def find_by_marker(self, ctx: CallContext, source_key: str) -> str | None:
        data = _gql(ctx, "query($q: String!) { products(first: 2, query: $q) { nodes { id } } }",
                    {"q": f"tag:'{_marker(source_key)}'"})
        nodes = (data.get("products") or {}).get("nodes") or []
        return nodes[0]["id"] if nodes else None

    def upsert_product(self, ctx, payload, external_id, idempotency_key) -> WriteResult:
        result = _gql(ctx, _SET, {"input": _input(payload, external_id)}).get("productSet") or {}
        problems = result.get("userErrors") or []
        if problems:
            message = "; ".join(f"{'.'.join(map(str, p.get('field') or []))}: {p.get('message')}" for p in problems)[:400]
            raise RejectedError(f"Shopify rejected the product: {message}", "Fix the fields mentioned, then retry.")
        product = result.get("product") or {}
        if not product.get("id"):
            raise RetryableError("Shopify did not confirm the product was saved.")
        return WriteResult(product["id"], created=external_id is None, url=product.get("onlineStoreUrl"))

    def get_product(self, ctx, external_id):
        data = _gql(ctx, "query($id: ID!) { product(id: $id) { id title status updatedAt } }", {"id": external_id})
        return data.get("product")

    def delist(self, ctx, external_id, idempotency_key) -> None:
        _gql(ctx, "mutation($i: ProductUpdateInput!) { productUpdate(product: $i) { userErrors { message } } }",
             {"i": {"id": external_id, "status": "DRAFT"}})

    def delete_product(self, ctx, external_id, idempotency_key) -> None:
        result = _gql(ctx, "mutation($i: ProductDeleteInput!) { productDelete(input: $i) { userErrors { message } } }",
                      {"i": {"id": external_id}}).get("productDelete") or {}
        problems = [p for p in (result.get("userErrors") or []) if "not exist" not in str(p.get("message", "")).lower()]
        if problems:
            raise RejectedError(f"Shopify could not delete the product: {problems[0].get('message')}")

    def list_products(self, ctx, cursor):
        raise UnsupportedOperation("Importing from Shopify is not available yet; export a CSV from Shopify and upload it.")


CONNECTOR = ShopifyConnector()
