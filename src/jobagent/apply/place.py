"""Where the posting itself says the job is, read on the application site.

A board's listing can call a job remote when the employer's own posting names
an office (a LinkedIn "Remote" that is Lonoke, AR on the company's Workday).
Before a handler fills anything, or asks for a sign-in, it reads the
posting's own location: the schema.org JobPosting most career sites embed,
then the site's own location fields. A job in a specific place that is none
of the places wanted, with nothing saying it is remote, is skipped with the
reason; a posting that says nothing definite is left to go ahead.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from jobagent.discovery.scoring.rules import NOT_REMOTE as _NOT_REMOTE
from jobagent.discovery.scoring.rules import SAYS_REMOTE as _SAYS_REMOTE
from jobagent.discovery.scoring.rules import place_matches

_IN_OFFICE = re.compile(r"\bhybrid\b|\bon-?site\b|\bin[- ]office\b", re.I)
# Locations too broad to rule anything out.
_BROAD = {
    "united states",
    "united states of america",
    "usa",
    "us",
    "u.s.",
    "u.s.a.",
    "america",
    "north america",
    "anywhere",
    "worldwide",
    "global",
    "multiple locations",
    "various",
}
_SEVERAL = re.compile(r"^\d+\s+locations?$", re.I)


@dataclass(slots=True)
class PostingPlace:
    """What the posting says: its locations, and whether it says it is remote
    (True), in an office (False), or neither (None)."""

    locations: list[str] = field(default_factory=list)
    remote: bool | None = None


def read_place(
    page: Any, selectors: Iterable[str] = (), remote_selectors: Iterable[str] = ()
) -> PostingPlace:
    place = PostingPlace()
    for posting in _json_ld_postings(page):
        _from_json_ld(posting, place)
    for selector in selectors:
        for text in _texts(page, selector):
            if text and text not in place.locations:
                place.locations.append(text)
    for selector in remote_selectors:
        for text in _texts(page, selector):
            if _IN_OFFICE.search(text):
                place.remote = False if place.remote is None else place.remote
            elif re.search(r"\bremote\b", text, re.I):
                place.remote = True
    if place.remote is None:
        if any(
            re.search(r"\bremote\b", loc, re.I) and not _IN_OFFICE.search(loc)
            for loc in place.locations
        ):
            place.remote = True
    if place.remote is None:
        text = _page_text(page)
        if _SAYS_REMOTE.search(text) and not _NOT_REMOTE.search(text):
            place.remote = True
    return place


def wrong_place(place: PostingPlace, wanted: Iterable[str]) -> str | None:
    """Why the job is somewhere not wanted, or None when it may go ahead."""
    wanted = [w.strip().lower() for w in wanted if w and w.strip()]
    if not wanted or place.remote is True or not place.locations:
        return None
    # "3 Locations" only counts what the rest lists; a broad place ("United
    # States") could be anywhere, so it keeps the job in.
    named = [loc for loc in place.locations if not _SEVERAL.match(loc.strip())]
    specific = [loc for loc in named if _specific(loc)]
    if not specific or len(specific) < len(named):
        return None
    places = [w for w in wanted if w != "remote"]
    for loc in specific:
        lower = loc.lower()
        if any(place_matches(want, lower) for want in places):
            return None
    where = "; ".join(specific)
    if not places:
        return f"the posting itself puts the job in {where}, not remote; only Remote is wanted"
    return f"the posting itself puts the job in {where}, not remote and not in {', '.join(places)}"


def _specific(location: str) -> bool:
    lower = location.strip().lower().rstrip(".")
    if not lower or lower in _BROAD or _SEVERAL.match(lower):
        return False
    return not re.search(r"\bremote\b", lower)


def _from_json_ld(posting: dict[str, Any], place: PostingPlace) -> None:
    kind = str(posting.get("jobLocationType") or "").upper()
    if "TELECOMMUTE" in kind or posting.get("applicantLocationRequirements"):
        place.remote = True
    locations = posting.get("jobLocation")
    for loc in locations if isinstance(locations, list) else [locations]:
        text = _address_text(loc)
        if text and text not in place.locations:
            place.locations.append(text)
    description = posting.get("description")
    if place.remote is None and isinstance(description, str):
        if _SAYS_REMOTE.search(description) and not _NOT_REMOTE.search(description):
            place.remote = True


def _address_text(loc: Any) -> str:
    if not isinstance(loc, dict):
        return str(loc).strip() if isinstance(loc, str) else ""
    address = loc.get("address")
    if isinstance(address, str):
        return address.strip()
    if not isinstance(address, dict):
        return str(loc.get("name") or "").strip()
    parts = [address.get("addressLocality"), address.get("addressRegion")]
    text = ", ".join(str(p).strip() for p in parts if p and str(p).strip())
    return text or str(address.get("addressCountry") or "").strip()


def _json_ld_postings(page: Any) -> list[dict[str, Any]]:
    try:
        blocks = page.eval_on_selector_all(
            "script[type='application/ld+json']", "els => els.map(e => e.textContent)"
        )
    except Exception:
        return []
    found: list[dict[str, Any]] = []
    for block in blocks or []:
        try:
            data = json.loads(block)
        except (TypeError, ValueError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                if item.get("@type") == "JobPosting" or "JobPosting" in (item.get("@type") or []):
                    found.append(item)
                stack.extend(v for k, v in item.items() if k == "@graph")
    return found


def _texts(page: Any, selector: str) -> list[str]:
    try:
        texts = page.eval_on_selector_all(
            selector, "els => els.map(e => e.innerText || e.textContent || '')"
        )
    except Exception:
        return []
    return [re.sub(r"\s+", " ", t).strip() for t in texts or [] if t and t.strip()]


def _page_text(page: Any) -> str:
    try:
        return page.inner_text("body", timeout=2000)
    except Exception:
        return ""
