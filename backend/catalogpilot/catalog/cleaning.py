"""Deterministic cleaning (FR-17). Rules first: anything here never needs a model."""

from __future__ import annotations

import re
import unicodedata
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import nh3

ALLOWED_TAGS = {
    "p", "br", "b", "strong", "i", "em", "u", "ul", "ol", "li", "h2", "h3", "h4", "span",
    "table", "thead", "tbody", "tr", "td", "th", "a", "blockquote",
}
ALLOWED_ATTRS = {"a": {"href", "title"}, "td": {"colspan", "rowspan"}, "th": {"colspan", "rowspan"}}

# Bidirectional overrides and zero-width characters used for spoofing (SEC-WEB-14).
_INVISIBLE = re.compile("[​-‏‪-‮⁠-⁩﻿]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WS = re.compile(r"\s+")

# ISO 4217 minor-unit exponents that differ from 2.
_EXPONENTS = {"JPY": 0, "KRW": 0, "VND": 0, "CLP": 0, "ISK": 0, "HUF": 2, "BHD": 3, "KWD": 3, "OMR": 3, "JOD": 3, "TND": 3}
_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR", "₨": "PKR"}


def clean_text(value: object, max_len: int | None = None) -> str:
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    text = _INVISIBLE.sub("", _CONTROL.sub("", text))
    text = _WS.sub(" ", text).strip()
    return text[:max_len] if max_len else text


def sanitize_html(value: object) -> str:
    """Strict allow-list sanitization; applied on ingest and again before publish (SEC-WEB-03)."""
    if value is None:
        return ""
    text = unicodedata.normalize("NFC", str(value))
    text = _INVISIBLE.sub("", _CONTROL.sub("", text))
    return nh3.clean(
        text,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        url_schemes={"http", "https", "mailto"},
        link_rel="noopener noreferrer nofollow",
        strip_comments=True,
    ).strip()


def html_to_text(html: str) -> str:
    return clean_text(nh3.clean(html or "", tags=set()))


def slugify(value: str) -> str:
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return text[:200]


def gtin_valid(code: str) -> bool:
    """GS1 check digit for GTIN-8/12/13/14. Invalid identifiers warn; they are never 'fixed'."""
    if not code.isdigit() or len(code) not in (8, 12, 13, 14):
        return False
    digits = [int(c) for c in code]
    body, check = digits[:-1], digits[-1]
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


def normalize_gtin(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    # Spreadsheets turn barcodes into floats ("1234567890123.0") or scientific notation.
    if re.fullmatch(r"\d+\.0+", text):
        text = text.split(".")[0]
    elif re.fullmatch(r"\d(\.\d+)?[eE]\+?\d+", text):
        try:
            text = str(int(Decimal(text)))
        except InvalidOperation:
            return text
    text = re.sub(r"[\s-]", "", text)
    return text or None


class MoneyError(ValueError):
    pass


def currency_exponent(currency: str) -> int:
    return _EXPONENTS.get(currency.upper(), 2)


def parse_money(value: object, default_currency: str) -> tuple[int, str] | None:
    """Parses '1,299.50', '1.299,50', '$19', '19.99 EUR' into integer minor units. Never floats."""
    if value is None:
        return None
    if isinstance(value, bool):
        raise MoneyError("not a number")
    currency = default_currency.upper()
    if isinstance(value, int):
        number = Decimal(value)
    elif isinstance(value, float):
        number = Decimal(repr(value))
    else:
        text = str(value).strip()
        if not text:
            return None
        for symbol, code in _CURRENCY_SYMBOLS.items():
            if symbol in text:
                currency = code
                text = text.replace(symbol, "")
        code_match = re.search(r"\b([A-Za-z]{3})\b", text)
        if code_match:
            currency = code_match.group(1).upper()
            text = text.replace(code_match.group(1), "")
        text = re.sub(r"[\s ']", "", text)
        if "," in text and "." in text:
            # The right-most separator is the decimal mark.
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            head, _, tail = text.rpartition(",")
            # '1,299' is a thousands group; '12,5' or '12,50' is a decimal comma.
            text = head.replace(",", "") + tail if len(tail) == 3 and head else head.replace(",", "") + "." + tail
        try:
            number = Decimal(text)
        except InvalidOperation as exc:
            raise MoneyError(f"cannot read '{value}' as a price") from exc
    if not number.is_finite():
        raise MoneyError("price is not finite")
    if number < 0:
        raise MoneyError("price cannot be negative")
    if number > Decimal(10) ** 11:  # checked before scaling: huge exponents cannot be quantized
        raise MoneyError("price is implausibly large")
    minor =(number * (Decimal(10) ** currency_exponent(currency))).quantize(Decimal(1), rounding=ROUND_HALF_UP)
    if minor > 10**13:
        raise MoneyError("price is implausibly large")
    return int(minor), currency


def format_money(minor: int | None, currency: str) -> str:
    if minor is None:
        return ""
    exp = currency_exponent(currency)
    return f"{Decimal(minor) / (Decimal(10) ** exp):.{exp}f}"


_WEIGHT_UNITS = {"g": Decimal(1), "kg": Decimal(1000), "mg": Decimal("0.001"), "lb": Decimal("453.59237"),
                 "lbs": Decimal("453.59237"), "oz": Decimal("28.349523125")}


def parse_weight_grams(value: object, default_unit: str = "g") -> float | None:
    if value is None or str(value).strip() == "":
        return None
    match = re.fullmatch(r"\s*([0-9]+(?:[.,][0-9]+)?)\s*([a-zA-Z]*)\s*", str(value))
    if not match:
        raise ValueError(f"cannot read '{value}' as a weight")
    unit = (match.group(2) or default_unit).lower()
    if unit not in _WEIGHT_UNITS:
        raise ValueError(f"unknown weight unit '{unit}'")
    grams = Decimal(match.group(1).replace(",", ".")) * _WEIGHT_UNITS[unit]
    return float(grams.quantize(Decimal("0.001")))


def parse_int(value: object) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        number = Decimal(str(value).strip().replace(",", ""))
    except InvalidOperation as exc:
        raise ValueError(f"cannot read '{value}' as a whole number") from exc
    if number != number.to_integral_value():
        raise ValueError(f"'{value}' is not a whole number")
    return int(number)


def split_list(value: object) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        parts = value
    else:
        parts = re.split(r"[,;|]", str(value))
    seen: dict[str, None] = {}
    for part in parts:
        cleaned = clean_text(part, 120)
        if cleaned:
            seen.setdefault(cleaned, None)
    return list(seen)
