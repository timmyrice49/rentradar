"""Index page -> per-unit page. The adapter the server-rendered operators need.

Discovery found JSON endpoints behind client-rendered sites, but on the
server-rendered half of the market it found nothing at all, twice: the shallow
pass missed them and the deep pass, following rendered building links, missed
them too. There is no XHR to capture because there is no XHR. Those operators
publish an availability index of links, and each link is a fully rendered page
with the unit's address, number, beds, baths and rent in the HTML.

Rockrose is the worked example: 52 unit links in the static source of
/availabilities/, each resolving to a page whose h1 reads "200 Water Street,
#708". No browser required for any of it.

This is the cheapest source type in the system -- plain HTTP, no rendering, no
endpoint to go stale -- and also the most brittle, since it depends on a page's
markup. So it leans on structure that rarely moves (the h1, a price line) and
declares a `min_expected` so a redesign fails the run loudly instead of quietly
emptying the operator's inventory.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit

from ..models import RawListing
from .base import Source, SourceError

log = logging.getLogger(__name__)

BOROUGHS = ["Manhattan", "Brooklyn", "Queens", "Bronx", "Staten Island"]


def borough_from_zip(zipcode: str | None) -> str | None:
    """NYC ZIP -> borough.

    Scanning the page text for a borough name is unreliable: Rockrose's
    footer says "Manhattan" on every page, so LIC and Brooklyn units were all
    being hinted as Manhattan -- and a wrong borough hint is worse than none,
    because it sends the geocoder to the wrong place. The ZIP is unambiguous.
    """
    if not zipcode or not zipcode.isdigit():
        return None
    z = int(zipcode)
    if 10001 <= z <= 10282:
        return "Manhattan"
    if 10301 <= z <= 10314:
        return "Staten Island"
    if 10451 <= z <= 10475:
        return "Bronx"
    if 11201 <= z <= 11256:
        return "Brooklyn"
    if z in (11004, 11005, 11109) or 11101 <= z <= 11120 or 11351 <= z <= 11697:
        return "Queens"
    return None

#: "Studio, 1 Bath, $4,795" / "2 Bed, 1.5 Baths, $6,200" / "1 Bedroom, 1 Bath"
_SPECS_RE = re.compile(
    r"(studio|\d+(?:\.\d)?\s*bed(?:room)?s?)\s*,\s*"
    r"(\d+(?:\.\d)?)\s*bath(?:room)?s?\s*"
    r"(?:,\s*\$\s?([\d,]+))?",
    re.I,
)
_PRICE_RE = re.compile(r"\$\s?([1-9][\d,]{2,6})(?:\s*/\s*(?:mo|month))?")
#: "200 Water Street, #708" -- address and unit in one heading.
_H1_RE = re.compile(r"^\s*(.+?)\s*,?\s*#\s*([A-Za-z0-9\-]+)\s*$")
_ZIP_RE = re.compile(r"\b(1\d{4})\b")
#: Concessions: "1 Month OP", "2 months free"
_CONCESSION_RE = re.compile(r"(\d+)\s*months?\s*(?:free|op\b)", re.I)


class HtmlIndexSource(Source):
    """Crawl an availability index, then each unit page it links to.

    Options:
        index_urls    pages listing unit links
        link_pattern  regex matching detail URLs in the index HTML
        max_details   cap on unit pages fetched per crawl
        borough_hint  fallback when the page does not name a borough
    """

    id = "htmlindex"
    delay = 1.0
    min_expected = 5

    def __init__(self, id: str, index_urls: list[str], link_pattern: str,
                 operator: str = "", max_details: int = 60,
                 borough_hint: str | None = None, no_fee: bool | None = None,
                 **kwargs):
        super().__init__(**kwargs)
        self.id = id
        self.index_urls = index_urls
        self.link_re = re.compile(link_pattern)
        self.operator = operator or id
        self.max_details = max_details
        self.borough_hint = borough_hint
        self.no_fee = no_fee

    # -- index -------------------------------------------------------------

    def _detail_links(self, html: str, base: str) -> list[str]:
        found: list[str] = []
        seen: set[str] = set()
        for m in self.link_re.finditer(html):
            url = urljoin(base, m.group(0))
            clean = urlsplit(url)._replace(query="", fragment="").geturl()
            if clean not in seen:
                seen.add(clean)
                found.append(clean)
        return found

    # -- detail ------------------------------------------------------------

    @staticmethod
    def _soup(html: str):
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            return None
        return BeautifulSoup(html, "html.parser")

    def _parse_detail(self, html: str, url: str) -> RawListing | None:
        soup = self._soup(html)
        if soup is not None:
            h1 = soup.find("h1")
            heading = h1.get_text(" ", strip=True) if h1 else ""
            h2 = soup.find("h2")
            neighborhood = h2.get_text(" ", strip=True) if h2 else None
            text = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))
        else:
            m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
            heading = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(1))).strip() \
                if m else ""
            neighborhood = None
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))

        if not heading:
            return None

        unit = None
        address = heading
        hm = _H1_RE.match(heading)
        if hm:
            address, unit = hm.group(1).strip(), hm.group(2).strip()

        # An h1 that is not an address is a marketing page, not a unit.
        if not re.match(r"\s*\d", address):
            return None

        beds = baths = price = None
        sm = _SPECS_RE.search(text)
        if sm:
            beds, baths, price = sm.group(1), sm.group(2), sm.group(3)
        if price is None:
            pm = _PRICE_RE.search(text)
            price = pm.group(1) if pm else None

        zipm = _ZIP_RE.search(text)
        zipcode = zipm.group(1) if zipm else None
        borough = borough_from_zip(zipcode)
        if borough is None and soup is not None:
            # Fall back to a borough named inside an address-ish element, so a
            # site-wide footer mention cannot win.
            el = soup.find(class_=re.compile("address", re.I))
            scope = el.get_text(" ", strip=True) if el else ""
            borough = next((b for b in BOROUGHS
                            if re.search(rf"\b{b}\b", scope)), None)
        months_free = None
        cm = _CONCESSION_RE.search(text)
        if cm:
            months_free = int(cm.group(1))

        # The path is the operator's own stable id for the unit.
        ref = urlsplit(url).path.strip("/").split("/")[-1] or url

        if neighborhood and (len(neighborhood) > 40 or "footer" in neighborhood.lower()):
            neighborhood = None

        return RawListing(
            source=self.id,
            source_url=url,
            address=address,
            unit_raw=unit,
            price_raw=price,
            beds_raw=beds,
            baths_raw=baths,
            title=heading,
            detail_url=url,
            borough_hint=borough or neighborhood or self.borough_hint,
            no_fee=self.no_fee,
            source_ref=ref,
            extra={
                "operator": self.operator,
                "neighborhood": neighborhood,
                "zip": zipcode,
                "months_free": months_free,
            },
        )

    # -- fetch -------------------------------------------------------------

    def fetch(self) -> list[RawListing]:
        links: list[str] = []
        errors: list[str] = []
        for index_url in self.index_urls:
            try:
                html = self.get(index_url)
            except SourceError as exc:
                errors.append(str(exc))
                continue
            new = [u for u in self._detail_links(html, index_url) if u not in links]
            links.extend(new)
            log.info("%s: %d unit links on %s", self.id, len(new), index_url)

        if not links:
            raise SourceError(
                f"{self.id}: no unit links matched on {len(self.index_urls)} index "
                f"page(s){' -- ' + errors[0] if errors else ''}")

        rows: list[RawListing] = []
        for url in links[: self.max_details]:
            try:
                page = self.get(url)
            except SourceError as exc:
                log.debug("%s: %s", self.id, exc)
                continue
            try:
                listing = self._parse_detail(page, url)
            except Exception:
                log.exception("%s: parse failed on %s", self.id, url)
                continue
            if listing:
                rows.append(listing)
        return rows
