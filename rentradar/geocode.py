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
import re
import urllib.parse
import urllib.request
from dataclasses import dataclass

from .normalize import infer_borough, normalize_street

log = logging.getLogger(__name__)

GEOSEARCH_URL = "https://geosearch.planninglabs.nyc/v2/search"
#: Independent fallback. HPD's Multiple Dwelling Registrations carry
#: housenumber / streetname / boro -> bin + block + lot for every building
#: with three or more units, which is most of the inventory this system
#: cares about. Coverage is partial (condos and co-ops are often absent), but
#: it runs on Socrata rather than on GeoSearch, so it survives the outages
#: that take the primary down -- and GeoSearch went down twice in one day
#: during this project, blocking BBL resolution and therefore every
#: downstream join and every lead-time measurement.
HPD_URL = "https://data.cityofnewyork.us/resource/tesw-yqqr.json"

BORO_ID = {"Manhattan": "1", "Bronx": "2", "Brooklyn": "3",
           "Queens": "4", "Staten Island": "5"}

#: HPD spells streets out in full and drops ordinal suffixes:
#: "410 E 20th St" is stored as housenumber 410, streetname "EAST 20 STREET".
_HPD_EXPAND = {
    "st": "STREET", "ave": "AVENUE", "av": "AVENUE", "blvd": "BOULEVARD",
    "pl": "PLACE", "rd": "ROAD", "dr": "DRIVE", "ct": "COURT",
    "ln": "LANE", "pkwy": "PARKWAY", "ter": "TERRACE", "plz": "PLAZA",
    "sq": "SQUARE", "n": "NORTH", "s": "SOUTH", "e": "EAST", "w": "WEST",
}
USER_AGENT = "RentRadar/0.1 (NYC rental availability monitor)"

# GeoSearch is a public good with no published rate limit. Be a good citizen:
# one request every 100ms is far below anything that would burden it, and the
# cache means we rarely sustain even that.
_MIN_INTERVAL = 0.1
_last_call = 0.0


class TransientGeocodeError(RuntimeError):
    """The provider was unreachable. Distinct from 'no such address' -- a
    transient failure must never be cached as a negative, or one outage
    poisons the cache for every address seen during it."""


@dataclass(frozen=True)
class GeoResult:
    bbl: str | None
    bin: str | None
    label: str
    borough: str | None
    lat: float | None
    lon: float | None
    confidence: float
    provider: str = "geosearch"

    @property
    def resolved(self) -> bool:
        return bool(self.bbl)


def hpd_street_form(street_remainder: str) -> str:
    """Render a street name the way HPD stores it."""
    out = []
    for tok in normalize_street(street_remainder).split():
        out.append(_HPD_EXPAND.get(tok, tok).upper())
    return " ".join(out)


def _split_address(address: str) -> tuple[str, str]:
    m = re.match(r"\s*(\d[\w-]*)\s+(.*)$", address or "")
    return (m.group(1), m.group(2)) if m else ("", address or "")


def hpd_lookup(address: str, borough: str | None, timeout: int = 20
               ) -> GeoResult | None:
    """Resolve via HPD registrations. Raises TransientGeocodeError if down."""
    house, street = _split_address(address)
    boro_id = BORO_ID.get(borough or "")
    if not (house and street and boro_id):
        return None
    params = {"$limit": 5, "boroid": boro_id,
              "streetname": hpd_street_form(street),
              "lowhousenumber": house}
    url = f"{HPD_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            rows = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise TransientGeocodeError(f"hpd: {exc}") from exc
    if not rows:
        return None
    r = rows[0]
    block, lot = r.get("block"), r.get("lot")
    bbl = (f"{boro_id}{int(block):05d}{int(lot):04d}"
           if block and lot and str(block).isdigit() and str(lot).isdigit()
           else None)
    return GeoResult(
        bbl=bbl, bin=r.get("bin"),
        label=f"{r.get('housenumber','')} {r.get('streetname','')}, "
              f"{(borough or '').upper()}".strip(),
        borough=borough,
        lat=None, lon=None,
        # Slightly below a GeoSearch hit: this is an exact-string table match
        # with no fuzzy handling, so it is right or it is absent.
        confidence=0.9,
        provider="hpd",
    )


class Geocoder:
    """GeoSearch client with a durable SQLite cache."""

    def __init__(self, conn: sqlite3.Connection, min_confidence: float = 0.7):
        self.conn = conn
        self.min_confidence = min_confidence
        self._ensure_table()
        self.stats = {"hit": 0, "miss": 0, "fail": 0, "low_confidence": 0,
                      "fallback": 0}

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
        result = self._parse(payload) if payload is not None else None

        if payload is None or result is None:
            # Primary gave nothing. Try the independent provider before
            # concluding the address does not exist -- during a GeoSearch
            # outage that conclusion would be wrong for every address.
            try:
                alt = hpd_lookup(address, borough)
            except TransientGeocodeError as exc:
                log.warning("fallback geocoder also unavailable: %s", exc)
                alt = None
            if alt is not None and alt.bbl:
                self.stats["fallback"] = self.stats.get("fallback", 0) + 1
                self.conn.execute(
                    "INSERT OR REPLACE INTO geocode_cache "
                    "(query, bbl, bin, label, borough, lat, lon, confidence) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (query, alt.bbl, alt.bin, alt.label, alt.borough,
                     alt.lat, alt.lon, alt.confidence),
                )
                self.conn.commit()
                return alt

        if payload is None:
            self.stats["fail"] += 1
            return None  # transient: do NOT cache, so we retry next run
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
