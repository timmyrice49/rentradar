"""Reddit — the social listing channel, through the official API.

This adapter exists because Facebook housing groups do not have a lawful
automated path. Reddit does: a documented OAuth API, a published rate limit, a
free tier, and terms that permit exactly this when you register an app and
identify yourself. Same kind of inventory — small landlords, sublets, lease
breaks, no-fee direct-from-owner posts — reachable without violating anything.

Set expectations correctly. This is free-text prose written by strangers, not
a feed. Precision is low and always will be:

  * Most posts are people *seeking* apartments, not offering them. The filter
    below discards those and will still let some through.
  * Addresses are usually absent. A post says "Bushwick, near the Jefferson L",
    not "123 Troutman St". Those geocode to a neighborhood at best, so they
    land at low confidence and should never be alerted on with the same
    weight as a resolved unit.
  * Scams are common in this channel. Never present a Reddit listing without
    the link to the original post and the author, so a renter can judge it.

Treat what comes out of here as leads for a human, not as inventory. The
honest use is coverage of stock that appears nowhere else, priced and located
roughly, with a link out.

Setup: register a "script" app at https://www.reddit.com/prefs/apps and set
REDDIT_CLIENT_ID / REDDIT_CLIENT_SECRET. The User-Agent must identify you —
Reddit blocks generic ones, and a real contact address is expected.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request

from ..models import RawListing
from .base import Source, SourceError

log = logging.getLogger(__name__)

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
API = "https://oauth.reddit.com"

DEFAULT_SUBS = ["NYCapartments", "AptsNYC", "nycrentals", "Brooklyn",
                "Queens", "astoria", "bushwick"]

# Posts that are offers, not searches.
OFFER_HINTS = re.compile(
    r"\b(for rent|available|no fee|nofee|lease break|leasebreak|sublet|"
    r"subletting|renting out|open house|showing|apartment available|"
    r"room available|taking over)\b", re.I)
SEEKING_HINTS = re.compile(
    r"\b(looking for|in search of|\bISO\b|need(ed)? (a|an)? ?(apartment|room)|"
    r"any (leads|recs)|help me find|wanted|advice|question|is this (normal|legal)|"
    r"am i being|should i|rate my|thoughts on)\b", re.I)

PRICE_RE = re.compile(r"\$\s?([1-9][\d,]{2,6})(?:\s*(?:/|per\s*)?\s*(?:mo|month))?", re.I)
BEDS_RE = re.compile(r"\b(studio|\d+\s*(?:bed\s?rooms?|beds?|br|bd))\b", re.I)
ADDRESS_RE = re.compile(
    r"\b(\d{1,4}(?:-\d{1,3})?\s+"
    r"(?:[NSEW]\.?\s+|(?:north|south|east|west)\s+)?"
    r"(?:\d{1,3}(?:st|nd|rd|th)|[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\s+"
    r"(?:street|st|avenue|ave|road|rd|place|pl|boulevard|blvd|drive|dr|"
    r"lane|ln|court|ct|terrace|parkway|pkwy)\b)", re.I)

NEIGHBORHOODS = [
    "astoria", "long island city", "sunnyside", "ridgewood", "bushwick",
    "williamsburg", "greenpoint", "bed-stuy", "bedford-stuyvesant",
    "crown heights", "prospect heights", "park slope", "sunset park",
    "bay ridge", "fort greene", "clinton hill", "flatbush", "harlem",
    "washington heights", "inwood", "chelsea", "east village",
    "west village", "lower east side", "upper east side",
    "upper west side", "hell's kitchen", "murray hill", "flushing",
    "jackson heights", "forest hills", "elmhurst", "mott haven", "dumbo",
]


class RedditSource(Source):
    """Read new posts from housing subreddits via the official OAuth API.

    Options:
        client_id / client_secret   or env REDDIT_CLIENT_ID / _SECRET
        subreddits                  list; defaults to NYC housing subs
        limit                       posts per subreddit per crawl (max 100)
        contact                     your contact string for the User-Agent
    """

    id = "reddit"
    delay = 1.0               # Reddit's documented free tier is 100 req/min
    min_expected = 0          # a quiet day is legitimately zero offers

    def __init__(self, id: str = "reddit", client_id: str | None = None,
                 client_secret: str | None = None,
                 subreddits: list[str] | None = None, limit: int = 100,
                 contact: str = "rentradar", **kwargs):
        super().__init__(**kwargs)
        self.id = id
        self.client_id = client_id or os.environ.get("REDDIT_CLIENT_ID")
        self.client_secret = client_secret or os.environ.get("REDDIT_CLIENT_SECRET")
        self.subreddits = subreddits or DEFAULT_SUBS
        self.limit = min(limit, 100)
        self.user_agent = f"python:rentradar:0.1 (by {contact})"
        self._token: str | None = None
        self._token_expires = 0.0

    # -- auth --------------------------------------------------------------

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_expires - 60:
            return self._token
        if not (self.client_id and self.client_secret):
            raise SourceError(
                f"{self.id}: no credentials. Register a script app at "
                "https://www.reddit.com/prefs/apps and set REDDIT_CLIENT_ID "
                "and REDDIT_CLIENT_SECRET.")

        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()).decode()
        body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
        req = urllib.request.Request(TOKEN_URL, data=body, headers={
            "Authorization": f"Basic {basic}",
            "User-Agent": self.user_agent,
            "Content-Type": "application/x-www-form-urlencoded",
        })
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read().decode())
        except Exception as exc:
            raise SourceError(f"{self.id}: token request failed: {exc}") from exc

        if "access_token" not in payload:
            raise SourceError(f"{self.id}: no access_token in response")
        self._token = payload["access_token"]
        self._token_expires = time.time() + float(payload.get("expires_in", 3600))
        return self._token

    def _api(self, path: str) -> dict:
        req = urllib.request.Request(f"{API}{path}", headers={
            "Authorization": f"Bearer {self._access_token()}",
            "User-Agent": self.user_agent,
        })
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode())

    # -- parsing -----------------------------------------------------------

    @staticmethod
    def is_offer(title: str, body: str) -> bool:
        """Keep posts offering a home; drop people looking for one.

        Titles carry the intent far more reliably than bodies (a seeker's post
        often quotes an offer), so the seeking test only looks at the title.
        """
        if SEEKING_HINTS.search(title):
            return False
        return bool(OFFER_HINTS.search(title) or OFFER_HINTS.search(body[:600]))

    @staticmethod
    def extract(title: str, body: str) -> dict:
        text = f"{title}\n{body}"
        prices = [p.replace(",", "") for p in PRICE_RE.findall(text)]
        plausible = [int(p) for p in prices if p.isdigit() and 500 <= int(p) <= 30_000]
        beds = BEDS_RE.search(text)
        addr = ADDRESS_RE.search(text)
        hood = next((h for h in NEIGHBORHOODS if h in text.lower()), None)
        return {
            "price": min(plausible) if plausible else None,
            "beds": beds.group(1) if beds else None,
            "address": addr.group(1).strip() if addr else None,
            "neighborhood": hood,
        }

    def _to_listing(self, post: dict, sub: str) -> RawListing | None:
        title = post.get("title") or ""
        body = post.get("selftext") or ""
        if not self.is_offer(title, body):
            return None

        got = self.extract(title, body)
        # A post with no price and no location is not a listing, it is chatter.
        if got["price"] is None:
            return None
        if not (got["address"] or got["neighborhood"]):
            return None

        return RawListing(
            source=self.id,
            source_url=f"https://www.reddit.com/r/{sub}/new/",
            address=got["address"] or got["neighborhood"] or "",
            unit_raw=None,
            price_raw=got["price"],
            beds_raw=got["beds"],
            title=title[:200],
            detail_url="https://www.reddit.com" + (post.get("permalink") or ""),
            borough_hint=got["neighborhood"],
            no_fee=bool(re.search(r"\bno[\s-]?fee\b", f"{title} {body}", re.I)) or None,
            source_ref=post.get("id"),
            extra={
                "kind": "social_post",
                "subreddit": sub,
                "author": post.get("author"),
                "created_utc": post.get("created_utc"),
                "neighborhood": got["neighborhood"],
                # Carried so the UI can always show the renter the original.
                # Never present one of these without it.
                "unverified": True,
                "excerpt": re.sub(r"\s+", " ", body)[:300],
            },
        )

    # -- fetch -------------------------------------------------------------

    def fetch(self) -> list[RawListing]:
        rows: list[RawListing] = []
        errors: list[str] = []
        for sub in self.subreddits:
            try:
                time.sleep(self.delay)
                data = self._api(f"/r/{sub}/new?limit={self.limit}")
            except SourceError:
                raise                       # credential problems are fatal
            except Exception as exc:
                errors.append(f"r/{sub}: {type(exc).__name__}")
                continue
            for child in (data.get("data", {}).get("children") or []):
                post = child.get("data") or {}
                try:
                    listing = self._to_listing(post, sub)
                except Exception:
                    log.exception("%s: failed on post %s", self.id, post.get("id"))
                    continue
                if listing:
                    rows.append(listing)

        if not rows and errors and len(errors) == len(self.subreddits):
            raise SourceError("; ".join(errors[:3]))
        return rows
