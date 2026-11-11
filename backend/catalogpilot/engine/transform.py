"""Master record -> channel payload, validation against declared capabilities, diff and risk.

Nothing here is silent: when a destination cannot represent something the result is an
explicit issue with a suggested fix (INT-07), never a quiet truncation.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from ..catalog.cleaning import gtin_valid, html_to_text, sanitize_html
from ..connectors.sdk import Capabilities
from ..models import Product

_VARIANT_PATH = re.compile(r"^variants\[sku=(.+)\]\.(price_minor|compare_at_minor|inventory)$")
OVERRIDABLE = {"title", "description_html", "tags", "product_type", "source_category", "brand", "seo", "media", "status"}


def pick_locale(value: dict[str, str] | None, locale: str) -> str:
    if not value:
        return ""
    return value.get(locale) or next(iter(value.values()), "")


def build_payload(product: Product, overrides: dict[str, Any], locale: str) -> dict[str, Any]:
    """Applies per-channel overrides on top of the master without modifying it (PUB-04)."""
    payload: dict[str, Any] = {
        "source_key": product.id,
        "title": pick_locale(product.title, locale),
        "description_html": sanitize_html(pick_locale(product.description_html, locale)),  # again, before publish
        "brand": product.brand,
        "product_type": product.product_type,
        "category": list(product.source_category or []),
        "taxonomy_id": product.mapped_taxonomy_id,
        "tags": list(product.tags or []),
        "status": product.status,
        "handle": product.handle,
        "seo": dict(product.seo or {}),
        "attributes": [{"key": a["key"], "value": a["value"]} for a in (product.attributes or [])],
        "media": [{"url": m["url"], "alt": pick_locale(m.get("alt"), locale)} for m in (product.media or []) if m.get("url")],
        "variants": [
            {k: v.get(k) for k in ("sku", "gtin", "mpn", "price_minor", "compare_at_minor", "currency",
                                    "inventory", "weight_grams", "option_values")}
            for v in (product.variants or [])
        ],  # cost_minor is deliberately never sent to a destination
        "local_only_media": sum(1 for m in (product.media or []) if not m.get("url")),
    }
    for path, value in overrides.items():
        if path in ("title", "description_html") and isinstance(value, dict):
            value = pick_locale(value, locale)
        if path == "description_html":
            value = sanitize_html(value)
        if path == "source_category":
            payload["category"] = value
        elif path in OVERRIDABLE:
            payload[path] = value
        else:
            match = _VARIANT_PATH.match(path)
            if match:
                for variant in payload["variants"]:
                    if variant.get("sku") == match.group(1):
                        variant[match.group(2)] = value
    return payload


def payload_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _issue(severity: str, field: str, code: str, message: str, suggestion: str = "") -> dict[str, str]:
    return {"severity": severity, "field": field, "code": code, "message": message, "suggestion": suggestion}


def validate(payload: dict[str, Any], caps: Capabilities) -> list[dict[str, str]]:
    """errors block the item; warnings are shown; nothing is changed here (FR-21)."""
    issues: list[dict[str, str]] = []
    title = payload.get("title") or ""
    if not title.strip():
        issues.append(_issue("error", "title", "required", "The product has no title.", "Add a title."))
    elif len(title) > caps.max_title_length:
        issues.append(_issue(
            "error", "title", "too_long",
            f"The title is {len(title)} characters; this channel allows {caps.max_title_length}.",
            "Shorten the title or set a shorter title just for this channel.",
        ))
    desc = payload.get("description_html") or ""
    if len(desc) > caps.max_description_length:
        issues.append(_issue("error", "description_html", "too_long",
                             f"The description is longer than {caps.max_description_length} characters.",
                             "Shorten the description for this channel."))
    if desc and not caps.supports_html and desc != html_to_text(desc):
        issues.append(_issue("warning", "description_html", "lossy_html",
                             "This channel does not support formatting; the description will be sent as plain text."))
    variants = payload.get("variants") or []
    if not variants:
        issues.append(_issue("error", "variants", "required", "The product has no variants or price.",
                             "Add at least a price."))
    if len(variants) > caps.max_variants:
        issues.append(_issue("error", "variants", "too_many",
                             f"{len(variants)} variants; this channel allows {caps.max_variants}.",
                             "Split the product or publish fewer variants to this channel."))
    option_names = {name for v in variants for name in (v.get("option_values") or {})}
    if len(option_names) > caps.max_options:
        issues.append(_issue("error", "variants.option_values", "too_many_options",
                             f"{len(option_names)} options; this channel allows {caps.max_options}."))
    seen_skus: set[str] = set()
    seen_options: set[str] = set()
    for i, v in enumerate(variants):
        label = v.get("sku") or f"variant {i + 1}"
        if caps.requires_price and v.get("price_minor") is None:
            issues.append(_issue("error", f"variants[{i}].price_minor", "required", f"{label} has no price.", "Add a price."))
        elif v.get("price_minor") == 0:
            issues.append(_issue("warning", f"variants[{i}].price_minor", "zero_price", f"{label} has a price of zero."))
        if caps.requires_sku and not v.get("sku"):
            issues.append(_issue("error", f"variants[{i}].sku", "required", f"{label} has no SKU.", "Add a SKU."))
        if v.get("sku"):
            if v["sku"] in seen_skus:
                issues.append(_issue("error", f"variants[{i}].sku", "duplicate", f"SKU '{v['sku']}' is used twice."))
            seen_skus.add(v["sku"])
        if len(variants) > 1:
            signature = json.dumps(v.get("option_values") or {}, sort_keys=True)
            if signature in seen_options:
                issues.append(_issue("error", f"variants[{i}].option_values", "duplicate",
                                     f"{label} has the same option values as another variant.",
                                     "Give each variant a distinct size, colour or other option."))
            seen_options.add(signature)
        if caps.requires_gtin and not v.get("gtin"):
            issues.append(_issue("error", f"variants[{i}].gtin", "required", f"{label} has no barcode (GTIN)."))
        if v.get("gtin") and not gtin_valid(v["gtin"]):
            issues.append(_issue("warning", f"variants[{i}].gtin", "invalid_check_digit",
                                 f"{label}: barcode '{v['gtin']}' has an invalid check digit."))
        if caps.currencies and v.get("currency") not in caps.currencies:
            issues.append(_issue("error", f"variants[{i}].currency", "unsupported",
                                 f"{label}: currency {v.get('currency')} is not supported by this channel."))
        if v.get("compare_at_minor") is not None and v.get("price_minor") is not None \
                and v["compare_at_minor"] < v["price_minor"]:
            issues.append(_issue("warning", f"variants[{i}].compare_at_minor", "below_price",
                                 f"{label}: compare-at price is lower than the price."))
    media = payload.get("media") or []
    if caps.requires_image and not media:
        issues.append(_issue("error", "media", "required", "This channel requires at least one image.",
                             "Add an image URL."))
    if len(media) > caps.max_images:
        issues.append(_issue("warning", "media", "too_many",
                             f"Only the first {caps.max_images} images will be sent."))
    if payload.get("local_only_media"):
        issues.append(_issue("warning", "media", "not_publicly_hosted",
                             f"{payload['local_only_media']} uploaded image(s) have no public URL yet and are not sent."))
    if len(payload.get("tags") or []) > caps.max_tags:
        issues.append(_issue("warning", "tags", "too_many", f"Only the first {caps.max_tags} tags will be sent."))
    return issues


def has_errors(issues: list[dict[str, str]]) -> bool:
    return any(i["severity"] == "error" for i in issues)


_SCALARS = ("title", "description_html", "brand", "product_type", "category", "taxonomy_id", "tags", "status",
            "handle", "seo", "attributes", "media")
_VARIANT_FIELDS = ("price_minor", "compare_at_minor", "currency", "inventory", "gtin", "mpn", "weight_grams",
                   "option_values")


def diff(before: dict[str, Any] | None, after: dict[str, Any]) -> list[dict[str, Any]]:
    """Field-level difference between what is live on the channel and what would be sent (PUB-07)."""
    if before is None:
        return [{"field": "*", "before": None, "after": "new listing"}]
    changes = [
        {"field": f, "before": before.get(f), "after": after.get(f)}
        for f in _SCALARS if before.get(f) != after.get(f)
    ]

    def keyed(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
        return {v.get("sku") or f"#{i}": v for i, v in enumerate(payload.get("variants") or [])}

    old, new = keyed(before), keyed(after)
    for key in old.keys() - new.keys():
        changes.append({"field": f"variants[{key}]", "before": "present", "after": "removed"})
    for key in new.keys() - old.keys():
        changes.append({"field": f"variants[{key}]", "before": None, "after": "added"})
    for key in old.keys() & new.keys():
        for f in _VARIANT_FIELDS:
            if old[key].get(f) != new[key].get(f):
                changes.append({"field": f"variants[{key}].{f}", "before": old[key].get(f), "after": new[key].get(f)})
    return changes


_HIGH = re.compile(r"\.(price_minor|compare_at_minor|currency|inventory)$")


def classify_risk(operation: str, changes: list[dict[str, Any]]) -> str:
    """Risk is independent of any AI confidence (06_AI_Design 3): price, stock, deletion,
    SKU, brand and category changes are always high."""
    if operation in ("delist", "delete"):
        return "high"
    if changes and changes[0]["field"] == "*":
        return "low"
    for change in changes:
        field = change["field"]
        if _HIGH.search(field) or field in ("brand", "category", "taxonomy_id"):
            return "high"
        if field.startswith("variants[") and change["after"] in ("removed", "added"):
            return "high"  # a removed variant is a deletion; an added/renamed SKU changes identity
    if any(c["field"] in ("status", "handle") for c in changes):
        return "medium"
    return "low"
