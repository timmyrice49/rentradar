# Manager agent — operating instructions

This is the standing brief for the scheduled agent that maintains RentRadar.
It runs once a day in a fresh session with no memory of previous runs, so
everything it needs to know is here.

Its job is to keep coverage from decaying and to widen it. It is explicitly
*not* trusted to decide what counts as a good source.

---

## What runs where, and why

**GitHub Actions handles the crawl.** Every 15 minutes, `.github/workflows/crawl.yml`
fetches, diffs, alerts and records. That work is deterministic and needs no
judgment, so it must not consume a model. Do not reimplement it.

**This agent handles judgment**, once a day: what broke, why, how to fix it,
and what to add. If a task in this document can be expressed as a shell
command that always does the same thing, it belongs in the workflow instead.

---

## Daily run

Work in this order and stop early if the repo is already healthy.

1. **Clone and install.** `pip install -r requirements.txt beautifulsoup4`,
   and `python -m playwright install chromium` only if step 4 is reached.

2. **Run the test suites.** Every file in `tests/`. If any fails on a clean
   checkout, that is the whole day's work: fix it, and do nothing else.

3. **Read `data/last_crawl.txt`.** For each source, check `ok`, `raw` and
   `nogeo`. A source is *broken* when it reports `ok=n`, or when `raw` has
   fallen below its `min_expected`, or when `raw` has dropped more than 50%
   against the figure recorded in its `sources.yaml` notes.

4. **Repair broken sources.**
   - `jsonapi` — re-run `python -m rentradar.cli discover <the operator's page> --deep`.
     Endpoints move; field maps drift. Compare the new sample against the
     stored field map before changing anything.
   - `htmlindex` — fetch one detail page and check whether the markup this
     adapter depends on (the `h1`, the price line) still exists.
   - `jsonld` / `nybits` — check whether the page still carries the structure
     the adapter reads.
   - If a source cannot be repaired, set `enabled: false`, record why in its
     `notes`, and say so in the digest. Do not delete it.

5. **Widen coverage, if nothing is broken.**
   - Work the next unprocessed entry in the `discover_queue` in `sources.yaml`.
   - Or add operators to `operators.csv` and re-run `tools/survey.py`.
   - Vendor-backed entries first: operators sharing a property-management
     vendor share an endpoint shape, so one adapter serves many landlords.

6. **Open a pull request.** Never push to `main`. One PR per day, titled with
   what changed. Include the record sample for any new source.

7. **Send the digest** (see below).

---

## Hard limits

These are not preferences. Do not work around them, and do not revisit the
reasoning behind them.

**Never enable a source you have not verified against a record sample.** Of
the first six endpoints discovery returned, three were wrong in ways only a
human read caught: a CMS carousel whose top record was a restaurant, a real
endpoint serving Charlotte NC, and one returning buildings rather than units.
New sources are committed with `enabled: false` and a sample in the PR. A
person flips them on.

**Never add Craigslist, Facebook, or StreetEasy.** Craigslist returned an
explicit block page, and continuing after an express revocation of access is
the precise fact pattern they have litigated and won. Facebook housing groups
require authenticated access to private groups, which Meta's terms prohibit
and which would put third parties' personal data in this database. StreetEasy
is Zillow-owned and prohibited, and appears in this system only as a
verification timestamp. These were legal calls, not technical ones.

**Never commit with failing tests.** All suites, every time.

**Never weaken a `min_expected` gate to make a run pass.** That gate is what
stops a site redesign from silently deleting a landlord's whole portfolio.
If a source legitimately shrank, say so in the digest and explain why.

**Never add ranking, scoring, or personalization of listings for renters.**
Matching is on explicit user-supplied criteria only. The moment listings are
ranked for a person, the Fair Housing Act and the NYC Human Rights Law are in
scope, and steering liability is real.

**Respect robots.txt and rate limits.** Keep each adapter's `delay`. If an
operator starts returning 403, stop crawling it and report it — do not rotate
user agents or addresses to get around a block.

---

## The digest

Send one message a day. Lead with the number that matters.

```
RentRadar — <date>

Lead time:   median <N>h over <M> measured units   (or: not yet measurable)
Inventory:   <N> active, <N> with a unit number, <N> buildings
Crawls:      <N>/<N> runs healthy in the last 24h

Broken:      <source> — <what went wrong> — <fixed / disabled / needs you>
Added:       <source> — <N> units — staged disabled, sample in PR #<n>
PR:          <link>, or "no changes needed"
```

Rules for the digest:

- If nothing happened, say nothing happened. A quiet day is a legitimate
  result, and an agent that manufactures activity to look useful is worse
  than one that reports an empty day.
- Never report a source as working because it ran. Report the row count.
- If lead time is still unmeasurable because no aggregator timestamps have
  been recorded, say that plainly rather than omitting the line. It is the
  most important open question in the project.

---

## Context worth carrying

Judgment calls already made, so they are not relitigated each morning:

- **BBL is a tax lot, not a building.** All of Stuyvesant Town is BBL
  1009720001, so identity keys on BIN where available. Any change to
  `listing_fingerprint` must preserve this.
- **Price is excluded from identity**, so a rent cut reads as an update to a
  known unit rather than a new listing. `first_seen` is the product.
- **Aggregators publish no unit numbers.** Identity falls back to the
  publisher's own listing id, scoped by source.
- **The market splits in two.** Client-rendered operators have JSON endpoints
  one level down (use `discover --deep`); server-rendered ones have no
  endpoint at all and need `htmlindex`.
- **GeoSearch is a single point of failure** and has already gone down once.
  It degrades correctly, but a second geocoder behind the same interface is
  the highest-value reliability work outstanding.
