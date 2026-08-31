"""RawListing -> Listing -> store. One code path for every source."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .geocode import Geocoder
from .models import Listing, RawListing, utcnow
from .normalize import (
    extract_unit_from_name, listing_fingerprint, normalize_unit,
    parse_beds, parse_price, to_utc_iso,
)
from .sources import Source, SourceError
from .store import Store

log = logging.getLogger(__name__)


@dataclass
class RunResult:
    source: str
    ok: bool
    n_raw: int = 0
    n_new: int = 0
    n_updated: int = 0
    n_gone: int = 0
    n_unresolved: int = 0
    error: str | None = None


def _sqft(raw) -> int | None:
    if raw is None:
        return None
    import re
    if isinstance(raw, (int, float)):
        v = int(raw)
    else:
        m = re.search(r"([\d,]{3,6})\s*(?:sq\.?\s*ft|sf|square feet)", str(raw), re.I)
        if not m:
            m = re.fullmatch(r"\s*([\d,]{3,6})\s*", str(raw))
        if not m:
            return None
        v = int(m.group(1).replace(",", ""))
    return v if 80 <= v <= 20_000 else None


def normalize(raw: RawListing, geo: Geocoder) -> Listing | None:
    """Normalize, geocode and fingerprint one raw record.

    Returns None only when the record has no usable address at all. Records
    that fail to geocode are still kept -- they are alertable, just flagged
    low-confidence, because a listing you can't join to PLUTO is still an
    apartment somebody wants to see.
    """
    address = (raw.address or "").strip()
    if not address:
        return None

    hit = geo.lookup(address, raw.borough_hint)

    unit_key = (normalize_unit(raw.unit_raw)
                or extract_unit_from_name(raw.title or "", address))
    beds = parse_beds(raw.beds_raw if raw.beds_raw is not None else raw.title)
    if beds is None:
        beds = parse_beds((raw.extra or {}).get("description"))
    price = parse_price(raw.price_raw)

    fp = listing_fingerprint(hit.bbl if hit else None, unit_key, address, beds,
                             source=raw.source, source_ref=raw.source_ref,
                             bin=hit.bin if hit else None)

    if hit and unit_key:
        confidence = "high"
    elif hit:
        confidence = "medium"          # building resolved, unit unknown
    else:
        confidence = "low"             # address never resolved

    now = utcnow()
    extra = dict(raw.extra or {})
    if hit:
        extra["geocode_label"] = hit.label
        extra["geocode_confidence"] = hit.confidence

    return Listing(
        fingerprint=fp,
        source=raw.source,
        source_url=raw.source_url,
        detail_url=raw.detail_url,
        address=(hit.label if hit else address),
        address_raw=address,
        bbl=hit.bbl if hit else None,
        bin=hit.bin if hit else None,
        borough=(hit.borough if hit else raw.borough_hint),
        lat=hit.lat if hit else _f(extra.get("lat")),
        lon=hit.lon if hit else _f(extra.get("lon")),
        unit_key=unit_key,
        unit_raw=raw.unit_raw,
        price=price,
        beds=beds,
        baths=parse_beds(raw.baths_raw) if raw.baths_raw is not None else None,
        sqft=_sqft(raw.sqft_raw),
        available_on=raw.available_on,
        listed_at=to_utc_iso(raw.listed_at),
        no_fee=raw.no_fee,
        first_seen=now,
        last_seen=now,
        confidence=confidence,
        extra=extra,
    )


def _f(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def run_source(source: Source, store: Store, geo: Geocoder,
               mark_missing_gone: bool = True) -> RunResult:
    """Crawl one source and fold its results into the store."""
    run_id = store.start_run(source.id)
    try:
        raws = source.fetch()
    except SourceError as exc:
        log.error("%s failed: %s", source.id, exc)
        store.finish_run(run_id, ok=False, error=str(exc))
        return RunResult(source.id, ok=False, error=str(exc))
    except Exception as exc:  # adapter bug -- never take the whole crawl down
        log.exception("%s crashed", source.id)
        store.finish_run(run_id, ok=False, error=f"{type(exc).__name__}: {exc}")
        return RunResult(source.id, ok=False, error=str(exc))

    res = RunResult(source.id, ok=True, n_raw=len(raws))

    # A source that suddenly returns almost nothing has broken, not sold out.
    # Refusing to mark inventory gone here is what keeps a CSS change from
    # deleting a landlord's entire portfolio from the database.
    suspicious = len(raws) < source.min_expected

    seen: set[str] = set()
    for raw in raws:
        listing = normalize(raw, geo)
        if listing is None:
            continue
        if listing.confidence == "low":
            res.n_unresolved += 1
        seen.add(listing.fingerprint)
        verdict = store.upsert(listing)
        if verdict == "new":
            res.n_new += 1
        elif verdict in ("price_change", "relisted"):
            res.n_updated += 1

    if mark_missing_gone and not suspicious:
        res.n_gone = store.mark_gone(source.id, seen)
    elif suspicious:
        log.warning(
            "%s returned %d rows (min_expected=%d) -- skipping off-market sweep",
            source.id, len(raws), source.min_expected,
        )
        res.ok = False
        res.error = f"only {len(raws)} rows returned"

    store.finish_run(run_id, ok=res.ok, n_raw=res.n_raw, n_new=res.n_new,
                     n_updated=res.n_updated, error=res.error)
    return res
