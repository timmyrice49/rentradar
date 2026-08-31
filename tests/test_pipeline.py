"""End-to-end test of the diff engine, alerts and lead-time ledger.

Runs entirely offline against an in-memory database with a stubbed geocoder,
because `first_seen` integrity is the one thing that must never regress: it is
the number the whole business claim rests on.
"""
import os, sqlite3, sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rentradar import leadtime
from rentradar.alerts import Criteria, Dispatcher, match_new
from rentradar.geocode import GeoResult
from rentradar.models import RawListing
from rentradar.pipeline import normalize, run_source
from rentradar.sources.base import Source
from rentradar.store import Store, connect

failures = 0


def check(label, got, want):
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label:50} got={got!r} want={want!r}")


class FakeGeo:
    """Deterministic geocoder: every address maps to one fixed building."""
    stats = {"hit": 0, "miss": 0, "fail": 0, "low_confidence": 0}

    def lookup(self, address, borough_hint=None):
        if "unknown" in address.lower():
            return None
        return GeoResult(bbl="1010327501", bin="1087264",
                         label="350 WEST 42 STREET, New York, NY, USA",
                         borough="Manhattan", lat=40.75, lon=-73.99,
                         confidence=1.0)


class FakeSource(Source):
    id = "fake"
    delay = 0.0
    min_expected = 1

    def __init__(self, rows):
        super().__init__()
        self.rows = rows

    def fetch(self):
        return self.rows


def mk(unit, price, title=None):
    return RawListing(source="fake", source_url="https://x",
                      address="350 West 42nd Street", unit_raw=unit,
                      price_raw=price, beds_raw="1 Bedroom",
                      title=title or f"Apt {unit} - 1 Bedroom")


conn = connect(":memory:")
store, geo = Store(conn), FakeGeo()

print("crawl 1: two units appear")
r1 = run_source(FakeSource([mk("12B", 4200), mk("14C", 4400)]), store, geo)
check("new listings", r1.n_new, 2)
check("nothing marked gone", r1.n_gone, 0)

print("\ncrawl 2: same units, one price cut")
r2 = run_source(FakeSource([mk("12B", 3950), mk("14C", 4400)]), store, geo)
check("no false new listings", r2.n_new, 0)
check("price change detected", r2.n_updated, 1)

row = conn.execute("SELECT first_seen, last_seen, price FROM listings "
                   "WHERE unit_key='12B'").fetchone()
check("price updated", row["price"], 3950)
check("first_seen preserved", row["first_seen"] <= row["last_seen"], True)
fs_12b = row["first_seen"]

print("\ncrawl 3: 14C rents, disappears from the feed")
r3 = run_source(FakeSource([mk("12B", 3950)]), store, geo)
check("marked off-market", r3.n_gone, 1)
check("active count", store.summary()["listings_active"], 1)

print("\ncrawl 4: 14C comes back (broken lease)")
r4 = run_source(FakeSource([mk("12B", 3950), mk("14C", 4100)]), store, geo)
check("relist is not a new listing", r4.n_new, 0)
check("relist counted as update", r4.n_updated, 1)
back = conn.execute("SELECT first_seen, gone_at FROM listings "
                    "WHERE unit_key='14C'").fetchone()
check("gone_at cleared", back["gone_at"], None)

print("\ncrawl 5: source breaks and returns nothing")
broken = FakeSource([])
broken.min_expected = 2
r5 = run_source(broken, store, geo)
check("run marked failed", r5.ok, False)
check("inventory NOT wiped", store.summary()["listings_active"], 2)

print("\ngeocode failure keeps the listing at low confidence")
l = normalize(RawListing(source="fake", source_url="x",
                         address="999 Unknown Way", unit_raw="1A",
                         price_raw=2500, beds_raw="1 Bedroom"), geo)
check("still produced", l is not None, True)
check("flagged low", l.confidence, "low")
check("no bbl", l.bbl, None)

print("\nalerts")
crit = Criteria(name="budget 1br", max_price=4000, min_beds=1,
                boroughs=["Manhattan"])
matches = match_new(store, crit, "2000-01-01T00:00:00+00:00")
check("one match under budget", len(matches), 1)
check("matched the right unit", matches[0][0]["unit_key"], "12B")

over = Criteria(name="too cheap", max_price=1000)
check("nothing matches an impossible budget",
      len(match_new(store, over, "2000-01-01T00:00:00+00:00")), 0)

sent = []
Dispatcher(sink=lambda s, b: sent.append(s)).send(crit, matches)
check("dispatcher fired once", len(sent), 1)

print("\nlead time")
fp = matches[0][0]["fingerprint"]
later = (datetime.fromisoformat(fs_12b) + timedelta(hours=52)).isoformat()
lead, wrote = leadtime.record_aggregator_hit(conn, fp, "streeteasy", later)
check("lead hours", round(lead, 1), 52.0)
check("reported as a new measurement", wrote, True)
again, wrote2 = leadtime.record_aggregator_hit(
    conn, fp, "streeteasy", (datetime.now(timezone.utc)).isoformat())
check("first hit is not overwritten", round(again, 1), 52.0)
check("second call reports no write", wrote2, False)
rep = leadtime.report(conn)
check("one measurement", rep["measured"], 1)
check("median", rep["median_hours"], 52.0)
check("share ahead of 24h", rep["share_ahead_24h"], 1.0)

print(f"\n{'PASS' if failures == 0 else str(failures) + ' FAILURES'}")
sys.exit(1 if failures else 0)
