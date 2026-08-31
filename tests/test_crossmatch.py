"""Tests for lead-time measurement.

This is the number the company rests on, so the tests care less about whether
matching works and more about whether it can lie. Each case below is a way the
measurement could flatter itself:

  * counting one aggregator listing against six identical units in a building
  * silently comparing our crawl clock against a publisher clock
  * quietly dropping the cases where the aggregator got there first
  * letting a later re-listing overwrite the original measurement
"""
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rentradar import crossmatch, leadtime
from rentradar.models import Listing, utcnow
from rentradar.sources.nybits import posted_to_timestamp
from rentradar.store import Store, connect

failures = 0
NOW = datetime(2026, 8, 31, 12, 0, 0, tzinfo=timezone.utc)


def check(label, got, want):
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label:54} got={got!r}")


def iso(**kw):
    return (NOW - timedelta(**kw)).isoformat(timespec="seconds")


def mk(store, fp, source, price, beds=1.0, bin_="1000001", unit="",
       listed_at=None, first_seen=None):
    ts = first_seen or utcnow()
    store.upsert(Listing(
        fingerprint=fp, source=source, source_url="x", detail_url=None,
        address="70 Pine Street", address_raw="70 Pine Street",
        bbl="1000730001", bin=bin_, borough="Manhattan", lat=None, lon=None,
        unit_key=unit, unit_raw=None, price=price, beds=beds, baths=None,
        sqft=None, available_on=None, listed_at=listed_at, no_fee=None,
        first_seen=ts, last_seen=ts, confidence="high", extra={},
    ))


print("posted stamp -> timestamp")
check("hours", posted_to_timestamp("11 hours", NOW), iso(hours=11))
check("yesterday", posted_to_timestamp("yesterday", NOW), iso(days=1))
check("days ago", posted_to_timestamp("12 days ago", NOW), iso(days=12))
check("unparseable", posted_to_timestamp("soon", NOW), None)

print("\nmatching")
conn = connect(":memory:")
store = Store(conn)
# Upstream: landlord published it 5 days ago.
mk(store, "up1", "tfcornerstone", 5608, unit="12B", listed_at=iso(days=5))
# Aggregator: same building, same beds, same rent, posted yesterday.
mk(store, "ag1", "nybits", 5608, listed_at=iso(days=1))

m = crossmatch.find_matches(conn)
check("one pair", len(m), 1)
check("exact rent match", m[0]["matched_by"], "building+beds+price")
check("both clocks are publisher dates", m[0]["basis"], "listed_at->listed_at")

res = crossmatch.run(conn)
check("recorded", res["recorded"], 1)
row = conn.execute("SELECT lead_hours, basis, matched_fp FROM lead_time "
                   "WHERE fingerprint='up1'").fetchone()
check("lead is 4 days", round(row["lead_hours"] / 24, 2), 4.0)
check("basis stored", row["basis"], "listed_at->listed_at")
check("audit trail to the aggregator row", row["matched_fp"], "ag1")

print("\nre-running does not double count or drift")
res2 = crossmatch.run(conn)
check("nothing new recorded", res2["recorded"], 0)
check("counted as already measured", res2["already_measured"], 1)
row2 = conn.execute("SELECT lead_hours FROM lead_time "
                    "WHERE fingerprint='up1'").fetchone()
check("original measurement unchanged", row2["lead_hours"], row["lead_hours"])

print("\none aggregator listing cannot be counted against many units")
conn2 = connect(":memory:")
s2 = Store(conn2)
for i in range(6):
    mk(s2, f"u{i}", "stuytown", 3000, unit=f"0{i}A", listed_at=iso(days=6))
mk(s2, "a0", "nybits", 3000, listed_at=iso(days=2))
m2 = crossmatch.find_matches(conn2)
check("six identical units, one aggregator row -> one pair", len(m2), 1)

print("\nnet-effective vs gross rent still pairs, exact is preferred")
conn3 = connect(":memory:")
s3 = Store(conn3)
mk(s3, "u_exact", "rockrose", 4000, unit="1A", listed_at=iso(days=3))
mk(s3, "u_near", "rockrose", 4100, unit="2A", listed_at=iso(days=3))
mk(s3, "a_exact", "nybits", 4000, listed_at=iso(days=1))
m3 = crossmatch.find_matches(conn3)
check("the exact-price unit wins the single aggregator row",
      m3[0]["upstream_fp"], "u_exact")
check("only one pair", len(m3), 1)

print("\nguards")
conn4 = connect(":memory:")
s4 = Store(conn4)
mk(s4, "ub", "rockrose", 3000, beds=0.0, unit="1A", listed_at=iso(days=3))
mk(s4, "ab", "nybits", 3000, beds=2.0, listed_at=iso(days=1))
check("bed count must agree", len(crossmatch.find_matches(conn4)), 0)

conn5 = connect(":memory:")
s5 = Store(conn5)
mk(s5, "uc", "rockrose", 3000, unit="1A", bin_="1000001", listed_at=iso(days=3))
mk(s5, "ac", "nybits", 3000, bin_="1000002", listed_at=iso(days=1))
check("different building, no pair", len(crossmatch.find_matches(conn5)), 0)

conn6 = connect(":memory:")
s6 = Store(conn6)
mk(s6, "ud", "rockrose", 3000, unit="1A", listed_at=iso(days=3))
mk(s6, "ad", "nybits", 3600, listed_at=iso(days=1))     # 20% apart
check("rent too far apart, no pair", len(crossmatch.find_matches(conn6)), 0)

print("\nevidence against the thesis is recorded, not dropped")
conn7 = connect(":memory:")
s7 = Store(conn7)
# Aggregator had it three days BEFORE the landlord's own site.
mk(s7, "ue", "rockrose", 3000, unit="1A", listed_at=iso(days=1))
mk(s7, "ae", "nybits", 3000, listed_at=iso(days=4))
r7 = crossmatch.run(conn7)
check("negative lead still recorded", r7["recorded"], 1)
lead7 = conn7.execute("SELECT lead_hours FROM lead_time "
                      "WHERE fingerprint='ue'").fetchone()["lead_hours"]
check("lead is negative", round(lead7 / 24), -3)
rep = leadtime.report(conn7)
check("share_ahead reflects the miss", rep["share_ahead"], 0.0)

print("\nreport always states which clocks produced the number")
rep1 = leadtime.report(conn)
check("basis breakdown present", rep1["basis"], {"listed_at->listed_at": 1})
check("aggregator named", rep1["vs"], {"nybits": 1})

print("\nimplausible pairs are rejected, not measured")
conn9 = connect(":memory:")
s9 = Store(conn9)
mk(s9, "ug", "rockrose", 3000, unit="1A", listed_at=iso(days=1))
mk(s9, "ag", "nybits", 3000, listed_at=iso(days=200))   # 200 days apart
r9 = crossmatch.run(conn9)
check("pair found", r9["pairs_found"], 1)
check("but rejected as implausible", r9["implausible"], 1)
check("nothing recorded", r9["recorded"], 0)

print("\ncensoring: a listing present at the first crawl is not evidence")
conn10 = connect(":memory:")
s10 = Store(conn10)
rid = s10.start_run("rockrose"); s10.finish_run(rid, ok=True)
mk(s10, "uh", "rockrose", 3000, unit="1A")
mk(s10, "ah", "nybits", 3000)
m10 = crossmatch.find_matches(conn10)
check("marked censored", m10[0]["basis"], "censored:present-at-first-crawl")
check("excluded from the report",
      leadtime.report(conn10).get("measured", 0), 0)

print("\nfalls back to crawl time, and says so")
conn8 = connect(":memory:")
s8 = Store(conn8)
mk(s8, "uf", "rockrose", 3000, unit="1A", first_seen=iso(days=2))
mk(s8, "af", "nybits", 3000, first_seen=iso(days=1))
m8 = crossmatch.find_matches(conn8)
check("basis marked as crawl-time on both sides",
      m8[0]["basis"], "first_seen->first_seen")

print(f"\n{'PASS' if failures == 0 else str(failures) + ' FAILURES'}")
sys.exit(1 if failures else 0)
