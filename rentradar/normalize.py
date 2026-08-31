"""Address + unit normalization and stable listing fingerprints.

Entity resolution is the hard part of this system: the same apartment shows up
as "350 W 42nd St #12B", "350 West 42 Street, Apt 12-B" and "Unit 12B, 350 W
42nd" across three sources. We normalize aggressively, then join on
(bbl, unit_key) which is the only pair stable enough to dedupe on.
"""
from __future__ import annotations

import hashlib
import re
import unicodedata

# --- street-type and directional canonicalization -------------------------

_STREET_TYPES = {
    "street": "st", "st.": "st", "str": "st",
    "avenue": "ave", "av": "ave", "ave.": "ave",
    "boulevard": "blvd", "blvd.": "blvd",
    "place": "pl", "pl.": "pl",
    "road": "rd", "rd.": "rd",
    "drive": "dr", "dr.": "dr",
    "court": "ct", "ct.": "ct",
    "terrace": "ter", "parkway": "pkwy", "plaza": "plz",
    "lane": "ln", "square": "sq", "turnpike": "tpke",
    "highway": "hwy", "circle": "cir", "expressway": "expy",
}

_DIRECTIONS = {
    "north": "n", "south": "s", "east": "e", "west": "w",
    "northeast": "ne", "northwest": "nw",
    "southeast": "se", "southwest": "sw",
}

_ORDINAL = re.compile(r"\b(\d+)(st|nd|rd|th)\b", re.I)

_UNIT_PREFIXES = re.compile(
    r"\b(apartment|apt|unit|suite|ste|residence|res|no|number|#)\b\.?\s*", re.I
)

BOROUGH_CODES = {
    "manhattan": 1, "new york": 1, "ny": 1,
    "bronx": 2, "the bronx": 2,
    "brooklyn": 3, "kings": 3,
    "queens": 4,
    "staten island": 5, "richmond": 5,
}

# Neighborhoods that geocoders resolve badly without their borough.
_NEIGHBORHOOD_BOROUGH = {
    "long island city": "Queens", "astoria": "Queens", "sunnyside": "Queens",
    "forest hills": "Queens", "flushing": "Queens", "jackson heights": "Queens",
    "ridgewood": "Queens", "rego park": "Queens", "elmhurst": "Queens",
    "williamsburg": "Brooklyn", "bushwick": "Brooklyn", "park slope": "Brooklyn",
    "bed-stuy": "Brooklyn", "bedford-stuyvesant": "Brooklyn", "dumbo": "Brooklyn",
    "greenpoint": "Brooklyn", "crown heights": "Brooklyn", "fort greene": "Brooklyn",
    "prospect heights": "Brooklyn", "bay ridge": "Brooklyn", "sunset park": "Brooklyn",
    "harlem": "Manhattan", "chelsea": "Manhattan", "soho": "Manhattan",
    "tribeca": "Manhattan", "upper east side": "Manhattan",
    "upper west side": "Manhattan", "east village": "Manhattan",
    "west village": "Manhattan", "murray hill": "Manhattan",
    "hell's kitchen": "Manhattan", "financial district": "Manhattan",
    "mott haven": "Bronx", "riverdale": "Bronx", "fordham": "Bronx",
    "st. george": "Staten Island",
}


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c)
    )


def normalize_street(raw: str) -> str:
    """Canonicalize a street address line for geocoder submission and matching."""
    if not raw:
        return ""
    s = _strip_accents(raw).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[.,]", " ", s)
    s = re.sub(r"[^\w\s\-]", " ", s)
    s = _ORDINAL.sub(r"\1", s)  # 42nd -> 42
    tokens = []
    for tok in s.split():
        tok = _DIRECTIONS.get(tok, tok)
        tok = _STREET_TYPES.get(tok, tok)
        tokens.append(tok)
    return " ".join(tokens).strip()


def infer_borough(text: str) -> str | None:
    """Best-effort borough from a free-text address or neighborhood name."""
    if not text:
        return None
    low = _strip_accents(text).lower()
    for name in ("staten island", "brooklyn", "queens", "bronx", "manhattan"):
        if name in low:
            return name.title() if name != "bronx" else "Bronx"
    for hood, boro in _NEIGHBORHOOD_BOROUGH.items():
        if hood in low:
            return boro
    return None


def normalize_unit(raw: str | None) -> str:
    """Reduce a unit designator to a comparable key.

    '#12-B' / 'Apt 12B' / 'Unit 12 B' all collapse to '12B'.
    Returns '' when there is no usable unit, which callers must treat as
    "building-level listing", never as a match to a real unit.
    """
    if not raw:
        return ""
    s = _strip_accents(str(raw)).upper()
    s = _UNIT_PREFIXES.sub("", s)
    s = re.sub(r"[^A-Z0-9]", "", s)
    # Drop a leading zero-pad that only some sources apply: 0012B -> 12B
    s = re.sub(r"^0+(?=\d)", "", s)
    return s


_DESCRIPTOR = re.compile(
    r"\b(\d+(\.\d)?[\s-]*)?(studio|bed\s?rooms?|bedrooms?|beds?|baths?|bathrooms?|"
    r"br|ba|bd|den|loft|convertible|duplex|penthouse|sq\.?\s?ft)\b\.?",
    re.I,
)

#: A house number plus a street name. Must be removed before hunting for a
#: unit, or "1-Bedroom at 70 Pine Street" yields unit "70" -- the house
#: number -- which then reads as a real unit, falsely reports high
#: confidence, and collapses every listing in the building onto one
#: fingerprint. Aggregators publish titles in exactly this shape.
_ADDRESS_LIKE = re.compile(
    r"\b\d{1,4}(?:-\d{1,3})?\s+"
    r"(?:[NSEW]\.?\s+|(?:north|south|east|west)\s+)?"
    r"(?:[A-Za-z0-9'.]+\s+){0,3}?"
    r"(?:street|st|avenue|ave|road|rd|place|pl|boulevard|blvd|drive|dr|"
    r"lane|ln|court|ct|terrace|ter|parkway|pkwy|plaza|plz|square|sq|"
    r"way|broadway|concourse|circle|cir)\b\.?",
    re.I,
)


_TS_FORMATS = ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d",
               "%m/%d/%Y", "%m/%d/%Y %H:%M:%S")


def to_utc_iso(value) -> str | None:
    """Coerce any publisher timestamp to a timezone-aware UTC ISO string.

    Sources are inconsistent: TF Cornerstone emits "2026-08-27 22:07:40" with
    no zone, Socrata emits ISO with a T, NYBits is computed and already aware.
    Mixing naive and aware values makes arithmetic raise, which is how the
    first live lead-time run died. Naive input is assumed to be UTC -- a few
    hours of error on a measurement quoted in days, and far better than
    discarding the date.
    """
    if value in (None, ""):
        return None
    from datetime import datetime, timezone
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        dt = None
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            for fmt in _TS_FORMATS:
                try:
                    dt = datetime.strptime(text, fmt)
                    break
                except ValueError:
                    continue
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def house_number(address: str) -> str:
    """Leading house number of an address line, '' if there isn't one."""
    m = re.match(r"\s*(\d{1,4}(?:-\d{1,3})?)\b", address or "")
    return m.group(1) if m else ""


def extract_unit_from_name(name: str, address_hint: str = "") -> str:
    """Pull a unit out of marketing strings like '1 QPS 004B - Studio'.

    The trap here is the trailing descriptor: naively taking the last
    digit-bearing token turns "101 West 15th Street 227 - 1 Bedroom" into unit
    "1", which then collides with every other one-bedroom in the building and
    silently destroys the fingerprint. So we cut the descriptor tail off first.
    """
    if not name:
        return ""

    # An explicit marker always wins.
    m = re.search(r"(?:#|\bapt\b\.?|\bunit\b|\bresidence\b)\s*([A-Za-z0-9\-]+)",
                  name, re.I)
    if m:
        return normalize_unit(m.group(1))

    # Drop the marketing tail: everything after the last " - " separator, then
    # any remaining bed/bath/studio words, then the street address itself.
    head = re.split(r"\s+[-–—]\s+", name)[0]
    head = _DESCRIPTOR.sub(" ", head)
    head = _ADDRESS_LIKE.sub(" ", head)

    # Veto the building's own house number. The street-suffix strip above
    # cannot catch addresses written without one -- "Studio at Gateway: 365
    # South End" -- and those were producing unit "365" for every studio in
    # the building. Comparing against the address the source actually gave us
    # is exact where suffix-guessing is not. A real unit that happens to equal
    # its house number is rare; losing it costs one confidence level, whereas
    # accepting a false one corrupts identity for the whole building.
    hn = normalize_unit(house_number(address_hint))

    strong: list[str] = []
    weak: list[str] = []
    for m in re.finditer(r"\b([A-Za-z]?\d{1,4}[A-Za-z]?)\b", head):
        tok = m.group(1)
        if not any(ch.isdigit() for ch in tok):
            continue
        if hn and normalize_unit(tok) == hn:
            continue
        # A letter suffix (4B) or zero-padding (004) marks a real designator.
        looks_like_unit = bool(re.search(r"[A-Za-z]$", tok)) or tok.startswith("0")
        # A number followed by a capitalised word is a house number in an
        # address phrase, not a unit: "Manhattan East: 227 East 66th", or a
        # sibling building in a complex, "Stuyvesant Town: 645 East 14th".
        # These differ from the listing's own house number, so the veto above
        # cannot see them.
        rest = head[m.end():].lstrip(" :,-–—")
        if re.match(r"[A-Z][a-z]", rest) and not looks_like_unit:
            continue
        (strong if looks_like_unit else weak).append(tok)

    if strong:
        return normalize_unit(strong[-1])
    if weak:
        return normalize_unit(weak[-1])
    return ""


#: "1 Bedroom", "2 Bed", "3BR", "2 bd" -- note the optional "room"/plural,
#: whose absence was quietly returning None for every 1-bedroom.
_BEDS_RE = re.compile(r"(\d+(?:\.\d)?)\s*[-\s]?(?:bed\s?rooms?|beds?|bd|br)\b", re.I)


def parse_beds(text: str | None) -> float | None:
    """Parse a bedroom count. Studio -> 0.0, '1 Bedroom' -> 1.0, '1BR+Den' -> 1.5."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text) if 0 <= float(text) <= 12 else None
    t = str(text)
    if re.search(r"\bstudios?\b|\bstu\b|\bjr\.?\s*1\b", t, re.I):
        return 0.0
    m = _BEDS_RE.search(t)
    if m:
        beds = float(m.group(1))
        if re.search(r"\bden\b|\bflex\b|\bconvertible\b", t, re.I):
            beds += 0.5
        return beds if 0 <= beds <= 12 else None
    if re.fullmatch(r"\s*\d+(\.\d)?\s*", t):
        v = float(t)
        return v if 0 <= v <= 12 else None
    return None


def parse_price(text) -> int | None:
    """Parse a monthly rent to whole dollars, rejecting implausible values."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        val = float(text)
    else:
        cleaned = re.sub(r"[^\d.]", "", str(text))
        if not cleaned or cleaned.count(".") > 1:
            return None
        try:
            val = float(cleaned)
        except ValueError:
            return None
    # NYC monthly rents outside this band are almost always a parse error
    # (an annual figure, a sale price, or a square-foot number).
    if not (500 <= val <= 100_000):
        return None
    return int(round(val))


def listing_fingerprint(bbl: str | None, unit_key: str, street: str, beds,
                        source: str = "", source_ref: str | None = None,
                        bin: str | None = None) -> str:
    """Stable identity for a unit across sources and across crawls.

    Price is deliberately excluded: a price change must read as an update to a
    known unit, not as a brand-new listing.

    The building key is BIN (Building Identification Number) when we have it,
    falling back to BBL. That distinction is not pedantic: BBL is a *tax lot*,
    and NYC's big campuses put dozens of buildings on one. All of Stuyvesant
    Town is BBL 1009720001, so unit "07-A" exists in 315 Avenue C and in 410
    East 20th Street and keying on BBL merged them -- 35 of 100 units
    disappeared into each other on the first crawl. Co-op City, LeFrak City,
    Penn South and Starrett City have the same shape, and they are exactly the
    high-turnover affordable stock this product cares most about. BIN is
    per-building, so it separates them correctly.

    Identity is taken from the strongest available key, in order:

      1. building + unit_key  the real thing. Dedupes the same apartment
                          across every source that publishes a unit number,
                          provided both resolve to the same building.
      2. bbl + source_ref for sources that identify a listing but not a unit.
                          Aggregators do this constantly -- three different
                          1-beds at 70 Pine Street, no unit numbers, three
                          different rents. Without the source's own listing id
                          they collapse into one fingerprint and two of the
                          three get recorded as price changes on a phantom.
                          Scoped by source, since ids are only unique within
                          one publisher. The cost is that the same apartment
                          seen through two ref-only sources stays two records:
                          the right trade, because over-merging corrupts
                          first_seen and under-merging only duplicates.
      3. bbl + beds       building-level listings with nothing finer.
      4. street + beds    nothing resolved; low confidence upstream.
    """
    building = bin or bbl
    if building and unit_key:
        basis = f"{building}|{unit_key}"
    elif building and source_ref:
        basis = f"{building}|ref:{source}:{source_ref}"
    elif building:
        basis = f"{building}||{beds if beds is not None else ''}"
    else:
        basis = (f"nobbl|{normalize_street(street)}|"
                 f"{unit_key or (f'ref:{source}:{source_ref}' if source_ref else '')}|"
                 f"{beds if beds is not None else ''}")
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:20]
