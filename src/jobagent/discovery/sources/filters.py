"""Prefilters a source applies before handing a posting back.

A company board lists every open role; scoring is for judging fit, not for
throwing away the sales and legal postings from an engineering search. These
keep the cheap, obvious rejects out of the database.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from jobagent.discovery.criteria import SearchCriteria


def _words(text: str) -> list[str]:
    # A dot inside a token is part of a name (node.js); at the edges it is punctuation
    # ('Engineer.', 'Sr.', '.NET' -> 'net'), so strip it after tokenising.
    tokens = (t.strip(".") for t in re.findall(r"[a-z0-9+#.]+", text.lower()))
    return [t for t in tokens if t]


def title_matches(title: str, criteria: SearchCriteria) -> bool:
    """True when the posting's title shares every word of at least one wanted title.

    'Software Engineer' matches 'Senior Software Engineer, Payments' and not
    'Software Sales'. With no wanted titles configured, everything matches.
    """
    wanted = criteria.normalised(criteria.titles)
    if not wanted:
        return True
    have = set(_words(str(title or "")))
    return any(set(_words(w)) <= have for w in wanted if _words(w))


def parse_when(value: str | int | float | None) -> datetime | None:
    """Accept ISO 8601 strings and epoch seconds or milliseconds."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        seconds = value / 1000 if value > 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=UTC)
        except (ValueError, OverflowError, OSError):
            # NaN from a pandas frame, or a number that is not a date at all.
            return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def within_age(
    posted: str | int | float | None, criteria: SearchCriteria, now: datetime | None = None
) -> bool:
    """True when the posting is recent enough, or when its date is unknown."""
    when = parse_when(posted)
    if when is None:
        return True
    now = now or datetime.now(UTC)
    return when >= now - timedelta(hours=criteria.max_age_hours)
