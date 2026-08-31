"""Core record types shared by every source adapter."""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class RawListing:
    """What a source adapter emits, before normalization or geocoding.

    Adapters do no cleanup beyond pulling fields out of their source format.
    All parsing, normalization and enrichment happens in one place downstream
    so that behavior is identical across sources and testable in isolation.
    """
    source: str                     # adapter id, e.g. "jsonld:stonehenge"
    source_url: str                 # page or endpoint the record came from
    address: str                    # street line as published
    unit_raw: str | None = None
    price_raw: object = None
    beds_raw: object = None
    baths_raw: object = None
    sqft_raw: object = None
    available_on: str | None = None
    title: str | None = None
    detail_url: str | None = None
    borough_hint: str | None = None
    no_fee: bool | None = None
    #: The publisher's own stable id for this listing, when it has one and no
    #: unit number. Keeps distinct listings in the same building distinct.
    #: Must be stable across crawls, or every crawl invents new listings.
    source_ref: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class Listing:
    """A normalized, geocoded unit. `fingerprint` is its identity."""
    fingerprint: str
    source: str
    source_url: str
    detail_url: str | None

    address: str                    # canonical label from GeoSearch when available
    address_raw: str
    bbl: str | None
    bin: str | None
    borough: str | None
    lat: float | None
    lon: float | None

    unit_key: str
    unit_raw: str | None
    price: int | None
    beds: float | None
    baths: float | None
    sqft: int | None
    available_on: str | None
    no_fee: bool | None

    first_seen: str
    last_seen: str
    confidence: str                 # "high" | "medium" | "low"
    extra: dict = field(default_factory=dict)

    def to_row(self) -> dict:
        d = asdict(self)
        d["extra"] = d["extra"] or {}
        return d
