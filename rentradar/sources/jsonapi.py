"""Replay a discovered JSON endpoint.

Most NYC management sites are client-rendered: the HTML is an empty shell and
the inventory arrives over XHR. `rentradar.discover` finds that XHR call once
with a headless browser; this adapter then polls it directly forever, at a
fraction of the cost and latency of rendering the page.

That split is the core of the crawl economics. Rendering ~200 sites every 15
minutes with Playwright is thousands of dollars a month of compute. Rendering
each site once a week to re-verify its endpoint, and otherwise hitting plain
JSON, is roughly the cost of a small VM.
"""
from __future__ import annotations

import json
from typing import Any

from ..models import RawListing
from .base import Source, SourceError


def dig(obj: Any, path: str, default=None):
    """Resolve a dotted path with numeric indices: 'data.items.0.price'."""
    if not path:
        return default
    cur = obj
    for part in path.split("."):
        if cur is None:
            return default
        if isinstance(cur, list):
            if not part.isdigit() or int(part) >= len(cur):
                return default
            cur = cur[int(part)]
        elif isinstance(cur, dict):
            if part not in cur:
                return default
            cur = cur[part]
        else:
            return default
    return cur if cur is not None else default


def find_records(obj: Any, min_len: int = 3) -> list[dict]:
    """Locate the largest list-of-dicts in an arbitrary JSON payload.

    Saves hand-writing a root path for every endpoint. Endpoints that wrap
    inventory in {"d": {"results": [...]}} or {"data": {"units": [...]}} both
    resolve without configuration.
    """
    best: list[dict] = []

    def walk(node):
        nonlocal best
        if isinstance(node, list):
            dicts = [x for x in node if isinstance(x, dict)]
            if len(dicts) >= min_len and len(dicts) > len(best):
                best = dicts
            for x in node:
                walk(x)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v)

    walk(obj)
    return best


class JsonApiSource(Source):
    """Poll a JSON endpoint and map its fields onto RawListing.

    Options:
        url        endpoint discovered by `rentradar.discover`
        fields     dotted-path map, e.g. {"address": "Address.Line1",
                   "price": "MinRent", "unit_raw": "UnitNumber"}
        root       optional dotted path to the record list; auto-detected
                   when omitted
        method     "GET" (default) or "POST"
        body       JSON body for POST endpoints, replayed verbatim
    """

    def __init__(self, id: str, url: str, fields: dict[str, str],
                 root: str | None = None, operator: str = "",
                 borough_hint: str | None = None, no_fee: bool | None = None,
                 headers: dict | None = None, **kwargs):
        super().__init__(**kwargs)
        self.id = id
        self.url = url
        self.fields = fields
        self.root = root
        self.operator = operator or id
        self.borough_hint = borough_hint
        self.no_fee = no_fee
        self.headers = headers or {}

    def fetch(self) -> list[RawListing]:
        body = self.get(self.url, accept="application/json, text/plain, */*")
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise SourceError(f"{self.id}: endpoint did not return JSON") from exc

        records = dig(payload, self.root) if self.root else find_records(payload)
        if not isinstance(records, list) or not records:
            raise SourceError(
                f"{self.id}: no record list found -- endpoint shape likely changed"
            )

        f = self.fields
        rows: list[RawListing] = []
        for rec in records:
            address = dig(rec, f.get("address", ""))
            if not address:
                continue
            rows.append(RawListing(
                source=self.id,
                source_url=self.url,
                address=str(address),
                unit_raw=dig(rec, f.get("unit_raw", "")),
                price_raw=dig(rec, f.get("price", "")),
                beds_raw=dig(rec, f.get("beds", "")),
                baths_raw=dig(rec, f.get("baths", "")),
                sqft_raw=dig(rec, f.get("sqft", "")),
                available_on=dig(rec, f.get("available_on", "")),
                title=dig(rec, f.get("title", "")),
                detail_url=dig(rec, f.get("detail_url", "")),
                borough_hint=dig(rec, f.get("borough", "")) or self.borough_hint,
                no_fee=self.no_fee,
                # The publisher's own id, when mapped. Keeps distinct listings
                # in one building apart where no unit number is published.
                source_ref=(str(dig(rec, f["source_ref"]))
                            if f.get("source_ref") and dig(rec, f["source_ref"])
                            else None),
                extra={"operator": self.operator},
            ))
        return rows
