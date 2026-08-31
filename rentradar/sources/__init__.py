"""Source registry: builds adapters from sources.yaml."""
from __future__ import annotations

from .base import Source, SourceError
from .housing_connect import HousingConnectSource
from .htmlindex import HtmlIndexSource
from .jsonapi import JsonApiSource
from .jsonld import JsonLdSource
from .nybits import NyBitsSource
from .reddit import RedditSource

ADAPTERS = {
    "jsonld": JsonLdSource,
    "jsonapi": JsonApiSource,
    "housing_connect": HousingConnectSource,
    "htmlindex": HtmlIndexSource,
    "nybits": NyBitsSource,
    "reddit": RedditSource,
}


def build(spec: dict) -> Source:
    """Instantiate one adapter from a sources.yaml entry."""
    spec = dict(spec)
    kind = spec.pop("type")
    if kind not in ADAPTERS:
        raise ValueError(f"unknown source type {kind!r}; have {sorted(ADAPTERS)}")
    spec.pop("enabled", None)
    spec.pop("notes", None)
    return ADAPTERS[kind](**spec)


def build_all(specs: list[dict]) -> list[Source]:
    return [build(s) for s in specs if s.get("enabled", True)]


__all__ = ["Source", "SourceError", "build", "build_all", "ADAPTERS"]
