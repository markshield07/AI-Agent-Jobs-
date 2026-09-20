"""Greenhouse's public job board API, one request per company board.

`boards-api.greenhouse.io` needs no auth and, asked with `content=true`,
returns every open role on a board together with its full description, so a
board costs one request and no per-posting fetch. The board's display name is
a second, optional request, made only when the criteria do not name the
company and only once a posting has actually passed the prefilters. A slug
the criteria list more than once is searched once.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any
from urllib.parse import quote

import httpx

from jobagent.discovery.criteria import SearchCriteria
from jobagent.discovery.http import get_json, make_client
from jobagent.discovery.models import RawJob
from jobagent.discovery.sources.filters import title_matches, within_age
from jobagent.discovery.text import html_to_text

log = logging.getLogger(__name__)

API_BASE = "https://boards-api.greenhouse.io/v1/boards"

# `InvalidURL` is not an `HTTPError`: a slug that cannot be made into a URL at
# all is a bad board, to be skipped like any other, not a crash for the source.
_BOARD_ERRORS = (httpx.HTTPError, httpx.InvalidURL, ValueError)


class GreenhouseSource:
    name = "greenhouse"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or make_client()

    def search(self, criteria: SearchCriteria) -> Iterator[RawJob]:
        seen: set[str] = set()
        for board in criteria.boards:
            if board.ats != "greenhouse":
                continue
            if board.slug in seen:
                log.debug("greenhouse board %r is listed again, skipping the repeat", board.slug)
                continue
            seen.add(board.slug)
            # A blank company is "not given", like None: it must fall through to the
            # board endpoint, since the store rejects every posting with a blank company.
            company = (board.company or "").strip() or None
            for entry in self._fetch_jobs(board.slug):
                if not _usable(entry):
                    log.debug("greenhouse board %r: posting without title/url", board.slug)
                    continue
                if not title_matches(entry["title"], criteria):
                    continue
                if not within_age(_posted_at(entry), criteria):
                    continue
                if company is None:
                    company = self._fetch_board_name(board.slug) or _slug_name(board.slug)
                yield _to_raw(entry, company)

    def _fetch_jobs(self, slug: str) -> list[Any]:
        """The board's postings, or an empty list when the board cannot be read."""
        try:
            payload = get_json(self.client, _board_url(slug, "jobs"), content="true")
        except _BOARD_ERRORS as exc:
            log.warning("greenhouse board %r could not be read: %s", slug, exc)
            return []
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            log.warning("greenhouse board %r returned no job list", slug)
            return []
        return jobs

    def _fetch_board_name(self, slug: str) -> str | None:
        try:
            payload = get_json(self.client, _board_url(slug))
        except _BOARD_ERRORS as exc:
            log.info("greenhouse board %r has no readable name, using the slug: %s", slug, exc)
            return None
        name = payload.get("name") if isinstance(payload, dict) else None
        return name.strip() if isinstance(name, str) and name.strip() else None


def _board_url(slug: str, *parts: str) -> str:
    # Quoted so a slug holding '/', '?' or '#' still names a board under
    # /v1/boards/ instead of steering the request elsewhere on the host.
    return "/".join((API_BASE, quote(slug, safe=""), *parts))


def _usable(entry: Any) -> bool:
    if not isinstance(entry, dict):
        return False
    title, url = entry.get("title"), entry.get("absolute_url")
    return bool(isinstance(title, str) and title.strip() and isinstance(url, str) and url.strip())


def _posted_at(entry: dict[str, Any]) -> str | None:
    posted = entry.get("first_published") or entry.get("updated_at")
    return posted if isinstance(posted, str) and posted else None


def _slug_name(slug: str) -> str:
    return slug.replace("-", " ").replace("_", " ").strip().title() or slug


def _to_raw(entry: dict[str, Any], company: str) -> RawJob:
    url = entry["absolute_url"].strip()
    location = entry.get("location")
    location_name = location.get("name") if isinstance(location, dict) else None
    if not isinstance(location_name, str) or not location_name.strip():
        location_name = None
    content = entry.get("content")
    # Empty text means "no description", which is what tells enrichment to fetch one.
    description = html_to_text(content) or None if isinstance(content, str) else None
    job_id = entry.get("id")
    return RawJob(
        url=url,
        title=entry["title"].strip(),
        company=company,
        source="greenhouse",
        location=location_name,
        description=description,
        apply_url=url,
        posted_at=_posted_at(entry),
        remote=True if location_name and "remote" in location_name.lower() else None,
        external_id=str(job_id) if job_id is not None else None,
        ats_type="greenhouse",
    )
