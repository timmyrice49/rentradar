"""Measure lead time by matching upstream listings against an aggregator.

This is the module that turns the whole system from a crawler into evidence.
Everything else exists to feed it.

The problem it solves: upstream sources publish apartment numbers, aggregators
mostly do not. NYBits gives a building, a bed count and a rent, but never a
unit. So `(building, unit)` -- the identity the rest of the system runs on --
cannot join the two sides, and the lead-time ledger stays empty forever.

The match used here is `(building, beds, price)`. Within one building and one
bed count, the rent is close to unique, so an exact price match is a strong
pair. A near-price match is offered as a weaker tier because aggregators
sometimes quote net-effective rent where the landlord quotes gross.

Three things keep this honest rather than flattering:

  * Every measurement records its `basis` -- which clocks were compared.
    A lead computed from two publisher-stated dates means something quite
    different from one where we substituted our own crawl time, and a number
    without that label is uninterpretable.
  * Negative leads are recorded, not discarded. If the aggregator had it
    first, that is evidence against the thesis and it belongs in the data.
  * Matches are one-to-one and first-write-wins, so a building with six
    identical studios cannot inflate the sample with six copies of one pairing.

What this is NOT: a measurement against StreetEasy. It measures lead over
NYBits. That is a real public aggregator and a real result, but the pitch
says StreetEasy, and the two must not be conflated in anything anyone raises
money on. Calibrate against a manual StreetEasy sample before making the
stronger claim.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from .leadtime import record_aggregator_hit

log = logging.getLogger(__name__)

#: Sources that publish inventory before the public aggregators do. Anything
#: not listed here is treated as an aggregator or as neither.
DEFAULT_UPSTREAM = ["stonehenge", "tfcornerstone", "stuytown", "rockrose"]
DEFAULT_AGGREGATORS = ["nybits"]

#: Fractional rent difference still treated as the same unit. Aggregators
#: quote net-effective rent where landlords quote gross, so requiring an
#: exact match would throw away most real pairs in concession-heavy buildings.
NEAR_PRICE_TOLERANCE = 0.04

#: A pair implying a lead of more than this many days is almost certainly not
#: the same apartment. Matching on (building, beds, price) cannot tell one
#: unit from another, and a listing that sits on an aggregator for months will
#: happily pair with a freshly listed unit at the same rent. Rejecting these
#: costs a little recall and buys the ability to trust the median.
MAX_PLAUSIBLE_LEAD_DAYS = 60


def _parse(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _clock(row) -> tuple[str, str]:
    """Best available time for a listing, and the name of that clock.

    `listed_at` is what the publisher says; `first_seen` is only when we
    started looking. On a cold crawl every first_seen is the same instant, so
    preferring the publisher's date is the difference between a real
    measurement and a database full of zeros.
    """
    if row["listed_at"] and _parse(row["listed_at"]):
        return row["listed_at"], "listed_at"
    return row["first_seen"], "first_seen"


def _building(row) -> str | None:
    return row["bin"] or row["bbl"]


def _second_run_by_source(conn) -> dict[str, str | None]:
    """Start time of each source's SECOND crawl.

    A listing whose first_seen predates that was already on the market when we
    began watching, so its first_seen records our start date, not the
    listing's. Comparing it to anything measures the database's age. Sources
    that have only ever run once have no second run, and everything from them
    is censored.
    """
    out: dict[str, str | None] = {}
    for (source,) in conn.execute("SELECT DISTINCT source FROM crawl_runs"):
        row = conn.execute(
            "SELECT started_at FROM crawl_runs WHERE source=? "
            "ORDER BY started_at LIMIT 1 OFFSET 1", (source,)
        ).fetchone()
        out[source] = row[0] if row else None
    return out


def _censors(second_run: dict, source: str, first_seen: str) -> bool:
    """Whether this listing's first_seen is uninformative.

    Censoring needs evidence of when we started watching. A source with no
    recorded runs at all (a directly-populated or imported database) gives us
    none, so we do not censor it; one recorded run means everything it holds
    was present at the start; two or more lets us name the cutoff.
    """
    if source not in second_run:
        return False
    cutoff = second_run[source]
    return cutoff is None or first_seen < cutoff


def find_matches(conn, upstream_sources=None, aggregator_sources=None,
                 tolerance: float = NEAR_PRICE_TOLERANCE) -> list[dict]:
    """Pair upstream listings with aggregator listings in the same building.

    Returns one dict per pair, best matches first, each upstream listing and
    each aggregator listing used at most once.
    """
    upstream_sources = upstream_sources or DEFAULT_UPSTREAM
    aggregator_sources = aggregator_sources or DEFAULT_AGGREGATORS

    def load(sources):
        marks = ",".join("?" for _ in sources)
        return conn.execute(
            f"SELECT fingerprint, source, bbl, bin, beds, price, address, "
            f"unit_key, first_seen, listed_at "
            f"FROM listings WHERE source IN ({marks}) "
            f"AND (bin IS NOT NULL OR bbl IS NOT NULL) AND price IS NOT NULL",
            sources,
        ).fetchall()

    ups = load(upstream_sources)
    aggs = load(aggregator_sources)
    if not ups or not aggs:
        return []

    second_run = _second_run_by_source(conn)

    by_building: dict[str, list] = {}
    for a in aggs:
        by_building.setdefault(_building(a), []).append(a)

    candidates: list[dict] = []
    for u in ups:
        for a in by_building.get(_building(u), []):
            # Bed count must agree when both sides state it. A studio is not a
            # two-bedroom no matter how close the rent.
            if u["beds"] is not None and a["beds"] is not None \
                    and abs(u["beds"] - a["beds"]) > 0.01:
                continue
            up, ap = u["price"], a["price"]
            if up == ap:
                kind, penalty = "building+beds+price", 0.0
            else:
                diff = abs(up - ap) / max(up, ap)
                if diff > tolerance:
                    continue
                kind, penalty = "building+beds+near-price", diff
            candidates.append({
                "upstream": u, "aggregator": a,
                "matched_by": kind, "penalty": penalty,
            })

    # Exact-price pairs first, then closest. One-to-one: a building with six
    # identical studios must not contribute six copies of the same pairing.
    candidates.sort(key=lambda c: c["penalty"])
    used_up: set[str] = set()
    used_agg: set[str] = set()
    matches: list[dict] = []
    for c in candidates:
        ufp = c["upstream"]["fingerprint"]
        afp = c["aggregator"]["fingerprint"]
        if ufp in used_up or afp in used_agg:
            continue
        used_up.add(ufp)
        used_agg.add(afp)

        u_time, u_clock = _clock(c["upstream"])
        a_time, a_clock = _clock(c["aggregator"])
        basis = f"{u_clock}->{a_clock}"

        # Left censoring. If a listing was already there in the first crawl,
        # we do not know when it appeared -- only when we started looking. A
        # first_seen comparison on such a row measures the database's birthday,
        # not a lead, and on a cold start that is every row. Mark it so the
        # report refuses to average it in.
        if u_clock == "first_seen" and _censors(
                second_run, c["upstream"]["source"], c["upstream"]["first_seen"]):
            basis = "censored:present-at-first-crawl"

        matches.append({
            "upstream_fp": ufp,
            "aggregator_fp": afp,
            "aggregator": c["aggregator"]["source"],
            "matched_by": c["matched_by"],
            "upstream_time": u_time,
            "aggregator_time": a_time,
            "basis": basis,
            "address": c["upstream"]["address"],
            "unit": c["upstream"]["unit_key"],
            "price": c["upstream"]["price"],
        })
    return matches


def run(conn, upstream_sources=None, aggregator_sources=None,
        tolerance: float = NEAR_PRICE_TOLERANCE) -> dict:
    """Find matches and write them to the lead-time ledger."""
    matches = find_matches(conn, upstream_sources, aggregator_sources, tolerance)

    recorded = skipped = unusable = implausible = 0
    for m in matches:
        u_at, a_at = _parse(m["upstream_time"]), _parse(m["aggregator_time"])
        if u_at is None or a_at is None:
            unusable += 1
            continue
        lead_days = abs((a_at - u_at).total_seconds()) / 86400.0
        if lead_days > MAX_PLAUSIBLE_LEAD_DAYS:
            implausible += 1
            continue

        result = record_aggregator_hit(
            conn, m["upstream_fp"], m["aggregator"], m["aggregator_time"],
            basis=m["basis"], matched_by=m["matched_by"],
            matched_fp=m["aggregator_fp"], our_time=m["upstream_time"],
        )
        if result is None:
            unusable += 1          # upstream listing not in the ledger
            continue
        lead, wrote = result
        if wrote:
            recorded += 1
            m["lead_hours"] = round(lead, 1)
        else:
            skipped += 1

    return {
        "pairs_found": len(matches),
        "recorded": recorded,
        "already_measured": skipped,
        "unusable": unusable,
        "implausible": implausible,
        "exact_price": sum(1 for m in matches
                           if m["matched_by"] == "building+beds+price"),
        "matches": matches,
    }
