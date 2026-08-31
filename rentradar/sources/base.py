"""Adapter contract and shared HTTP behavior.

Every source implements `fetch() -> list[RawListing]`. Adapters must not
normalize, geocode, or write to the database -- that keeps one code path for
data quality no matter how many sources you add.
"""
from __future__ import annotations

import gzip
import io
import logging
import time
import urllib.error
import urllib.request
import zlib
from abc import ABC, abstractmethod

from ..models import RawListing

log = logging.getLogger(__name__)

DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


class SourceError(RuntimeError):
    pass


class Source(ABC):
    """Base adapter.

    `id` must be stable forever: it scopes off-market detection, so renaming a
    source orphans its inventory.
    """
    id: str = "base"
    #: Politeness delay between requests to the same host, in seconds.
    delay: float = 1.5
    #: A source returning fewer than this many rows is treated as broken
    #: rather than as "everything got rented", which prevents a layout change
    #: from silently marking a landlord's whole portfolio off-market.
    min_expected: int = 1

    def __init__(self, **kwargs):
        self.opts = kwargs
        self._last_request = 0.0

    @abstractmethod
    def fetch(self) -> list[RawListing]:
        ...

    # -- helpers -----------------------------------------------------------

    def get(self, url: str, timeout: int = 25, accept: str = "text/html,*/*") -> str:
        delta = time.monotonic() - self._last_request
        if delta < self.delay:
            time.sleep(self.delay - delta)
        self._last_request = time.monotonic()

        req = urllib.request.Request(url, headers={
            "User-Agent": self.opts.get("user_agent", DEFAULT_UA),
            "Accept": accept,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        })
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                enc = (resp.headers.get("Content-Encoding") or "").lower()
                if enc == "gzip":
                    raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
                elif enc == "deflate":
                    raw = zlib.decompress(raw, -zlib.MAX_WBITS)
                return raw.decode("utf-8", "ignore")
        except urllib.error.HTTPError as exc:
            raise SourceError(f"{self.id}: HTTP {exc.code} for {url}") from exc
        except Exception as exc:
            raise SourceError(f"{self.id}: {type(exc).__name__} for {url}") from exc
