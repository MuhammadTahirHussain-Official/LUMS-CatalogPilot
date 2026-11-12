"""Normalizes product data that arrives from another platform into the catalog's shape.

Data received from a store is untrusted input and is validated like user input (SEC-OUT-08):
every string is cleaned and length-limited, HTML is sanitized, numbers are range-checked,
URLs must be http(s). Anything unusable raises ``ValueError`` with a seller-readable reason,
so one bad product is reported and skipped instead of stopping an import.
"""

from __future__ import annotations

import re
from typing import Any

from .cleaning import clean_text, normalize_gtin, sanitize_html, slugify

MAX_VARIANTS = 2000
MAX_MEDIA = 250
_CURRENCY = re.compile(r"^[A-Z]{3}$")
_URL = re.compile(r"^https?://[^\s<>\"']{4,2000}$", re.I)


def _localized(value: Any, locale: str, limit: int, html: bool = False) -> dict[str, str]:
    if isinstance(value, str):
        value = {locale: value}
    if not isinstance(value, dict):
        return {}
    out = {}
    for key, text in list(value.items())[:20]:
        if not isinstance(text, str) or not re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?", str(key)):
            continue
        cleaned = sanitize_html(text[: limit * 2])[:limit] if html else clean_text(text, limit)
        if cleaned:
            out[str(key)] = cleaned
    return out


def _int(value: Any, name: str, maximum: int) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:  # bool, text or NaN
        raise ValueError(f"{name} is not a number")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError(f"{name} is not a whole number")
    number = int(value)
    if number < 0 or number > maximum:
        raise ValueError(f"{name} is out of range")
    return number


def normalize_state(raw: Any, locale: str, default_currency: str) -> tuple[dict[str, Any], list[str]]:
    """Returns (state, notes). Notes are non-fatal things the seller should know about."""
    if not isinstance(raw, dict):
        raise ValueError("the store returned something that is not a product")
    notes: list[str] = []
    title = _localized(raw.get("title"), locale, 500)
    if not title:
        raise ValueError("the product has no title")
    variants_in = raw.get("variants") or []
    if not isinstance(variants_in, list):
        raise ValueError("the product's variants are unreadable")
    if len(variants_in) > MAX_VARIANTS:
        notes.append(f"only the first {MAX_VARIANTS} of {len(variants_in)} variants were imported")
        variants_in = variants_in[:MAX_VARIANTS]
    variants = []
    seen_skus: set[str] = set()
    for index, v in enumerate(variants_in):
        if not isinstance(v, dict):
            raise ValueError(f"variant {index + 1} is unreadable")
        currency = str(v.get("currency") or default_currency).upper()
        if not _CURRENCY.match(currency):
            raise ValueError(f"variant {index + 1} has an invalid currency")
        sku = clean_text(v.get("sku"), 200) or None
        if sku and sku in seen_skus:
            notes.append(f"SKU '{sku}' appears on more than one variant")
        if sku:
            seen_skus.add(sku)
        options = v.get("option_values") if isinstance(v.get("option_values"), dict) else {}
        weight = v.get("weight_grams")
        variants.append({
            "sku": sku,
            "gtin": normalize_gtin(v.get("gtin")) if v.get("gtin") else None,
            "mpn": clean_text(v.get("mpn"), 120) or None,
            "price_minor": _int(v.get("price_minor"), "price", 10**13),
            "compare_at_minor": _int(v.get("compare_at_minor"), "compare-at price", 10**13),
            "cost_minor": _int(v.get("cost_minor"), "cost", 10**13),
            "currency": currency,
            "inventory": max(0, v["inventory"]) if isinstance(v.get("inventory"), int) and not isinstance(v.get("inventory"), bool) else None,
            "weight_grams": float(weight) if isinstance(weight, (int, float)) and not isinstance(weight, bool) and 0 <= weight < 10**7 else None,
            "option_values": {clean_text(k, 80): clean_text(val, 200) for k, val in list(options.items())[:10]
                              if clean_text(k, 80) and clean_text(val, 200)},
        })
    if not variants:
        variants = [{"sku": None, "gtin": None, "mpn": None, "price_minor": None, "compare_at_minor": None,
                     "cost_minor": None, "currency": default_currency, "inventory": None, "weight_grams": None,
                     "option_values": {}}]
        notes.append("the product has no price")
    media = []
    for m in (raw.get("media") or [])[: MAX_MEDIA * 2] if isinstance(raw.get("media"), list) else []:
        url = m.get("url") if isinstance(m, dict) else m
        if isinstance(url, str) and _URL.match(url.strip()) and len(media) < MAX_MEDIA:
            alt = m.get("alt") if isinstance(m, dict) else ""
            media.append({"url": url.strip(), "position": len(media),
                          "alt": {locale: clean_text(alt, 300)} if isinstance(alt, str) and clean_text(alt, 300) else {}})
    if not media:
        notes.append("no images")
    tags = raw.get("tags") if isinstance(raw.get("tags"), list) else []
    category = raw.get("source_category") if isinstance(raw.get("source_category"), list) else []
    status = raw.get("status") if raw.get("status") in ("draft", "active", "archived") else "draft"
    seo = raw.get("seo") if isinstance(raw.get("seo"), dict) else {}
    state = {
        "title": title,
        "description_html": _localized(raw.get("description_html"), locale, 65_000, html=True),
        "brand": clean_text(raw.get("brand"), 200) or None,
        "product_type": clean_text(raw.get("product_type"), 200) or None,
        "source_category": [clean_text(c, 120) for c in category[:10] if clean_text(c, 120)],
        "mapped_taxonomy_id": None,
        "tags": list(dict.fromkeys(clean_text(t, 120) for t in tags[:250] if clean_text(t, 120))),
        "status": status,
        "handle": slugify(str(raw.get("handle") or "")) or None,
        "seo": {k: clean_text(seo.get(k), 500) for k in ("title", "description") if clean_text(seo.get(k), 500)},
        "attributes": [], "media": media, "variants": variants, "platform_extras": {},
    }
    if not state["brand"]:
        notes.append("no brand")
    if not state["source_category"] and not state["product_type"]:
        notes.append("no category")
    return state, notes
