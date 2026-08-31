"""Address -> BBL/BIN via NYC Planning's GeoSearch API.

BBL (Borough-Block-Lot) is the join key for the entire system. Every NYC
dataset worth having -- PLUTO, HPD registrations, DOB filings, ACRIS, the DHCR
rent-stabilized list -- keys on it. Resolve to BBL once at ingest and every
downstream enrichment becomes a cheap lookup.

GeoSearch is free, needs no API key, and is maintained by NYC Planning. We
cache aggressively anyway: an address resolves to the same BBL forever, so a
cache hit rate near 100% is the steady state and we should almost never hit
the network after the first crawl of a building.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .normalize import infer_borough, normalize_street

log = logging.getLogger(__name__)

GEOSEARCH_URL = "https://geosearch.planninglabs.nyc/v2/search"
USER_AGENT = "RentRadar/0.1 (NYC rental availability monitor)"

# GeoSearch is a public good with no published rate limit. Be a good citizen:
# one request every 100ms is far below anything that would burden it, and the
# cache means we rarely sustain even that.
_MIN_INTERVAL = 0.1
_last_call = 0.0


@dataclass(frozen=True)
class GeoResult:
    bbl: str | None
    bin: str | None
    label: str
    borough: str | None
    lat: float | None
    lon: float | None
    confidence: float

    @property
    def resolved(self) -> bool:
        return bool(self.bbl)


class Geocoder:
    """GeoSearch client with a durable SQLite cache."""

    def __init__(self, conn: sqlite3.Connection, min_confidence: float = 0.7):
        self.conn = conn
        self.min_confidence = min_confidence
        self._ensure_table()
        self.stats = {"hit": 0, "miss": 0, "fail": 0, "low_confidence": 0}

    def _ensure_table(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS geocode_cache (
                query       TEXT PRIMARY KEY,
                bbl         TEXT,
                bin         TEXT,
                label       TEXT,
                borough     TEXT,
                lat         REAL,
                lon         REAL,
                confidence  REAL,
                fetched_at  TEXT DEFAULT (datetime('now'))
            )
            """
        )
        self.conn.commit()

    # -- network ----------------------------------------------------------

    def _fetch(self, text: str) -> dict | None:
        global _last_call
        delta = time.monotonic() - _last_call
        if delta < _MIN_INTERVAL:
            time.sleep(_MIN_INTERVAL - delta)
        _last_call = time.monotonic()

        url = f"{GEOSEARCH_URL}?{urllib.parse.urlencode({'text': text, 'size': 1})}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # network, timeout, malformed JSON
            log.warning("geosearch failed for %r: %s", text, exc)
            return None

    @staticmethod
    def _parse(payload: dict) -> GeoResult | None:
        feats = payload.get("features") or []
        if not feats:
            return None
        f = feats[0]
        props = f.get("properties", {})
        pad = (props.get("addendum") or {}).get("pad") or {}
        coords = (f.get("geometry") or {}).get("coordinates") or [None, None]
        return GeoResult(
            bbl=pad.get("bbl"),
            bin=pad.get("bin"),
            label=props.get("label", ""),
            borough=props.get("borough"),
            lat=coords[1],
            lon=coords[0],
            confidence=float(props.get("confidence") or 0.0),
        )

    # -- public -----------------------------------------------------------

    def lookup(self, address: str, borough_hint: str | None = None) -> GeoResult | None:
        """Resolve a free-text NYC address to BBL/BIN. Cached forever on success."""
        if not address or not address.strip():
            return None

        # Sources hand us localities, not boroughs -- "Chelsea", "Long Island
        # City". Resolve those to a borough first; GeoSearch disambiguates far
        # better with "Queens" than with "Long Island City".
        borough = infer_borough(borough_hint or "") or infer_borough(address)
        suffix = borough or (borough_hint or "").strip() or ""

        # Normalizing before querying collapses "350 W 42nd St." and
        # "350 West 42 Street" onto one cache entry instead of two.
        base = normalize_street(address)
        query = f"{base} {suffix}".strip() if suffix else base
        if not query:
            return None

        row = self.conn.execute(
            "SELECT bbl, bin, label, borough, lat, lon, confidence "
            "FROM geocode_cache WHERE query = ?",
            (query,),
        ).fetchone()
        if row is not None:
            self.stats["hit"] += 1
            if row[0] is None and row[6] == -1.0:
                return None  # cached negative
            return GeoResult(row[0], row[1], row[2], row[3], row[4], row[5], row[6])

        self.stats["miss"] += 1
        payload = self._fetch(query)
        if payload is None:
            self.stats["fail"] += 1
            return None  # transient: do NOT cache, so we retry next run

        result = self._parse(payload)
        if result is None or result.confidence < self.min_confidence:
            if result is not None:
                self.stats["low_confidence"] += 1
            # Cache the negative so a permanently-bad address does not get
            # re-queried on every single crawl.
            self.conn.execute(
                "INSERT OR REPLACE INTO geocode_cache "
                "(query, bbl, confidence) VALUES (?, NULL, -1.0)",
                (query,),
            )
            self.conn.commit()
            return None

        self.conn.execute(
            "INSERT OR REPLACE INTO geocode_cache "
            "(query, bbl, bin, label, borough, lat, lon, confidence) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (query, result.bbl, result.bin, result.label, result.borough,
             result.lat, result.lon, result.confidence),
        )
        self.conn.commit()
        return result
