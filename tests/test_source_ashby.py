from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from jobagent.discovery.criteria import BoardRef, SearchCriteria
from jobagent.discovery.sources import ashby
from jobagent.discovery.sources.ashby import AshbySource

BOARD = "/posting-api/job-board/{slug}"
FOREVER = 1_000_000  # hours; makes the age prefilter a no-op

# (status, body). A str body is served verbatim, anything else as JSON.
Route = tuple[int, Any]


def source_for(routes: dict[str, Route], seen: list[httpx.Request] | None = None) -> AshbySource:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        status, body = routes.get(request.url.path, (404, {"error": "not found"}))
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    return AshbySource(httpx.Client(transport=httpx.MockTransport(handler)))


def ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def yearly(low: Any, high: Any, currency: str = "USD") -> dict[str, Any]:
    return {
        "compensationType": "Salary",
        "interval": "1 YEAR",
        "minValue": low,
        "maxValue": high,
        "currencyCode": currency,
    }


def hourly(low: Any, high: Any) -> dict[str, Any]:
    return {
        "compensationType": "Salary",
        "interval": "1 HOUR",
        "minValue": low,
        "maxValue": high,
        "currencyCode": "USD",
    }


def job(job_id: str = "j1", title: str = "Software Engineer", **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": job_id,
        "title": title,
        "department": "Engineering",
        "team": "Platform",
        "employmentType": "FullTime",
        "location": "New York",
        "secondaryLocations": [],
        "publishedAt": ago(1),
        "isListed": True,
        "isRemote": False,
        "descriptionHtml": "<p>Plain job.</p>",
        "descriptionPlain": "Plain job.",
        "jobUrl": f"https://jobs.ashbyhq.com/acme/{job_id}",
        "applyUrl": f"https://jobs.ashbyhq.com/acme/{job_id}/application",
        "compensation": None,
    }
    entry.update(extra)
    return entry


def board(*jobs: Any, slug: str = "acme") -> dict[str, Route]:
    return {BOARD.format(slug=slug): (200, {"jobs": list(jobs)})}


def criteria_for(*boards: BoardRef, **overrides: Any) -> SearchCriteria:
    overrides.setdefault("titles", [])
    overrides.setdefault("max_age_hours", FOREVER)
    if not boards:
        boards = (ACME,)
    return SearchCriteria(boards=list(boards), **overrides)


ACME = BoardRef(ats="ashby", slug="acme", company="Acme Inc")


def test_maps_every_field_and_asks_for_compensation():
    seen: list[httpx.Request] = []
    source = source_for(
        board(
            job(
                "7e1a",
                title="  Senior Software Engineer, Payments ",
                location="San Francisco",
                secondaryLocations=[{"location": "New York"}, {"location": "Remote - US"}],
                publishedAt="2026-09-19T10:00:00.000Z",
                isRemote=True,
                descriptionPlain="We build & ship things.\n\nPython, SQL.",
                jobUrl="https://jobs.ashbyhq.com/acme/7e1a",
                applyUrl="https://jobs.ashbyhq.com/acme/7e1a/application",
                compensation={
                    "compensationTierSummary": "$150K – $200K • Offers Equity",
                    "summaryComponents": [
                        yearly(150000, 200000),
                        {"compensationType": "EquityPercentage", "interval": "NONE"},
                    ],
                },
            )
        ),
        seen,
    )

    jobs = list(source.search(criteria_for()))

    assert len(jobs) == 1
    raw = jobs[0]
    assert raw.url == "https://jobs.ashbyhq.com/acme/7e1a"
    assert raw.apply_url == "https://jobs.ashbyhq.com/acme/7e1a/application"
    assert raw.title == "Senior Software Engineer, Payments"
    assert raw.company == "Acme Inc"
    assert raw.source == "ashby"
    assert raw.ats_type == "ashby"
    assert raw.location == "San Francisco; New York; Remote - US"
    assert raw.remote is True
    assert raw.external_id == "7e1a"
    assert raw.posted_at == "2026-09-19T10:00:00.000Z"
    assert raw.description == "We build & ship things.\n\nPython, SQL."
    assert (raw.salary_min, raw.salary_max) == (150000, 200000)

    assert len(seen) == 1
    assert seen[0].url.host == "api.ashbyhq.com"
    assert seen[0].url.path == BOARD.format(slug="acme")
    assert seen[0].url.params["includeCompensation"] == "true"


def test_company_defaults_to_the_slug_title_cased():
    source = source_for(board(job(), slug="acme-robotics"))
    [raw] = source.search(criteria_for(BoardRef(ats="ashby", slug="acme-robotics")))
    assert raw.company == "Acme Robotics"


def test_apply_url_falls_back_to_job_url():
    source = source_for(board(job("a", applyUrl=None), job("b", applyUrl="  ")))
    jobs = list(source.search(criteria_for()))
    assert [raw.apply_url for raw in jobs] == [raw.url for raw in jobs]


def test_unlisted_postings_are_skipped_but_an_absent_flag_is_listed():
    entry = job("d")
    del entry["isListed"]
    source = source_for(
        board(job("a", isListed=False), job("b", isListed=True), job("c", isListed=None), entry)
    )
    ids = sorted(raw.external_id for raw in source.search(criteria_for()))
    assert ids == ["b", "c", "d"]


def test_description_falls_back_to_html_then_to_nothing():
    source = source_for(
        board(
            job("plain", descriptionPlain="Plain wins.", descriptionHtml="<p>HTML loses.</p>"),
            job(
                "html",
                descriptionPlain="   ",
                descriptionHtml="<div><p>From &amp; HTML.</p><ul><li>Python</li></ul></div>",
            ),
            job("none", descriptionPlain=None, descriptionHtml=None),
            job("empty", descriptionPlain="", descriptionHtml=""),
            job("junk", descriptionPlain=12, descriptionHtml={"not": "html"}),
        )
    )
    by_id = {raw.external_id: raw.description for raw in source.search(criteria_for())}
    assert by_id["plain"] == "Plain wins."
    assert [line for line in by_id["html"].splitlines() if line] == ["From & HTML.", "Python"]
    assert by_id["none"] is None, "no text at all leaves the description for enrichment"
    assert by_id["empty"] is None
    assert by_id["junk"] is None


def test_secondary_locations_are_appended_and_junk_is_ignored():
    source = source_for(
        board(
            job("one", location="Berlin", secondaryLocations=[]),
            job("two", location="Berlin", secondaryLocations=[{"location": "London"}]),
            job(
                "dedupe",
                location="Berlin",
                secondaryLocations=[{"location": "Berlin"}, {"location": " London "}],
            ),
            job("only-secondary", location=None, secondaryLocations=[{"location": "London"}]),
            job(
                "junk",
                location="Berlin",
                secondaryLocations=[None, "Paris", {"location": ""}, {"name": "Rome"}, 3],
            ),
            job("not-a-list", location="Berlin", secondaryLocations={"location": "London"}),
            job("nothing", location="  ", secondaryLocations=None),
        )
    )
    by_id = {raw.external_id: raw.location for raw in source.search(criteria_for())}
    assert by_id == {
        "one": "Berlin",
        "two": "Berlin; London",
        "dedupe": "Berlin; London",
        "only-secondary": "London",
        "junk": "Berlin",
        "not-a-list": "Berlin",
        "nothing": None,
    }


def test_remote_is_only_read_from_a_boolean():
    source = source_for(
        board(
            job("yes", isRemote=True, location="Remote"),
            job("no", isRemote=False, location="Remote"),
            job("null", isRemote=None),
            job("text", isRemote="true"),
            job("int", isRemote=1),
        )
    )
    by_id = {raw.external_id: raw.remote for raw in source.search(criteria_for())}
    assert by_id == {"yes": True, "no": False, "null": None, "text": None, "int": None}
    # "Remote" in the location does not override an explicit isRemote: False.


@pytest.mark.parametrize(
    ("compensation", "expected"),
    [
        pytest.param(
            {"summaryComponents": [yearly(120000, 160000)]}, (120000, 160000), id="yearly"
        ),
        pytest.param(
            {"summaryComponents": [yearly(120000.5, 160000.9)]},
            (120000, 160000),
            id="floats-truncate",
        ),
        pytest.param({"summaryComponents": [yearly(120000, None)]}, (120000, None), id="min-only"),
        pytest.param(
            {
                "summaryComponents": [
                    {"compensationType": "Salary", "interval": "yearly", "minValue": 90000}
                ]
            },
            (90000, None),
            id="interval-any-spelling-of-year",
        ),
        pytest.param(
            {"summaryComponents": [hourly(50, 70), yearly(120000, 160000)]},
            (120000, 160000),
            id="first-yearly-salary-wins-over-earlier-hourly",
        ),
        pytest.param(
            {"summaryComponents": [yearly(100000, 120000), yearly(200000, 250000)]},
            (100000, 120000),
            id="first-yearly-salary-wins",
        ),
        pytest.param({"summaryComponents": [hourly(50, 70)]}, (None, None), id="hourly-only"),
        pytest.param(
            {"summaryComponents": [{"compensationType": "Salary", "minValue": 90000}]},
            (None, None),
            id="no-interval",
        ),
        pytest.param(
            {
                "summaryComponents": [
                    {
                        "compensationType": "EquityPercentage",
                        "interval": "1 YEAR",
                        "minValue": 1,
                        "maxValue": 2,
                    }
                ]
            },
            (None, None),
            id="equity-with-a-yearly-interval-is-not-salary",
        ),
        pytest.param(
            {"summaryComponents": [yearly("120000", True)]},
            (None, None),
            id="non-numeric-amounts",
        ),
        pytest.param({"summaryComponents": []}, (None, None), id="no-components"),
        pytest.param({"summaryComponents": [None, "x", 3]}, (None, None), id="junk-components"),
        pytest.param({"summaryComponents": "nope"}, (None, None), id="components-not-a-list"),
        pytest.param({"compensationTierSummary": "$120K+"}, (None, None), id="summary-only"),
        pytest.param({}, (None, None), id="empty"),
        pytest.param(None, (None, None), id="null"),
        pytest.param("120k-160k", (None, None), id="not-an-object"),
    ],
)
def test_compensation_parsing(compensation, expected):
    source = source_for(board(job(compensation=compensation)))
    [raw] = source.search(criteria_for())
    assert (raw.salary_min, raw.salary_max) == expected


def test_missing_compensation_key_means_no_salary():
    entry = job()
    del entry["compensation"]
    source = source_for(board(entry))
    [raw] = source.search(criteria_for())
    assert (raw.salary_min, raw.salary_max) == (None, None)


def test_title_prefilter_keeps_only_wanted_roles():
    source = source_for(
        board(
            job("1", title="Senior Software Engineer, Payments"),
            job("2", title="Account Executive"),
            job("3", title="Software Sales Engineer"),
            job("4", title="Staff Machine Learning Engineer"),
        )
    )
    crit = criteria_for(titles=["software engineer", "Machine Learning Engineer"])
    ids = sorted(raw.external_id for raw in source.search(crit))
    assert ids == ["1", "3", "4"]


def test_age_prefilter_drops_stale_postings_and_keeps_undated_ones():
    source = source_for(
        board(
            job("fresh", publishedAt=ago(1)),
            job("stale", publishedAt=ago(24 * 10)),
            job("undated", publishedAt=None),
            job("blank", publishedAt=""),
            job("garbage", publishedAt="not a date"),
            job("wrong-type", publishedAt={"when": "now"}),
        )
    )
    jobs = list(source.search(criteria_for(max_age_hours=72)))
    by_id = {raw.external_id: raw.posted_at for raw in jobs}
    assert sorted(by_id) == ["blank", "fresh", "garbage", "undated", "wrong-type"]
    assert by_id["fresh"].endswith("Z")
    assert by_id["undated"] is None and by_id["blank"] is None and by_id["wrong-type"] is None
    assert by_id["garbage"] == "not a date", "an unparseable date is passed through as given"


@pytest.mark.parametrize(
    "bad_route",
    [
        (500, {"error": "boom"}),
        (404, {"error": "not found"}),
        (200, "<html>rate limited</html>"),
        (200, {"jobs": "nope"}),
        (200, {"jobs": None}),
        (200, [1, 2, 3]),
        (200, "null"),
    ],
)
def test_one_failing_board_does_not_stop_the_others(caplog, bad_route):
    source = source_for(
        {
            BOARD.format(slug="broken"): bad_route,
            BOARD.format(slug="acme"): (200, {"jobs": [job("1"), job("2")]}),
        }
    )
    crit = criteria_for(BoardRef(ats="ashby", slug="broken", company="Broken Co"), ACME)

    with caplog.at_level(logging.WARNING, logger="jobagent.discovery.sources.ashby"):
        jobs = list(source.search(crit))

    assert sorted(raw.external_id for raw in jobs) == ["1", "2"]
    assert all(raw.company == "Acme Inc" for raw in jobs)
    assert any("'broken'" in record.getMessage() for record in caplog.records)


def test_a_connection_error_is_treated_like_a_failed_board():
    def handler(request: httpx.Request) -> httpx.Response:
        if "broken" in request.url.path:
            raise httpx.ConnectError("no route to host", request=request)
        return httpx.Response(200, json={"jobs": [job("1")]})

    source = AshbySource(httpx.Client(transport=httpx.MockTransport(handler)))
    crit = criteria_for(BoardRef(ats="ashby", slug="broken", company="Broken Co"), ACME)
    jobs = list(source.search(crit))
    assert [raw.company for raw in jobs] == ["Acme Inc"]


def test_malformed_postings_are_skipped_without_raising(caplog):
    source = source_for(
        board(
            {"id": "no-title", "jobUrl": "https://jobs.ashbyhq.com/acme/1"},
            {"id": "no-url", "title": "Software Engineer"},
            {"id": "blank-title", "title": "   ", "jobUrl": "https://jobs.ashbyhq.com/acme/3"},
            {"id": "blank-url", "title": "Software Engineer", "jobUrl": ""},
            {"id": "int-title", "title": 42, "jobUrl": "https://jobs.ashbyhq.com/acme/5"},
            {"id": "url-not-str", "title": "Software Engineer", "jobUrl": ["x"]},
            None,
            "junk",
            [],
            7,
            job("good"),
        )
    )
    with caplog.at_level(logging.WARNING, logger="jobagent.discovery.sources.ashby"):
        jobs = list(source.search(criteria_for()))
    assert [raw.external_id for raw in jobs] == ["good"]
    assert any("no-title" in record.getMessage() for record in caplog.records)


def test_a_posting_that_blows_up_is_skipped_and_logged(monkeypatch, caplog):
    def boom(markup: str | None) -> str:
        raise RuntimeError("bad markup")

    monkeypatch.setattr(ashby, "html_to_text", boom)
    source = source_for(
        board(
            job("html-only", descriptionPlain=None, descriptionHtml="<p>x</p>"),
            job("plain", descriptionPlain="fine"),
        )
    )
    with caplog.at_level(logging.ERROR, logger="jobagent.discovery.sources.ashby"):
        jobs = list(source.search(criteria_for()))
    assert [raw.external_id for raw in jobs] == ["plain"]
    assert any("'acme'" in record.getMessage() for record in caplog.records)


def test_a_posting_without_an_id_still_yields():
    entry = job()
    del entry["id"]
    source = source_for(board(entry, job(""), job(9)))
    ids = [raw.external_id for raw in source.search(criteria_for())]
    assert ids == [None, None, "9"], "ids are strings; a missing or blank one is None"


def test_boards_on_other_ats_are_ignored():
    seen: list[httpx.Request] = []
    source = source_for(board(job("1")), seen)
    crit = criteria_for(BoardRef(ats="lever", slug="acme"), BoardRef(ats="greenhouse", slug="acme"))
    assert list(source.search(crit)) == []
    assert seen == []


def test_no_boards_means_no_requests():
    seen: list[httpx.Request] = []
    source = source_for({}, seen)
    assert list(source.search(SearchCriteria())) == []
    assert seen == []


def test_search_is_lazy_and_visits_each_board_once():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"jobs": [job("1"), job("2")]})

    source = AshbySource(httpx.Client(transport=httpx.MockTransport(handler)))
    crit = criteria_for(ACME, BoardRef(ats="ashby", slug="beta", company="Beta"))
    it = source.search(crit)
    assert calls == []
    next(it)
    assert calls == [BOARD.format(slug="acme")]
    assert len(list(it)) == 3
    assert calls == [BOARD.format(slug="acme"), BOARD.format(slug="beta")]
