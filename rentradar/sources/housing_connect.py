"""NYC Housing Connect -- income-restricted affordable lotteries.

This is the one source that is both genuinely affordable and structurally
absent from StreetEasy. Rents are set by AMI band rather than by the market,
so a two-bedroom at 40% AMI can be a third of the market rate on the same
block. It is public data with a real API and no scraping question at all.

Dataset: "Advertised Lotteries on Housing Connect by Lottery" (vy5i-a666) on
NYC Open Data / Socrata.

Note the shape difference from a rental listing: a lottery is building-level,
not unit-level, and has an application deadline rather than a lease date. We
carry it through the same pipeline anyway so that one alert stream covers both
market and affordable inventory, and mark it in `extra` so the UI can present
it as a deadline rather than a showing.
"""
from __future__ import annotations

import json
import urllib.parse

from ..models import RawListing
from .base import Source, SourceError

SOCRATA = "https://data.cityofnewyork.us/resource/vy5i-a666.json"

BOROUGH = {"MN": "Manhattan", "BX": "Bronx", "BK": "Brooklyn", "BR": "Brooklyn",
           "QN": "Queens", "SI": "Staten Island"}

# Statuses observed in the live dataset: Active (32), Tenant Selection (803),
# All Units Filled (831), Closed (54). Only "Active" still takes applications;
# "Tenant Selection" means the deadline has passed and the list is being
# worked. Verified against a $group query rather than assumed -- the obvious
# guesses ("Open", "Accepting Applications") appear nowhere in the data.
OPEN_STATUSES = {"active"}


class HousingConnectSource(Source):
    id = "nyc_housing_connect"
    delay = 0.5
    min_expected = 1

    def __init__(self, app_token: str | None = None, only_open: bool = True,
                 limit: int = 1000, **kwargs):
        super().__init__(**kwargs)
        self.app_token = app_token
        self.only_open = only_open
        self.limit = limit

    def fetch(self) -> list[RawListing]:
        params = {
            "$limit": self.limit,
            "$order": "lottery_start_date DESC",
            "$where": "development_type='Rental'",
        }
        url = f"{SOCRATA}?{urllib.parse.urlencode(params)}"
        if self.app_token:
            url += f"&$$app_token={urllib.parse.quote(self.app_token)}"

        body = self.get(url, accept="application/json")
        try:
            records = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SourceError(f"{self.id}: bad JSON from Socrata") from exc

        rows: list[RawListing] = []
        for rec in records:
            status = (rec.get("lottery_status") or "").strip().lower()
            if self.only_open and status not in OPEN_STATUSES:
                continue

            lat, lon = rec.get("latitude"), rec.get("longitude")
            # "Multiple" appears when a lottery spans several buildings.
            multi = lat == "Multiple" or rec.get("postcode") == "Multiple"

            name = rec.get("lottery_name") or f"Lottery {rec.get('lottery_id')}"
            boro = BOROUGH.get((rec.get("borough") or "").upper())
            zipc = rec.get("postcode") if not multi else None

            rows.append(RawListing(
                source=self.id,
                source_url=("https://housingconnect.nyc.gov/PublicWeb/"
                            f"search-lotteries?lotteryId={rec.get('lottery_id')}"),
                # Housing Connect does not publish a street line in this
                # dataset; we carry the development name + zip and let the
                # geocoder do what it can. Unresolved rows are still useful --
                # they carry coordinates.
                address=f"{name} {zipc or ''}".strip(),
                unit_raw=None,
                price_raw=None,             # rent is per-AMI-band, not a scalar
                beds_raw=None,
                title=name,
                detail_url="https://housingconnect.nyc.gov/PublicWeb/search-lotteries",
                borough_hint=boro,
                no_fee=True,                # lotteries never carry a broker fee
                extra={
                    "kind": "affordable_lottery",
                    "lottery_id": rec.get("lottery_id"),
                    "status": rec.get("lottery_status"),
                    "deadline": rec.get("lottery_end_date"),
                    "unit_count": rec.get("unit_count"),
                    "community_board": rec.get("community_board"),
                    "ami_bands": {
                        k.replace("applied_income_ami_", ""): v
                        for k, v in rec.items()
                        if k.startswith("applied_income_ami_")
                    },
                    "unit_mix": {
                        k.replace("unit_distribution_", ""): v
                        for k, v in rec.items()
                        if k.startswith("unit_distribution_")
                    },
                    "lat": None if multi else lat,
                    "lon": None if multi else lon,
                    "multi_building": multi,
                },
            ))
        return rows
