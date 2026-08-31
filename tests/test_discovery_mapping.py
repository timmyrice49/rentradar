"""Offline tests for endpoint discovery and JSON replay.

`discover` itself needs a headless browser with network egress, which a
locked-down CI box will not have. The parts that actually carry bugs --
locating the record list inside an arbitrary payload, and guessing the field
map from key names -- are pure functions and are tested here against payload
shapes taken from real property-management APIs (Yardi RentCafe, Entrata,
AppFolio).
"""
import json, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rentradar.discover import _map_fields
from rentradar.sources.jsonapi import JsonApiSource, dig, find_records

failures = 0


def check(label, got, want):
    global failures
    ok = got == want
    if not ok:
        failures += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label:46} got={got!r}")


# Yardi RentCafe wraps results in {"d": {"results": [...]}}
RENTCAFE = {"d": {"__count": 3, "results": [
    {"ApartmentName": "12B", "PropertyAddress": "350 West 42nd Street",
     "MinimumRent": 4200, "Beds": 1, "Baths": 1, "SQFT": 720,
     "AvailableDate": "2026-10-01", "PropertyUrl": "https://x/12b"},
    {"ApartmentName": "14C", "PropertyAddress": "350 West 42nd Street",
     "MinimumRent": 4400, "Beds": 1, "Baths": 1, "SQFT": 730,
     "AvailableDate": "2026-09-15", "PropertyUrl": "https://x/14c"},
    {"ApartmentName": "PH1", "PropertyAddress": "350 West 42nd Street",
     "MinimumRent": 9800, "Beds": 3, "Baths": 2, "SQFT": 1900,
     "AvailableDate": "2026-11-01", "PropertyUrl": "https://x/ph1"},
]}}

# Entrata-style: nested address object, deeper wrapper
ENTRATA = {"response": {"result": {"units": [
    {"unitNumber": "5A", "address": {"line1": "1 Wall St", "city": "New York"},
     "marketRent": "3950", "bedrooms": "2", "bathrooms": "1",
     "squareFeet": "890", "availableOn": "2026-09-20"},
    {"unitNumber": "9F", "address": {"line1": "1 Wall St", "city": "New York"},
     "marketRent": "3100", "bedrooms": "1", "bathrooms": "1",
     "squareFeet": "610", "availableOn": "2026-09-05"},
    {"unitNumber": "11D", "address": {"line1": "1 Wall St", "city": "New York"},
     "marketRent": "3300", "bedrooms": "1", "bathrooms": "1",
     "squareFeet": "640", "availableOn": "2026-10-10"},
]}}}

# A payload with a decoy list that must NOT win: amenities are longer-looking
# but are not dicts, while a nav list of dicts is shorter than inventory.
DECOY = {"nav": [{"label": "Home"}, {"label": "About"}],
         "amenities": ["gym", "roof", "laundry", "doorman", "bike room"],
         "listings": [{"streetAddress": f"{i} Main St", "rent": 2000 + i,
                       "unit": f"{i}A", "bedrooms": 1} for i in range(6)]}

print("find_records")
check("rentcafe wrapper", len(find_records(RENTCAFE)), 3)
check("entrata nested", len(find_records(ENTRATA)), 3)
check("picks inventory over nav", len(find_records(DECOY)), 6)

print("\n_map_fields")
m, score = _map_fields(find_records(RENTCAFE))
check("rentcafe address", m.get("address"), "PropertyAddress")
check("rentcafe price", m.get("price"), "MinimumRent")
check("rentcafe unit", m.get("unit_raw"), "ApartmentName")
check("rentcafe beds", m.get("beds"), "Beds")
check("rentcafe scores well", score >= 13, True)

m2, _ = _map_fields(find_records(ENTRATA))
check("entrata dotted address", m2.get("address"), "address.line1")
check("entrata price", m2.get("price"), "marketRent")
check("entrata unit", m2.get("unit_raw"), "unitNumber")

print("\ndig")
check("dotted", dig(ENTRATA, "response.result.units.0.unitNumber"), "5A")
check("missing returns default", dig(ENTRATA, "response.nope.x", "-"), "-")
check("index past end", dig(ENTRATA, "response.result.units.9.x"), None)

print("\nJsonApiSource end-to-end (payload injected, no network)")
src = JsonApiSource(id="test", url="https://x/api", fields=m,
                    operator="Test Co", borough_hint="Manhattan")
src.get = lambda *a, **k: json.dumps(RENTCAFE)   # stub the HTTP layer
rows = src.fetch()
check("row count", len(rows), 3)
check("address mapped", rows[0].address, "350 West 42nd Street")
check("unit mapped", rows[0].unit_raw, "12B")
check("price mapped", rows[0].price_raw, 4200)

print(f"\n{'PASS' if failures == 0 else str(failures) + ' FAILURES'}")
sys.exit(1 if failures else 0)
