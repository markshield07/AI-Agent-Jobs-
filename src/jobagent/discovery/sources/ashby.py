"""Ashby's public job board API: one no-auth JSON call per company board.

`api.ashbyhq.com/posting-api/job-board/{slug}` returns every listed role with
its full description, so a board costs one request and no per-posting fetch.
Asked with `includeCompensation=true` it adds a pay summary, whose shape is
only partly documented and often absent: only a yearly salary component
becomes salary_min/max, so an hourly range is dropped rather than stored as
if it were annual. Ashby has no company endpoint, so the display name is the
board's `company` or the slug title-cased. Every field is read defensively
and a posting missing its title or URL is skipped rather than raised on.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

import httpx

from jobagent.discovery.criteria import SearchCriteria
from jobagent.discovery.http import get_json, make_client
from jobagent.discovery.models import RawJob
from jobagent.discovery.sources.filters import title_matches, within_age
from jobagent.discovery.text import html_to_text

log = logging.getLogger(__name__)

BOARD_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"


class AshbySource:
    name = "ashby"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or make_client()

    def search(self, criteria: SearchCriteria) -> Iterator[RawJob]:
        for board in criteria.boards:
            if board.ats != "ashby":
                continue
            company = board.company or _company_from_slug(board.slug)
            for posting in self._fetch_jobs(board.slug):
                try:
                    job = _to_job(posting, company, criteria)
                except Exception:
                    log.exception("ashby: skipping unreadable posting on board %r", board.slug)
                    continue
                if job is not None:
                    yield job

    def _fetch_jobs(self, slug: str) -> list[Any]:
        """The board's postings, or an empty list when the board cannot be read."""
        try:
            payload = get_json(self.client, BOARD_URL.format(slug=slug), includeCompensation="true")
        except (httpx.HTTPError, httpx.InvalidURL, ValueError) as exc:
            log.warning("ashby: board %r could not be read: %s", slug, exc)
            return []
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            log.warning("ashby: board %r returned no job list", slug)
            return []
        return jobs


def _company_from_slug(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title() or slug


def _to_job(posting: Any, company: str, criteria: SearchCriteria) -> RawJob | None:
    if not isinstance(posting, dict):
        log.warning("ashby: skipping non-object posting %r", posting)
        return None
    # Only an explicit False is an unlisted role; a missing flag is treated as listed.
    if posting.get("isListed") is False:
        return None
    title = _text(posting.get("title"))
    url = _text(posting.get("jobUrl"))
    if not title or not url:
        log.warning("ashby: skipping posting %r without title or jobUrl", posting.get("id"))
        return None
    if not title_matches(title, criteria):
        return None
    posted_at = _text(posting.get("publishedAt"))
    if not within_age(posted_at, criteria):
        return None

    remote = posting.get("isRemote")
    salary_min, salary_max = _salary(posting.get("compensation"))
    external_id = posting.get("id")

    return RawJob(
        url=url,
        title=title,
        company=company,
        source="ashby",
        location=_location(posting),
        description=_description(posting),
        apply_url=_text(posting.get("applyUrl")) or url,
        salary_min=salary_min,
        salary_max=salary_max,
        posted_at=posted_at,
        remote=remote if isinstance(remote, bool) else None,
        external_id=str(external_id) if external_id not in (None, "") else None,
        ats_type="ashby",
    )


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _location(posting: dict[str, Any]) -> str | None:
    names: list[str] = []
    primary = _text(posting.get("location"))
    if primary:
        names.append(primary)
    secondary = posting.get("secondaryLocations")
    for entry in secondary if isinstance(secondary, list) else []:
        name = _text(entry.get("location")) if isinstance(entry, dict) else None
        if name and name not in names:
            names.append(name)
    return "; ".join(names) or None


def _description(posting: dict[str, Any]) -> str | None:
    plain = _text(posting.get("descriptionPlain"))
    if plain:
        return plain
    markup = posting.get("descriptionHtml")
    # Empty text means "no description", which is what tells enrichment to fetch one.
    return (html_to_text(markup) or None) if isinstance(markup, str) else None


def _salary(compensation: Any) -> tuple[int | None, int | None]:
    if not isinstance(compensation, dict):
        return None, None
    components = compensation.get("summaryComponents")
    for component in components if isinstance(components, list) else []:
        if not isinstance(component, dict):
            continue
        kind, interval = component.get("compensationType"), component.get("interval")
        if not (isinstance(kind, str) and kind.strip().lower() == "salary"):
            continue
        if not (isinstance(interval, str) and "year" in interval.lower()):
            continue
        return _amount(component.get("minValue")), _amount(component.get("maxValue"))
    return None, None


def _amount(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return int(value)
