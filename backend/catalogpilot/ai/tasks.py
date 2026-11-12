"""AI task definitions: input minimization, the deterministic (rules) implementation of each
task, and the validators every output must pass regardless of which provider produced it.

Output from a model is untrusted input. Validators are the guardrail: they reject new URLs,
contact details, scripts, and any brand / number / unit that is not in the source (AIR-12/13).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any

from ..catalog.cleaning import clean_text, html_to_text, sanitize_html
from . import taxonomy

TASKS = ("title", "description", "attributes", "category")  # benchmarked and calibrated
ALL_TASKS = (*TASKS, "seo")  # seo has no benchmark yet: always reviewed, never auto-approved
PROMPT_VERSIONS = {"title": "title_normalize@3", "description": "description_cleanup@2",
                   "attributes": "attribute_extract@2", "category": "category_map@2", "seo": "seo_snippet@1"}
FIELD_OF = {"title": "title", "description": "description_html", "attributes": "attributes",
            "category": "mapped_taxonomy_id", "seo": "seo"}
RISK_OF = {"title": "low", "description": "low", "attributes": "low", "category": "high", "seo": "low"}

_URL = re.compile(r"(https?://|www\.)\S+|\b[\w.-]+\.(com|net|org|io|shop|store|co)\b(/\S*)?", re.I)
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_PHONE = re.compile(r"(?<!\w)(\+?\d[\d\s().-]{8,}\d)(?!\w)")
_NUMBER = re.compile(r"\d+(?:[.,]\d+)?")
_PROMO = re.compile(
    r"\b(free\s+shipping|best\s+price|hot\s+sale|limited\s+(time\s+)?offer|buy\s+now|sale!*|"
    r"100%\s+(original|genuine|authentic)|top\s+quality|cheap(est)?|discount(ed)?|clearance|"
    r"order\s+now|fast\s+delivery|new\s+arrival)\b[!.]*", re.I)
_CLAIMS = re.compile(
    r"\b(fda|ce|iso\s?\d*|organic|certified|clinically|medical|cures?|waterproof|hypoallergenic|"
    r"bpa[- ]free|guarantee[ds]?|warranty|patented|award[- ]winning|doctor|approved)\b", re.I)
_SMALL = {"a", "an", "and", "as", "at", "but", "by", "for", "in", "nor", "of", "on", "or", "the", "to", "with", "vs"}
_UNITS = r"(?:ml|l|g|kg|mg|oz|lb|lbs|cm|mm|m|in|inch|ft|w|v|mah|gb|tb|mb|hz|pcs|pc|pack|ct)"
_COLORS = ("black white red blue green yellow orange purple pink brown grey gray beige navy teal gold silver "
           "ivory maroon olive cream turquoise burgundy khaki charcoal").split()
_MATERIALS = ("cotton polyester wool silk linen leather suede denim nylon bamboo steel aluminum aluminium wood "
              "glass ceramic plastic rubber silicone canvas velvet fleece cashmere titanium brass copper").split()
_SIZE = re.compile(r"(?<![\w-])(XXS|XS|S|M|L|XL|XXL|XXXL|\dXL)(?![\w-])")
_DIM = re.compile(rf"(?<!\w)(\d+(?:[.,]\d+)?)\s?({_UNITS})(?!\w)", re.I)


# ---------------------------------------------------------------- input minimization

def minimize(task: str, product_state: dict[str, Any], locale: str) -> dict[str, Any]:
    """Only the fields the task needs leave the application (AIR-08). Prices, cost, stock,
    barcodes and anything resembling contact data are never sent."""
    def text(value: dict[str, str] | None) -> str:
        return (value or {}).get(locale) or next(iter((value or {}).values()), "")

    title = text(product_state.get("title"))
    description = html_to_text(text(product_state.get("description_html")))[:4000]
    base = {"title": title, "brand": product_state.get("brand") or "",
            "product_type": product_state.get("product_type") or "", "locale": locale}
    if task in ("description", "attributes", "category", "seo"):
        base["description"] = _EMAIL.sub("[email]", _PHONE.sub("[phone]", description))
    if task == "description":
        base["description_html"] = text(product_state.get("description_html"))[:8000]
    if task == "category":
        base["source_category"] = " > ".join(product_state.get("source_category") or [])
        base["tags"] = (product_state.get("tags") or [])[:20]
    return base


# ---------------------------------------------------------------- deterministic implementations

_ACRONYMS = {"usb", "led", "uv", "diy", "uhd", "hd", "tv", "dvd", "pc", "ac", "dc", "xl", "xxl", "xs", "xxs",
             "hdmi", "ssd", "gps", "lcd", "rgb", "nfc", "bpa", "spf", "eu", "uk", "usa", "us", "ii", "iii", "iv"}


def _recase_word(core: str, brand: str, first: bool) -> str:
    low = core.lower()
    if brand and low == brand.lower():
        return brand
    if any(ch.isdigit() for ch in core):
        return core.upper() if len(core) <= 4 else core  # 4k -> 4K; SKU-like tokens stay as written
    if "-" in core:
        return "-".join(_recase_word(part, brand, first and i == 0) if part else part
                        for i, part in enumerate(core.split("-")))
    if low in _ACRONYMS:
        return core.upper()
    if not first and low in _SMALL:
        return low
    if "'" in core:
        head, _, tail = core.partition("'")
        return head[:1].upper() + head[1:].lower() + "'" + tail.lower()
    return core[:1].upper() + core[1:].lower()


def _smart_title(text: str, brand: str, mode: str) -> str:
    """mode 'mixed': the seller's casing carries information, so only shouted words are fixed.
    mode 'upper' / 'lower': casing was lost; every word is re-cased from scratch."""
    out = []
    for i, word in enumerate(text.split(" ")):
        core = word.strip(",.;:()[]")
        if not core:
            continue
        if mode == "mixed":
            fixed = _recase_word(core, brand, i == 0) if core.isalpha() and core.isupper() and len(core) > 5 else core
        else:
            fixed = _recase_word(core, brand, i == 0)
        out.append(word.replace(core, fixed, 1))
    return " ".join(out)


DEFAULT_PREFERENCES: dict[str, Any] = {
    "title_style": "title",       # title | sentence | keep
    "remove_promo": True,         # strip "FREE SHIPPING!!!", "BEST PRICE" and similar
    "brand_first": False,         # put the brand at the start of the title when it is known
    "max_title_length": None,     # shorten at a word boundary (None = leave length alone)
}


def clean_preferences(raw: dict[str, Any] | None) -> dict[str, Any]:
    """The seller's answers to "how should we improve your products?". Unknown keys and
    out-of-range values are dropped, never trusted."""
    prefs = dict(DEFAULT_PREFERENCES)
    raw = raw if isinstance(raw, dict) else {}
    if raw.get("title_style") in ("title", "sentence", "keep"):
        prefs["title_style"] = raw["title_style"]
    for key in ("remove_promo", "brand_first"):
        if isinstance(raw.get(key), bool):
            prefs[key] = raw[key]
    length = raw.get("max_title_length")
    if isinstance(length, int) and not isinstance(length, bool) and 20 <= length <= 255:
        prefs["max_title_length"] = length
    return prefs


def _sentence_case(text: str, brand: str) -> str:
    out = []
    for i, word in enumerate(text.split(" ")):
        core = word.strip(",.;:()[]")
        low = core.lower()
        if not core:
            continue
        if brand and low == brand.lower():
            fixed = brand
        elif low in _ACRONYMS or any(ch.isdigit() for ch in core) or (core != low and core != core.upper() and core[1:] != core[1:].lower()):
            fixed = core.upper() if low in _ACRONYMS else core  # acronyms, model numbers, iPhone-style words
        elif i == 0:
            fixed = core[:1].upper() + core[1:].lower()
        else:
            fixed = low
        out.append(word.replace(core, fixed, 1))
    return " ".join(out)


def rule_title(inp: dict[str, Any]) -> dict[str, Any]:
    original = inp["title"]
    prefs = clean_preferences(inp.get("preferences"))
    brand = inp.get("brand") or ""
    text = original
    # A title is never the place for links or contact details. Drop the sentence that carries
    # one; if that would leave nothing, remove just the address.
    pieces = re.split(r"(?<=[.!?])\s+", text)
    kept = [s for s in pieces if not any(p.search(s) for p in (_URL, _EMAIL, _PHONE))]
    if kept and len(kept) < len(pieces):
        text = " ".join(kept)
    for pattern in (_URL, _EMAIL, _PHONE):
        text = pattern.sub(" ", text)
    if prefs["remove_promo"]:
        text = _PROMO.sub(" ", text)
    text = re.sub(r"[!*~|#]{1,}", " ", text)
    text = re.sub(r"([,.\-_/])\1+", r"\1", text)  # collapse repeated punctuation
    text = clean_text(text)
    while True:  # separators left dangling where promo text was removed
        trimmed = re.sub(r"^\s*[-,/]\s*|\s*[-,/]\s*$", "", text).strip()
        if trimmed == text:
            break
        text = trimmed
    text = re.sub(r"\s+[-,/]\s+(?=[-,/]\s)", " ", text)
    # Casing is judged on what is left after promo text is gone ("nice shirt - HOT SALE" is a
    # lower-case title). If no word is written Capitalized, the casing carries no information
    # ("mens cotton shirt BLUE") and the whole title is re-cased.
    letters = [c for c in text if c.isalpha()]
    upper = sum(1 for c in letters if c.isupper())
    has_capitalized = bool(re.search(r"(?<![A-Za-z])[A-Z][a-z]{2,}", text))
    mode = "upper" if upper > 0.6 * max(1, len(letters)) else "mixed" if has_capitalized else "lower"
    text = re.sub(r"\bmens\b", "Men's", text, flags=re.I)
    text = re.sub(r"\bwomens\b", "Women's", text, flags=re.I)
    notes = []
    if prefs["title_style"] == "sentence":
        text = clean_text(_sentence_case(_smart_title(text, brand, mode), brand))
        notes.append("sentence case")
    elif prefs["title_style"] == "title":
        text = clean_text(_smart_title(text, brand, mode))
    else:
        text = clean_text(text)
    if prefs["brand_first"] and brand and text:
        without = re.sub(rf"(?i)(?<!\w){re.escape(brand)}(?!\w)[\s,:-]*", "", text).strip(" -,:")
        if without and not text.lower().startswith(brand.lower()):
            text = clean_text(f"{brand} {without}")
            notes.append("brand first")
    limit = prefs["max_title_length"]
    if limit and len(text) > limit:
        cut = text[:limit + 1].rsplit(" ", 1)[0].rstrip(" ,-/")
        if len(cut) >= 10:
            text = cut
            notes.append(f"shortened to {limit} characters")
    removed = [m.group(0).strip() for m in _PROMO.finditer(original)] if prefs["remove_promo"] else []
    rationale = ("Removed promotional text; " if removed else "") + "tidied capitalization and spacing"
    return {"title": text or original, "changed": text != original and bool(text),
            "rationale": rationale + (" (" + ", ".join(notes) + ")" if notes else "") + ".",
            "removed_tokens": removed,
            # Re-cased from scratch, or restyled to the seller's taste: either way the result was
            # not covered by the benchmark the auto-approve threshold was measured on.
            "casing_recovered": mode != "mixed" or bool(notes)}


def rule_seo(inp: dict[str, Any]) -> dict[str, Any]:
    """Search-result title and description built only from the product's own words."""
    title = rule_title({**inp, "preferences": None})["title"]
    seo_title = title if len(title) <= 60 else title[:61].rsplit(" ", 1)[0].rstrip(" ,-/")
    # Whole sentences that carry a link or contact detail are dropped; removing only the
    # address would leave "Visit or mail" behind in a search result.
    sentences = re.split(r"(?<=[.!?])\s+", clean_text(inp.get("description") or ""))
    kept = [s for s in sentences if "[email]" not in s and "[phone]" not in s
            and not any(p.search(s) for p in (_URL, _EMAIL, _PHONE))]
    text = clean_text(_PROMO.sub(" ", " ".join(kept)))
    if len(text) > 155:
        cut = text[:156]
        stop = max(cut.rfind(". "), cut.rfind("! "))
        text = cut[: stop + 1] if stop >= 60 else cut.rsplit(" ", 1)[0].rstrip(" ,;:-") + "…"
    description = text or seo_title
    return {"seo_title": seo_title, "seo_description": description, "changed": True,
            "rationale": "Written from the product's own title and description for search results."}


def rule_description(inp: dict[str, Any]) -> dict[str, Any]:
    original = inp.get("description_html") or ""
    html = sanitize_html(original)
    html = re.sub(r"<(p|span|li|b|strong|i|em|h2|h3|h4)>\s*(&nbsp;|\s)*</\1>", "", html)  # empty elements
    html = re.sub(r"(<br\s*/?>\s*){3,}", "<br><br>", html)
    removed = []

    def strip_sentences(match: re.Match) -> str:
        # Text between tags: drop each sentence that carries a link, email address or phone
        # number. Removing only the token would leave "Visit now!" behind.
        kept = []
        for sentence in re.split(r"(?<=[.!?])\s+", match.group(0)):
            hits = [m.group(0) for pat in (_URL, _EMAIL, _PHONE) for m in pat.finditer(sentence)]
            if hits:
                removed.extend(hits)
            else:
                kept.append(sentence)
        return " ".join(kept)

    html = re.sub(r"<a\b[^>]*>(.*?)</a>", r"\1", html, flags=re.S)  # unwrap links, keep their text
    html = re.sub(r"(?<=>)[^<>]+(?=<)|^[^<>]+$", strip_sentences, html)
    html = re.sub(r"<(p|li)>\s*</\1>", "", html)
    html = re.sub(r"[ \t]{2,}", " ", html).strip()
    return {"description_html": html, "changed": html != original,
            "rationale": ("Removed links and contact details; " if removed else "") + "cleaned formatting.",
            "removed_tokens": removed[:20]}


def rule_attributes(inp: dict[str, Any]) -> dict[str, Any]:
    text = f"{inp['title']} {inp.get('description', '')}"
    lowered = text.lower()
    found: dict[str, str] = {}
    colors = [c for c in _COLORS if re.search(rf"\b{c}\b", lowered)]
    if len(colors) == 1:  # two colours is ambiguous: say nothing rather than guess
        found["Color"] = colors[0].capitalize()  # the seller's own spelling is kept (gray stays gray)
    materials = [m for m in _MATERIALS if re.search(rf"\b{m}\b", lowered)]
    if len(materials) == 1:
        found["Material"] = materials[0].capitalize()
    sizes = _SIZE.findall(inp["title"])
    if len(set(sizes)) == 1:
        found["Size"] = sizes[0]
    dims = _DIM.findall(inp["title"])
    if len(dims) == 1:
        value, unit = dims[0]
        key = {"ml": "Volume", "l": "Volume", "g": "Weight", "kg": "Weight", "mg": "Weight", "oz": "Weight",
               "lb": "Weight", "lbs": "Weight", "w": "Power", "v": "Voltage", "mah": "Battery capacity",
               "gb": "Storage", "tb": "Storage", "mb": "Storage", "hz": "Frequency", "pcs": "Pack size",
               "pc": "Pack size", "pack": "Pack size", "ct": "Pack size"}.get(unit.lower(), "Length")
        found[key] = f"{value} {unit}"
    return {"attributes": [{"key": k, "value": v} for k, v in found.items()], "changed": bool(found),
            "rationale": "Found in the title or description." if found else "Nothing could be extracted with certainty."}


def rule_category(inp: dict[str, Any], candidates: list[tuple[taxonomy.Node, float]]) -> dict[str, Any]:
    if not candidates:
        return {"taxonomy_id": None, "changed": False, "rationale": "No category matched."}
    best = candidates[0][0]
    return {"taxonomy_id": best.id, "changed": True, "rationale": f"Best match: {best.path}."}


def category_query(inp: dict[str, Any]) -> str:
    # The title and type count double: a description mentions many incidental nouns.
    return " ".join([inp["title"], inp["title"], inp.get("product_type", ""), inp.get("product_type", ""),
                     inp.get("source_category", ""), " ".join(inp.get("tags") or []), inp.get("description", "")[:300]])


# ---------------------------------------------------------------- validators

@dataclass
class Report:
    checks: dict[str, bool] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)
    edit_ratio: float = 0.0

    def check(self, name: str, ok: bool, reason: str = "") -> None:
        self.checks[name] = ok
        if not ok and reason:
            self.reasons.append(reason)

    @property
    def passed(self) -> bool:
        return all(self.checks.values())

    @property
    def pass_ratio(self) -> float:
        return sum(self.checks.values()) / len(self.checks) if self.checks else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"checks": self.checks, "reasons": self.reasons, "edit_ratio": round(self.edit_ratio, 4)}


def _edit_ratio(a: str, b: str) -> float:
    return 1.0 - SequenceMatcher(None, a.lower(), b.lower(), autojunk=False).ratio()


def _numbers(text: str) -> set[str]:
    return {n.replace(",", ".") for n in _NUMBER.findall(text)}


def _contacts(text: str) -> set[str]:
    return {m.group(0).lower() for p in (_URL, _EMAIL, _PHONE) for m in p.finditer(text)}


def _no_new(source: str, output: str, report: Report) -> None:
    report.check("no_new_contacts_urls", _contacts(output) <= _contacts(source),
                 "adds a link, email address or phone number")
    report.check("no_new_numbers", _numbers(output) <= _numbers(source), "adds a number that is not in the source")
    new_claims = {m.group(0).lower() for m in _CLAIMS.finditer(output)} - {m.group(0).lower() for m in _CLAIMS.finditer(source)}
    report.check("no_new_claims", not new_claims, f"adds a claim not in the source: {', '.join(sorted(new_claims))}")
    report.check("no_markup", not re.search(r"<\s*(script|iframe|object|embed|style)|on\w+\s*=|javascript:", output, re.I),
                 "contains active content")


def validate_output(task: str, inp: dict[str, Any], out: dict[str, Any],
                    candidates: list[tuple[taxonomy.Node, float]] | None = None) -> Report:
    report = Report()
    source_all = " ".join(str(inp.get(k, "")) for k in ("title", "brand", "product_type", "description"))
    if task == "title":
        title = out.get("title")
        ok_type = isinstance(title, str) and 0 < len(title.strip()) <= 255 and "\n" not in title
        report.check("schema", ok_type, "title is missing, multi-line or too long")
        if not ok_type:
            return report
        # Removing recognised promotional phrases is the safest kind of edit, so the change is
        # measured against the title with those phrases already taken out (recomputed here,
        # never taken from the model's own account of what it removed).
        stripped = clean_text(re.sub(r"[\s\-,/!*|]+$", "", _PROMO.sub(" ", inp["title"])))
        report.edit_ratio = min(_edit_ratio(inp["title"], title), _edit_ratio(stripped, title))
        report.check("edit_ratio", report.edit_ratio <= 0.6, "changes too much of the title")
        _no_new(source_all, title, report)
        src_words = set(taxonomy._TOKEN.findall(source_all.lower()))
        new_words = {w for w in taxonomy._TOKEN.findall(title.lower()) if w not in src_words and len(w) > 2}
        new_words -= {"men", "women", "kids"}
        report.check("no_new_entities", not new_words, f"adds words not in the source: {', '.join(sorted(new_words)[:5])}")
        brand = (inp.get("brand") or "").lower()
        report.check("keeps_brand", not brand or brand not in inp["title"].lower() or brand in title.lower(),
                     "drops the brand name")
    elif task == "description":
        html = out.get("description_html")
        ok_type = isinstance(html, str) and len(html) <= 65_000
        report.check("schema", ok_type, "description is missing or too long")
        if not ok_type:
            return report
        report.check("safe_html", sanitize_html(html) == html.strip(), "contains markup outside the allow-list")
        source_text, output_text = html_to_text(inp.get("description_html") or ""), html_to_text(html)
        report.edit_ratio = _edit_ratio(source_text, output_text)
        report.check("edit_ratio", report.edit_ratio <= 0.6, "rewrites too much of the description")
        report.check("not_emptied", bool(output_text.strip()) or not source_text.strip(), "removes the whole description")
        _no_new(source_text + " " + source_all, output_text, report)
    elif task == "attributes":
        attrs = out.get("attributes")
        ok_type = isinstance(attrs, list) and len(attrs) <= 30 and all(
            isinstance(a, dict) and isinstance(a.get("key"), str) and isinstance(a.get("value"), str)
            and 0 < len(a["key"]) <= 60 and 0 < len(a["value"]) <= 200 for a in attrs)
        report.check("schema", ok_type, "attributes are malformed")
        if not ok_type:
            return report
        lowered = source_all.lower()
        ungrounded = [a["value"] for a in attrs if a["value"].lower().replace(" ", "") not in lowered.replace(" ", "")]
        report.check("grounded", not ungrounded, f"values not found in the source: {', '.join(ungrounded[:3])}")
        _no_new(source_all, " ".join(a["value"] for a in attrs), report)
    elif task == "seo":
        seo_title, seo_desc = out.get("seo_title"), out.get("seo_description")
        ok_type = (isinstance(seo_title, str) and isinstance(seo_desc, str) and 0 < len(seo_title) <= 70
                   and 0 < len(seo_desc) <= 160 and len(seo_title.splitlines()) == 1)
        report.check("schema", ok_type, "search title or description is missing or too long")
        if not ok_type:
            return report
        combined = f"{seo_title} {seo_desc}"
        _no_new(source_all, combined, report)
        # Stricter than "nothing new": search text may carry no link or contact detail at all,
        # even one that was already in the product's description.
        report.check("no_contacts", not _contacts(combined) and "[email]" not in combined and "[phone]" not in combined,
                     "contains a link or contact detail")
        src_words = set(taxonomy._TOKEN.findall(source_all.lower())) | {"men", "women"}
        new_words = {w for w in taxonomy._TOKEN.findall(combined.lower()) if w not in src_words and len(w) > 2}
        report.check("no_new_entities", not new_words, f"adds words not in the source: {', '.join(sorted(new_words)[:5])}")
    elif task == "category":
        allowed = {node.id for node, _ in (candidates or [])}
        chosen = out.get("taxonomy_id")
        report.check("schema", chosen is None or isinstance(chosen, str), "category id is malformed")
        report.check("in_candidates", chosen is None or chosen in allowed, "category is not one of the candidates")
    return report
