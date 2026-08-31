"""Command line interface.

    python -m rentradar.cli crawl                 # run every enabled source
    python -m rentradar.cli crawl --source stonehenge
    python -m rentradar.cli discover URL          # find a site's JSON endpoint
    python -m rentradar.cli listings --max-price 3000 --min-beds 1
    python -m rentradar.cli alerts --since 2026-08-31T00:00:00+00:00
    python -m rentradar.cli stats
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from . import leadtime
from .alerts import Criteria, Dispatcher, match_new
from .geocode import Geocoder
from .pipeline import run_source
from .sources import build_all
from .store import Store, connect

DEFAULT_DB = os.environ.get("RENTRADAR_DB", "rentradar.db")
DEFAULT_SOURCES = os.environ.get("RENTRADAR_SOURCES", "sources.yaml")


def load_sources(path: str) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"no source registry at {path}")
    text = open(path).read()
    if path.endswith((".yaml", ".yml")):
        try:
            import yaml
        except ImportError:
            sys.exit("pip install pyyaml, or use a .json registry")
        data = yaml.safe_load(text)
    else:
        data = json.loads(text)
    return data.get("sources", data)


def cmd_crawl(args) -> int:
    conn = connect(args.db)
    store, geo = Store(conn), Geocoder(conn)
    specs = load_sources(args.sources)
    if args.source:
        specs = [s for s in specs if s.get("id") in args.source]
        if not specs:
            sys.exit(f"no source matching {args.source}")

    results = []
    for source in build_all(specs):
        print(f"-> {source.id}", flush=True)
        results.append(run_source(source, store, geo,
                                  mark_missing_gone=not args.no_sweep))

    print(f"\n{'source':28} {'ok':>3} {'raw':>5} {'new':>5} {'upd':>5} "
          f"{'gone':>5} {'nogeo':>6}")
    print("-" * 62)
    for r in results:
        print(f"{r.source:28} {'y' if r.ok else 'n':>3} {r.n_raw:>5} {r.n_new:>5} "
              f"{r.n_updated:>5} {r.n_gone:>5} {r.n_unresolved:>6}"
              + (f"   {r.error}" if r.error else ""))

    g = geo.stats
    print(f"\ngeocoder: {g['hit']} cached, {g['miss']} fetched, "
          f"{g['fail']} failed, {g['low_confidence']} below threshold")
    return 0 if all(r.ok for r in results) else 1


def load_queue(path: str) -> list[dict]:
    if not os.path.exists(path):
        sys.exit(f"no registry at {path}")
    try:
        import yaml
    except ImportError:
        sys.exit("pip install pyyaml to read the discover queue")
    data = yaml.safe_load(open(path).read()) or {}
    return data.get("discover_queue", [])


def cmd_discover(args) -> int:
    from .discover import deep_discover, discover, to_source_spec

    if args.deep:
        discover_fn = lambda u, **kw: deep_discover(
            u, max_buildings=args.buildings, **kw)[0]
    else:
        discover_fn = discover

    if args.queue:
        return _discover_queue(args, discover_fn, to_source_spec)

    if not args.url:
        sys.exit("give a URL, or --queue sources.yaml")
    cands = discover_fn(args.url, wait_ms=args.wait,
                        headless=not args.headed, proxy=args.proxy)
    if not cands:
        print("No listing-shaped JSON endpoint found.")
        print("Either the site renders server-side (try the jsonld adapter), "
              "or inventory loads behind an interaction.")
        return 1

    for i, c in enumerate(cands[: args.top], 1):
        print(f"\n[{i}] score {c.score}  records {c.n_records}  {c.method}")
        if c.origin:
            print(f"    found on: {c.origin[:150]}")
        print(f"    {c.url[:160]}")
        print(f"    fields: {json.dumps(c.fields)}")
        print(f"    sample: {json.dumps(c.sample, default=str)[:400]}")

    spec = to_source_spec(cands[0], args.id or "discovered_source")
    print("\n--- paste into sources.yaml ---")
    try:
        import yaml
        print(yaml.safe_dump({"sources": [spec]}, sort_keys=False))
    except ImportError:
        print(json.dumps({"sources": [spec]}, indent=2))
    return 0


def _discover_queue(args, discover, to_source_spec) -> int:
    """Work down the ranked queue, emitting a source spec for each hit.

    Vendor-backed entries come first on purpose: every operator running the
    same property-management vendor shares an endpoint shape, so the first
    successful discovery against a vendor is reusable across all of them.
    """
    queue = load_queue(args.queue)
    if args.vendor:
        queue = [q for q in queue if args.vendor.lower() in
                 str(q.get("vendor", "")).lower()]
    if args.tier:
        queue = [q for q in queue if str(q.get("tier")) == str(args.tier)]
    queue = queue[args.offset: args.offset + args.top]
    if not queue:
        sys.exit("queue empty after filters")

    found, specs = 0, []
    for i, entry in enumerate(queue, 1):
        label = entry.get("operator", entry.get("id"))
        print(f"\n[{i}/{len(queue)}] {label}  ({entry.get('survey','?')}"
              f"{'/' + entry['vendor'] if entry.get('vendor') else ''})")
        print(f"    {entry['url']}")
        try:
            cands = discover(entry["url"], wait_ms=args.wait,
                             headless=not args.headed,
                             proxy=getattr(args, "proxy", None))
        except Exception as exc:
            print(f"    ! {type(exc).__name__}: {exc}")
            continue
        if not cands:
            print("    no listing-shaped endpoint found")
            continue
        best = cands[0]
        found += 1
        print(f"    OK  score {best.score}, {best.n_records} records")
        print(f"        {best.url[:150]}")
        if best.origin and best.origin != entry["url"]:
            print(f"        via building page: {best.origin[:120]}")
        print(f"        fields: {json.dumps(best.fields)}")
        specs.append(to_source_spec(best, entry["id"],
                                    operator=entry.get("operator", "")))

    print(f"\n{found}/{len(queue)} endpoints discovered")
    if specs:
        try:
            import yaml
            text = yaml.safe_dump({"sources": specs}, sort_keys=False)
        except ImportError:
            text = json.dumps({"sources": specs}, indent=2)
        if args.out:
            with open(args.out, "w") as fh:
                fh.write(text)
            print(f"wrote {args.out} -- review each field map, then merge "
                  f"into sources.yaml")
        else:
            print("\n--- paste into sources.yaml ---\n" + text)
    return 0 if found else 1


def cmd_listings(args) -> int:
    store = Store(connect(args.db))
    rows = store.active(limit=args.limit, max_price=args.max_price,
                        min_beds=args.min_beds, borough=args.borough)
    if not rows:
        print("no matching active listings")
        return 0
    for r in rows:
        price = f"${r['price']:,}" if r["price"] else "  n/a "
        beds = f"{r['beds']:g}br" if r["beds"] is not None else "  ?"
        unit = f"#{r['unit_key']}" if r["unit_key"] else "-"
        print(f"{price:>8}  {beds:>4}  {unit:<8} {r['borough'] or '?':<13} "
              f"{(r['address'] or '')[:46]:<46} {r['confidence']:<6} {r['source']}")
    print(f"\n{len(rows)} listings")
    return 0


def cmd_alerts(args) -> int:
    store = Store(connect(args.db))
    since = args.since or (
        datetime.now(timezone.utc) - timedelta(hours=args.hours)
    ).isoformat(timespec="seconds")

    if args.criteria:
        from .alerts import load_criteria
        crits = load_criteria(args.criteria)
    else:
        crits = [Criteria(name="cli", max_price=args.max_price,
                          min_beds=args.min_beds,
                          boroughs=args.borough.split(",") if args.borough else [],
                          require_no_fee=args.no_fee)]

    disp, total = Dispatcher(), 0
    for c in crits:
        total += disp.send(c, match_new(store, c, since))
    if total == 0:
        print(f"no new matches since {since}")
    return 0


def cmd_stats(args) -> int:
    conn = connect(args.db)
    store = Store(conn)
    print("== inventory ==")
    for k, v in store.summary().items():
        print(f"  {k:22} {v}")

    print("\n== lead time vs aggregators ==")
    for k, v in leadtime.report(conn).items():
        print(f"  {k:22} {v}")

    print("\n== recent crawls ==")
    rows = conn.execute(
        "SELECT source, started_at, ok, n_raw, n_new, error FROM crawl_runs "
        "ORDER BY id DESC LIMIT 12"
    ).fetchall()
    for r in rows:
        print(f"  {r['started_at']}  {r['source']:24} ok={r['ok']} "
              f"raw={r['n_raw']:<5} new={r['n_new']:<5} {r['error'] or ''}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="rentradar")
    p.add_argument("--db", default=DEFAULT_DB)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    c = sub.add_parser("crawl", help="run source adapters")
    c.add_argument("--sources", default=DEFAULT_SOURCES)
    c.add_argument("--source", action="append", help="limit to this source id")
    c.add_argument("--no-sweep", action="store_true",
                   help="do not mark missing listings off-market")
    c.set_defaults(fn=cmd_crawl)

    d = sub.add_parser("discover", help="find a site's listings JSON endpoint")
    d.add_argument("url", nargs="?")
    d.add_argument("--id", help="source id for the generated spec")
    d.add_argument("--queue", nargs="?", const=DEFAULT_SOURCES,
                   help="work down discover_queue in this registry "
                        "(default sources.yaml)")
    d.add_argument("--vendor", help="queue mode: only this PMS vendor")
    d.add_argument("--tier", help="queue mode: only this tier")
    d.add_argument("--out", help="queue mode: write generated specs here")
    d.add_argument("--wait", type=int, default=6000)
    d.add_argument("--top", type=int, default=3,
                   help="single URL: candidates to show. queue: operators to try")
    d.add_argument("--headed", action="store_true")
    d.add_argument("--deep", action="store_true",
                   help="follow rendered building links one level down "
                        "before giving up on an operator")
    d.add_argument("--buildings", type=int, default=4,
                   help="with --deep, building pages to try per operator")
    d.add_argument("--offset", type=int, default=0,
                   help="queue mode: skip this many entries first")
    d.add_argument("--proxy", help="route the browser through this proxy "
                                   "(default: direct, ignoring env proxies)")
    d.set_defaults(fn=cmd_discover)

    l = sub.add_parser("listings", help="query stored inventory")
    l.add_argument("--limit", type=int, default=40)
    l.add_argument("--max-price", type=int)
    l.add_argument("--min-beds", type=float)
    l.add_argument("--borough")
    l.set_defaults(fn=cmd_listings)

    a = sub.add_parser("alerts", help="dispatch matches for new listings")
    a.add_argument("--since")
    a.add_argument("--hours", type=int, default=24)
    a.add_argument("--criteria", help="JSON file of saved searches")
    a.add_argument("--max-price", type=int)
    a.add_argument("--min-beds", type=float)
    a.add_argument("--borough")
    a.add_argument("--no-fee", action="store_true")
    a.set_defaults(fn=cmd_alerts)

    s = sub.add_parser("stats", help="inventory and lead-time report")
    s.set_defaults(fn=cmd_stats)

    args = p.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    return args.fn(args)


if __name__ == "__main__":
    # Piping output into `head` closes stdout early; exit quietly rather than
    # dumping a BrokenPipeError traceback over the user's terminal.
    import signal
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, ValueError):
        pass
    raise SystemExit(main())
