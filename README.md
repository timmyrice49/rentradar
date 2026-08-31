# RentRadar

Find NYC rental inventory upstream of the public aggregators, and measure by
how much.

StreetEasy is a syndication endpoint, not where listings are born. Every unit
exists first in a property-management system, on a landlord's own site, or in
a licensed broker feed, and takes hours to weeks to surface publicly.
RentRadar reads those upstream sources, resolves every unit to a real NYC
building, detects the moment a unit first appears, and alerts on it.

The system is built around one number: **measured lead time over the public
aggregators**. If that number is small, the thesis is wrong and you should
know within a week rather than after a seed round.

## Status

Working, verified against live sources:

| Piece | State |
|---|---|
| `jsonld` adapter | Verified: 10/10 units off Stonehenge NYC with address, unit, beds, sqft, price |
| `housing_connect` adapter | Verified: 31 active affordable lotteries from NYC Open Data |
| BBL resolution via NYC GeoSearch | Verified: 38/41 listings resolved to a real tax lot, 0 failures |
| Diff engine (new / price change / gone / relisted) | Verified by test |
| Lead-time ledger | Verified by test |
| Alerts + saved searches | Verified by test |
| `discover` (headless endpoint finder) | Logic tested offline; needs a browser with network egress to run live |
| `tools/survey.py` | Verified: 122 operators triaged in 72s |
| `tools/drilldown.py` | Verified: walks portfolio → building pages |
| `cli discover --queue` | Plumbing verified; endpoint capture needs a browser |
| `nybits` adapter | Verified: ~340 live listings, 5 boroughs, 0 collisions |
| `htmlindex` adapter | Verified: 52 Rockrose units, address + unit + rent, no browser |
| `jsonapi` adapter | Verified live: TF Cornerstone 124 units, StuyTown 96 |
| `discover --deep` | Verified: follows rendered building links; 5/10 on tier-1 SPAs |
| `reddit` adapter | Parser + filter tested offline; needs your API credentials |
| `crossmatch` (lead time) | Verified: 42 pairs found on live data, 34 on an exact rent match |
| HPD fallback geocoder | Verified: resolves to the same BBL/BIN as GeoSearch when the primary is down |

## What a 122-operator survey found

`tools/survey.py` was run against `operators.csv`, a registry of NYC
management companies and large landlords. The result should shape how you
spend engineering time, because it contradicts the obvious plan.

| | |
|---|---|
| Operators surveyed | 122 |
| Reachable from a plain HTTP client | 96 |
| Exposing inventory as structured data | **2** |
| Running an identifiable PMS vendor | 13 |
| Client-rendered shells | 32 |
| Server-rendered, no structured data | 49 |
| Disallowed by robots.txt | **0** |

Three findings, each verified rather than assumed:

**Nobody publishes inventory in HTML.** Of 14 sampled server-rendered
"availability" pages — real ones, like `/availabilities` and
`/search-result`, not homepages — **zero** contained a single inline price.
Those pages are portfolio indexes; units live one level deeper behind a
client-side widget. A generic HTML listing parser would have been wasted
work, which is the sort of thing worth spending an afternoon to find out.

**Building-level is no better.** `tools/drilldown.py` walked from portfolio
pages to individual building pages on tier-1 operators. Four of five had no
building links in raw HTML at all; the one that did returned 8 of 8 client-
rendered shells. So the ingestion target is a *building*, not an operator,
and there are 5–40 of them per operator.

**Nothing is forbidden, everything is technical.** Not one of the 122 sites
disallows this crawl in robots.txt. The barrier is JavaScript, not policy —
which means `discover` is the primary ingestion mechanism rather than a
fallback, and the two-speed design (render once, poll the found endpoint
forever) is what makes the economics work.

**Corollary: build vendor adapters, not site scrapers.** 13 operators run
Yardi/RentCafe, AppFolio, Nestio or On-Site. Every operator on the same
vendor shares an endpoint shape, so one adapter per vendor covers many
landlords. Vendor portals return 403 to plain HTTP clients, so they must be
discovered through a browser — `cli discover --queue --vendor rentcafe`
works down exactly that list.

The full results are in `survey_results.csv`; the ranked work queue is the
`discover_queue` section of `sources.yaml`.

### What discovery actually returned

Running the queue split the market cleanly in two, and each half needs a
different adapter.

**Client-rendered operators do have JSON endpoints, one level down.** The
shallow pass hit 3 of 13; adding `--deep`, which follows *rendered* building
links, took a comparable slice to 5 of 10. TF Cornerstone (124 units with
apartment numbers) and StuyTown (96 units with per-building addresses and
square footage) are live sources from this. Manhattan Skyline yields 27 units
but publishes only lat/lng, so it needs reverse geocoding before it can join
on BBL.

**Server-rendered operators have no endpoint at all** — no XHR to capture,
twice over. They publish an availability index of links to fully rendered unit
pages. That is what `htmlindex` is for, and Rockrose proves it: 52 unit links
in static HTML, each page carrying address, apartment number, rent and
concession. No browser anywhere in that path.

Three discovered endpoints were rejected on review, which is the point of
printing a record sample: Two Trees' `/api/slides` is the CMS carousel (top
record was a restaurant), Greystar's endpoint was real but resolved to
Charlotte NC, and WinnCompanies returns buildings rather than units. Also
worth noting `aemgmt.com` in `operators.csv` is an HVAC company, not A&E Real
Estate — a bad domain guess, not a dead site.

## Install

```bash
pip install -r requirements.txt
python -m playwright install chromium      # only needed for `discover`
```

Python 3.10+. No other services required — storage is SQLite.

## Use

```bash
python -m rentradar.cli crawl                       # all enabled sources
python -m rentradar.cli crawl --source stonehenge
python -m rentradar.cli listings --max-price 4500 --min-beds 1
python -m rentradar.cli alerts --hours 24 --max-price 4000 --borough Manhattan
python -m rentradar.cli stats                       # inventory + lead time
python -m rentradar.cli discover https://site/apartments --id newsite
```

Run the crawl on a schedule — every 15 minutes is the right cadence for the
affordable end of the market, where units clear in under 48 hours:

```
*/15 * * * *  cd /srv/rentradar && python -m rentradar.cli crawl >> crawl.log 2>&1
```

## How it fits together

```
sources.yaml
     │
     ▼
source adapters ──► RawListing   (no parsing, no cleanup — just extraction)
     │
     ▼
pipeline.normalize
     ├── normalize.py   address / unit / beds / price canonicalization
     ├── geocode.py     NYC GeoSearch → BBL + BIN   (cached forever)
     └── fingerprint    sha1(bbl, unit_key)  ← price deliberately excluded
     │
     ▼
store.upsert ──► new | price_change | relisted | seen
     │              │
     │              └──► listing_events   (full history)
     ▼
lead_time ledger ──► "we were N hours ahead"
     │
     ▼
alerts.match_new ──► Dispatcher
```

### Three decisions that carry the system

**BBL is the join key.** NYC Planning's GeoSearch turns any address into a
Borough-Block-Lot number for free, with no API key. Every dataset worth having
— PLUTO, HPD registrations, DOB filings, ACRIS, the DHCR rent-stabilized list
— keys on BBL. Resolve once at ingest and every future enrichment is a local
join. Addresses resolve to the same BBL forever, so the cache reaches ~100%
hit rate and the network drops out of the hot path.

**Identity excludes price.** A listing's fingerprint is `sha1(bbl, unit_key)`.
If price were part of it, every rent cut would look like a brand-new listing,
`first_seen` would be garbage, and the one metric that matters would be a lie.

**A source that returns almost nothing has broken, not sold out.** Each
adapter declares `min_expected`. Fall below it and the off-market sweep is
skipped and the run is marked failed. Without this, one CSS change silently
deletes a landlord's entire portfolio from the database.

## Adding a source

Try the generic adapters before writing code.

1. **JSON-LD first.** Management companies embed schema.org markup for SEO.
   Add the URL to `sources.yaml` under `type: jsonld` and see what comes back
   — it either works immediately or returns nothing.
2. **Otherwise, discover the XHR.** Most large operators ship an empty HTML
   shell and load inventory over XHR. `cli discover <url>` renders the page
   once with a headless browser, captures JSON responses, ranks the ones
   shaped like apartments, and prints a `jsonapi` spec to paste in. After
   that the site is polled as plain JSON and never rendered again.

That split is the crawl economics. Rendering 200 sites every 15 minutes with
Playwright costs thousands a month; rendering each once a week to re-verify
its endpoint and otherwise hitting JSON costs about one small VM.

## Testing

```bash
python tests/test_normalize.py          # parsing regressions
python tests/test_discovery_mapping.py  # endpoint discovery + JSON replay
python tests/test_pipeline.py           # diff engine, alerts, lead time
python tests/test_social_sources.py     # NYBits parsing, Reddit offer filter
```

Every case in `test_normalize.py` is a bug that actually happened against live
data. Two worth knowing about:

- Taking the last digit-bearing token out of `"101 West 15th Street 227 - 1
  Bedroom"` yields unit `"1"`, which collides the fingerprints of every
  one-bedroom in the building — real listings then look like price changes on
  one phantom unit. The descriptor tail has to come off first.
- `"1 Bedroom"` parsed to `None` because the regex required a word boundary
  right after `bed`. Every one-bedroom in the system was silently bed-less.

## Measuring lead time

`cli crossmatch` pairs upstream listings with aggregator listings and writes
the result to the lead-time ledger. Aggregators publish a building, a bed
count and a rent but no apartment number, so the match is
`(building, beds, price)` -- within one building and bed count the rent is
close to unique.

Four guards stop it producing a flattering number, and on the first live run
**all four fired and left zero usable measurements**. That is the correct
outcome, and worth understanding before you read any figure it emits:

1. **Mixed clocks are excluded.** Comparing our `first_seen` against a
   publisher's posting date guarantees a negative lead on a young database:
   ours says "now", theirs says "four days ago". Only same-clock pairs count,
   and every measurement records its `basis`.
2. **Left-censored listings are excluded.** If a unit was already on the
   market when the crawler started, its `first_seen` records our start date,
   not the listing's. On a cold database that is every row. A source needs two
   crawls before any of its `first_seen` values mean anything.
3. **Implausible pairs are rejected.** More than 60 days apart and it is
   almost certainly not the same apartment -- a stale aggregator listing
   pairing with a freshly listed unit at the same rent.
4. **One-to-one matching.** A building with six identical studios cannot
   contribute six copies of one pairing.

So the honest status is: the machinery works, and it needs the crawler to
accumulate history rather than more code. Once each source has run twice,
newly-appearing units give clean `first_seen -> first_seen` measurements.

And note what it measures: lead over **NYBits**, not over StreetEasy. Real,
but weaker than the pitch. Calibrate with a manual StreetEasy sample -- their
Property History shows a listed date retroactively, so one session can date a
hundred listings you found today -- before making the stronger claim.

## The informal channel: what's reachable and what isn't

Small landlords and rent-stabilized walk-ups have no websites, so the obvious
move is to go where they post informally. Three candidates were evaluated;
only one of the obvious two survived, and a better third turned up.

**Facebook housing groups — not built, and not buildable lawfully.** The posts
live in private groups, so reading them means authenticated access, which is
categorically different from crawling public pages. Meta's terms prohibit
automated collection, the Groups API was restricted after Cambridge Analytica
and only works for apps a group admin installs, and Meta has an active
litigation record on exactly this. It would also put group members' personal
data in your database. There is no compliant version of this, so there is no
adapter for it.

**Craigslist — they blocked us, and continuing is the exact fact pattern they
win on.** A request to their public RSS endpoint returned `403` with a page
that reads "Your request has been blocked." Craigslist sued 3Taps after a
cease-and-desist plus IP block and won a CFAA claim on the theory that access
continued after authorization was expressly revoked. An explicit block page is
that revocation. Circumventing it would be the single clearest legal red line
in this project, so this adapter does not exist either. (The 403 may be this
sandbox's datacenter IP rather than a universal block — but the right response
to a block page is to stop, not to change addresses until it goes away.)

**NYBits — built, and it turned out to be the better source anyway.**
Server-rendered, permitted by robots.txt, stable per-listing ids, 340 live NYC
listings across all five boroughs in a single crawl. Critically it republishes
inventory from operators whose own sites are unreadable — Rose Associates,
Beam Living, Kings and Queens Leasing and Halcyon Management all appear, and
three of those the direct survey could not reach at all. It carries a
publisher-supplied freshness stamp and a net-effective-rent flag, which is the
honest affordability signal post-FARE-Act.

**Reddit — built, disabled pending your API credentials.** This is the lawful
version of the Facebook idea: the same small-landlord, sublet and lease-break
inventory, through a documented OAuth API with a published rate limit. Precision
is low by construction — it is prose written by strangers, most posts are people
seeking rather than offering, addresses are usually just a neighborhood, and
scams are common in this channel. Every row is tagged `unverified` and carries a
link to the original post. Treat the output as leads for a human, not inventory.
Register a script app at `reddit.com/prefs/apps`, set `REDDIT_CLIENT_ID` and
`REDDIT_CLIENT_SECRET`, then enable `reddit_nyc` in `sources.yaml`.

### Identity without unit numbers

Aggregators publish a building and a rent but no apartment number. Three
different 1-beds at 70 Pine Street at $5,608, $5,727 and $5,913 would collapse
into one fingerprint, and two of the three would be recorded as price changes
on a phantom unit. So `listing_fingerprint` now takes the publisher's own
listing id as a fallback key, scoped by source. The trade is that the same
apartment seen through two id-only sources stays two records — the right way
round, because over-merging corrupts `first_seen` and under-merging only
duplicates.

Getting there surfaced two more real parsing bugs, both now covered by tests:
`"1-Bedroom at 70 Pine Street"` yielded unit `"70"` from the house number
(faking high confidence and re-collapsing the building), and
`"Studio at Gateway: 365 South End"` yielded `"365"` because the address has no
street suffix to strip. Units are now vetoed against the listing's own house
number, and a number followed by a capitalised word is treated as an address
rather than a unit.

## What is deliberately not here

**StreetEasy is not a source.** Zillow-owned, ToS-prohibited, actively
enforced — and strategically pointless, since anything sourced there is late
by definition. It appears only as a verification timestamp handed to
`leadtime.record_aggregator_hit`, never as inventory.

**REBNY RLS is not a scraping target.** It is a licensed feed: a Direct Data
License Feed Application, a member brokerage, monthly committee review, a
signed agreement. That is a corporate-development track, not an engineering
one — but it is the single largest available step-change in coverage, because
broker listings enter RLS before they syndicate anywhere.

**No ranking model.** The moment you rank or recommend listings for a person
you are in scope for the Fair Housing Act and the NYC Human Rights Law.
`Criteria` is a closed set of explicit, user-supplied fields, every match
records its reasons, and nothing infers anything about the renter. Add
personalization only with counsel and an audit trail.

## Known limits

- Stonehenge's JSON-LD block carries 10 of its 70 units; the rest load
  client-side. Run `discover` against it to pick up the remainder.
- `discover` could not be exercised live in the build sandbox — Chromium has
  no network egress there. The pure functions it depends on are tested; the
  browser path needs one run on a normal machine before you trust it.
- Housing Connect is building-level, not unit-level, and prices are AMI bands
  rather than a scalar, so those rows carry no `price`.
- SQLite is right until you have concurrent writers. The schema is
  Postgres-compatible; the migration is mechanical.
- **GeoSearch is a single point of failure, and it went down during this
  build** — HTTP 503 for the last stretch of testing, after resolving 38/41
  listings cleanly earlier the same day. The system degrades correctly:
  transient failures are not cached, listings are still stored and alertable
  at `low` confidence, and the next crawl re-resolves them. But BBL is the
  join key for everything, so before this is load-bearing, add a second
  geocoder behind the same interface — NYC's GeoService API
  (`geoservice.planning.nyc.gov`) and `locatenyc.io` are the alternates.
- 19 of the 26 unreachable operators failed with connection resets from the
  build sandbox's egress filter rather than from anything the sites did.
  Re-run `tools/survey.py` from an ordinary network before treating any of
  them as blocked. Two more were bad domain guesses in `operators.csv`; five
  genuinely refuse a default user agent with HTTP 403.
