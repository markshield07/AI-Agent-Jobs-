"""The aggregator boards (Indeed, LinkedIn, ...) through python-jobspy.

jobspy scrapes the boards' public listings, so one (title, location) query is
one scrape per site and rate limits arrive fast: the query count is capped and
a failed query is skipped, not fatal. The library is imported inside `search`
so this module loads even where the package is missing. Named `jobspy_source`
so it does not shadow the installed `jobspy` package.

jobspy hands back a pandas DataFrame in which a missing value is NaN or NaT,
not None, so every row is turned into a plain dict of Nones first and the
field mapping never sees pandas.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
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

    def __init__(self, max_queries: int = 6, scrape: Callable[..., Any] | None = None) -> None:
        self.max_queries = max_queries
        self._scrape = scrape

    def search(self, criteria: SearchCriteria) -> Iterator[RawJob]:
        if not criteria.jobspy_sites or not criteria.titles:
            return
        scrape = self._scrape or _import_scrape()
        if scrape is None:
            return

        seen: set[str] = set()
        for title, location in _queries(criteria, self.max_queries):
            remote = location is not None and location.lower() == "remote"
            try:
                frame = scrape(
                    site_name=list(criteria.jobspy_sites),
                    search_term=title,
                    location=None if remote else location,
                    is_remote=remote,
                    results_wanted=criteria.results_per_query,
                    hours_old=criteria.max_age_hours,
                    country_indeed=INDEED_COUNTRY,
                )
                rows = _records(frame)
            except ImportError as exc:
                log.warning("jobspy: python-jobspy cannot be used, skipping it: %s", exc)
                return
            except Exception as exc:
                log.warning("jobspy: query %r in %r failed: %s", title, location, exc)
                continue

            for row in rows:
                job = _to_job(row)
                if job is None:
                    log.debug("jobspy: skipping a row without url, title or company: %r", row)
                    continue
                if job.url in seen:
                    continue
                seen.add(job.url)
                yield job


def _import_scrape() -> Callable[..., Any] | None:
    try:
        from jobspy import scrape_jobs
    except ImportError as exc:
        log.warning("jobspy: python-jobspy is not installed, skipping it: %s", exc)
        return None
    return scrape_jobs


def _queries(criteria: SearchCriteria, max_queries: int) -> list[tuple[str, str | None]]:
    """Every title x location pair, first spelling of a repeat kept, capped."""
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
        queries = queries[:max_queries]
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
