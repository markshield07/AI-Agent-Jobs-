"""Lever's public postings feed: one no-auth JSON call per company board.

Lever has no company endpoint, so the display name is the board's `company`
or the slug title-cased. `createdAt` is epoch milliseconds; `parse_when`
tells the unit apart by magnitude. The response shape is only partly
documented, so every field is read defensively and a posting missing its
title or URL is skipped rather than raised on.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import httpx

from jobagent.discovery.criteria import SearchCriteria
from jobagent.discovery.http import get_json, make_client
from jobagent.discovery.models import RawJob
from jobagent.discovery.sources.filters import parse_when, title_matches, within_age
from jobagent.discovery.text import html_to_text

log = logging.getLogger(__name__)

POSTINGS_URL = "https://api.lever.co/v0/postings/{slug}"

# Lever's interval vocabulary is not documented. These are the yearly spellings
# seen in the wild; an hourly or monthly range is dropped rather than stored as
# if it were an annual figure.
_YEARLY_INTERVALS = frozenset({"per-year-salary", "yearly", "year"})


class LeverSource:
    name = "lever"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or make_client()

    def search(self, criteria: SearchCriteria) -> Iterator[RawJob]:
        for board in criteria.boards:
            if board.ats != "lever":
                continue
            try:
                postings = get_json(self.client, POSTINGS_URL.format(slug=board.slug), mode="json")
            except Exception:
                log.exception("lever: board %r could not be fetched", board.slug)
                continue
            if not isinstance(postings, list):
                log.warning("lever: board %r returned %s, not a list", board.slug, type(postings))
                continue

            company = board.company or _company_from_slug(board.slug)
            for posting in postings:
                try:
                    job = _to_job(posting, company, criteria)
                except Exception:
                    log.exception("lever: skipping unreadable posting on board %r", board.slug)
                    continue
                if job is not None:
                    yield job


def _company_from_slug(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title() or slug


def _to_job(posting: Any, company: str, criteria: SearchCriteria) -> RawJob | None:
    if not isinstance(posting, dict):
        log.warning("lever: skipping non-object posting %r", posting)
        return None
    title = _text(posting.get("text"))
    url = _text(posting.get("hostedUrl"))
    if not title or not url:
        log.warning("lever: skipping posting %r without title or hostedUrl", posting.get("id"))
        return None
    if not title_matches(title, criteria):
        return None

    created = posting.get("createdAt")
    if not isinstance(created, int | float | str):
        created = None
    if not within_age(created, criteria):
        return None
    when = parse_when(created)

    location = _location(posting.get("categories"))
    salary_min, salary_max = _salary(posting.get("salaryRange"))
    external_id = posting.get("id")

    return RawJob(
        url=url,
        title=title,
        company=company,
        source="lever",
        location=location,
        description=_description(posting),
        apply_url=_text(posting.get("applyUrl")) or url,
        salary_min=salary_min,
        salary_max=salary_max,
        posted_at=when.isoformat() if when else None,
        remote=_remote(posting.get("workplaceType"), location),
        external_id=str(external_id) if external_id not in (None, "") else None,
        ats_type="lever",
    )


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _location(categories: Any) -> str | None:
    if not isinstance(categories, dict):
        return None
    location = _text(categories.get("location"))
    if location:
        return location
    everywhere = categories.get("allLocations")
    if isinstance(everywhere, list):
        names = [name.strip() for name in everywhere if isinstance(name, str) and name.strip()]
        if names:
            return ", ".join(names)
    return None


def _remote(workplace: Any, location: str | None) -> bool | None:
    kind = workplace.strip().lower() if isinstance(workplace, str) else ""
    if kind == "remote" or (location and "remote" in location.lower()):
        return True
    if kind in ("onsite", "hybrid"):
        return False
    return None


def _salary(salary_range: Any) -> tuple[int | None, int | None]:
    if not isinstance(salary_range, dict):
        return None, None
    interval = salary_range.get("interval")
    if interval and str(interval).strip().lower() not in _YEARLY_INTERVALS:
        return None, None
    return _amount(salary_range.get("min")), _amount(salary_range.get("max"))


def _amount(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value.replace(",", "")))
        except ValueError:
            return None
    return None


def _description(posting: dict[str, Any]) -> str | None:
    parts: list[str] = []
    plain = _text(posting.get("descriptionPlain"))
    parts.append(plain or html_to_text(posting.get("description")))

    lists = posting.get("lists")
    for item in lists if isinstance(lists, list) else []:
        if not isinstance(item, dict):
            continue
        heading = _text(item.get("text"))
        body = html_to_text(item.get("content"))
        if body:
            parts.append(f"{heading}\n{body}" if heading else body)

    parts.append(_text(posting.get("additionalPlain")) or "")
    joined = "\n\n".join(part for part in parts if part)
    return joined or None
