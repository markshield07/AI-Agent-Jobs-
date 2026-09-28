"""The aggregator boards (Indeed, LinkedIn, ...) through python-jobspy.

jobspy scrapes the boards' public listings, so one (title, location) query is
one scrape per site and rate limits arrive fast: the query count is capped and
a failed query is skipped, not fatal. A Remote query sends Indeed its remote
filter in a search of its own (Indeed ignores it next to a max age) and keeps
what the boards' remote filters chose as remote. The library is imported inside `search`
so this module loads even where the package is missing. Named `jobspy_source`
so it does not shadow the installed `jobspy` package.

jobspy hands back a pandas DataFrame in which a missing value is NaN or NaT,
not None, so every row is turned into a plain dict of Nones first and the
field mapping never sees pandas.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

from jobagent.discovery.ats import detect_ats
from jobagent.discovery.criteria import SearchCriteria
from jobagent.discovery.models import RawJob

log = logging.getLogger(__name__)

INDEED_COUNTRY = "USA"

# jobspy's interval vocabulary is yearly/monthly/weekly/daily/hourly. When it
# could not tell, five-figure amounts are taken as yearly: an hourly rate
# never gets there, and a monthly one rarely does.
_YEARLY_FLOOR = 10_000


class JobSpySource:
    name = "jobspy"

    def __init__(
        self,
        max_queries: int = 6,
        scrape: Callable[..., Any] | None = None,
        window: Callable[[], int] | None = None,
        on_site_only: bool = False,
    ) -> None:
        self.max_queries = max_queries
        # Only jobs that apply on the board's own form: LinkedIn's Easy Apply
        # and Indeed Apply (JOBAGENT_APPLY_SITES=linkedin,indeed).
        self.on_site_only = on_site_only
        self._scrape = scrape
        # Which slice of the queries this run takes when there are more than
        # the cap: the hour since the epoch by default, so hourly runs walk
        # through every title and place in turn instead of repeating the first.
        self._window = window or (lambda: int(time.time() // 3600))

    def search(self, criteria: SearchCriteria) -> Iterator[RawJob]:
        if not criteria.jobspy_sites or not criteria.titles:
            return
        scrape = self._scrape or _import_scrape()
        if scrape is None:
            return

        seen: set[str] = set()
        for title, location in _queries(criteria, self.max_queries, self._window()):
            for call, board_remote in _calls(criteria, title, location, self.on_site_only):
                try:
                    rows = _records(scrape(**call))
                except ImportError as exc:
                    log.warning("jobspy: python-jobspy cannot be used, skipping it: %s", exc)
                    return
                except Exception as exc:
                    log.warning("jobspy: query %r in %r failed: %s", title, location, exc)
                    continue
                if call["hours_old"] is None:
                    rows = [r for r in rows if _fresh(r.get("date_posted"), criteria.max_age_hours)]
                usual = None if board_remote or not location else _usual_state(rows)

                for row in rows:
                    job = _to_job(row)
                    if job is None:
                        log.debug("jobspy: skipping a row without url, title or company: %r", row)
                        continue
                    if job.url in seen:
                        continue
                    seen.add(job.url)
                    if board_remote:
                        # The board's own remote filter chose it. jobspy's is_remote
                        # column only says "remote" appears somewhere in the text.
                        job.remote = True
                    elif location and _state(job.location) in (None, usual):
                        # Within the board's radius of the place searched; a posting
                        # in another state than the rest is a board padding its list.
                        job.found_near = location
                    yield job


def _calls(
    criteria: SearchCriteria, title: str, location: str | None, on_site_only: bool = False
) -> list[tuple[dict[str, Any], bool]]:
    """The scrape_jobs calls for one query, each with whether the boards'
    remote filter applies to it.

    Indeed takes one filter per search: given a max age, it drops the remote
    filter and returns every job in the country. So a Remote query asks
    Indeed on its own with the remote filter and no age, and the age is
    checked here; the other boards take both at once.

    `on_site_only` searches LinkedIn and Indeed alone, for jobs that apply on
    their own forms: LinkedIn's Easy Apply filter goes with the others, and a
    place query asks Indeed for Indeed Apply jobs, again with no age. Indeed
    cannot add that filter to its remote one, so a Remote query stays as it
    was there and the posting itself shows how it applies."""
    sites = list(criteria.jobspy_sites)
    if on_site_only:
        sites = [site for site in sites if site in ("linkedin", "indeed")]
        if not sites:
            return []
    remote = location is not None and location.lower() == "remote"
    base = {
        "search_term": title,
        "location": None if remote else location,
        "is_remote": remote,
        "results_wanted": criteria.results_per_query,
        "hours_old": criteria.max_age_hours,
        "country_indeed": INDEED_COUNTRY,
    }
    if on_site_only:
        calls: list[tuple[dict[str, Any], bool]] = []
        if "indeed" in sites:
            indeed = {"site_name": ["indeed"], **base, "hours_old": None}
            calls.append((indeed, True) if remote else ({**indeed, "easy_apply": True}, False))
        if "linkedin" in sites:
            calls.append(({"site_name": ["linkedin"], **base, "easy_apply": True}, remote))
        return calls
    if not (remote and "indeed" in sites):
        return [({"site_name": sites, **base}, remote)]
    calls = [({"site_name": ["indeed"], **base, "hours_old": None}, True)]
    others = [site for site in sites if site != "indeed"]
    if others:
        calls.append(({"site_name": others, **base}, True))
    return calls


_STATES = {
    "alabama": "AL",
    "alaska": "AK",
    "arizona": "AZ",
    "arkansas": "AR",
    "california": "CA",
    "colorado": "CO",
    "connecticut": "CT",
    "delaware": "DE",
    "florida": "FL",
    "georgia": "GA",
    "hawaii": "HI",
    "idaho": "ID",
    "illinois": "IL",
    "indiana": "IN",
    "iowa": "IA",
    "kansas": "KS",
    "kentucky": "KY",
    "louisiana": "LA",
    "maine": "ME",
    "maryland": "MD",
    "massachusetts": "MA",
    "michigan": "MI",
    "minnesota": "MN",
    "mississippi": "MS",
    "missouri": "MO",
    "montana": "MT",
    "nebraska": "NE",
    "nevada": "NV",
    "new hampshire": "NH",
    "new jersey": "NJ",
    "new mexico": "NM",
    "new york": "NY",
    "north carolina": "NC",
    "north dakota": "ND",
    "ohio": "OH",
    "oklahoma": "OK",
    "oregon": "OR",
    "pennsylvania": "PA",
    "rhode island": "RI",
    "south carolina": "SC",
    "south dakota": "SD",
    "tennessee": "TN",
    "texas": "TX",
    "utah": "UT",
    "vermont": "VT",
    "virginia": "VA",
    "washington": "WA",
    "west virginia": "WV",
    "wisconsin": "WI",
    "wyoming": "WY",
    "district of columbia": "DC",
}


def _state(location: str | None) -> str | None:
    """The US state a posting's location names ("Irvine, CA, US" -> "CA"), if any."""
    for part in (location or "").split(",")[1:]:
        part = part.strip()
        if len(part) == 2 and part.isalpha() and part.isupper() and part in _STATES.values():
            return part
        if part.lower() in _STATES:
            return _STATES[part.lower()]
    return None


def _usual_state(rows: list[dict[str, Any]]) -> str | None:
    """The state most of one search's results are in."""
    counts: dict[str, int] = {}
    for row in rows:
        state = _state(_text(row.get("location")))
        if state:
            counts[state] = counts.get(state, 0) + 1
    return max(counts, key=lambda k: counts[k]) if counts else None


def _fresh(posted: Any, max_age_hours: int | None) -> bool:
    """Whether a posting is within the age wanted; one with no date is kept."""
    if not max_age_hours or posted is None:
        return True
    if isinstance(posted, str):
        try:
            posted = datetime.fromisoformat(posted)
        except ValueError:
            return True
    now = datetime.now(UTC)
    if isinstance(posted, datetime):
        stamp = posted if posted.tzinfo else posted.replace(tzinfo=UTC)
        return stamp >= now - timedelta(hours=max_age_hours)
    if isinstance(posted, date):
        # A day only: kept if any part of that day falls inside the window.
        return posted >= (now - timedelta(hours=max_age_hours)).date()
    return True


def _import_scrape() -> Callable[..., Any] | None:
    try:
        from jobspy import scrape_jobs
    except ImportError as exc:
        log.warning("jobspy: python-jobspy is not installed, skipping it: %s", exc)
        return None
    return scrape_jobs


def _queries(
    criteria: SearchCriteria, max_queries: int, window: int = 0
) -> list[tuple[str, str | None]]:
    """Every title x location pair, first spelling of a repeat kept, capped.

    Over the cap, run `window` takes the next `max_queries` pairs after the
    previous run's, wrapping around, so no title or place is left out for good.
    """
    titles = _distinct(criteria.titles)
    locations = _distinct(criteria.locations) or [None]
    queries: list[tuple[str, str | None]] = [
        (title, location) for title in titles for location in locations
    ]
    if len(queries) > max_queries:
        log.warning(
            "jobspy: %d of %d queries dropped to stay under the cap of %d",
            len(queries) - max_queries,
            len(queries),
            max_queries,
        )
        start = (window * max_queries) % len(queries)
        queries = (queries + queries)[start : start + max_queries]
    return queries


def _distinct(values: list[str]) -> list[str]:
    kept: dict[str, str] = {}
    for value in values:
        cleaned = value.strip() if isinstance(value, str) else ""
        if cleaned:
            kept.setdefault(cleaned.lower(), cleaned)
    return list(kept.values())


def _records(frame: Any) -> list[dict[str, Any]]:
    """The DataFrame's rows as plain dicts, every NaN and NaT turned into None."""
    import pandas as pd

    if not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    return [
        {key: _scalar(value, pd.isna) for key, value in record.items()}
        for record in frame.to_dict("records")
    ]


def _scalar(value: Any, isna: Callable[[Any], Any]) -> Any:
    try:
        return None if bool(isna(value)) else value
    except (TypeError, ValueError):
        return value


def _to_job(row: dict[str, Any]) -> RawJob | None:
    url = _text(row.get("job_url"))
    title = _text(row.get("title"))
    company = _text(row.get("company"))
    if not (url and title and company):
        return None

    apply_url = _text(row.get("job_url_direct")) or url
    salary_min, salary_max = _salary(
        row.get("interval"), row.get("min_amount"), row.get("max_amount")
    )
    external_id = row.get("id")
    return RawJob(
        url=url,
        title=title,
        company=company,
        source=_site(row.get("site")),
        location=_text(row.get("location")),
        description=_text(row.get("description")),
        apply_url=apply_url,
        salary_min=salary_min,
        salary_max=salary_max,
        posted_at=_when(row.get("date_posted")),
        remote=_flag(row.get("is_remote")),
        external_id=str(external_id) if external_id is not None else None,
        ats_type=detect_ats(apply_url),
    )


def _text(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _site(value: Any) -> str:
    name = _text(getattr(value, "value", value))
    return name.lower() if name else JobSpySource.name


def _when(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return _text(str(value))


def _flag(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float):
        return bool(value)
    if isinstance(value, str):
        word = value.strip().lower()
        if word in ("true", "yes", "1"):
            return True
        if word in ("false", "no", "0"):
            return False
    return None


def _salary(interval: Any, low: Any, high: Any) -> tuple[int | None, int | None]:
    """A yearly range as ints; anything paid by the hour, day, week or month is dropped."""
    kind = _text(str(interval)) if interval is not None else None
    if kind is not None and kind.lower() != "yearly":
        return None, None
    amounts = (_amount(low), _amount(high))
    if kind is None and any(a is not None and a < _YEARLY_FLOOR for a in amounts):
        return None, None
    return amounts


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
