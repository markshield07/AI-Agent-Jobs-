"""Dice's own search, for the jobs that apply through Dice Easy Apply.

python-jobspy has no Dice scraper, so this reads Dice's search page,
dice.com/jobs, with the filters the site's own controls set:
`filters.easyApply=true` (only postings that apply on Dice),
`filters.employmentType=FULLTIME` (Dice's contract listings are mostly
staffing firms' corp-to-corp roles), `filters.postedDate` for the age wanted,
and `filters.workplaceTypes=Remote` for a Remote search or `location=` with a
radius for a place. Each result card gives the posting's id, title, company,
place and employment type; the description is read later, from the
posting's own page, by the enrichment stage.

The page is read over plain HTTP first. Dice draws its results with
Next.js, which may leave them out of the HTML sent; when no result card is
in it, the page is opened in a headless browser and read once drawn. Either
way it is one page per (title, place) query, capped like JobSpy's.

The URL parameters and the card markup come from the Dice bots consulted
(kristipatithoyajakshakashyap/DiceAutoApply `dice_bot.py`, thandava34/
auto-apply-dice-jobs `fetch_jobs_with_requests`, both September 2026, and
vivekshekharrai-del/AIApplyJobs `dice_scraper.py`); none was checked
against the live site from here.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from bs4 import BeautifulSoup, Tag

from jobagent.discovery.criteria import SearchCriteria
from jobagent.discovery.http import get_text, make_client
from jobagent.discovery.models import RawJob
from jobagent.discovery.sources.filters import title_matches
from jobagent.discovery.sources.jobspy_source import _queries

if TYPE_CHECKING:
    import httpx

log = logging.getLogger(__name__)

SEARCH_URL = "https://www.dice.com/jobs"
DETAIL_URL = "https://www.dice.com/job-detail/{}"
RADIUS_MILES = 30
_DETAIL = re.compile(r"/job-detail/([0-9a-zA-Z-]{8,})")
_VIEW_DETAILS = re.compile(r"^View Details for (.+?)(?: \([0-9a-f]{32}\))?$")
# Employment types a full-time search still lets through now and then.
_NOT_FULL_TIME = re.compile(r"contract|third party|corp.to.corp|c2c|part.time", re.IGNORECASE)


class DiceSource:
    name = "dice"

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        max_queries: int = 6,
        render: Callable[[str], str] | None = None,
        window: Callable[[], int] | None = None,
    ) -> None:
        self._client = client
        self.max_queries = max_queries
        self._render = render if render is not None else _render
        # The same hourly walk through the queries as JobSpy's.
        self._window = window or (lambda: int(time.time() // 3600))

    def search(self, criteria: SearchCriteria) -> Iterator[RawJob]:
        if not criteria.titles:
            return
        own = self._client is None
        client = self._client or make_client()
        seen: set[str] = set()
        try:
            for title, location in _queries(criteria, self.max_queries, self._window()):
                url = search_url(title, location, criteria)
                cards = self._cards(client, url)
                remote = (location or "").strip().lower() == "remote"
                for card in cards:
                    job = _to_job(card)
                    if job is None or job.url in seen:
                        continue
                    if card.get("employment") and _NOT_FULL_TIME.search(card["employment"]):
                        continue
                    if not title_matches(job.title, criteria):
                        continue
                    seen.add(job.url)
                    if remote:
                        job.remote = True
                    elif location:
                        job.found_near = location
                    yield job
        finally:
            if own:
                client.close()

    def _cards(self, client: httpx.Client, url: str) -> list[dict[str, str]]:
        try:
            cards = parse_results(get_text(client, url))
        except Exception as exc:
            log.info("dice: %s over HTTP failed (%s); opening it in a browser", url, exc)
            cards = []
        if cards:
            return cards
        try:
            return parse_results(self._render(url))
        except Exception as exc:
            log.warning("dice: could not read %s: %s", url, exc)
            return []


def search_url(title: str, location: str | None, criteria: SearchCriteria) -> str:
    """Dice's search page for one title and place, Easy Apply and full-time only."""
    params: dict[str, str | int] = {"q": title}
    remote = (location or "").strip().lower() == "remote"
    if location and not remote:
        params["location"] = location
        params["filters.radius"] = RADIUS_MILES
    params["filters.easyApply"] = "true"
    params["filters.employmentType"] = "FULLTIME"
    params["filters.postedDate"] = _posted(criteria.max_age_hours)
    if remote:
        params["filters.workplaceTypes"] = "Remote"
    params["pageSize"] = min(criteria.results_per_query, 100)
    return f"{SEARCH_URL}?{urlencode(params)}"


def _posted(hours: int) -> str:
    if hours <= 24:
        return "ONE"
    if hours <= 72:
        return "THREE"
    return "SEVEN"


def parse_results(html: str) -> list[dict[str, str]]:
    """The result cards on a Dice search page: id, title, company, place and
    employment type, each as text ("" when the card has none)."""
    soup = BeautifulSoup(html or "", "html.parser")
    nodes = soup.select("[data-testid='job-card']") or soup.select("[data-job-guid]")
    cards = [c for c in (_card(n) for n in nodes) if c]
    if cards:
        return cards
    # No cards as such: every link to a posting, by its own text.
    out: dict[str, dict[str, str]] = {}
    for link in soup.select("a[href*='/job-detail/']"):
        found = _DETAIL.search(str(link.get("href") or ""))
        title = _title(link)
        if found and title and found.group(1) not in out:
            out[found.group(1)] = {
                "id": found.group(1),
                "title": title,
                "company": "",
                "location": "",
                "employment": "",
            }
    return list(out.values())


def _card(node: Tag) -> dict[str, str] | None:
    guid = str(node.get("data-job-guid") or "")
    if not guid:
        for link in node.select("a[href*='/job-detail/']"):
            found = _DETAIL.search(str(link.get("href") or ""))
            if found:
                guid = found.group(1)
                break
    title_el = node.select_one("[data-testid='job-search-job-detail-link']")
    title = _title(title_el) if title_el else ""
    if not title:
        overlay = node.select_one("[data-testid='job-search-job-card-link']")
        title = _title(overlay) if overlay else ""
    if not guid or not title:
        return None
    company = node.select_one("[data-testid='job-card-company-name']") or node.select_one(
        "a[href*='company-profile'] p"
    )
    place = node.select_one("p.text-zinc-600")
    employment = node.select_one("#employmentType-label, [id='employmentType-label']")
    return {
        "id": guid,
        "title": title,
        "company": _text(company),
        "location": _text(place),
        "employment": _text(employment),
    }


def _title(el: Tag) -> str:
    text = _text(el)
    if text:
        return text
    label = str(el.get("aria-label") or "").strip()
    found = _VIEW_DETAILS.match(label)
    return found.group(1).strip() if found else label


def _text(el: Tag | None) -> str:
    if el is None:
        return ""
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def _to_job(card: dict[str, str]) -> RawJob | None:
    if not card.get("id") or not card.get("title"):
        return None
    url = DETAIL_URL.format(card["id"])
    location = card.get("location") or None
    return RawJob(
        url=url,
        title=card["title"],
        # A card with no company name still applies; the posting page names it.
        company=card.get("company") or "Unknown (Dice)",
        source="dice",
        location=location,
        apply_url=url,
        remote=True if location and re.search(r"\bremote\b", location, re.I) else None,
        external_id=card["id"],
        ats_type="dice",
        applies_on_board=True,
    )


def _render(url: str) -> str:
    """The search page as a browser draws it: headless, no account, once."""
    from playwright.sync_api import sync_playwright

    from jobagent.apply.browser.session import USER_AGENT, launch_options
    from jobagent.config import get_settings

    options = {**launch_options(get_settings()), "headless": True}
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**options)
        try:
            page = browser.new_page(user_agent=USER_AGENT, locale="en-US")
            page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            try:
                page.wait_for_selector(
                    "[data-testid='job-card'], a[href*='/job-detail/']", timeout=15_000
                )
            except Exception:
                pass  # no results, or a page that never drew them: read what is there
            return page.content()
        finally:
            browser.close()
