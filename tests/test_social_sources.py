"""Tests for the aggregator and social-channel adapters.

The Reddit tests matter more than they look. That adapter reads free-text
prose written by strangers, so its whole value depends on the offer/seeking
filter and the extraction being conservative. A false positive here is a
scam post or somebody's rant in a renter's alerts.

Both suites run fully offline: HTTP is stubbed, so nothing here depends on a
live site or on API credentials.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rentradar.geocode import GeoResult
from rentradar.normalize import listing_fingerprint
from rentradar.pipeline import normalize
from rentradar.sources.nybits import NyBitsSource
from rentradar.sources.reddit import RedditSource

failures = 0


def check(label, got, want):
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label:52} got={got!r}")


# ---------------------------------------------------------------------------
# NYBits — markup copied from a live search-results page
# ---------------------------------------------------------------------------

CARD = """
<article class="card">
  <div class="card__wrapper">
    <div class="card__labels"><span class="card__label">No fee</span></div>
    <div class="card__content">
      <p class="card__info">
        <span>Posted &lt; 11 hours</span>
        <span>Rose Associates, Inc, Manager</span>
      </p>
      <a class="card__title" href="https://www.nybits.com/apartmentlistings/%s.html">
        <h3>1-Bedroom at 70 Pine Street</h3>
      </a>
      <p class="card__adress">70 Pine Street (Financial District, MN)</p>
      <div class="card__price"><span>$%s/month</span> net effective rent</div>
      <div class="card__facilities">
        <ul><li>1 Bed</li><li>1.0 Bath</li></ul>
        <ul><li>Doorman</li><li>Gym</li><li>18 elevators</li></ul>
      </div>
    </div>
  </div>
</article>
"""

PAGE = "<html><body>" + "".join(
    CARD % (ref, price) for ref, price in
    [("e27e7d85e8a1" + "0" * 20, "5,608"),
     ("c3daa1bafd91" + "0" * 20, "5,727"),
     ("41bec6a75233" + "0" * 20, "5,913")]
) + "</body></html>"


class StubbedNyBits(NyBitsSource):
    def get(self, url, timeout=25, accept="text/html"):
        return PAGE


print("NYBits parsing")
src = StubbedNyBits(searches=["https://www.nybits.com/search/x.html"])
rows = src.fetch()
check("three distinct cards parsed", len(rows), 3)
r = rows[0]
check("street extracted", r.address, "70 Pine Street")
check("borough from code", r.borough_hint, "Manhattan")
check("neighborhood kept", r.extra["neighborhood"], "Financial District")
check("price", r.price_raw, "5,608")
check("net effective flagged", r.extra["net_effective"], True)
check("no-fee label", r.no_fee, True)
check("beds", r.beds_raw, "1 Bed")
check("baths", r.baths_raw, "1.0")
check("hours-old freshness parsed", r.extra["posted"], "11 hours")
check("manager cleaned of 'Posted'", r.extra["manager"], "Rose Associates, Inc, Manager")
check("amenities from <li>", r.extra["amenities"], ["Doorman", "Gym", "18 elevators"])
check("stable ref captured", r.source_ref.startswith("e27e7d85e8a1"), True)
check("detail url", r.detail_url.endswith(".html"), True)
check("no unit published", r.unit_raw, None)

print("\nsame-building units stay distinct  (the bug this source would cause)")
BBL = "1000730001"
fps = {listing_fingerprint(BBL, "", "70 Pine Street", 1.0,
                           source="nybits", source_ref=x.source_ref)
       for x in rows}
check("three fingerprints, not one", len(fps), 3)
naive = {listing_fingerprint(BBL, "", "70 Pine Street", 1.0) for _ in rows}
check("without source_ref they would collapse", len(naive), 1)

print("\ncross-crawl stability")
again = StubbedNyBits(searches=["https://www.nybits.com/search/x.html"]).fetch()
check("same refs next crawl", [x.source_ref for x in again],
      [x.source_ref for x in rows])


class FakeGeo:
    stats = {"hit": 0, "miss": 0, "fail": 0, "low_confidence": 0}

    def lookup(self, address, borough_hint=None):
        return GeoResult(BBL, "1001234", "70 PINE STREET, New York, NY, USA",
                         "Manhattan", 40.70, -74.00, 1.0)


print("\npipeline integration")
listings = [normalize(x, FakeGeo()) for x in rows]
check("all normalized", all(l is not None for l in listings), True)
check("distinct after normalize", len({l.fingerprint for l in listings}), 3)
check("confidence medium (building known, unit not)",
      {l.confidence for l in listings}, {"medium"})
check("prices parsed", sorted(l.price for l in listings), [5608, 5727, 5913])
check("beds parsed", {l.beds for l in listings}, {1.0})

# ---------------------------------------------------------------------------
# Reddit — offer/seeking filter and extraction
# ---------------------------------------------------------------------------

print("\nReddit offer filter")
cases = [
    ("No fee 1BR available in Bushwick, $2400/mo", "", True, "plain offer"),
    ("Lease break: studio in Astoria $2,100", "", True, "lease break"),
    ("[ISO] Looking for a 1BR under $2500 in Brooklyn", "", False, "ISO seeker"),
    ("Looking for apartment available in Harlem", "", False, "seeker quoting 'available'"),
    ("Is $3000 normal for a studio in the East Village?", "", False, "question"),
    ("Rate my apartment search strategy", "", False, "chatter"),
    ("Subletting my Greenpoint 2 bed, $3,200", "", True, "sublet"),
    ("Advice on my broker fee", "no fee available", False, "advice post"),
]
for title, body, want, label in cases:
    check(label, RedditSource.is_offer(title, body), want)

print("\nReddit extraction")
got = RedditSource.extract(
    "No fee 1BR available in Bushwick", "Asking $2,400/month at 123 Troutman Street.")
check("price", got["price"], 2400)
check("beds", got["beds"], "1BR")
check("address", got["address"], "123 Troutman Street")
check("neighborhood", got["neighborhood"], "bushwick")

got2 = RedditSource.extract("Studio available in Astoria", "$1,950 a month, no fee")
check("no address is fine", got2["address"], None)
check("neighborhood carries it", got2["neighborhood"], "astoria")

got3 = RedditSource.extract("Sublet", "Deposit is $50 and rent is $2,800")
check("implausible price ignored, real one kept", got3["price"], 2800)

got4 = RedditSource.extract("Room in Bushwick", "Utilities around $120")
check("no plausible rent -> none", got4["price"], None)


class StubbedReddit(RedditSource):
    POSTS = [
        {"id": "abc123", "title": "No fee 1BR available in Bushwick",
         "selftext": "Asking $2,400/month at 123 Troutman Street. DM me.",
         "author": "someone", "permalink": "/r/NYCapartments/comments/abc123/x/"},
        {"id": "def456", "title": "[ISO] Looking for 1BR under $2500",
         "selftext": "Any leads appreciated", "author": "seeker",
         "permalink": "/r/NYCapartments/comments/def456/y/"},
        {"id": "ghi789", "title": "Is my landlord allowed to do this?",
         "selftext": "They raised rent $300", "author": "asker",
         "permalink": "/r/NYCapartments/comments/ghi789/z/"},
    ]

    def _api(self, path):
        return {"data": {"children": [{"data": p} for p in self.POSTS]}}


print("\nReddit end-to-end (stubbed API)")
rs = StubbedReddit(subreddits=["NYCapartments"], client_id="x", client_secret="y")
rs.delay = 0
out = rs.fetch()
check("only the real offer survives", len(out), 1)
check("post id as ref", out[0].source_ref, "abc123")
check("address preferred over neighborhood", out[0].address, "123 Troutman Street")
check("links to original post", out[0].detail_url.endswith("/abc123/x/"), True)
check("flagged unverified", out[0].extra["unverified"], True)
check("no-fee detected", out[0].no_fee, True)

print("\nReddit fails loudly without credentials")
bare = RedditSource(subreddits=["NYCapartments"])
bare.client_id = bare.client_secret = None
try:
    bare.fetch()
    check("raises SourceError", False, True)
except Exception as exc:
    check("raises SourceError naming the fix",
          "prefs/apps" in str(exc), True)

print(f"\n{'PASS' if failures == 0 else str(failures) + ' FAILURES'}")
sys.exit(1 if failures else 0)
