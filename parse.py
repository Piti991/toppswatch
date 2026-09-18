"""Stock detection for es.topps.com product pages.

Detection runs in confidence order and stops at the first conclusive answer:

  1. JSON-LD   (<script type="application/ld+json"> -> offers.availability)
  2. Meta tags (og:availability / product:availability)
  3. Embedded JSON near the product handle ("available": true/false)
  4. Visible text markers ("Agotado" vs "Anadir a la cesta")

Every result carries the method and the evidence string that produced it, so
`--once --debug` tells you exactly why a verdict was reached.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Iterable

try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    BeautifulSoup = None


# --------------------------------------------------------------------------
# Marker vocabulary (accent-insensitive, lowercased before matching)
# --------------------------------------------------------------------------

IN_STOCK_MARKERS = [
    "anadir a la cesta",
    "anadir rapido",
    "add to cart",
    "add to bag",
    "quick add",
    "comprar ahora",
    "buy now",
]

OUT_OF_STOCK_MARKERS = [
    "agotado",
    "sold out",
    "sin existencias",
    "sin stock",
    "no disponible",
    "out of stock",
    "avisame",
    "notify me",
    "unavailable",
]

# Everything after one of these headings belongs to recommendation carousels,
# not the product itself. Trimming here stops a sold-out related item from
# poisoning the verdict.
RELATED_SECTION_MARKERS = [
    "tambien te puede interesar",
    "tambien te podria gustar",
    "productos relacionados",
    "you may also like",
    "related products",
    "recently viewed",
    "vistos recientemente",
    "completa tu coleccion",
]

SCHEMA_IN_STOCK = {"instock", "onlineonly", "instoreonly", "limitedavailability"}
SCHEMA_OUT_OF_STOCK = {"outofstock", "soldout", "discontinued", "backorder"}
SCHEMA_PREORDER = {"preorder", "presale"}


@dataclass
class StockResult:
    """Outcome of parsing a single product page."""

    in_stock: bool | None = None
    title: str | None = None
    price: str | None = None
    method: str = "none"
    evidence: str = ""
    preorder: bool = False
    notes: list[str] = field(default_factory=list)

    @property
    def conclusive(self) -> bool:
        return self.in_stock is not None


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def normalize(text: str) -> str:
    """Lowercase and strip accents so 'Añadir' matches 'anadir'."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    stripped = stripped.replace("\u00ae", "").replace("\u2122", "")
    return re.sub(r"\s+", " ", stripped).strip().lower()


def visible_text(html: str) -> str:
    """Extract rendered text, dropping script/style noise."""
    if BeautifulSoup is None:  # pragma: no cover
        return re.sub(r"<[^>]+>", " ", html)
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return soup.get_text(" ", strip=True)


def trim_related(text_norm: str) -> str:
    """Cut the page at the first recommendation carousel heading."""
    cut = len(text_norm)
    for marker in RELATED_SECTION_MARKERS:
        idx = text_norm.find(marker)
        if idx != -1:
            cut = min(cut, idx)
    return text_norm[:cut]


def _iter_json_objects(payload: Any) -> Iterable[dict]:
    """Walk arbitrarily nested JSON, yielding every dict."""
    if isinstance(payload, dict):
        yield payload
        for value in payload.values():
            yield from _iter_json_objects(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _iter_json_objects(item)


def _classify_availability(value: str) -> tuple[bool | None, bool]:
    """Map a schema.org availability string to (in_stock, is_preorder)."""
    token = normalize(value).replace(" ", "")
    token = token.rsplit("/", 1)[-1]
    if token in SCHEMA_IN_STOCK:
        return True, False
    if token in SCHEMA_OUT_OF_STOCK:
        return False, False
    if token in SCHEMA_PREORDER:
        return True, True
    return None, False


# --------------------------------------------------------------------------
# Strategy 1 — JSON-LD
# --------------------------------------------------------------------------


def from_json_ld(html: str) -> StockResult:
    result = StockResult(method="json-ld")
    blocks = re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html,
        re.DOTALL | re.IGNORECASE,
    )
    for block in blocks:
        try:
            payload = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for obj in _iter_json_objects(payload):
            types = obj.get("@type")
            types = [types] if isinstance(types, str) else (types or [])
            if not any(normalize(str(t)) in {"product", "productgroup"} for t in types):
                continue
            if isinstance(obj.get("name"), str):
                result.title = obj["name"]
            offers = obj.get("offers")
            offer_list = offers if isinstance(offers, list) else [offers]
            for offer in offer_list:
                if not isinstance(offer, dict):
                    continue
                if offer.get("price") is not None:
                    result.price = str(offer["price"])
                availability = offer.get("availability")
                if isinstance(availability, str):
                    in_stock, preorder = _classify_availability(availability)
                    if in_stock is not None:
                        result.in_stock = in_stock
                        result.preorder = preorder
                        result.evidence = f"offers.availability={availability}"
                        return result
    return result


# --------------------------------------------------------------------------
# Strategy 2 — meta tags
# --------------------------------------------------------------------------


def from_meta_tags(html: str) -> StockResult:
    result = StockResult(method="meta")
    pattern = (
        r'<meta[^>]+(?:property|name)=["\'](?:og:availability|product:availability|'
        r'availability)["\'][^>]+content=["\']([^"\']+)["\']'
    )
    for match in re.finditer(pattern, html, re.IGNORECASE):
        in_stock, preorder = _classify_availability(match.group(1))
        if in_stock is not None:
            result.in_stock = in_stock
            result.preorder = preorder
            result.evidence = f"meta availability={match.group(1)}"
            return result
    return result


# --------------------------------------------------------------------------
# Strategy 3 — embedded JSON near the product handle
# --------------------------------------------------------------------------

_AVAILABLE_KEY = re.compile(
    r'\\?"(available|availableForSale|isAvailable|inStock|purchasable)\\?"\s*:\s*'
    r'\\?"?(true|false)\\?"?',
    re.IGNORECASE,
)
_QUANTITY_KEY = re.compile(
    r'\\?"(quantityAvailable|inventoryQuantity|availableQuantity|stockLevel)\\?"'
    r'\s*:\s*(-?\d+)',
    re.IGNORECASE,
)


def from_embedded_json(html: str, handle: str | None = None, window: int = 4000) -> StockResult:
    """Scan Next.js / RSC payloads for availability flags.

    When `handle` is given, only flags within `window` characters of a mention
    of that handle count — otherwise a sold-out recommendation elsewhere in the
    payload could flip the verdict.
    """
    result = StockResult(method="embedded-json")

    spans: list[tuple[int, int]] = []
    if handle:
        for match in re.finditer(re.escape(handle), html):
            spans.append((max(0, match.start() - window), match.end() + window))
    if not spans:
        spans = [(0, len(html))]

    def in_span(pos: int) -> bool:
        return any(start <= pos <= end for start, end in spans)

    flags: list[tuple[str, bool]] = []
    for match in _AVAILABLE_KEY.finditer(html):
        if in_span(match.start()):
            flags.append((match.group(0)[:80], match.group(2).lower() == "true"))
    for match in _QUANTITY_KEY.finditer(html):
        if in_span(match.start()):
            flags.append((match.group(0)[:80], int(match.group(2)) > 0))

    if not flags:
        return result

    values = {value for _, value in flags}
    if len(values) == 1:
        result.in_stock = values.pop()
        result.evidence = "; ".join(text for text, _ in flags[:3])
        return result

    # Mixed signals inside the window — refuse to guess, let text markers decide.
    result.notes.append(
        f"embedded json had {len(flags)} conflicting availability flags; skipped"
    )
    return result


# --------------------------------------------------------------------------
# Strategy 4 — visible text markers
# --------------------------------------------------------------------------


def from_text_markers(html: str) -> StockResult:
    result = StockResult(method="text")
    text_norm = trim_related(normalize(visible_text(html)))

    oos_hit = next((m for m in OUT_OF_STOCK_MARKERS if m in text_norm), None)
    in_hit = next((m for m in IN_STOCK_MARKERS if m in text_norm), None)

    if in_hit and not oos_hit:
        result.in_stock = True
        result.evidence = f"found {in_hit!r}"
    elif oos_hit and not in_hit:
        result.in_stock = False
        result.evidence = f"found {oos_hit!r}"
    elif in_hit and oos_hit:
        # Both present: whichever appears first is the product's own button,
        # since the product block precedes anything else on the page.
        if text_norm.index(in_hit) < text_norm.index(oos_hit):
            result.in_stock = True
            result.evidence = f"{in_hit!r} precedes {oos_hit!r}"
        else:
            result.in_stock = False
            result.evidence = f"{oos_hit!r} precedes {in_hit!r}"
        result.notes.append("both in-stock and out-of-stock markers present")

    price = re.search(r"[€$£]\s?\d[\d.,]*", text_norm)
    if price:
        result.price = price.group(0).replace(" ", "")
    return result


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def extract_title(html: str) -> str | None:
    match = re.search(
        r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']',
        html,
        re.IGNORECASE,
    )
    if match:
        return match.group(1).strip()
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.DOTALL | re.IGNORECASE)
    if match:
        return re.sub(r"\s+", " ", match.group(1)).strip()
    return None


def detect_stock(html: str, handle: str | None = None, strategies: list[str] | None = None) -> StockResult:
    """Run detection strategies in order, returning the first conclusive result."""
    available = {
        "json-ld": lambda: from_json_ld(html),
        "meta": lambda: from_meta_tags(html),
        "embedded-json": lambda: from_embedded_json(html, handle),
        "text": lambda: from_text_markers(html),
    }
    order = strategies or ["json-ld", "meta", "embedded-json", "text"]

    notes: list[str] = []
    fallback = StockResult(method="none", evidence="no strategy produced a verdict")

    for name in order:
        runner = available.get(name)
        if runner is None:
            notes.append(f"unknown strategy {name!r}")
            continue
        result = runner()
        notes.extend(result.notes)
        if result.title and not fallback.title:
            fallback.title = result.title
        if result.price and not fallback.price:
            fallback.price = result.price
        if result.conclusive:
            result.title = result.title or fallback.title or extract_title(html)
            result.price = result.price or fallback.price
            result.notes = notes
            return result

    fallback.title = fallback.title or extract_title(html)
    fallback.notes = notes
    return fallback


# --------------------------------------------------------------------------
# Product discovery from collection / sitemap pages
# --------------------------------------------------------------------------

PRODUCT_HREF = re.compile(r'/products/([A-Za-z0-9%\u00ae\u2122._~\-]+)')


def discover_handles(html: str) -> list[str]:
    """Pull unique /products/<handle> slugs out of a listing or sitemap page."""
    seen: dict[str, None] = {}
    for match in PRODUCT_HREF.finditer(html):
        handle = match.group(1).strip().rstrip("/")
        if not handle or handle.endswith((".js", ".css", ".json", ".map")):
            continue
        seen.setdefault(handle, None)
    return list(seen)


def handle_matches(handle: str, patterns: list[str]) -> bool:
    """True if the handle matches any configured keyword/regex pattern."""
    target = normalize(handle.replace("%C2%AE", "").replace("-", " "))
    for pattern in patterns:
        try:
            if re.search(normalize(pattern), target):
                return True
        except re.error:
            if normalize(pattern) in target:
                return True
    return False
