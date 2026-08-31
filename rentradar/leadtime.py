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
from datetime import datetime


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def record_aggregator_hit(conn, fingerprint: str, aggregator: str,
                          seen_at: str) -> float | None:
    """Log when an aggregator first showed a unit we already knew about.

    Returns lead time in hours, or None if we never saw the unit ourselves.
    Only the first hit per unit is kept -- a later re-listing must not
    overwrite the original measurement.
    """
    row = conn.execute(
        "SELECT our_first_seen, aggregator_seen, lead_hours "
        "FROM lead_time WHERE fingerprint=?",
        (fingerprint,),
    ).fetchone()
    if row is None:
        return None
    if row["aggregator_seen"] is not None:
        return row["lead_hours"]

    lead = (_parse(seen_at) - _parse(row["our_first_seen"])).total_seconds() / 3600.0
    conn.execute(
        "UPDATE lead_time SET aggregator=?, aggregator_seen=?, lead_hours=? "
        "WHERE fingerprint=?",
        (aggregator, seen_at, lead, fingerprint),
    )
    conn.commit()
    return lead


def report(conn) -> dict:
    rows = conn.execute(
        "SELECT lead_hours FROM lead_time WHERE lead_hours IS NOT NULL"
    ).fetchall()
    leads = [r["lead_hours"] for r in rows]
    pending = conn.execute(
        "SELECT COUNT(*) FROM lead_time WHERE aggregator_seen IS NULL"
    ).fetchone()[0]

    if not leads:
        return {"measured": 0, "pending": pending,
                "note": "no aggregator timestamps recorded yet"}

    leads.sort()
    return {
        "measured": len(leads),
        "pending": pending,
        "median_hours": round(statistics.median(leads), 1),
        "mean_hours": round(statistics.fmean(leads), 1),
        "p10_hours": round(leads[max(0, int(len(leads) * 0.10) - 1)], 1),
        "p90_hours": round(leads[min(len(leads) - 1, int(len(leads) * 0.90))], 1),
        "share_ahead": round(sum(1 for x in leads if x > 0) / len(leads), 3),
        "share_ahead_24h": round(sum(1 for x in leads if x >= 24) / len(leads), 3),
        "share_ahead_72h": round(sum(1 for x in leads if x >= 72) / len(leads), 3),
    }
