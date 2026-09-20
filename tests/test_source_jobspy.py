from __future__ import annotations

import datetime
import logging
from typing import Any

import pandas as pd
import pytest

from jobagent.discovery.criteria import SearchCriteria
from jobagent.discovery.sources.jobspy_source import JobSpySource

NAN = float("nan")
LOGGER = "jobagent.discovery.sources.jobspy_source"


def row(**overrides: Any) -> dict[str, Any]:
    """One DataFrame row the way jobspy fills it for an Indeed posting."""
    base: dict[str, Any] = {
        "id": "in-abc123",
        "site": "indeed",
        "job_url": "https://www.indeed.com/viewjob?jk=abc123",
        "job_url_direct": "https://boards.greenhouse.io/acme/jobs/4021",
        "title": "Senior Software Engineer",
        "company": "Acme",
        "location": "Austin, TX, US",
        "date_posted": datetime.date(2026, 9, 19),
        "job_type": "fulltime",
        "salary_source": "direct_data",
        "interval": "yearly",
        "min_amount": 150000.0,
        "max_amount": 190000.0,
        "currency": "USD",
        "is_remote": False,
        "description": "We build **things**.\n\n- Python\n- SQL",
    }
    base.update(overrides)
    return base


def frame(*rows: dict[str, Any]) -> pd.DataFrame:
    return pd.DataFrame(list(rows))


class Scraper:
    """A stand-in for jobspy.scrape_jobs.

    Records the keyword arguments of every call and answers the calls in order
    from `answers`: a DataFrame is returned, an exception instance is raised.
    Once the answers run out, every further call gets an empty DataFrame.
    """

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> pd.DataFrame:
        self.calls.append(kwargs)
        answer = self.answers.pop(0) if self.answers else pd.DataFrame()
        if isinstance(answer, BaseException):
            raise answer
        return answer


def criteria(**overrides: Any) -> SearchCriteria:
    values: dict[str, Any] = {
        "titles": ["Software Engineer"],
        "locations": ["Austin, TX"],
        "jobspy_sites": ["indeed", "linkedin"],
    }
    values.update(overrides)
    return SearchCriteria(**values)


def search(scraper: Scraper, crit: SearchCriteria | None = None, **kwargs: Any) -> list:
    return list(JobSpySource(scrape=scraper, **kwargs).search(crit or criteria()))


def test_maps_every_field_and_passes_the_criteria_to_scrape_jobs():
    scraper = Scraper(frame(row()))
    crit = criteria(max_age_hours=48, results_per_query=25)

    jobs = search(scraper, crit)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.url == "https://www.indeed.com/viewjob?jk=abc123"
    assert job.apply_url == "https://boards.greenhouse.io/acme/jobs/4021"
    assert job.title == "Senior Software Engineer"
    assert job.company == "Acme"
    assert job.source == "indeed"
    assert job.location == "Austin, TX, US"
    assert job.description == "We build **things**.\n\n- Python\n- SQL"
    assert (job.salary_min, job.salary_max) == (150000, 190000)
    assert type(job.salary_min) is int and type(job.salary_max) is int
    assert job.posted_at == "2026-09-19"
    assert job.remote is False
    assert job.external_id == "in-abc123"
    assert job.ats_type == "greenhouse", "the ATS is read off the direct link, not the board"

    assert scraper.calls == [
        {
            "site_name": ["indeed", "linkedin"],
            "search_term": "Software Engineer",
            "location": "Austin, TX",
            "is_remote": False,
            "results_wanted": 25,
            "hours_old": 48,
            "country_indeed": "USA",
        }
    ]


def test_nan_cells_are_treated_as_missing():
    scraper = Scraper(
        frame(
            row(
                id=NAN,
                job_url_direct=NAN,
                location=NAN,
                description=NAN,
                date_posted=pd.NaT,
                interval=NAN,
                min_amount=NAN,
                max_amount=NAN,
                is_remote=NAN,
            )
        )
    )

    [job] = search(scraper)

    assert job.external_id is None
    assert job.apply_url == job.url
    assert job.ats_type == "indeed"
    assert job.location is None
    assert job.description is None
    assert job.posted_at is None
    assert (job.salary_min, job.salary_max) == (None, None)
    assert job.remote is None


def test_blank_text_and_none_cells_are_missing_too():
    scraper = Scraper(frame(row(description="   ", location="", job_url_direct=None)))

    [job] = search(scraper)

    assert job.description is None
    assert job.location is None
    assert job.apply_url == job.url


def test_columns_jobspy_did_not_produce_are_optional():
    scraper = Scraper(
        frame(
            {
                "site": "linkedin",
                "job_url": "https://www.linkedin.com/jobs/view/1",
                "title": "Engineer",
                "company": "Acme",
            }
        )
    )

    [job] = search(scraper)

    assert job.source == "linkedin"
    assert job.apply_url == job.url
    assert job.ats_type == "linkedin"
    assert job.external_id is None
    assert job.posted_at is None
    assert job.remote is None
    assert (job.salary_min, job.salary_max) == (None, None)


def test_rows_without_url_title_or_company_are_skipped():
    scraper = Scraper(
        frame(
            row(job_url=NAN, id="no-url"),
            row(job_url="  ", id="blank-url"),
            row(title=NAN, id="no-title"),
            row(title="", id="blank-title"),
            row(company=NAN, id="no-company"),
            row(company="   ", id="blank-company"),
            row(company=42, id="odd-company"),
            row(id="good", job_url="https://www.indeed.com/viewjob?jk=good"),
        )
    )

    jobs = search(scraper)

    assert [job.external_id for job in jobs] == ["good"]


@pytest.mark.parametrize("spelling", ["Remote", "remote", "REMOTE", "  remote "])
def test_a_remote_location_becomes_the_is_remote_flag(spelling):
    scraper = Scraper()

    search(scraper, criteria(locations=[spelling]))

    [call] = scraper.calls
    assert call["location"] is None
    assert call["is_remote"] is True


def test_other_locations_are_passed_through_as_text():
    scraper = Scraper()

    search(scraper, criteria(locations=["Remote", "New York, NY"]))

    assert [(c["search_term"], c["location"], c["is_remote"]) for c in scraper.calls] == [
        ("Software Engineer", None, True),
        ("Software Engineer", "New York, NY", False),
    ]


def test_queries_are_every_title_by_every_location():
    scraper = Scraper()
    crit = criteria(
        titles=["Backend Engineer", "Platform Engineer"], locations=["Remote", "Austin"]
    )

    search(scraper, crit)

    assert [(c["search_term"], c["location"]) for c in scraper.calls] == [
        ("Backend Engineer", None),
        ("Backend Engineer", "Austin"),
        ("Platform Engineer", None),
        ("Platform Engineer", "Austin"),
    ]


def test_repeated_and_blank_titles_and_locations_do_not_spend_queries():
    scraper = Scraper()
    crit = criteria(
        titles=["Backend Engineer", "backend engineer", "  ", ""],
        locations=["Remote", "remote", "Austin", " austin "],
    )

    search(scraper, crit)

    assert [(c["search_term"], c["location"]) for c in scraper.calls] == [
        ("Backend Engineer", None),
        ("Backend Engineer", "Austin"),
    ]


def test_no_locations_means_one_unlocated_query_per_title():
    scraper = Scraper()

    search(scraper, criteria(locations=[]))

    [call] = scraper.calls
    assert call["location"] is None
    assert call["is_remote"] is False


def test_the_query_cap_drops_the_tail_and_says_how_many(caplog):
    scraper = Scraper()
    crit = criteria(titles=["A", "B", "C"], locations=["X", "Y", "Z"])

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        search(scraper, crit, max_queries=4)

    assert [(c["search_term"], c["location"]) for c in scraper.calls] == [
        ("A", "X"),
        ("A", "Y"),
        ("A", "Z"),
        ("B", "X"),
    ]
    messages = [r.getMessage() for r in caplog.records]
    assert any("5 of 9 queries dropped" in m for m in messages), messages


def test_the_default_cap_is_six(caplog):
    scraper = Scraper()
    crit = criteria(titles=["A", "B", "C", "D"], locations=["X", "Y"])

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        search(scraper, crit)

    assert len(scraper.calls) == 6
    assert any("2 of 8 queries dropped" in r.getMessage() for r in caplog.records)


def test_nothing_is_logged_when_the_cap_is_not_hit(caplog):
    scraper = Scraper()

    with caplog.at_level(logging.DEBUG, logger=LOGGER):
        search(scraper, criteria(titles=["A", "B"], locations=["X"]))

    assert len(scraper.calls) == 2
    assert not any("dropped" in r.getMessage() for r in caplog.records)


def test_a_failing_query_is_logged_and_the_others_still_run(caplog):
    scraper = Scraper(
        RuntimeError("429 Too Many Requests"),
        frame(row(id="second", job_url="https://www.indeed.com/viewjob?jk=second")),
        ValueError("garbled page"),
        frame(row(id="fourth", job_url="https://www.indeed.com/viewjob?jk=fourth")),
    )
    crit = criteria(titles=["A", "B"], locations=["X", "Y"])

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        jobs = search(scraper, crit)

    assert [job.external_id for job in jobs] == ["second", "fourth"]
    assert len(scraper.calls) == 4
    failures = [r.getMessage() for r in caplog.records if "failed" in r.getMessage()]
    assert len(failures) == 2
    assert "429 Too Many Requests" in failures[0] and "'A'" in failures[0]
    assert "garbled page" in failures[1]


def test_a_result_that_is_not_a_dataframe_yields_nothing_without_raising():
    scraper = Scraper(None, "not a frame", pd.DataFrame(), frame(row()))
    crit = criteria(titles=["A", "B"], locations=["X", "Y"])

    jobs = search(scraper, crit)

    assert [job.external_id for job in jobs] == ["in-abc123"]


def test_the_same_url_is_yielded_once_per_search():
    same = "https://www.indeed.com/viewjob?jk=same"
    scraper = Scraper(
        frame(
            row(id="1", job_url=same, title="Software Engineer"),
            row(id="2", job_url=same, title="Software Engineer II"),
            row(id="3", job_url="https://www.indeed.com/viewjob?jk=other"),
        ),
        frame(row(id="4", job_url=same, site="linkedin")),
    )
    crit = criteria(locations=["Remote", "Austin"])

    jobs = search(scraper, crit)

    assert [job.external_id for job in jobs] == ["1", "3"]
    assert jobs[0].title == "Software Engineer", "the first sighting of a url wins"

    again = list(JobSpySource(scrape=Scraper(frame(row(job_url=same)))).search(criteria()))
    assert len(again) == 1, "dedupe is per search() call, not per source"


@pytest.mark.parametrize(
    ("interval", "low", "high", "expected"),
    [
        ("yearly", 150000.0, 190000.0, (150000, 190000)),
        ("Yearly", 150000.0, NAN, (150000, None)),
        ("yearly", NAN, 190000.0, (None, 190000)),
        ("yearly", NAN, NAN, (None, None)),
        ("yearly", "120,000", "150000.0", (120000, 150000)),
        ("hourly", 40.0, 60.0, (None, None)),
        ("monthly", 12000.0, 15000.0, (None, None)),
        ("weekly", 3000.0, 4000.0, (None, None)),
        ("daily", 600.0, 800.0, (None, None)),
        (NAN, 120000.0, 150000.0, (120000, 150000)),
        (None, 120000.0, 150000.0, (120000, 150000)),
        (NAN, 10000.0, 20000.0, (10000, 20000)),
        (NAN, 40.0, 60.0, (None, None)),
        (NAN, 9999.0, 150000.0, (None, None)),
        (NAN, NAN, 150000.0, (None, 150000)),
        (NAN, NAN, NAN, (None, None)),
        ("yearly", True, 190000.0, (None, 190000)),
        ("yearly", "lots", 190000.0, (None, 190000)),
    ],
)
def test_salary_keeps_yearly_ranges_only(interval, low, high, expected):
    scraper = Scraper(frame(row(interval=interval, min_amount=low, max_amount=high)))

    [job] = search(scraper)

    assert (job.salary_min, job.salary_max) == expected
    assert all(value is None or type(value) is int for value in expected)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (True, True),
        (False, False),
        (1, True),
        (0, False),
        ("true", True),
        ("False", False),
        ("maybe", None),
        (NAN, None),
        (None, None),
    ],
)
def test_remote_comes_from_the_is_remote_column(value, expected):
    scraper = Scraper(frame(row(is_remote=value)))

    [job] = search(scraper)

    assert job.remote is expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (datetime.date(2026, 9, 19), "2026-09-19"),
        (pd.Timestamp("2026-09-19"), "2026-09-19T00:00:00"),
        (datetime.datetime(2026, 9, 19, 8, 30, tzinfo=datetime.UTC), "2026-09-19T08:30:00+00:00"),
        ("2026-09-19", "2026-09-19"),
        ("", None),
        (pd.NaT, None),
        (None, None),
    ],
)
def test_posted_at_is_the_iso_form_of_date_posted(value, expected):
    scraper = Scraper(frame(row(date_posted=value)))

    [job] = search(scraper)

    assert job.posted_at == expected


@pytest.mark.parametrize(
    ("site", "expected"),
    [
        ("indeed", "indeed"),
        ("LinkedIn", "linkedin"),
        ("zip_recruiter", "zip_recruiter"),
        ("Glassdoor ", "glassdoor"),
        (NAN, "jobspy"),
        (None, "jobspy"),
    ],
)
def test_source_is_the_site_column_lowercased(site, expected):
    scraper = Scraper(frame(row(site=site)))

    [job] = search(scraper)

    assert job.source == expected


def test_external_id_is_stringified():
    scraper = Scraper(frame(row(id=4021)))

    [job] = search(scraper)

    assert job.external_id == "4021"


@pytest.mark.parametrize(
    ("direct", "expected"),
    [
        ("https://jobs.lever.co/acme/1", "lever"),
        ("https://jobs.ashbyhq.com/acme/1", "ashby"),
        ("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/1", "workday"),
        ("https://careers.acme.example/jobs/1", None),
        (NAN, "indeed"),
    ],
)
def test_ats_type_is_detected_from_the_apply_url(direct, expected):
    scraper = Scraper(frame(row(job_url_direct=direct)))

    [job] = search(scraper)

    assert job.ats_type == expected


def test_yields_nothing_and_scrapes_nothing_without_sites_or_titles():
    for crit in (criteria(jobspy_sites=[]), criteria(titles=[]), SearchCriteria()):
        scraper = Scraper(frame(row()))
        assert search(scraper, crit) == []
        assert scraper.calls == []


def test_search_is_lazy():
    scraper = Scraper(frame(row()), frame(row(job_url="https://www.indeed.com/viewjob?jk=2")))
    source = JobSpySource(scrape=scraper)

    it = source.search(criteria(locations=["Remote", "Austin"]))

    assert scraper.calls == []
    next(it)
    assert len(scraper.calls) == 1


def test_an_import_error_from_the_scraper_ends_the_search_quietly(caplog):
    scraper = Scraper(ImportError("No module named 'jobspy'"))
    crit = criteria(titles=["A", "B"], locations=["X", "Y"])

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        jobs = search(scraper, crit)

    assert jobs == []
    assert len(scraper.calls) == 1, "a missing library will not appear for the next query"
    assert any("No module named 'jobspy'" in r.getMessage() for r in caplog.records)


def test_the_default_scraper_is_jobspy_scrape_jobs_imported_on_first_search(monkeypatch):
    import jobspy

    scraper = Scraper(frame(row()))
    monkeypatch.setattr(jobspy, "scrape_jobs", scraper)

    jobs = list(JobSpySource().search(criteria()))

    assert [job.external_id for job in jobs] == ["in-abc123"]
    assert scraper.calls[0]["search_term"] == "Software Engineer"


def test_a_missing_jobspy_package_is_logged_and_yields_nothing(monkeypatch, caplog):
    import builtins

    real_import = builtins.__import__

    def refuse_jobspy(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "jobspy" or name.startswith("jobspy."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", refuse_jobspy)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        jobs = list(JobSpySource().search(criteria()))

    assert jobs == []
    assert any("not installed" in r.getMessage() for r in caplog.records)
