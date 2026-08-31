"""Find the JSON endpoint behind a client-rendered listings page.

Empirically, most large NYC management sites ship an empty HTML shell and load
inventory over XHR. Rather than write and maintain a bespoke scraper per site,
we render the page once with a headless browser, watch the network, and keep
whichever response looks like a list of apartments. The result is a
`jsonapi` source spec you paste into sources.yaml -- after which that site is
polled as cheap plain JSON and never rendered again until it breaks.

Run:  python -m rentradar.cli discover https://example.com/apartments
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# Field-name heuristics, matched case-insensitively against record keys.
SIGNALS = {
    # `line1` shows up as the leaf of `address.line1` in Entrata-shaped
    # payloads, so patterns are matched against the full dotted path as well
    # as the leaf -- matching the leaf alone missed nested address objects.
    "address": [r"^address", r"street", r"addr", r"^line1$", r"property.*address",
                r"building.*(name|address)"],
    # Gross first: net-effective rent is the discounted headline that hides
    # free months, and quoting it as the rent misleads renters.
    # Gross first: net-effective rent is the discounted headline that hides
    # free months, and quoting it as the rent misleads renters. The word
    # boundary matters -- a bare "rent" pattern matched Manhattan Skyline's
    # boolean `is_rental` field and mapped the rent to True.
    "price":   [r"gross.?rent", r"market.?rent", r"^min.*rent", r"^rent$",
                r"rent$", r"\brent\b", r"^price$", r"price", r"amount"],
    # "^apartment" on its own matters: TF Cornerstone's payload calls the
    # unit field plainly "Apartment", which an apartment.?(number|name)
    # pattern misses -- and losing the unit number costs the strongest
    # identity key there is.
    # unitNumber before a bare ^unit prefix: StuyTown exposes both
    # `unitNumber` ("07-A") and `unitSpk` ("P~NYST31~B~315~U~07-A"), and
    # ^unit alone grabbed the opaque key instead of the human unit.
    "unit_raw": [r"^unit.?(number|name)$", r"^apartment$", r"^unit$",
                 r"apartment.?(number|name)", r"^apt", r"^number$", r"^unit"],
    "beds":    [r"bed", r"^br$", r"bedroom"],
    "baths":   [r"bath", r"^ba$"],
    "sqft":    [r"sq.?f", r"square.?feet", r"area", r"size"],
    "available_on": [r"avail", r"move.?in", r"ready.?date"],
    "detail_url": [r"url", r"link", r"permalink", r"href"],
}

SKIP_HOSTS = re.compile(
    r"google|facebook|doubleclick|segment|hotjar|sentry|datadog|newrelic|"
    r"cloudflareinsights|gtm\.|analytics|intercom|hubspot|optimizely|"
    r"cdn\.jsdelivr|unpkg|fonts\.",
    re.I,
)


@dataclass
class Candidate:
    url: str
    method: str
    n_records: int
    score: int
    fields: dict[str, str]
    sample: dict = field(default_factory=dict)
    root_hint: str | None = None
    #: Which page the endpoint was captured on -- the portfolio page or a
    #: specific building page. Matters because a per-building endpoint has to
    #: be templated across buildings before it is a usable source.
    origin: str = ""


def _flatten_keys(rec: dict, prefix: str = "", depth: int = 0) -> dict[str, object]:
    """Dotted-path view of a record, two levels deep."""
    out: dict[str, object] = {}
    for k, v in rec.items():
        path = f"{prefix}{k}"
        if isinstance(v, dict) and depth < 2:
            out.update(_flatten_keys(v, f"{path}.", depth + 1))
        elif not isinstance(v, (list, dict)):
            out[path] = v
    return out


def _map_fields(records: list[dict]) -> tuple[dict[str, str], int]:
    """Guess a field map from record keys. Returns (map, confidence score)."""
    flat = _flatten_keys(records[0])
    keys = list(flat.keys())
    mapping: dict[str, str] = {}

    for target, patterns in SIGNALS.items():
        best = None
        for pat in patterns:
            for k in keys:
                leaf = k.split(".")[-1]
                if re.search(pat, leaf, re.I) or re.search(pat, k, re.I):
                    val = flat[k]
                    # A boolean is never a price, a bedroom count or an
                    # address, however tempting its name.
                    if isinstance(val, bool):
                        continue
                    # Prefer a key whose value is actually populated.
                    if val in (None, "", 0) and best is not None:
                        continue
                    best = k
                    break
            if best:
                break
        if best:
            mapping[target] = best

    score = 0
    if "address" in mapping:
        score += 5
    if "price" in mapping:
        score += 3
    if "unit_raw" in mapping:
        score += 3
    if "beds" in mapping:
        score += 2
    score += min(len(records), 40) // 10
    return mapping, score


def _launch_kwargs(headless: bool, proxy: str | None) -> dict:
    """Networking. Chromium silently inherits HTTPS_PROXY from the environment,
    and a proxy other tools tolerate can leave every navigation dying with
    ERR_CONNECTION_RESET -- which reads exactly like the site blocking you. So
    the default is an explicit direct connection; a proxy is used only when the
    caller names one."""
    args = ["--disable-quic", "--disable-blink-features=AutomationControlled"]
    kw: dict = {"headless": headless}
    if proxy:
        kw["proxy"] = {"server": proxy}
    else:
        args.append("--no-proxy-server")
    kw["args"] = args
    return kw


UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


class _Browser:
    """One Chromium instance reused across many page visits.

    Deep discovery renders a portfolio page and then several building pages
    per operator; paying browser startup for each would dominate the runtime.
    """

    def __init__(self, headless=True, proxy=None):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        self.browser = self._pw.chromium.launch(**_launch_kwargs(headless, proxy))
        self.ctx = self.browser.new_context(
            user_agent=UA, viewport={"width": 1440, "height": 1000},
            ignore_https_errors=True)

    def close(self):
        try:
            self.browser.close()
        finally:
            self._pw.stop()

    def visit(self, url: str, wait_ms: int, scroll: bool = True,
              collect_links: bool = False):
        """Load a page, capture JSON responses, optionally read rendered links.

        Reading links from the *rendered* DOM is the whole point: the static
        drill-down found no building links on four of five operators because
        their portfolio pages ship no anchors in the HTML at all.
        """
        captured: list[tuple[str, str, object]] = []
        page = self.ctx.new_page()

        def on_response(resp):
            try:
                if SKIP_HOSTS.search(resp.url):
                    return
                if "json" not in (resp.headers or {}).get("content-type", "").lower():
                    return
                body = resp.text()
                if len(body) < 200:
                    return
                captured.append((resp.url, resp.request.method, json.loads(body)))
            except Exception:
                pass  # a body we cannot read is simply not a candidate

        page.on("response", on_response)
        links: list[tuple[str, str]] = []
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            page.wait_for_timeout(wait_ms)
            if scroll:
                for _ in range(3):
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(1000)
            if collect_links:
                links = page.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => [e.href, (e.innerText||'').trim().slice(0,80)])",
                ) or []
        except Exception as exc:
            log.warning("navigation issue on %s: %s", url, exc)
        finally:
            try:
                page.close()
            except Exception:
                pass
        return captured, links


def _candidates_from(captured, seen_bases: set[str] | None = None,
                     origin: str = "") -> list[Candidate]:
    from .sources.jsonapi import find_records
    seen_bases = seen_bases if seen_bases is not None else set()
    out: list[Candidate] = []
    for u, method, payload in captured:
        base = u.split("?")[0]
        if base in seen_bases:
            continue
        recs = find_records(payload, min_len=3)
        if not recs:
            continue
        mapping, score = _map_fields(recs)
        if "address" not in mapping and "unit_raw" not in mapping:
            continue
        seen_bases.add(base)
        out.append(Candidate(url=u, method=method, n_records=len(recs),
                             score=score, fields=mapping,
                             sample=_first_dict(recs[0]), origin=origin))
    return out


def discover(url: str, wait_ms: int = 6000, headless: bool = True,
             scroll: bool = True, proxy: str | None = None) -> list[Candidate]:
    """Render `url`, capture JSON responses, and rank listing-shaped payloads."""
    br = _Browser(headless=headless, proxy=proxy)
    try:
        captured, _ = br.visit(url, wait_ms, scroll=scroll)
    finally:
        br.close()
    out = _candidates_from(captured, origin=url)
    out.sort(key=lambda c: (-c.score, -c.n_records))
    return out


# Links that plausibly lead to one building's page.
_BUILDING_PATH = re.compile(
    r"/(buildings?|propert(y|ies)|residences?|communit(y|ies)|apartments?|"
    r"listings?|locations?|our-buildings|portfolio|rentals?)/[^/?#]{2,}", re.I)
_SKIP_LINK = re.compile(
    r"/(about|team|contact|careers?|news|press|blog|privacy|terms|legal|"
    r"accessibility|amenit|neighborhood|gallery|resident|owner|invest|"
    r"sustainab|search$|faq|login|apply)", re.I)
_ADDRESSY = re.compile(
    r"\b\d{1,4}(?:-\d{1,3})?\s+[\w'.]+(?:\s+[\w'.]+){0,3}\s*$|"
    r"\b\d{1,4}(?:-\d{1,3})?[\s-]+(?:e|w|n|s|east|west|north|south)?[\s-]*"
    r"\d{1,3}(?:st|nd|rd|th)?\b", re.I)


def rank_building_links(links, base_url: str, cap: int = 6) -> list[tuple[str, str]]:
    """Pick the links most likely to be individual building pages."""
    from urllib.parse import urlsplit
    base_host = urlsplit(base_url).netloc.lower().removeprefix("www.")
    scored: dict[str, tuple[str, int]] = {}
    for href, text in links:
        try:
            parts = urlsplit(href)
        except Exception:
            continue
        if parts.scheme not in ("http", "https"):
            continue
        host = parts.netloc.lower().removeprefix("www.")
        if host != base_host and not host.endswith("." + base_host):
            continue
        path = parts.path
        if not path or path == "/" or _SKIP_LINK.search(path):
            continue
        label = re.sub(r"\s+", " ", text or "").strip()
        score = 0
        if _BUILDING_PATH.search(path):
            score += 4
        if _ADDRESSY.search(label):
            score += 4
        if _ADDRESSY.search(path.replace("-", " ")):
            score += 3
        depth = path.strip("/").count("/")
        if depth >= 1:
            score += 2
        if depth >= 3:
            score -= 2            # deep marketing pages, not building pages
        if not score:
            continue
        clean = parts._replace(query="", fragment="").geturl()
        if clean.rstrip("/") == base_url.rstrip("/"):
            continue
        if clean not in scored or scored[clean][1] < score:
            scored[clean] = (label or path, score)
    ranked = sorted(scored.items(), key=lambda kv: -kv[1][1])[:cap]
    return [(u, meta[0]) for u, meta in ranked]


def deep_discover(url: str, wait_ms: int = 5000, headless: bool = True,
                  proxy: str | None = None, max_buildings: int = 4):
    """Portfolio page -> rendered building links -> discover on each.

    The shallow pass missed 33 of 39 targets, and the drill-down showed why:
    operators publish a portfolio index, and inventory only loads on an
    individual building's page. This walks that second level with a real
    browser, which is also the only way to see the links at all.

    Returns (candidates, building_links_tried).
    """
    br = _Browser(headless=headless, proxy=proxy)
    seen: set[str] = set()
    out: list[Candidate] = []
    tried: list[tuple[str, str]] = []
    try:
        captured, links = br.visit(url, wait_ms, collect_links=True)
        out += _candidates_from(captured, seen, origin=url)

        tried = rank_building_links(links, url, cap=max_buildings)
        for burl, label in tried:
            cap2, _ = br.visit(burl, wait_ms, scroll=True)
            out += _candidates_from(cap2, seen, origin=burl)
    finally:
        br.close()
    out.sort(key=lambda c: (-c.score, -c.n_records))
    return out, tried


def _first_dict(rec: dict, cap: int = 25) -> dict:
    flat = _flatten_keys(rec)
    return dict(list(flat.items())[:cap])


def to_source_spec(cand: Candidate, source_id: str, operator: str = "",
                   borough_hint: str | None = None) -> dict:
    """Render a discovered candidate as a sources.yaml entry."""
    return {
        "type": "jsonapi",
        "id": source_id,
        "url": cand.url,
        "operator": operator or source_id,
        "borough_hint": borough_hint,
        "fields": cand.fields,
        "enabled": True,
        "notes": (f"auto-discovered; {cand.n_records} records, score {cand.score}. "
                  "Verify field map against the sample before trusting."),
    }
