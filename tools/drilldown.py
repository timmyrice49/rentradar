"""Operator -> building -> availability: find the pages that actually hold units.

The survey answers "what shape is this operator's site". This answers the next
question, and it exists because of what the survey found: on 122 NYC operators,
only 2 exposed inventory in the first-level HTML, and *zero* of 14 sampled
server-rendered listing pages carried a single inline price.

The reason is structural. An operator's "apartments" page is a portfolio index
of buildings; the units live one level deeper, on a per-building page, usually
behind a client-side widget or a property-management vendor's portal. So
ingestion targets are buildings, not operators — and there are 5 to 40 of them
per operator.

This tool walks that second level: from a portfolio page it extracts building
links, fetches a sample, and reports which carry structured data, a vendor
portal, prices inline, or nothing readable without a browser.

    python tools/drilldown.py https://operator.com/apartments --max 12
    python tools/drilldown.py --from-survey survey_results.csv --tier 1 --max 8
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import re
import sys
import urllib.parse

from survey import (PMS_HOSTS, count_listing_nodes, fetch,  # noqa: E402
                    find_pms, robots_verdict)

PRICE_RE = re.compile(r"\$\s?[1-9]\d{0,2},\d{3}(?:\b|/)")
BED_RE = re.compile(r"\b(studio|\d\s*(?:bed|br|bedroom))s?\b", re.I)
UNIT_RE = re.compile(r"(?:#|\bapt\.?\b|\bunit\b)\s*[A-Z]?\d{1,4}[A-Z]?\b", re.I)

# A building page's URL or link text usually contains a street address.
ADDRESS_HINT = re.compile(
    r"\b\d{1,4}(?:-\d{1,3})?[\s\-]"
    r"(?:e|w|n|s|east|west|north|south)?[\s\-]*"
    r"\d{1,3}(?:st|nd|rd|th)?[\s\-](?:st|street|ave|avenue|pl|place|rd|road|"
    r"blvd|boulevard|dr|drive|ln|lane|ct|court|ter|terrace|pkwy|plaza|plz)\b",
    re.I,
)
BUILDING_PATH = re.compile(
    r"/(building|buildings|property|properties|residence|residences|"
    r"community|communities|apartment|apartments|listing|listings|"
    r"our-buildings|portfolio|locations?)/[^/]+", re.I
)
SKIP_PATH = re.compile(
    r"/(about|team|contact|careers|news|press|blog|privacy|terms|search|"
    r"amenities|neighborhood|gallery|resident|owner|invest|sustainab)", re.I
)


def building_links(html: str, base_url: str, cap: int = 60) -> list[tuple[str, str]]:
    """Same-site links that look like individual building pages."""
    base_host = urllib.parse.urlsplit(base_url).netloc.lower()
    found: dict[str, tuple[str, int]] = {}

    for href, inner in re.findall(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>',
                                  html, re.S | re.I):
        label = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", inner)).strip()
        if len(label) > 90:
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        parts = urllib.parse.urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        host = parts.netloc.lower()
        same_site = host == base_host or host.endswith("." + base_host)
        vendor_host = any(host.endswith(d) for d in PMS_HOSTS)
        if not (same_site or vendor_host):
            continue
        path = parts.path
        if SKIP_PATH.search(path) and not vendor_host:
            continue
        if path in ("", "/"):
            continue

        score = 0
        if vendor_host:
            score += 6                       # a vendor portal is the jackpot
        if ADDRESS_HINT.search(label):
            score += 5
        if ADDRESS_HINT.search(path.replace("-", " ")):
            score += 4
        if BUILDING_PATH.search(path):
            score += 3
        if path.strip("/").count("/") >= 1:
            score += 1                       # depth suggests a leaf page
        if not score:
            continue

        clean = parts._replace(query="", fragment="").geturl()
        if clean not in found or found[clean][1] < score:
            found[clean] = (label or path, score)

    ranked = sorted(found.items(), key=lambda kv: -kv[1][1])[:cap]
    return [(url, meta[0]) for url, meta in ranked]


def inspect(url: str, base_host: str) -> dict:
    """Classify one building page by what a crawler could get out of it."""
    row = {"url": url, "verdict": "", "prices": 0, "beds": 0, "units": 0,
           "vendor": "", "note": ""}
    try:
        final, html = fetch(url, timeout=14)
    except Exception as exc:
        row["verdict"] = "ERR"
        row["note"] = type(exc).__name__
        return row

    nodes, declared = count_listing_nodes(html)
    vendor, vurl = find_pms(html, base_host)
    prices = set(PRICE_RE.findall(html))
    beds = BED_RE.findall(html)
    units = UNIT_RE.findall(html)
    kb = len(html) // 1024

    row.update(prices=len(prices), beds=len(beds), units=len(units),
               vendor=vendor.replace(" (ref only)", ""))

    if nodes >= 3:
        row["verdict"] = "JSONLD"
        row["note"] = f"{nodes} nodes" + (f" of {declared}" if declared > nodes else "")
    elif len(prices) >= 3 and beds:
        row["verdict"] = "INLINE"
        row["note"] = f"{len(prices)} prices, {len(units)} unit refs — parseable"
    elif vendor:
        row["verdict"] = "VENDOR"
        row["note"] = f"{vendor}" + (f" @ {urllib.parse.urlsplit(vurl).netloc}" if vurl else "")
    elif kb < 40:
        row["verdict"] = "SPA"
        row["note"] = f"{kb}KB shell"
    else:
        row["verdict"] = "OPAQUE"
        row["note"] = f"{kb}KB, no prices or units in source"
    return row


def drill(portfolio_url: str, max_pages: int = 10, workers: int = 6) -> dict:
    try:
        base, html = fetch(portfolio_url, timeout=16)
    except Exception as exc:
        return {"url": portfolio_url, "error": f"{type(exc).__name__}", "pages": []}

    base_host = urllib.parse.urlsplit(base).netloc.lower().removeprefix("www.")
    links = building_links(html, base)
    sample = links[:max_pages]
    if not sample:
        return {"url": portfolio_url, "error": "no building links found",
                "n_links": 0, "pages": []}

    with cf.ThreadPoolExecutor(workers) as ex:
        pages = list(ex.map(lambda l: inspect(l[0], base_host), sample))
    for page, (_, label) in zip(pages, sample):
        page["label"] = label
    return {"url": portfolio_url, "n_links": len(links), "pages": pages,
            "robots": robots_verdict(base, base)}


VERDICT_ORDER = {"JSONLD": 0, "INLINE": 1, "VENDOR": 2, "SPA": 3,
                 "OPAQUE": 4, "ERR": 5}


def report(name: str, result: dict) -> dict:
    print(f"\n{'=' * 96}\n{name}\n  {result['url']}")
    if result.get("error"):
        print(f"  -> {result['error']}")
        return {}
    print(f"  {result['n_links']} building links found, sampled {len(result['pages'])}"
          f"  ·  robots: {result.get('robots')}")
    print(f"  {'verdict':8} {'$':>3} {'bed':>4} {'unit':>5}  page")
    counts: dict[str, int] = {}
    for p in sorted(result["pages"], key=lambda p: VERDICT_ORDER.get(p["verdict"], 9)):
        counts[p["verdict"]] = counts.get(p["verdict"], 0) + 1
        label = (p.get("label") or "")[:36]
        print(f"  {p['verdict']:8} {p['prices']:>3} {p['beds']:>4} {p['units']:>5}"
              f"  {label:38} {p['note'][:38]}")
    return counts


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url", nargs="?", help="an operator portfolio / apartments page")
    ap.add_argument("--from-survey", help="survey_results.csv")
    ap.add_argument("--tier", help="with --from-survey, restrict to this tier")
    ap.add_argument("--verdicts", default="HTML,SPA",
                    help="with --from-survey, which survey verdicts to drill")
    ap.add_argument("--operators", type=int, default=6)
    ap.add_argument("--max", type=int, default=10, help="building pages per operator")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args(argv)

    if args.insecure:
        import survey
        survey.INSECURE_FALLBACK = True

    jobs: list[tuple[str, str]] = []
    if args.url:
        jobs.append((urllib.parse.urlsplit(args.url).netloc, args.url))
    if args.from_survey:
        keep = {v.strip().upper() for v in args.verdicts.split(",")}
        rows = list(csv.DictReader(open(args.from_survey)))
        rows = [r for r in rows if r["verdict"] in keep and r["listings_url"]]
        if args.tier:
            rows = [r for r in rows if r["tier"] == args.tier]
        jobs += [(r["name"], r["listings_url"]) for r in rows[: args.operators]]
    if not jobs:
        ap.error("give a URL or --from-survey")

    totals: dict[str, int] = {}
    for name, url in jobs:
        for verdict, n in report(name, drill(url, args.max)).items():
            totals[verdict] = totals.get(verdict, 0) + n

    if totals:
        total = sum(totals.values())
        print(f"\n{'=' * 96}\nacross {len(jobs)} operators, {total} building pages:")
        for v, n in sorted(totals.items(), key=lambda kv: VERDICT_ORDER.get(kv[0], 9)):
            print(f"  {v:8} {n:4}  ({n / total:.0%})")
        crawlable = totals.get("JSONLD", 0) + totals.get("INLINE", 0) + totals.get("VENDOR", 0)
        print(f"\n{crawlable}/{total} ({crawlable / total:.0%}) reachable without a browser")
    return 0


if __name__ == "__main__":
    sys.path.insert(0, __file__.rsplit("/", 1)[0])
    raise SystemExit(main())
