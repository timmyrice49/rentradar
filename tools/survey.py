"""Source-acquisition triage: which operator sites are cheap to ingest?

Give it a registry of NYC management companies and it answers, per operator,
the only question that matters before you write any scraping code:

    how expensive is it to get this landlord's inventory, and am I allowed to?

For each domain it fetches the homepage, follows the site's own navigation to
find the listings page, and classifies what it finds:

    JSONLD   schema.org listing markup in the page — free, zero custom code
    PMS      a property-management backend (Yardi/RentCafe, Entrata,
             AppFolio, Funnel...) — often on a separate host that is easier
             to read than the marketing site. The URL is captured.
    SPA      client-rendered shell — run `cli discover` to find its XHR
    HTML     server-rendered, no structured data — discover, else a parser
    BLOCKED  refuses our user agent
    DEAD     domain does not resolve

It also reads robots.txt and reports whether the listings path is permitted,
because "can I" is a separate question from "can it be parsed" and you want
both answers in the same table.

    python tools/survey.py operators.csv -o survey_results.csv
    python tools/survey.py --urls https://a.com https://b.com
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import csv
import gzip
import io
import json
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
import zlib

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
BOT_UA = "RentRadarBot/0.1 (+https://example.com/bot)"

# Third-party property-management hosts. A link to one of these is the single
# most useful thing this tool can find: it is the operator's real inventory
# endpoint, usually far cleaner than their marketing site.
PMS_HOSTS = {
    "rentcafe.com": "Yardi RentCafe", "securecafe.com": "Yardi RentCafe",
    "yardi.com": "Yardi", "entrata.com": "Entrata",
    "prospectportal.com": "Entrata", "appfolio.com": "AppFolio",
    "appfolio.us": "AppFolio", "knockrentals.com": "Knock",
    "funnelleasing.com": "Funnel", "nestio.com": "Nestio",
    "realpage.com": "RealPage", "onesite.realpage.com": "RealPage OneSite",
    "buildium.com": "Buildium", "rentmanager.com": "Rent Manager",
    "showmojo.com": "ShowMojo", "resman.com": "ResMan",
    "on-site.com": "On-Site", "zumper.com": "Zumper",
}
# Bare-token matching is a trap: "on-site" appears on nearly every apartment
# site ("on-site laundry", "on-site super") and produced a dozen false PMS
# hits on the first run. Only tokens that cannot occur as ordinary English are
# allowed as a loose fingerprint, and even those must appear as a hostname.
PMS_TOKENS = {"rentcafe", "securecafe", "appfolio", "entrata", "prospectportal",
              "knockrentals", "funnelleasing", "nestio", "realpage",
              "buildium", "rentmanager", "showmojo"}

LISTING_TYPES = {"product", "apartment", "accommodation",
                 "realestatelisting", "residence"}

# Anchor text / href fragments that mean "inventory lives here", best first.
NAV_PATTERNS = [
    (r"available[\s\-_]?(apartment|unit|residence|home|rental)", 10),
    (r"apartments?[\s\-_]?for[\s\-_]?rent", 10),
    (r"\bavailabilit(y|ies)\b", 9),
    (r"find[\s\-_]?(an?[\s\-_])?(apartment|home|rental)", 9),
    (r"\bapartment[\s\-_]?search\b", 9),
    (r"\brentals?\b", 7),
    (r"\bapartments?\b", 7),
    (r"\bresidences?\b", 5),
    (r"\bresidential\b", 5),
    (r"\bleasing\b", 5),
    (r"\bproperties\b", 4),
    (r"\bportfolio\b", 3),
    (r"\bcommunities\b", 4),
    (r"\blistings?\b", 6),
]

# Tried only when the homepage exposes no usable navigation.
FALLBACK_PATHS = ["/apartments", "/availability", "/rentals", "/residential",
                  "/available-apartments", "/properties"]

_robots_cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def _read(resp) -> str:
    raw = resp.read()
    enc = (resp.headers.get("Content-Encoding") or "").lower()
    if enc == "gzip":
        raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
    elif enc == "deflate":
        raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", "ignore")


#: Set by --insecure. A TLS-intercepting corporate or sandbox proxy makes
#: perfectly healthy sites look dead with a certificate error, which would
#: otherwise be reported as "the operator blocks us" — a materially wrong
#: conclusion. Retrying once without verification tells you which it was.
INSECURE_FALLBACK = False
_insecure_ctx = None


def fetch(url: str, timeout: int = 14, ua: str = UA) -> tuple[str, str]:
    """Return (final_url, html). Raises on failure."""
    global _insecure_ctx
    req = urllib.request.Request(url, headers={
        "User-Agent": ua,
        "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.geturl(), _read(resp)
    except urllib.error.URLError as exc:
        import ssl
        is_tls = isinstance(getattr(exc, "reason", None), ssl.SSLError)
        if not (INSECURE_FALLBACK and is_tls):
            raise
        if _insecure_ctx is None:
            _insecure_ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_insecure_ctx) as resp:
            return resp.geturl(), _read(resp)


def robots_for(base: str) -> urllib.robotparser.RobotFileParser | None:
    """Fetch and parse robots.txt once per host. None means none published."""
    host = urllib.parse.urlsplit(base).netloc
    if host in _robots_cache:
        return _robots_cache[host]
    rp = None
    try:
        _, body = fetch(urllib.parse.urljoin(base, "/robots.txt"), timeout=8)
        rp = urllib.robotparser.RobotFileParser()
        rp.parse(body.splitlines())
    except Exception:
        rp = None                      # no robots.txt = nothing disallowed
    _robots_cache[host] = rp
    return rp


def robots_verdict(base: str, path_url: str) -> str:
    rp = robots_for(base)
    if rp is None:
        return "none"
    try:
        return "allow" if rp.can_fetch(BOT_UA, path_url) else "DISALLOW"
    except Exception:
        return "none"


# --------------------------------------------------------------------------
# classification
# --------------------------------------------------------------------------

def count_listing_nodes(html: str) -> tuple[int, int]:
    """(listing nodes present in JSON-LD, largest declared ItemList length)."""
    found = declared = 0
    for block in re.findall(
        r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I
    ):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            try:
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", block.strip()))
            except json.JSONDecodeError:
                continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                t = node.get("@type")
                types = ({t.lower()} if isinstance(t, str)
                         else {str(x).lower() for x in t} if isinstance(t, list)
                         else set())
                if types & LISTING_TYPES:
                    found += 1
                for key in ("numberOfItems",):
                    if isinstance(node.get(key), int):
                        declared = max(declared, node[key])
                me = node.get("mainEntity")
                if isinstance(me, dict) and isinstance(me.get("numberOfItems"), int):
                    declared = max(declared, me["numberOfItems"])
                stack.extend(node.values())
    return found, declared


def find_pms(html: str, base_host: str) -> tuple[str, str]:
    """Return (vendor, url) for any property-management host referenced."""
    for href in re.findall(r'(?:href|src|action)="(https?://[^"]+)"', html):
        host = urllib.parse.urlsplit(href).netloc.lower()
        if base_host and host.endswith(base_host):
            continue
        for domain, vendor in PMS_HOSTS.items():
            if host.endswith(domain):
                return vendor, href
    low = html.lower()
    for token in PMS_TOKENS:
        # Require a hostname-shaped occurrence, not a bare word.
        if re.search(rf"\b{re.escape(token)}\.(com|net|us|io)\b", low):
            return PMS_HOSTS.get(f"{token}.com", token) + " (ref only)", ""
    return "", ""


def nav_candidates(html: str, base_url: str, limit: int = 3) -> list[str]:
    """Same-site links that look like they lead to inventory, best first."""
    base_host = urllib.parse.urlsplit(base_url).netloc.lower()
    scored: dict[str, int] = {}
    for href, text in re.findall(r'<a\b[^>]*href="([^"#]+)"[^>]*>(.*?)</a>',
                                 html, re.S | re.I):
        label = re.sub(r"<[^>]+>", " ", text)
        label = re.sub(r"\s+", " ", label).strip().lower()
        if len(label) > 60:
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        parts = urllib.parse.urlsplit(absolute)
        if parts.scheme not in ("http", "https"):
            continue
        host = parts.netloc.lower()
        if host != base_host and not host.endswith("." + base_host) \
                and not base_host.endswith("." + host):
            continue
        haystack = f"{label} {parts.path.lower()}"
        best = 0
        for pattern, weight in NAV_PATTERNS:
            if re.search(pattern, haystack):
                best = max(best, weight)
        if not best:
            continue
        # Prefer shallow paths: /apartments beats /about/team/apartments-guy
        best -= parts.path.strip("/").count("/")
        clean = parts._replace(query="", fragment="").geturl()
        scored[clean] = max(scored.get(clean, 0), best)

    ranked = sorted(scored.items(), key=lambda kv: -kv[1])
    return [u for u, _ in ranked[:limit]]


def classify(html: str, url: str, base_host: str) -> tuple[str, str, int]:
    """Return (verdict, note, rank) — lower rank sorts first."""
    kb = len(html) // 1024
    found, declared = count_listing_nodes(html)
    vendor, pms_url = find_pms(html, base_host)

    if found >= 3:
        note = f"{found} listing nodes"
        if declared > found:
            note += f" of {declared} declared (rest client-side)"
        return "JSONLD", note, 0
    if vendor:
        return "PMS", (f"{vendor}" + (f" @ {pms_url[:70]}" if pms_url else
                                      " (fingerprint only)")), 1
    if kb < 40:
        return "SPA", f"{kb}KB shell — run discover", 2
    return "HTML", f"{kb}KB, no structured data", 3


# --------------------------------------------------------------------------
# per-operator survey
# --------------------------------------------------------------------------

def survey_domain(op: dict, delay: float = 0.0) -> dict:
    name, domain = op["name"], op["domain"].strip()
    out = {"name": name, "domain": domain, "tier": op.get("tier", ""),
           "verdict": "", "listings_url": "", "robots": "", "note": ""}
    if delay:
        time.sleep(delay)

    home_html = home_url = None
    last_err = ""
    for candidate in (f"https://{domain}/", f"https://www.{domain}/"):
        try:
            home_url, home_html = fetch(candidate)
            break
        except urllib.error.HTTPError as exc:
            last_err = f"HTTP {exc.code}"
            if exc.code in (401, 403, 406, 429):
                out["verdict"], out["note"] = "BLOCKED", f"{last_err} on homepage"
                return out
        except (socket.gaierror, urllib.error.URLError) as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, socket.gaierror) or "Name or service" in str(reason):
                last_err = "DNS"
            else:
                last_err = type(reason).__name__
        except Exception as exc:
            last_err = type(exc).__name__

    if home_html is None:
        out["verdict"] = "DEAD" if last_err == "DNS" else "BLOCKED"
        out["note"] = f"unreachable ({last_err})"
        return out

    base_host = urllib.parse.urlsplit(home_url).netloc.lower().removeprefix("www.")

    # The homepage itself sometimes is the listings page.
    verdict, note, rank = classify(home_html, home_url, base_host)
    best = (rank, verdict, note, home_url)

    targets = nav_candidates(home_html, home_url)
    if not targets:
        targets = [urllib.parse.urljoin(home_url, p) for p in FALLBACK_PATHS[:3]]

    for target in targets:
        if best[0] == 0:
            break                      # already found free structured data
        try:
            final_url, html = fetch(target)
        except Exception:
            continue
        v, n, r = classify(html, final_url, base_host)
        # On a tie, prefer the navigated page over the homepage: both look
        # like plain HTML, but only one of them is where the inventory is.
        # Without this the tool kept reporting homepages as the listings URL.
        if r < best[0] or (r == best[0] and best[3] == home_url
                           and final_url != home_url):
            best = (r, v, n, final_url)

    _, verdict, note, url = best
    out["verdict"] = verdict
    out["listings_url"] = url
    out["note"] = note
    out["robots"] = robots_verdict(home_url, url)
    return out


# --------------------------------------------------------------------------

def load_registry(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as fh:
        lines = [l for l in fh if not l.lstrip().startswith("#")]
    for row in csv.DictReader(lines):
        if row.get("domain", "").strip():
            rows.append(row)
    return rows


ORDER = {"JSONLD": 0, "PMS": 1, "SPA": 2, "HTML": 3, "BLOCKED": 4, "DEAD": 5}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Triage operator sites by ingestion cost")
    ap.add_argument("registry", nargs="?", help="operators.csv")
    ap.add_argument("--urls", nargs="*", default=[])
    ap.add_argument("-o", "--out", help="write results CSV here")
    ap.add_argument("--workers", type=int, default=10)
    ap.add_argument("--tier", help="only this tier (1, 2 or 3)")
    ap.add_argument("--insecure", action="store_true",
                    help="retry TLS failures without certificate verification, "
                         "to tell an intercepting proxy apart from a dead host")
    args = ap.parse_args(argv)

    if args.insecure:
        globals()["INSECURE_FALLBACK"] = True

    ops: list[dict] = []
    if args.registry:
        ops = load_registry(args.registry)
        if args.tier:
            ops = [o for o in ops if o.get("tier") == args.tier]
    for u in args.urls:
        host = urllib.parse.urlsplit(u if "//" in u else "https://" + u).netloc
        ops.append({"name": host, "domain": host.removeprefix("www."), "tier": ""})
    if not ops:
        ap.error("give a registry CSV or --urls")

    print(f"surveying {len(ops)} operators with {args.workers} workers...\n",
          file=sys.stderr)
    started = time.monotonic()
    with cf.ThreadPoolExecutor(args.workers) as ex:
        results = list(ex.map(survey_domain, ops))
    results.sort(key=lambda r: (ORDER.get(r["verdict"], 9), r["name"].lower()))

    print(f"{'verdict':8} {'t':1} {'robots':8} {'operator':32} note")
    print("-" * 118)
    for r in results:
        flag = "!" if r["robots"] == "DISALLOW" else " "
        print(f"{r['verdict']:8} {r['tier']:1} {r['robots']:8}{flag}"
              f"{r['name'][:31]:32} {r['note'][:58]}")

    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    reachable = sum(v for k, v in counts.items() if k in ("JSONLD", "PMS", "SPA", "HTML"))
    free = counts.get("JSONLD", 0)
    cheap = free + counts.get("PMS", 0)
    blocked = sum(1 for r in results if r["robots"] == "DISALLOW")

    print(f"\n{'':8} " + "  ".join(f"{k}={v}" for k, v in
                                   sorted(counts.items(), key=lambda kv: ORDER.get(kv[0], 9))))
    print(f"reachable {reachable}/{len(results)}  ·  "
          f"zero-code {free}  ·  zero-or-low-code {cheap}  ·  "
          f"robots-disallowed {blocked}  ·  {time.monotonic() - started:.0f}s")

    if args.out:
        with open(args.out, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["name", "domain", "tier", "verdict",
                                               "robots", "listings_url", "note"])
            w.writeheader()
            w.writerows(results)
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
