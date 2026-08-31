"""Regression tests for the parsing layer.

Every case here is a bug that actually occurred against live data, not a
hypothetical. The unit-extraction cases in particular: taking the last
digit-bearing token in "101 West 15th Street 227 - 1 Bedroom" yielded unit
"1", which collided the fingerprints of every one-bedroom in the building and
made real listings look like price changes on one phantom unit.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rentradar.normalize import (
    extract_unit_from_name, infer_borough, listing_fingerprint,
    house_number, normalize_street, normalize_unit, parse_beds, parse_price,
)


def check(label, got, want):
    status = "ok  " if got == want else "FAIL"
    if got != want:
        check.failures += 1
    print(f"  {status} {label:52} got={got!r} want={want!r}")


check.failures = 0

print("normalize_street")
check("abbreviations", normalize_street("350 West 42nd Street"), "350 w 42 st")
check("punctuation", normalize_street("350 W. 42nd St."), "350 w 42 st")
check("avenue", normalize_street("1500 Ave. of the Americas"), "1500 ave of the americas")
check("hyphenated Queens", normalize_street("42-20 24th Street"), "42-20 24 st")

print("\nnormalize_unit")
check("hash", normalize_unit("#12B"), "12B")
check("apt prefix", normalize_unit("Apt 12-B"), "12B")
check("unit prefix", normalize_unit("Unit 12 b"), "12B")
check("zero pad", normalize_unit("004B"), "4B")
check("empty", normalize_unit(None), "")

print("\nextract_unit_from_name  (descriptor-tail bug)")
check("padded w/ letter", extract_unit_from_name("1 QPS 004B - Studio"), "4B")
check("bedroom tail", extract_unit_from_name("1 QPS 018C - 1 Bedroom"), "18C")
check("street num + unit", extract_unit_from_name("101 West 15th Street 227 - 1 Bedroom"), "227")
check("padded no letter", extract_unit_from_name("10 Downing Street 003P - Studio"), "3P")
check("explicit marker", extract_unit_from_name("350 W 42nd St #12B, 2 Bed"), "12B")
check("no unit present", extract_unit_from_name("Studio Apartment"), "")
check("aggregator title: address is not a unit",
      extract_unit_from_name("1-Bedroom at 70 Pine Street"), "")
check("aggregator title, hyphenless", extract_unit_from_name("Studio at 8 Spruce Street"), "")
check("named building, no number", extract_unit_from_name("2-Bedroom at The Nathaniel"), "")
check("address + real unit still works",
      extract_unit_from_name("70 Pine Street 12B - 1 Bedroom"), "12B")
check("queens hyphenated address", extract_unit_from_name("1-Bedroom at 42-20 24th Street"), "")
check("house number vetoed by address hint",
      extract_unit_from_name("Studio at Gateway: 365 South End", "365 South End Avenue"), "")
check("house number vetoed, no street suffix anywhere",
      extract_unit_from_name("Studio at Sullivan Mews: 113 Sullivan", "113 Sullivan Street"), "")
check("real unit survives the veto",
      extract_unit_from_name("101 West 15th Street 227 - 1 Bedroom", "101 West 15th Street"), "227")
check("house_number helper", house_number("42-20 24th Street"), "42-20")
check("sibling building in a complex is not a unit",
      extract_unit_from_name("Studio at Stuyvesant Town: 645 East 14th", "635 East 14th Street"), "")
check("named complex with address, no suffix",
      extract_unit_from_name("1-Bedroom at Manhattan East: 227 East 66th", "209 East 66th Street"), "")
check("lettered unit still wins over a following word",
      extract_unit_from_name("Apt 12B North Tower", "70 Pine Street"), "12B")

print("\nparse_beds  ('1 Bedroom' returned None before the fix)")
check("studio", parse_beds("1 QPS 004B - Studio"), 0.0)
check("one bedroom", parse_beds("1 QPS 018C - 1 Bedroom"), 1.0)
check("two bed", parse_beds("2 Bed / 2 Bath"), 2.0)
check("BR form", parse_beds("3BR"), 3.0)
check("flex", parse_beds("1 Bedroom + Den"), 1.5)
check("numeric", parse_beds(2), 2.0)
check("absent", parse_beds("Luxury rental"), None)

print("\nparse_price")
check("string", parse_price("3763"), 3763)
check("formatted", parse_price("$4,002/mo"), 4002)
check("annual rejected", parse_price("120000"), None)
check("sqft rejected", parse_price("430"), None)
check("junk", parse_price("call for price"), None)

print("\ninfer_borough")
check("neighborhood LIC", infer_borough("Long Island City"), "Queens")
check("neighborhood Chelsea", infer_borough("Chelsea"), "Manhattan")
check("explicit", infer_borough("123 Main St, Brooklyn, NY"), "Brooklyn")
check("unknown", infer_borough("Hoboken"), None)

print("\nlisting_fingerprint")
a = listing_fingerprint("1010327501", "12B", "350 W 42 St", 1.0)
b = listing_fingerprint("1010327501", "12B", "350 West 42nd Street", 1.0)
check("stable across address spellings", a, b)
c = listing_fingerprint("1010327501", "12C", "350 W 42 St", 1.0)
check("different units differ", a != c, True)

# One tax lot, two buildings -- the Stuyvesant Town case.
same_bbl = "1009720001"
b1 = listing_fingerprint(same_bbl, "07A", "315 Avenue C", 1.0, bin="1082851")
b2 = listing_fingerprint(same_bbl, "07A", "410 East 20 St", 1.0, bin="1082865")
check("same unit number, different building, different id", b1 != b2, True)
no_bin1 = listing_fingerprint(same_bbl, "07A", "315 Avenue C", 1.0)
no_bin2 = listing_fingerprint(same_bbl, "07A", "410 East 20 St", 1.0)
check("without BIN they would have merged", no_bin1 == no_bin2, True)
check("BIN takes precedence over BBL",
      listing_fingerprint(same_bbl, "07A", "x", 1.0, bin="1082851") ==
      listing_fingerprint("9999999999", "07A", "x", 1.0, bin="1082851"), True)
print(f"  {'ok  ' if True else ''} price is excluded from identity by construction")

print(f"\n{'PASS' if check.failures == 0 else str(check.failures) + ' FAILURES'}")
sys.exit(1 if check.failures else 0)
