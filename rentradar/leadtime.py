"""Measure the only metric that decides whether this is a business.

The claim is "we find apartments before StreetEasy". That claim is either
true by some number of days or it is marketing. This module records, for each
unit we found upstream, when it later surfaced on a public aggregator, and
reports the distribution.

Important: we do not crawl StreetEasy. Verification runs off whatever public
aggregator signal you can lawfully obtain -- a partner feed, a manual spot
check, or a syndication timestamp the landlord shares. `record_aggregator_hit`
is the single entry point, so the source of that timestamp is a business
decision, not a code change.

Build this on day one. If the measured median lead is four hours you have a
feature; if it is six days you have a company, and you will want the receipts
when you say so to an investor.
"""
from __future__ import annotations

import statistics
from datetime import datetime, timezone


def _parse(ts: str) -> datetime:
    """Always returns an aware datetime; naive input is read as UTC."""
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def record_aggregator_hit(conn, fingerprint: str, aggregator: str,
                          seen_at: str, basis: str = "first_seen->first_seen",
                          matched_by: str | None = None,
                          matched_fp: str | None = None,
                          our_time: str | None = None) -> tuple[float, bool] | None:
    """Log when an aggregator first showed a unit we already knew about.

    Returns (lead_hours, wrote) or None if we never saw the unit ourselves.
    `wrote` is False when a measurement already existed: only the first hit
    per unit is kept, because a later re-listing must not overwrite the
    original result -- that is how a lead-time figure quietly drifts toward
    whatever you last observed.

    `our_time` overrides the stored first_seen when the upstream source
    published its own listing date; `basis` records which clocks were
    compared so the number stays interpretable.
    """
    row = conn.execute(
        "SELECT our_first_seen, aggregator_seen, lead_hours "
        "FROM lead_time WHERE fingerprint=?",
        (fingerprint,),
    ).fetchone()
    if row is None:
        return None
    if row["aggregator_seen"] is not None:
        return row["lead_hours"], False

    ours = our_time or row["our_first_seen"]
    lead = (_parse(seen_at) - _parse(ours)).total_seconds() / 3600.0
    conn.execute(
        "UPDATE lead_time SET aggregator=?, aggregator_seen=?, lead_hours=?, "
        "basis=?, matched_by=?, matched_fp=?, our_first_seen=? "
        "WHERE fingerprint=?",
        (aggregator, seen_at, lead, basis, matched_by, matched_fp, ours,
         fingerprint),
    )
    conn.commit()
    return lead, True


def is_comparable(basis: str | None) -> bool:
    """True when both sides of a measurement used the SAME kind of clock.

    A mixed comparison is not a weak measurement, it is a broken one. On a
    cold database our first_seen is "now" for everything, so comparing it
    against an aggregator's published posting date from four days ago
    guarantees a negative lead for every row -- the system would confidently
    report that it is losing, when all it has measured is its own start time.
    Same-clock pairs only: listed_at vs listed_at is a real answer, and
    first_seen vs first_seen becomes one once the crawler has been running
    long enough to have observed both sides itself.
    """
    if not basis or basis.startswith("censored:"):
        return False
    if "->" not in basis:
        return False
    left, right = basis.split("->", 1)
    return left == right


def report(conn) -> dict:
    rows = conn.execute(
        "SELECT lead_hours, basis FROM lead_time WHERE lead_hours IS NOT NULL"
    ).fetchall()
    excluded = [r for r in rows if not is_comparable(r["basis"])]
    rows = [r for r in rows if is_comparable(r["basis"])]
    leads = [r["lead_hours"] for r in rows]
    pending = conn.execute(
        "SELECT COUNT(*) FROM lead_time WHERE aggregator_seen IS NULL"
    ).fetchone()[0]

    if not leads:
        note = "no aggregator timestamps recorded yet"
        if excluded:
            note = (f"{len(excluded)} pairs found, none of them usable yet -- "
                    "either the two sides used different clocks, or the "
                    "listing was already on the market when this database "
                    "started watching. Both resolve themselves as the crawler "
                    "accumulates history; neither is fixable in code.")
        return {"measured": 0, "pending": pending,
                "excluded_mixed_basis": len(excluded), "note": note}

    bases: dict[str, int] = {}
    for r in rows:
        bases[r["basis"]] = bases.get(r["basis"], 0) + 1

    leads.sort()
    return {
        "measured": len(leads),
        "pending": pending,
        # Pairs matched but thrown out because the two sides used different
        # clocks. Reported, never silently dropped.
        "excluded_mixed_basis": len(excluded),
        "median_hours": round(statistics.median(leads), 1),
        "mean_hours": round(statistics.fmean(leads), 1),
        "p10_hours": round(leads[max(0, int(len(leads) * 0.10) - 1)], 1),
        "p90_hours": round(leads[min(len(leads) - 1, int(len(leads) * 0.90))], 1),
        "share_ahead": round(sum(1 for x in leads if x > 0) / len(leads), 3),
        "share_ahead_24h": round(sum(1 for x in leads if x >= 24) / len(leads), 3),
        "share_ahead_72h": round(sum(1 for x in leads if x >= 72) / len(leads), 3),
        # Never report a lead time without saying which clocks produced it.
        "basis": bases,
        "vs": dict(conn.execute(
            "SELECT aggregator, COUNT(*) FROM lead_time "
            "WHERE lead_hours IS NOT NULL GROUP BY aggregator").fetchall()),
        "caveat": ("small sample" if len(leads) < 30 else None),
    }
