"""Remote-only job boards: We Work Remotely, Working Nomads and Himalayas.

Every one of them lists only remote jobs and publishes them for anyone to
read: We Work Remotely as RSS feeds, Working Nomads and Himalayas as public
JSON. None takes applications itself. A listing sends the applicant to the
employer, so a posting is kept only when that link leads to a form the agent
already fills (Greenhouse or Lever); anything else is dropped here, before it
costs a model call or a tailored resume.

Each board is read at most every few hours (`min_interval_hours`), which is
also what their terms ask for: Himalayas refreshes its data once a day. The
board's own page is the posting's URL, so every job keeps its link back to
where it was listed (the boards' one condition for using their feeds).

Remote.co was asked for too, but it publishes no feed or API (its listings sit
behind FlexJobs), so it is not here.
"""

from __future__ import annotations

import logging
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Iterator
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urljoin

import httpx

from jobagent.discovery.ats import detect_ats
from jobagent.discovery.criteria import SearchCriteria
from jobagent.discovery.http import get_json, get_text, make_client
from jobagent.discovery.models import RawJob
from jobagent.discovery.sources.filters import parse_when, title_matches, within_age
from jobagent.discovery.text import html_to_text

log = logging.getLogger(__name__)

# The boards here, by the name a job's `source` carries.
REMOTE_BOARDS = ("weworkremotely", "workingnomads", "himalayas")
REMOTE_ONLY_BOARDS = frozenset(REMOTE_BOARDS)
# Where an application can go: the ATS forms the agent fills that Mark agreed
# to (2026-11-17). An employer's own careers page or Workday is not one.
APPLY_ATS = frozenset({"greenhouse", "lever"})

# A listing restricted to somewhere the US is part of, or to nowhere at all.
_US_OK = re.compile(
    r"\b(?:us|usa|u\.s\.a?\.?|united states|america|americas|north america|anywhere"
    r"|worldwide|world|global)\b",
    re.I,
)

_HREF = re.compile(r"""href\s*=\s*["']([^"'#][^"']*)["']""", re.I)

_HTTP_ERRORS = (httpx.HTTPError, httpx.InvalidURL, ValueError)


def us_friendly(location: str | None) -> bool:
    """Whether someone in the US can take the job: no restriction, or one
    that names the US, North America, the Americas or anywhere."""
    text = (location or "").strip()
    return text.lower() in ("", "remote") or bool(_US_OK.search(text))


def resolve_apply_url(client: httpx.Client, url: str | None) -> str | None:
    """The Greenhouse or Lever form a board's apply link leads to, or None.

    A link that is already one is used as it is. Otherwise it is opened once:
    a redirect that lands on one counts (Working Nomads' "go" links), and so
    does the first link on the page to one (We Work Remotely's "Apply" button).
    """
    if not url:
        return None
    if detect_ats(url) in APPLY_ATS:
        return url
    try:
        response = client.get(url)
        response.raise_for_status()
    except _HTTP_ERRORS as exc:
        log.info("could not open %s to find where it applies: %s", url, exc)
        return None
    final = str(response.url)
    if detect_ats(final) in APPLY_ATS:
        return final
    for href in _HREF.findall(response.text or ""):
        link = urljoin(final, href.strip())
        if link.startswith("http") and detect_ats(link) in APPLY_ATS:
            return link
    return None


class _RemoteBoard:
    """What the three boards share: the client, the pause between reads, and
    turning a listing into a job only when it applies on a supported form."""

    name = "remote"
    min_interval_hours = 6.0

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or make_client()

    def _job(
        self,
        criteria: SearchCriteria,
        *,
        url: str,
        title: str,
        company: str,
        location: str | None,
        description: str | None,
        apply_link: str | None,
        posted_at: str | None,
        external_id: str | None = None,
        salary: tuple[int | None, int | None] = (None, None),
    ) -> RawJob | None:
        if not (url and title and company):
            return None
        if not title_matches(title, criteria) or not within_age(posted_at, criteria):
            return None
        if not us_friendly(location):
            log.debug("%s: %r is not open to the US (%s)", self.name, title, location)
            return None
        apply_url = resolve_apply_url(self.client, apply_link)
        if apply_url is None:
            log.info("%s: %r at %s applies somewhere unsupported", self.name, title, company)
            return None
        return RawJob(
            url=url,
            title=title,
            company=company,
            source=self.name,
            location=location or "Remote",
            description=description or None,
            apply_url=apply_url,
            salary_min=salary[0],
            salary_max=salary[1],
            posted_at=posted_at,
            remote=True,
            external_id=external_id,
            ats_type=detect_ats(apply_url),
        )


class WeWorkRemotelySource(_RemoteBoard):
    """We Work Remotely's category RSS feeds. An item's title is
    "Company: Job title"; its link is the listing, whose Apply button is
    the employer's form."""

    name = "weworkremotely"
    FEEDS = (
        "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
        "https://weworkremotely.com/categories/remote-management-and-finance-jobs.rss",
        "https://weworkremotely.com/categories/all-other-remote-jobs.rss",
    )

    def search(self, criteria: SearchCriteria) -> Iterator[RawJob]:
        seen: set[str] = set()
        for feed in self.FEEDS:
            try:
                items = parse_rss(get_text(self.client, feed))
            except (*_HTTP_ERRORS, ET.ParseError) as exc:
                log.warning("weworkremotely feed %s could not be read: %s", feed, exc)
                continue
            for item in items:
                link = item.get("link") or item.get("guid") or ""
                if not link or link in seen:
                    continue
                seen.add(link)
                company, _, title = (item.get("title") or "").partition(": ")
                if not title:
                    company, title = "", company
                job = self._job(
                    criteria,
                    url=link,
                    title=title.strip(),
                    company=company.strip(),
                    location=item.get("region"),
                    description=html_to_text(item.get("description") or ""),
                    apply_link=link,
                    posted_at=_rfc822(item.get("pubDate")),
                    external_id=item.get("guid"),
                )
                if job is not None:
                    yield job


class WorkingNomadsSource(_RemoteBoard):
    """Working Nomads' public job list: one JSON call with every listing.
    Its `url` is a "go" link that redirects to where the job applies."""

    name = "workingnomads"
    API = "https://www.workingnomads.com/api/exposed_jobs/"

    def search(self, criteria: SearchCriteria) -> Iterator[RawJob]:
        try:
            listings = get_json(self.client, self.API)
        except _HTTP_ERRORS as exc:
            log.warning("workingnomads could not be read: %s", exc)
            return
        for entry in listings if isinstance(listings, list) else []:
            if not isinstance(entry, dict):
                continue
            url = _str(entry.get("url"))
            job = self._job(
                criteria,
                url=url,
                title=_str(entry.get("title")),
                company=_str(entry.get("company_name")),
                location=_str(entry.get("location")) or None,
                description=html_to_text(_str(entry.get("description"))),
                apply_link=url,
                posted_at=_str(entry.get("pub_date")) or None,
            )
            if job is not None:
                yield job


class HimalayasSource(_RemoteBoard):
    """Himalayas' public search API, asked once per wanted title for jobs open
    to the US, newest first. `applicationLink` is where the job applies."""

    name = "himalayas"
    API = "https://himalayas.app/jobs/api/search"

    def search(self, criteria: SearchCriteria) -> Iterator[RawJob]:
        seen: set[str] = set()
        for query in _queries(criteria.titles):
            try:
                payload = get_json(self.client, self.API, q=query, country="US", sort="recent")
            except _HTTP_ERRORS as exc:
                log.warning("himalayas search %r failed: %s", query, exc)
                continue
            jobs = payload.get("jobs") if isinstance(payload, dict) else None
            for entry in jobs if isinstance(jobs, list) else []:
                if not isinstance(entry, dict):
                    continue
                guid = _str(entry.get("guid"))
                if not guid or guid in seen:
                    continue
                seen.add(guid)
                places = entry.get("locationRestrictions")
                names = [
                    p if isinstance(p, str) else _str((p or {}).get("name"))
                    for p in (places if isinstance(places, list) else [])
                ]
                job = self._job(
                    criteria,
                    url=guid,
                    title=_str(entry.get("title")),
                    company=_str(entry.get("companyName")),
                    location=", ".join(n for n in names if n) or None,
                    description=html_to_text(_str(entry.get("description"))),
                    apply_link=_str(entry.get("applicationLink")) or guid,
                    posted_at=_epoch(entry.get("pubDate")),
                    external_id=guid,
                    salary=_annual(entry),
                )
                if job is not None:
                    yield job


def remote_board_sources(
    names: Iterable[str], client: httpx.Client | None = None
) -> list[_RemoteBoard]:
    """The boards named (any of REMOTE_BOARDS), in that order, each once."""
    kinds = {
        "weworkremotely": WeWorkRemotelySource,
        "workingnomads": WorkingNomadsSource,
        "himalayas": HimalayasSource,
    }
    wanted = dict.fromkeys(n.strip().lower() for n in names)
    return [kinds[n](client) for n in wanted if n in kinds]


def parse_rss(text: str) -> list[dict[str, str]]:
    """Each <item> of an RSS 2.0 feed as {child tag: text}, namespaces dropped."""
    root = ET.fromstring(text)
    items: list[dict[str, str]] = []
    for item in root.iter("item"):
        fields: dict[str, str] = {}
        for child in item:
            tag = child.tag.rsplit("}", 1)[-1]
            if child.text and tag not in fields:
                fields[tag] = child.text.strip()
        items.append(fields)
    return items


def _queries(titles: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(t.strip() for t in titles if t and t.strip()))


def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _rfc822(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).isoformat()
    except (TypeError, ValueError):
        return None


def _epoch(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    when = parse_when(value)
    return when.isoformat() if when else None


def _annual(entry: dict[str, Any]) -> tuple[int | None, int | None]:
    if str(entry.get("salaryPeriod") or "").lower() not in ("annual", "yearly", "year"):
        return None, None
    if str(entry.get("currency") or "USD").upper() != "USD":
        return None, None

    def amount(value: Any) -> int | None:
        return (
            int(value) if isinstance(value, int | float) and not isinstance(value, bool) else None
        )

    return amount(entry.get("minSalary")), amount(entry.get("maxSalary"))
