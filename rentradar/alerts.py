"""Saved searches and alert dispatch.

Speed is the product. A matched listing should leave this module within
seconds of the crawl that found it, because NYC inventory at the affordable
end clears in under 48 hours.

Fair Housing note, and this is not boilerplate: the moment you rank or filter
listings for a person you are in scope for the FHA and NYC Human Rights Law.
Match on the criteria the renter typed and on nothing else. No demographic
inference, no "similar renters also liked", no neighborhood scoring derived
from anything that correlates with a protected class. `Criteria` is
deliberately a closed set of explicit, user-supplied fields, and every match
writes its reasons so you can reconstruct why any given person saw any given
listing.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class Criteria:
    """A renter's saved search. Every field is explicitly user-supplied."""
    name: str
    max_price: int | None = None
    min_beds: float | None = None
    max_beds: float | None = None
    boroughs: list[str] = field(default_factory=list)
    zip_codes: list[str] = field(default_factory=list)
    require_no_fee: bool = False
    min_sqft: int | None = None
    #: Skip listings we could not resolve to a real building.
    min_confidence: str = "medium"

    _RANK = {"low": 0, "medium": 1, "high": 2}

    def match(self, row) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if self._RANK.get(row["confidence"], 0) < self._RANK[self.min_confidence]:
            return False, ["below confidence floor"]

        if self.max_price is not None:
            if row["price"] is None:
                return False, ["no price published"]
            if row["price"] > self.max_price:
                return False, [f"price {row['price']} > {self.max_price}"]
            reasons.append(f"${row['price']:,}/mo within budget")

        if self.min_beds is not None:
            if row["beds"] is None or row["beds"] < self.min_beds:
                return False, ["too few bedrooms"]
        if self.max_beds is not None and row["beds"] is not None \
                and row["beds"] > self.max_beds:
            return False, ["more bedrooms than requested"]
        if row["beds"] is not None:
            reasons.append(f"{row['beds']:g} bed")

        if self.boroughs:
            if row["borough"] not in self.boroughs:
                return False, [f"borough {row['borough']}"]
            reasons.append(row["borough"])

        if self.require_no_fee and not row["no_fee"]:
            return False, ["not confirmed no-fee"]

        if self.min_sqft is not None:
            if row["sqft"] is None or row["sqft"] < self.min_sqft:
                return False, ["under minimum square footage"]

        return True, reasons


def match_new(store, criteria: Criteria, since_iso: str) -> list[tuple]:
    """Return (row, reasons) for newly-seen listings matching `criteria`."""
    out = []
    for row in store.new_since(since_iso):
        ok, reasons = criteria.match(row)
        if ok:
            out.append((row, reasons))
    return out


def format_alert(row, reasons: list[str]) -> str:
    price = f"${row['price']:,}/mo" if row["price"] else "price on request"
    unit = f" #{row['unit_key']}" if row["unit_key"] else ""
    fee = " · no fee" if row["no_fee"] else ""
    sqft = f" · {row['sqft']} sqft" if row["sqft"] else ""
    link = row["detail_url"] or row["source_url"]
    return (
        f"{price} — {row['address']}{unit}\n"
        f"  {', '.join(reasons)}{sqft}{fee}\n"
        f"  first seen {row['first_seen']} via {row['source']}\n"
        f"  {link}"
    )


class Dispatcher:
    """Pluggable delivery. `console` is the default; swap in your channel.

    Deliberately not wired to a specific vendor: the interesting engineering
    is upstream, and every channel worth having (APNs/FCM push, Telegram,
    Twilio, Postmark) is a ten-line `send` implementation.
    """

    def __init__(self, sink=None):
        self.sink = sink or self._console
        self.sent: list[dict] = []

    @staticmethod
    def _console(subject: str, body: str) -> None:
        print(f"\n=== {subject} ===\n{body}")

    def send(self, criteria: Criteria, matches: list[tuple]) -> int:
        if not matches:
            return 0
        body = "\n\n".join(format_alert(r, why) for r, why in matches)
        subject = f"{len(matches)} new match{'es' if len(matches) > 1 else ''} for {criteria.name}"
        self.sink(subject, body)
        self.sent.append({"criteria": criteria.name, "n": len(matches)})
        return len(matches)


def load_criteria(path: str) -> list[Criteria]:
    with open(path) as fh:
        return [Criteria(**c) for c in json.load(fh)]
