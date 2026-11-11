"""Column mapping: suggestions (rules first) and row -> UPM record conversion (HUB-06/08/09)."""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from ..catalog.cleaning import (
    MoneyError,
    clean_text,
    gtin_valid,
    normalize_gtin,
    parse_int,
    parse_money,
    parse_weight_grams,
    sanitize_html,
    slugify,
    split_list,
)

PRODUCT_FIELDS = [
    "external_id", "group_id", "handle", "title", "description", "brand", "product_type", "category",
    "tags", "status", "seo_title", "seo_description", "image_urls",
]
VARIANT_FIELDS = [
    "sku", "gtin", "mpn", "price", "compare_at_price", "cost", "currency", "inventory", "weight",
    "option1_name", "option1_value", "option2_name", "option2_value", "option3_name", "option3_value",
]
TARGET_FIELDS = PRODUCT_FIELDS + VARIANT_FIELDS
KEY_FIELDS = ("sku", "handle", "gtin", "external_id")

_SYNONYMS: dict[str, list[str]] = {
    "external_id": ["id", "productid", "externalid", "itemid", "productcode"],
    "group_id": ["itemgroupid", "parentid", "parent", "parentsku", "groupid"],
    "handle": ["handle", "slug", "urlkey", "permalink"],
    "title": ["title", "name", "productname", "producttitle", "itemname"],
    "description": ["description", "bodyhtml", "body", "longdescription", "productdescription", "descriptionhtml"],
    "brand": ["brand", "vendor", "manufacturer", "brands"],
    "product_type": ["type", "producttype", "itemtype"],
    "category": ["category", "categories", "googleproductcategory", "productcategory", "collection", "department"],
    "tags": ["tags", "keywords", "labels"],
    "status": ["status", "published", "visibility", "availability"],
    "seo_title": ["seotitle", "metatitle"],
    "seo_description": ["seodescription", "metadescription"],
    "image_urls": ["imagesrc", "image", "images", "imagelink", "imageurl", "additionalimagelink", "photo", "picture"],
    "sku": ["sku", "variantsku", "itemsku", "stockkeepingunit", "articlenumber"],
    "gtin": ["gtin", "barcode", "variantbarcode", "ean", "upc", "isbn", "ean13"],
    "mpn": ["mpn", "manufacturerpartnumber", "partnumber"],
    "price": ["price", "variantprice", "regularprice", "saleprice", "unitprice", "retailprice"],
    "compare_at_price": ["compareatprice", "variantcompareatprice", "msrp", "listprice", "originalprice"],
    "cost": ["cost", "costperitem", "costprice", "wholesaleprice", "unitcost"],
    "currency": ["currency", "currencycode"],
    "inventory": ["inventory", "variantinventoryqty", "stock", "quantity", "qty", "stockquantity", "inventoryquantity"],
    "weight": ["weight", "variantgrams", "weightkg", "weightg", "shippingweight", "weightlbs"],
    "option1_name": ["option1name"], "option1_value": ["option1value", "size"],
    "option2_name": ["option2name"], "option2_value": ["option2value", "color", "colour"],
    "option3_name": ["option3name"], "option3_value": ["option3value", "material"],
}
_LOOKUP = {syn: target for target, syns in _SYNONYMS.items() for syn in syns}
_WEIGHT_HINT = {"variantgrams": "g", "weightkg": "kg", "weightg": "g", "weightlbs": "lb"}
_IMPLIED_OPTION = {"size": "Size", "color": "Color", "colour": "Colour", "material": "Material"}


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", header.lower())


def suggest_mapping(columns: list[str], sample: list[dict[str, str]]) -> dict[str, Any]:
    """Deterministic suggestion with a confidence per column. The user always confirms."""
    used: set[str] = set()
    out: dict[str, str | None] = {}
    confidence: dict[str, float] = {}
    weight_unit = "g"
    option_names: dict[str, str] = {}
    for col in columns:
        norm = _norm(col)
        target = _LOOKUP.get(norm)
        score = 1.0 if target else 0.0
        if target is None:
            # Prefix/suffix tolerant match: "Variant Price (USD)" -> price
            for syn, cand in _LOOKUP.items():
                if len(syn) >= 4 and (norm.startswith(syn) or norm.endswith(syn)):
                    target, score = cand, 0.7
                    break
        if target == "price" and norm == "saleprice" and "price" in used:
            target = None
        if target and target in used and target != "image_urls":
            target, score = None, 0.0
        if target:
            used.add(target)
            if norm in _WEIGHT_HINT:
                weight_unit = _WEIGHT_HINT[norm]
            if norm in _IMPLIED_OPTION:
                option_names[target.replace("_value", "_name")] = _IMPLIED_OPTION[norm]
        out[col] = target
        confidence[col] = score
    # Unmapped columns with real values become attributes rather than being dropped.
    for col in columns:
        if out[col] is None and any(r.get(col, "").strip() for r in sample):
            out[col] = f"attribute:{clean_text(col, 60)}"
            confidence[col] = 0.4
    key = next((k for k in ("handle", "sku", "external_id", "gtin") if k in used), None)
    return {
        "columns": out, "confidence": confidence, "key": key, "weight_unit": weight_unit,
        "option_names": option_names,
    }


def validate_mapping(mapping: dict[str, Any], columns: list[str]) -> list[str]:
    problems = []
    cols = mapping.get("columns") or {}
    targets = [t for t in cols.values() if t]
    for col in cols:
        if col not in columns:
            problems.append(f"column '{col}' is not in the file")
    for target in targets:
        if target not in TARGET_FIELDS and not target.startswith("attribute:"):
            problems.append(f"unknown target field '{target}'")
    dupes = {t for t in targets if targets.count(t) > 1 and t != "image_urls" and not t.startswith("attribute:")}
    for d in sorted(dupes):
        problems.append(f"'{d}' is mapped from more than one column")
    if "title" not in targets:
        problems.append("a column must be mapped to 'title'")
    key = mapping.get("key")
    if key not in KEY_FIELDS:
        problems.append("choose the key used to match existing products: sku, handle, gtin or external_id")
    elif key not in targets:
        problems.append(f"the key '{key}' must be mapped from a column")
    return problems


@dataclass
class RowIssue:
    row: int
    level: str  # error | warning
    message: str


@dataclass
class Record:
    external_key: str
    state: dict[str, Any]
    rows: list[int] = field(default_factory=list)


_STATUS = {
    "active": "active", "published": "active", "publish": "active", "true": "active", "1": "active",
    "yes": "active", "in stock": "active", "in_stock": "active", "visible": "active",
    "draft": "draft", "false": "draft", "0": "draft", "no": "draft", "hidden": "draft",
    "out of stock": "active", "out_of_stock": "active", "archived": "archived",
}


def build_records(
    rows: Iterable[dict[str, str]],
    mapping: dict[str, Any],
    locale: str,
    default_currency: str,
) -> tuple[list[Record], list[RowIssue], int]:
    """Groups rows into products (one row per variant) and converts values. Returns
    (records, issues, total_rows). A row with an error is skipped; others still import."""
    cols: dict[str, str] = {c: t for c, t in (mapping.get("columns") or {}).items() if t}
    by_target: dict[str, list[str]] = {}
    for col, target in cols.items():
        by_target.setdefault(target, []).append(col)
    key = mapping["key"]
    weight_unit = mapping.get("weight_unit", "g")
    option_names = mapping.get("option_names") or {}
    group_field = "group_id" if "group_id" in by_target else ("handle" if key == "handle" else None)

    def get(row: dict[str, str], target: str) -> str:
        for col in by_target.get(target, []):
            value = row.get(col, "")
            if value and value.strip():
                return value
        return ""

    records: dict[str, Record] = {}
    issues: list[RowIssue] = []
    sku_owner: dict[str, str] = {}
    total = 0
    for number, row in enumerate(rows, start=2):  # row 1 is the header
        total += 1
        try:
            key_value = clean_text(get(row, key), 300)
            identity = clean_text(get(row, group_field), 300) if group_field else ""
            identity = identity or key_value
            if not identity:
                raise ValueError(f"missing {key}")
            record = records.get(identity)
            if record is None:
                title = clean_text(get(row, "title"), 500)
                if not title:
                    raise ValueError("missing title")
                images: list[str] = []
                state = {
                    "title": {locale: title},
                    "description_html": {locale: sanitize_html(get(row, "description"))} if get(row, "description") else {},
                    "brand": clean_text(get(row, "brand"), 200) or None,
                    "product_type": clean_text(get(row, "product_type"), 200) or None,
                    "source_category": [clean_text(p, 120) for p in re.split(r"\s*>\s*", get(row, "category")) if p.strip()],
                    "tags": split_list(get(row, "tags")),
                    "status": _STATUS.get(get(row, "status").strip().lower(), "draft") if get(row, "status") else "draft",
                    "handle": slugify(get(row, "handle")) or None,
                    "seo": {k: v for k, v in {
                        "title": clean_text(get(row, "seo_title"), 255),
                        "description": clean_text(get(row, "seo_description"), 500),
                    }.items() if v},
                    "attributes": [], "media": [], "variants": [], "platform_extras": {},
                    "mapped_taxonomy_id": None,
                }
                if get(row, "external_id"):
                    state["platform_extras"]["external_id"] = clean_text(get(row, "external_id"), 300)
                record = Record(identity, state)
                record._images = images  # type: ignore[attr-defined]
                record._attrs = {}  # type: ignore[attr-defined]
            # Media: every mapped image column on every row, order preserved, de-duplicated.
            for col in by_target.get("image_urls", []):
                for url in re.split(r"[|,\s]+", row.get(col, "")):
                    url = url.strip()
                    if not url:
                        continue
                    if not re.match(r"^https?://[^\s]+$", url, re.I):
                        issues.append(RowIssue(number, "warning", f"image '{url[:80]}' is not an http(s) URL; skipped"))
                    elif url not in record._images:  # type: ignore[attr-defined]
                        record._images.append(url)  # type: ignore[attr-defined]
            for target in by_target:
                if target.startswith("attribute:"):
                    value = clean_text(get(row, target), 2000)
                    if value:
                        record._attrs.setdefault(target.split(":", 1)[1], value)  # type: ignore[attr-defined]

            variant: dict[str, Any] = {"sku": clean_text(get(row, "sku"), 200) or None}
            currency = clean_text(get(row, "currency"), 3).upper() or default_currency
            if not re.fullmatch(r"[A-Z]{3}", currency):
                raise ValueError(f"'{currency}' is not a currency code")
            variant["currency"] = currency
            for target, field_name in (("price", "price_minor"), ("compare_at_price", "compare_at_minor"), ("cost", "cost_minor")):
                parsed = parse_money(get(row, target) or None, currency)
                variant[field_name] = parsed[0] if parsed else None
                if parsed and parsed[1] != currency and not get(row, "currency"):
                    variant["currency"] = currency = parsed[1]
            gtin = normalize_gtin(get(row, "gtin") or None)
            if gtin and not gtin_valid(gtin):
                issues.append(RowIssue(number, "warning", f"barcode '{gtin[:20]}' has an invalid check digit; kept as-is"))
            variant["gtin"] = gtin
            variant["mpn"] = clean_text(get(row, "mpn"), 120) or None
            inventory = parse_int(get(row, "inventory") or None)
            if inventory is not None and inventory < 0:
                issues.append(RowIssue(number, "warning", "negative stock imported as 0"))
                inventory = 0
            variant["inventory"] = inventory
            variant["weight_grams"] = parse_weight_grams(get(row, "weight") or None, weight_unit)
            options = {}
            for n in ("1", "2", "3"):
                value = clean_text(get(row, f"option{n}_value"), 200)
                name = clean_text(get(row, f"option{n}_name"), 80) or option_names.get(f"option{n}_name") or f"Option {n}"
                if value and not (name.lower() == "title" and value.lower() == "default title"):
                    options[name] = value
            variant["option_values"] = options
            if variant["compare_at_minor"] is not None and variant["price_minor"] is not None \
                    and variant["compare_at_minor"] < variant["price_minor"]:
                issues.append(RowIssue(number, "warning", "compare-at price is lower than price"))
            if variant["sku"]:
                owner = sku_owner.setdefault(variant["sku"], identity)
                if owner != identity:
                    issues.append(RowIssue(number, "warning", f"SKU '{variant['sku']}' also appears on another product"))
                elif any(v["sku"] == variant["sku"] for v in record.state["variants"]):
                    raise ValueError(f"duplicate SKU '{variant['sku']}' within the same product")
            has_variant_data = any(variant[k] not in (None, {}, "") for k in
                                   ("sku", "price_minor", "gtin", "inventory", "option_values"))
            if has_variant_data or not record.state["variants"]:
                if len(record.state["variants"]) >= 2000:
                    raise ValueError("more than 2000 variants on one product")
                record.state["variants"].append(variant)
            record.rows.append(number)
            records[identity] = record
        except (ValueError, MoneyError) as exc:
            issues.append(RowIssue(number, "error", str(exc)))

    for record in records.values():
        record.state["media"] = [{"url": u, "position": i, "alt": {}} for i, u in enumerate(record._images)]  # type: ignore[attr-defined]
        record.state["attributes"] = [{"key": k, "value": v, "source": "explicit"} for k, v in record._attrs.items()]  # type: ignore[attr-defined]
    return list(records.values()), issues, total


def iter_error_rows(issues: list[RowIssue]) -> Iterator[list[str]]:
    yield ["row", "level", "message"]
    for issue in sorted(issues, key=lambda i: i.row):
        yield [str(issue.row), issue.level, issue.message]
