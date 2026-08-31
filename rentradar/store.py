"""Persistence and first-seen detection.

The product is "we saw it first", so the only number that matters is a
trustworthy `first_seen`. Two rules protect it:

  1. A listing's identity (`fingerprint`) never includes price or any other
     mutable field, so a rent drop is an update, not a new listing.
  2. `first_seen` is written exactly once, on insert, and never updated.

SQLite is correct for the MVP: single writer, no ops burden, and it will hold
several million listing-events without complaint. The schema is deliberately
Postgres-compatible so the migration is mechanical once you need concurrent
writers.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict

from .models import Listing, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    fingerprint   TEXT PRIMARY KEY,
    source        TEXT NOT NULL,
    source_url    TEXT,
    detail_url    TEXT,
    address       TEXT,
    address_raw   TEXT,
    bbl           TEXT,
    bin           TEXT,
    borough       TEXT,
    lat           REAL,
    lon           REAL,
    unit_key      TEXT,
    unit_raw      TEXT,
    price         INTEGER,
    beds          REAL,
    baths         REAL,
    sqft          INTEGER,
    available_on  TEXT,
    no_fee        INTEGER,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    gone_at       TEXT,
    confidence    TEXT,
    extra         TEXT
);
CREATE INDEX IF NOT EXISTS idx_listings_bbl      ON listings(bbl);
CREATE INDEX IF NOT EXISTS idx_listings_first    ON listings(first_seen);
CREATE INDEX IF NOT EXISTS idx_listings_active   ON listings(gone_at) WHERE gone_at IS NULL;

-- Full history of observed field changes, so price cuts are queryable and we
-- can prove when a unit's terms moved.
CREATE TABLE IF NOT EXISTS listing_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint  TEXT NOT NULL,
    observed_at  TEXT NOT NULL,
    event        TEXT NOT NULL,          -- new | price_change | relisted | gone
    old_price    INTEGER,
    new_price    INTEGER,
    payload      TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_fp ON listing_events(fingerprint);

-- Lead-time ledger: when we first saw a unit vs. when it surfaced on a
-- public aggregator. This table is the company's entire thesis, measured.
CREATE TABLE IF NOT EXISTS lead_time (
    fingerprint      TEXT PRIMARY KEY,
    our_first_seen   TEXT NOT NULL,
    aggregator       TEXT,
    aggregator_seen  TEXT,
    lead_hours       REAL
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    ok           INTEGER,
    n_raw        INTEGER DEFAULT 0,
    n_new        INTEGER DEFAULT 0,
    n_updated    INTEGER DEFAULT 0,
    error        TEXT
);
"""


def connect(path: str = "rentradar.db") -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


class Store:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # -- crawl bookkeeping -------------------------------------------------

    def start_run(self, source: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO crawl_runs (source, started_at) VALUES (?, ?)",
            (source, utcnow()),
        )
        self.conn.commit()
        return cur.lastrowid

    def finish_run(self, run_id: int, ok: bool, n_raw=0, n_new=0, n_updated=0,
                   error: str | None = None) -> None:
        self.conn.execute(
            "UPDATE crawl_runs SET finished_at=?, ok=?, n_raw=?, n_new=?, "
            "n_updated=?, error=? WHERE id=?",
            (utcnow(), int(ok), n_raw, n_new, n_updated, error, run_id),
        )
        self.conn.commit()

    # -- the diff engine ---------------------------------------------------

    def upsert(self, listing: Listing) -> str:
        """Insert or update one listing. Returns 'new', 'relisted', 'price_change'
        or 'seen'. `first_seen` is never overwritten."""
        now = listing.last_seen
        row = self.conn.execute(
            "SELECT price, gone_at FROM listings WHERE fingerprint = ?",
            (listing.fingerprint,),
        ).fetchone()

        if row is None:
            d = asdict(listing)
            d["extra"] = json.dumps(d.get("extra") or {})
            d["no_fee"] = None if d["no_fee"] is None else int(d["no_fee"])
            cols = ", ".join(d.keys())
            marks = ", ".join("?" for _ in d)
            self.conn.execute(
                f"INSERT INTO listings ({cols}) VALUES ({marks})", tuple(d.values())
            )
            self._event(listing.fingerprint, "new", None, listing.price)
            self.conn.execute(
                "INSERT OR IGNORE INTO lead_time (fingerprint, our_first_seen) "
                "VALUES (?, ?)",
                (listing.fingerprint, listing.first_seen),
            )
            self.conn.commit()
            return "new"

        old_price, gone_at = row["price"], row["gone_at"]
        verdict = "seen"

        if gone_at is not None:
            # The unit came back. That is a real signal (failed lease, broken
            # lease) and worth alerting on, but it is NOT a new first_seen.
            self.conn.execute(
                "UPDATE listings SET gone_at=NULL WHERE fingerprint=?",
                (listing.fingerprint,),
            )
            self._event(listing.fingerprint, "relisted", old_price, listing.price)
            verdict = "relisted"

        if listing.price is not None and old_price is not None \
                and listing.price != old_price:
            self._event(listing.fingerprint, "price_change", old_price, listing.price)
            verdict = "price_change" if verdict == "seen" else verdict

        self.conn.execute(
            "UPDATE listings SET last_seen=?, price=COALESCE(?, price), "
            "available_on=COALESCE(?, available_on), detail_url=COALESCE(?, detail_url) "
            "WHERE fingerprint=?",
            (now, listing.price, listing.available_on, listing.detail_url,
             listing.fingerprint),
        )
        self.conn.commit()
        return verdict

    def _event(self, fp: str, event: str, old: int | None, new: int | None,
               payload: dict | None = None) -> None:
        self.conn.execute(
            "INSERT INTO listing_events "
            "(fingerprint, observed_at, event, old_price, new_price, payload) "
            "VALUES (?,?,?,?,?,?)",
            (fp, utcnow(), event, old, new, json.dumps(payload) if payload else None),
        )

    def mark_gone(self, source: str, seen_fingerprints: set[str]) -> int:
        """Anything this source carried last run but not this run is off-market.

        Scoped per source so one adapter breaking cannot wipe another's
        inventory. A source that returns zero rows is treated as a failure by
        the caller and never reaches this method.
        """
        rows = self.conn.execute(
            "SELECT fingerprint FROM listings WHERE source=? AND gone_at IS NULL",
            (source,),
        ).fetchall()
        gone = [r["fingerprint"] for r in rows if r["fingerprint"] not in seen_fingerprints]
        for fp in gone:
            self.conn.execute(
                "UPDATE listings SET gone_at=? WHERE fingerprint=?", (utcnow(), fp)
            )
            self._event(fp, "gone", None, None)
        self.conn.commit()
        return len(gone)

    # -- queries -----------------------------------------------------------

    def active(self, limit: int = 100, max_price: int | None = None,
               min_beds: float | None = None, borough: str | None = None):
        sql = "SELECT * FROM listings WHERE gone_at IS NULL"
        args: list = []
        if max_price is not None:
            sql += " AND price IS NOT NULL AND price <= ?"
            args.append(max_price)
        if min_beds is not None:
            sql += " AND beds IS NOT NULL AND beds >= ?"
            args.append(min_beds)
        if borough:
            sql += " AND borough = ?"
            args.append(borough)
        sql += " ORDER BY first_seen DESC LIMIT ?"
        args.append(limit)
        return self.conn.execute(sql, args).fetchall()

    def new_since(self, iso_ts: str):
        return self.conn.execute(
            "SELECT * FROM listings WHERE first_seen > ? AND gone_at IS NULL "
            "ORDER BY first_seen DESC",
            (iso_ts,),
        ).fetchall()

    def summary(self) -> dict:
        q = lambda s: self.conn.execute(s).fetchone()[0]
        return {
            "listings_total": q("SELECT COUNT(*) FROM listings"),
            "listings_active": q("SELECT COUNT(*) FROM listings WHERE gone_at IS NULL"),
            "with_bbl": q("SELECT COUNT(*) FROM listings WHERE bbl IS NOT NULL"),
            "with_unit": q("SELECT COUNT(*) FROM listings WHERE unit_key != ''"),
            "high_confidence": q(
                "SELECT COUNT(*) FROM listings WHERE confidence='high'"),
            "events": q("SELECT COUNT(*) FROM listing_events"),
            "distinct_buildings": q(
                "SELECT COUNT(DISTINCT bbl) FROM listings WHERE bbl IS NOT NULL"),
        }
