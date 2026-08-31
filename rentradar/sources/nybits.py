"""NYBits — a NYC no-fee listing aggregator that is actually readable.

Why this source exists in a system built to avoid aggregators: the 122-operator
survey found that almost no NYC landlord exposes inventory to a static
crawler. NYBits does. It is server-rendered, permitted by robots.txt, carries
a stable per-listing id, and — the useful part — republishes inventory from
operators whose own sites are unreadable JavaScript shells (Rose Associates,
Beam Living, Gateway Plaza and others all appear here).

It is genuinely upstream of nothing, so it does not serve the lead-time
thesis on its own. What it does is give you real NYC inventory on day one:
enough to exercise the pipeline, seed the building graph, and measure how your
own upstream sources compare. Treat it as scaffolding and ground truth, not as
the product.

Two of its fields are worth more than the rent:

  * "Posted yesterday" / "Posted 3 days ago" — a publisher-supplied freshness
    signal, which is exactly the axis this whole system competes on.
  * "net effective rent" — the discounted headline number that hides free
    months. Gross rent is higher. Post-FARE-Act, true move-in cost is the
    honest measure of affordability, and conflating the two misleads renters.

Structure: 24 static search URLs (4 Manhattan sub-areas + Brooklyn + Queens,
each x studio/1br/2br/3+), 20 listings per page, ordered newest first.
Pagination is a session-bound POST with opaque tokens, so we deliberately do
not paginate — fanning out across narrow searches gets the fresh end of every
segment without touching session state.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote, urljoin

from ..models import RawListing
from .base import Source, SourceError

log = logging.getLogger(__name__)

BASE = "https://www.nybits.com"

AREAS = ["downtown-manhattan", "midtown-manhattan", "upper-manhattan",
         "uptown-manhattan", "brooklyn", "queens"]
BEDS = ["studio", "1br", "2br", "3more"]

#: Borough codes NYBits prints in the address parenthetical.
BOROUGH = {"MN": "Manhattan", "BK": "Brooklyn", "BX": "Bronx",
           "QN": "Queens", "SI": "Staten Island"}

_REF_RE = re.compile(r"/apartmentlistings/([0-9a-f]{16,64})\.html", re.I)
#: Some cards link to a session-bound handler instead of a listing page. The
#: jsessionid in those URLs churns every request, but the query parameter
#: carries a stable building/listing key ("113_sullivan_st:vtnzl"), which is
#: what we need for identity. Without this fallback those cards get no ref and
#: collide with their neighbours in the same building.
_ALT_REF_RE = re.compile(r"[?&]%21\w+=([a-z0-9_]+(?:%3A|:)[a-z0-9]+)", re.I)
#: NYBits prints freshness several ways: "Posted today", "Posted yesterday",
#: "Posted 3 days ago", "Posted < 11 hours". The last form is the most
#: valuable and was the one the first version of this pattern missed, which
#: also left the literal text stranded in the manager field.
_POSTED_RE = re.compile(
    r"posted\s+(?:<\s*)?(today|yesterday|\d+\s*(?:hour|day|week)s?(?:\s+ago)?)",
    re.I,
)


def posted_to_timestamp(posted: str | None, now: datetime | None = None) -> str | None:
    """"11 hours" / "yesterday" / "12 days ago" -> an ISO timestamp.

    This is the aggregator side of every lead-time measurement, and it is far
    better than using our own crawl time: on a cold crawl every listing looks
    new at the same instant, which would report a lead of zero for the entire
    database. Resolution is coarse (a day-grained stamp is a day-grained
    answer) and it is the publisher's claim rather than an observation, so
    treat it as approximate -- but approximate and real beats precise and
    meaningless.
    """
    if not posted:
        return None
    now = now or datetime.now(timezone.utc)
    p = posted.strip().lower()
    if p == "today":
        delta = timedelta(0)
    elif p == "yesterday":
        delta = timedelta(days=1)
    else:
        m = re.match(r"(\d+)\s*(hour|day|week)", p)
        if not m:
            return None
        n = int(m.group(1))
        unit = m.group(2)
        delta = {"hour": timedelta(hours=n),
                 "day": timedelta(days=n),
                 "week": timedelta(weeks=n)}[unit]
    return (now - delta).isoformat(timespec="seconds")


def default_searches() -> list[str]:
    return [f"{BASE}/search/{a}-rentals-{b}.html" for a in AREAS for b in BEDS]


class NyBitsSource(Source):
    """Parse NYBits search-result pages into RawListings.

    Options:
        searches  list of search URLs; defaults to all 24 area x bedroom pages
        max_pages cap on how many of those to fetch per crawl
    """

    id = "nybits"
    delay = 2.0            # deliberately gentle: this is someone's small site
    min_expected = 20

    def __init__(self, id: str = "nybits", searches: list[str] | None = None,
                 max_pages: int = 24, **kwargs):
        super().__init__(**kwargs)
        self.id = id
        self.searches = searches or default_searches()
        self.max_pages = max_pages

    # -- parsing ----------------------------------------------------------

    def _cards(self, html: str):
        """Yield the markup of each listing card.

        bs4 when available (the template is stable but hand-written, with
        unclosed tags in places); a regex split otherwise, so the adapter
        still runs in a bare environment.
        """
        try:
            from bs4 import BeautifulSoup
        except ImportError:
            for m in re.finditer(r'<article class="card">(.*?)</article>',
                                 html, re.S | re.I):
                yield m.group(1), None
            return
        soup = BeautifulSoup(html, "html.parser")
        for art in soup.find_all("article", class_="card"):
            yield str(art), art

    @staticmethod
    def _text(node, selector_class: str) -> str:
        if node is None:
            return ""
        el = node.find(class_=selector_class)
        return re.sub(r"\s+", " ", el.get_text(" ", strip=True)) if el else ""

    def _parse_card(self, raw_html: str, node, page_url: str) -> RawListing | None:
        # --- link + stable id ---
        m = _REF_RE.search(raw_html)
        detail_url = None
        ref = None
        if m:
            ref = m.group(1)
            detail_url = urljoin(BASE, m.group(0))
        else:
            alt = _ALT_REF_RE.search(raw_html)
            if alt:
                ref = unquote(alt.group(1))

        if node is not None:
            title = self._text(node, "card__title")
            address_line = self._text(node, "card__adress")   # their spelling
            price_txt = self._text(node, "card__price")
            info = self._text(node, "card__info")
            labels = self._text(node, "card__labels")
            facilities = self._text(node, "card__facilities")
        else:
            def grab(cls):
                mm = re.search(rf'class="{cls}"[^>]*>(.*?)</(?:div|p|a)>',
                               raw_html, re.S | re.I)
                return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", mm.group(1))
                              ).strip() if mm else ""
            title = grab("card__title")
            address_line = grab("card__adress")
            price_txt = grab("card__price")
            info = grab("card__info")
            labels = grab("card__labels")
            facilities = grab("card__facilities")

        if not address_line:
            return None

        # "355 South End Avenue (Battery Park City, MN)"
        street, hood, boro = address_line, None, None
        paren = re.search(r"^(.*?)\s*\(([^,)]+)(?:,\s*([A-Z]{2}))?\)\s*$",
                          address_line)
        if paren:
            street = paren.group(1).strip()
            hood = paren.group(2).strip()
            boro = BOROUGH.get((paren.group(3) or "").upper())

        # "$5,295/month" possibly followed by "net effective rent"
        net_effective = "net effective" in price_txt.lower()
        price = None
        pm = re.search(r"\$\s?([\d,]+)", price_txt)
        if pm:
            price = pm.group(1)

        beds_txt = ""
        bm = re.search(r"(studio|\d+(?:\.\d)?\s*bed)", facilities, re.I)
        if bm:
            beds_txt = bm.group(1)
        if not beds_txt:
            beds_txt = title      # "1-Bedroom at 70 Pine Street"

        baths = None
        bath_m = re.search(r"(\d+(?:\.\d)?)\s*bath", facilities, re.I)
        if bath_m:
            baths = bath_m.group(1)

        posted = None
        pmatch = _POSTED_RE.search(info)
        if pmatch:
            posted = re.sub(r"\s+", " ", pmatch.group(1).lower()).strip()

        # The manager/rental-office credit sits after the "Posted ..." span.
        manager = re.sub(r"\s+", " ", _POSTED_RE.sub("", info)).strip(" ,·<>")
        manager = manager or None

        # Amenities are <li> items; splitting the flattened text is unreliable
        # because get_text collapses everything to single spaces.
        amenities: list[str] = []
        if node is not None:
            fac = node.find(class_="card__facilities")
            if fac:
                amenities = [li.get_text(" ", strip=True) for li in fac.find_all("li")]
        else:
            amenities = re.findall(r"<li[^>]*>(.*?)</li>", raw_html, re.S | re.I)
            amenities = [re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", a)).strip()
                         for a in amenities]
        amenities = [a for a in amenities
                     if a and not re.search(r"\bbed\b|\bbath\b", a, re.I)]

        return RawListing(
            source=self.id,
            source_url=page_url,
            address=street,
            unit_raw=None,                 # NYBits never publishes unit numbers
            price_raw=price,
            beds_raw=beds_txt,
            baths_raw=baths,
            sqft_raw=None,
            available_on=None,
            listed_at=posted_to_timestamp(posted),
            title=title or None,
            detail_url=detail_url,
            borough_hint=boro or hood,
            no_fee=("no fee" in labels.lower()) or None,
            source_ref=ref,                # what keeps same-building units apart
            extra={
                "aggregator": "nybits",
                "neighborhood": hood,
                "manager": manager or None,
                "posted": posted,
                "net_effective": net_effective,
                "amenities": amenities[:8],
            },
        )

    # -- fetch ------------------------------------------------------------

    def fetch(self) -> list[RawListing]:
        rows: list[RawListing] = []
        seen_refs: set[str] = set()
        errors: list[str] = []

        for url in self.searches[: self.max_pages]:
            try:
                html = self.get(url)
            except SourceError as exc:
                errors.append(str(exc))
                continue
            n_before = len(rows)
            for raw_html, node in self._cards(html):
                try:
                    listing = self._parse_card(raw_html, node, url)
                except Exception:
                    log.exception("%s: card parse failed on %s", self.id, url)
                    continue
                if listing is None:
                    continue
                # The same listing appears in both the all-beds and the
                # per-bedroom search; keep one.
                if listing.source_ref:
                    if listing.source_ref in seen_refs:
                        continue
                    seen_refs.add(listing.source_ref)
                rows.append(listing)
            log.debug("%s: %s -> %d listings", self.id, url, len(rows) - n_before)

        if not rows and errors:
            raise SourceError("; ".join(errors[:3]))
        return rows
