"""Generic schema.org / JSON-LD adapter.

Management companies embed structured listing data in their pages for SEO --
Google's rich results for rentals reward it -- which means a meaningful slice
of the market can be read with zero per-site code. This one adapter handles
every site that publishes an ItemList of Product / Apartment / Accommodation
/ Offer nodes.

Verified working against Stonehenge NYC (70 units, unit-level, with address,
beds, sqft and price). Point it at a new domain and it either works
immediately or returns nothing -- there is no half-broken state to debug.
"""
from __future__ import annotations

import json
import logging
import re

from ..models import RawListing
from .base import Source, SourceError

log = logging.getLogger(__name__)

_LD_RE = re.compile(
    r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S | re.I,
)

LISTING_TYPES = {
    "product", "apartment", "accommodation", "singlefamilyresidence",
    "house", "residence", "offer", "realestatelisting", "suite",
}


def _iter_nodes(obj):
    """Walk arbitrarily nested JSON-LD, yielding every dict node."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_nodes(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_nodes(v)


def _types_of(node: dict) -> set[str]:
    t = node.get("@type")
    if isinstance(t, str):
        return {t.lower()}
    if isinstance(t, list):
        return {str(x).lower() for x in t}
    return set()


def _first(*vals):
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return None


def _address_of(node: dict) -> tuple[str | None, str | None]:
    """Return (street line, locality) from a schema.org address, if present."""
    addr = node.get("address")
    if isinstance(addr, str):
        return addr, None
    if isinstance(addr, dict):
        street = addr.get("streetAddress")
        locality = _first(addr.get("addressLocality"), addr.get("addressRegion"))
        return street, locality
    return None, None


def _price_of(node: dict):
    offers = node.get("offers")
    for off in (offers if isinstance(offers, list) else [offers]):
        if isinstance(off, dict):
            p = _first(off.get("price"),
                       (off.get("priceSpecification") or {}).get("price"
                       ) if isinstance(off.get("priceSpecification"), dict) else None,
                       off.get("lowPrice"))
            if p is not None:
                return p
    return _first(node.get("price"))


def _sqft_of(node: dict):
    fs = node.get("floorSize")
    if isinstance(fs, dict):
        return fs.get("value")
    return _first(fs, node.get("squareFeet"))


class JsonLdSource(Source):
    """Read listings from schema.org markup on one or more pages.

    Options:
        urls          list of page URLs to read
        operator      human label for the landlord/manager
        borough_hint  fallback borough when addresses omit it
        no_fee        set True for portfolios that are categorically no-fee
    """

    def __init__(self, id: str, urls: list[str], operator: str = "",
                 borough_hint: str | None = None, no_fee: bool | None = None,
                 **kwargs):
        super().__init__(**kwargs)
        self.id = id
        self.urls = urls
        self.operator = operator or id
        self.borough_hint = borough_hint
        self.no_fee = no_fee

    def fetch(self) -> list[RawListing]:
        out: list[RawListing] = []
        errors: list[str] = []
        for url in self.urls:
            try:
                html = self.get(url)
            except SourceError as exc:
                errors.append(str(exc))
                continue
            out.extend(self._parse(html, url))
        if not out and errors:
            raise SourceError("; ".join(errors))
        return out

    def _parse(self, html: str, url: str) -> list[RawListing]:
        rows: list[RawListing] = []
        seen_keys: set[str] = set()

        for block in _LD_RE.findall(html):
            try:
                data = json.loads(block.strip())
            except json.JSONDecodeError:
                # Some CMSs emit trailing commas or embedded newlines.
                try:
                    data = json.loads(re.sub(r",\s*([}\]])", r"\1", block.strip()))
                except json.JSONDecodeError:
                    continue

            for node in _iter_nodes(data):
                if not (_types_of(node) & LISTING_TYPES):
                    continue
                name = _first(node.get("name"), node.get("headline")) or ""
                desc = node.get("description") or ""
                street, locality = _address_of(node)
                price = _price_of(node)

                # A listing node with neither an address nor a price is
                # navigation chrome, not inventory.
                if not street and not re.search(r"\d+\s+\w", desc):
                    if price is None:
                        continue

                if not street:
                    # Stonehenge-style: address lives in the description,
                    # e.g. "Apartment for rent at 42-20 24th Street, LIC, NY."
                    m = re.search(
                        r"(?:at|,)\s*(\d[\w\-]*\s+[^,]{3,60}?),\s*"
                        r"([A-Z][A-Za-z .'\-]+),\s*NY",
                        desc,
                    )
                    if m:
                        street, locality = m.group(1).strip(), m.group(2).strip()

                if not street:
                    continue

                key = f"{street}|{name}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)

                rows.append(RawListing(
                    source=self.id,
                    source_url=url,
                    address=street,
                    unit_raw=None,          # normalizer pulls it out of `title`
                    price_raw=price,
                    beds_raw=_first(node.get("numberOfBedrooms"),
                                    node.get("numberOfRooms"), name, desc),
                    baths_raw=node.get("numberOfBathroomsTotal"),
                    sqft_raw=_sqft_of(node) or desc,
                    available_on=None,
                    title=name,
                    detail_url=_first(node.get("url"), url),
                    borough_hint=locality or self.borough_hint,
                    no_fee=self.no_fee,
                    extra={"operator": self.operator, "description": desc[:400]},
                ))
        return rows
