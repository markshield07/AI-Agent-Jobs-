from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from jobagent.discovery.criteria import BoardRef, SearchCriteria
from jobagent.discovery.sources.greenhouse import GreenhouseSource

JOBS = "/v1/boards/{slug}/jobs"
BOARD = "/v1/boards/{slug}"
LOGGER = "jobagent.discovery.sources.greenhouse"

# (status, body). A str body is served verbatim, anything else as JSON.
Route = tuple[int, Any]


def source_for(
    routes: dict[str, Route], seen: list[httpx.Request] | None = None
) -> GreenhouseSource:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        status, body = routes.get(request.url.path, (404, {"error": "not found"}))
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)

    return GreenhouseSource(httpx.Client(transport=httpx.MockTransport(handler)))


def ago(hours: float) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def job(job_id: int = 1, title: str = "Software Engineer", **extra: Any) -> dict[str, Any]:
    entry = {
        "id": job_id,
        "title": title,
        "absolute_url": f"https://boards.greenhouse.io/acme/jobs/{job_id}",
        "updated_at": ago(1),
        "location": {"name": "New York, NY"},
        "content": "&lt;p&gt;Plain job.&lt;/p&gt;",
    }
    entry.update(extra)
    return entry


def criteria_for(*boards: BoardRef, **overrides: Any) -> SearchCriteria:
    overrides.setdefault("titles", [])
    return SearchCriteria(boards=list(boards), **overrides)


ACME = BoardRef(ats="greenhouse", slug="acme", company="Acme Inc")


def test_maps_every_field_and_unescapes_the_content():
    seen: list[httpx.Request] = []
    source = source_for(
        {
            JOBS.format(slug="acme"): (
                200,
                {
                    "jobs": [
                        job(
                            4021,
                            title="  Senior Software Engineer, Payments ",
                            first_published="2026-09-19T10:00:00-04:00",
                            updated_at="2026-09-19T12:00:00-04:00",
                            location={"name": "Remote - US"},
                            content=(
                                "&lt;div&gt;&lt;p&gt;We build &amp;amp; ship things.&lt;/p&gt;"
                                "&lt;ul&gt;&lt;li&gt;Python&lt;/li&gt;&lt;li&gt;SQL&lt;/li&gt;"
                                "&lt;/ul&gt;&lt;/div&gt;"
                            ),
                            departments=[{"name": "Engineering"}],
                            offices=[{"name": "Remote"}],
                            metadata=[],
                        )
                    ]
                },
            )
        },
        seen,
    )
    crit = criteria_for(ACME, max_age_hours=24 * 365 * 10)

    jobs = list(source.search(crit))

    assert len(jobs) == 1
    raw = jobs[0]
    assert raw.url == "https://boards.greenhouse.io/acme/jobs/4021"
    assert raw.apply_url == raw.url
    assert raw.title == "Senior Software Engineer, Payments"
    assert raw.company == "Acme Inc"
    assert raw.source == "greenhouse"
    assert raw.ats_type == "greenhouse"
    assert raw.location == "Remote - US"
    assert raw.remote is True
    assert raw.external_id == "4021"
    assert raw.posted_at == "2026-09-19T10:00:00-04:00", "first_published wins over updated_at"
    lines = [line for line in raw.description.splitlines() if line]
    assert lines == ["We build & ship things.", "Python", "SQL"], "tags gone, entities unescaped"
    assert raw.salary_min is None and raw.salary_max is None

    assert [r.url.path for r in seen] == [JOBS.format(slug="acme")]
    assert seen[0].url.params.get("content") == "true"


def test_posted_at_falls_back_to_updated_at():
    when = ago(2)
    source = source_for(
        {JOBS.format(slug="acme"): (200, {"jobs": [job(updated_at=when)]})},
    )
    (raw,) = source.search(criteria_for(ACME))
    assert raw.posted_at == when


def test_remote_and_location_are_none_when_the_api_gives_none():
    source = source_for(
        {
            JOBS.format(slug="acme"): (
                200,
                {
                    "jobs": [
                        job(1, location=None),
                        job(2, location={"name": "London"}),
                        job(3, location={"name": " "}),
                        job(4, location={"name": "REMOTE (EMEA)"}),
                    ]
                },
            )
        }
    )
    by_id = {raw.external_id: raw for raw in source.search(criteria_for(ACME))}
    assert by_id["1"].location is None and by_id["1"].remote is None
    assert by_id["2"].location == "London" and by_id["2"].remote is None
    assert by_id["3"].location is None and by_id["3"].remote is None
    assert by_id["4"].remote is True


def test_missing_content_leaves_the_description_for_enrichment():
    source = source_for(
        {
            JOBS.format(slug="acme"): (
                200,
                {"jobs": [job(1, content=None), job(2, content=""), job(3, content=12)]},
            )
        }
    )
    jobs = list(source.search(criteria_for(ACME)))
    assert [raw.description for raw in jobs] == [None, None, None]


def test_title_prefilter_keeps_only_wanted_roles():
    source = source_for(
        {
            JOBS.format(slug="acme"): (
                200,
                {
                    "jobs": [
                        job(1, title="Senior Software Engineer, Payments"),
                        job(2, title="Account Executive"),
                        job(3, title="Software Sales Engineer"),
                        job(4, title="Staff Machine Learning Engineer"),
                    ]
                },
            )
        }
    )
    crit = criteria_for(ACME, titles=["software engineer", "Machine Learning Engineer"])
    ids = sorted(raw.external_id for raw in source.search(crit))
    assert ids == ["1", "3", "4"]


def test_age_prefilter_drops_stale_postings_and_keeps_undated_ones():
    source = source_for(
        {
            JOBS.format(slug="acme"): (
                200,
                {
                    "jobs": [
                        job(1, first_published=ago(1)),
                        job(2, first_published=ago(24 * 10)),
                        job(3, updated_at=ago(24 * 10)),
                        job(4, first_published=ago(24 * 10), updated_at=ago(1)),
                        job(5, updated_at=None),
                        job(6, updated_at="not a date"),
                    ]
                },
            )
        }
    )
    ids = sorted(raw.external_id for raw in source.search(criteria_for(ACME, max_age_hours=72)))
    assert ids == ["1", "5", "6"], "first_published is the age, even when updated_at is recent"


def test_company_name_uses_the_criteria_when_given():
    seen: list[httpx.Request] = []
    source = source_for(
        {
            JOBS.format(slug="acme"): (200, {"jobs": [job(1), job(2)]}),
            BOARD.format(slug="acme"): (200, {"name": "Acme Corporation"}),
        },
        seen,
    )
    board = BoardRef(ats="greenhouse", slug="acme", company="  Acme Inc ")
    jobs = list(source.search(criteria_for(board)))
    assert {raw.company for raw in jobs} == {"Acme Inc"}, "given name wins, whitespace trimmed"
    assert BOARD.format(slug="acme") not in [r.url.path for r in seen]


@pytest.mark.parametrize(
    ("company", "board_route", "expected"),
    [
        ("", (200, {"name": "Acme Corporation"}), "Acme Corporation"),
        ("   ", (200, {"name": "Acme Corporation"}), "Acme Corporation"),
        ("", (500, {"error": "boom"}), "Acme Corp"),
    ],
    ids=["empty-to-endpoint", "spaces-to-endpoint", "empty-to-slug"],
)
def test_a_blank_company_in_the_criteria_is_treated_as_not_given(company, board_route, expected):
    seen: list[httpx.Request] = []
    source = source_for(
        {
            JOBS.format(slug="acme-corp"): (200, {"jobs": [job(1), job(2)]}),
            BOARD.format(slug="acme-corp"): board_route,
        },
        seen,
    )
    board = BoardRef(ats="greenhouse", slug="acme-corp", company=company)

    jobs = list(source.search(criteria_for(board)))

    assert [raw.company for raw in jobs] == [expected, expected]
    assert all(raw.company.strip() for raw in jobs), "the store rejects a blank company"
    assert [r.url.path for r in seen].count(BOARD.format(slug="acme-corp")) == 1


def test_company_name_comes_from_the_board_endpoint_once():
    seen: list[httpx.Request] = []
    source = source_for(
        {
            JOBS.format(slug="acme"): (200, {"jobs": [job(1), job(2), job(3)]}),
            BOARD.format(slug="acme"): (200, {"name": "  Acme Corporation "}),
        },
        seen,
    )
    board = BoardRef(ats="greenhouse", slug="acme")
    jobs = list(source.search(criteria_for(board)))
    assert len(jobs) == 3
    assert {raw.company for raw in jobs} == {"Acme Corporation"}
    assert [r.url.path for r in seen].count(BOARD.format(slug="acme")) == 1


@pytest.mark.parametrize(
    "board_route",
    [(500, {"error": "boom"}), (200, "<html>not json</html>"), (200, {}), (200, {"name": " "})],
    ids=["500", "not-json", "no-name", "blank-name"],
)
def test_company_name_falls_back_to_the_slug_when_the_board_endpoint_fails(board_route):
    source = source_for(
        {
            JOBS.format(slug="acme-corp"): (200, {"jobs": [job(1)]}),
            BOARD.format(slug="acme-corp"): board_route,
        }
    )
    board = BoardRef(ats="greenhouse", slug="acme-corp")
    (raw,) = source.search(criteria_for(board))
    assert raw.company == "Acme Corp"


def test_board_name_is_not_requested_when_nothing_passes_the_prefilters():
    seen: list[httpx.Request] = []
    source = source_for(
        {
            JOBS.format(slug="acme"): (200, {"jobs": [job(1, title="Account Executive")]}),
            BOARD.format(slug="acme"): (200, {"name": "Acme Corporation"}),
        },
        seen,
    )
    board = BoardRef(ats="greenhouse", slug="acme")
    assert list(source.search(criteria_for(board, titles=["engineer"]))) == []
    assert [r.url.path for r in seen] == [JOBS.format(slug="acme")]


def test_a_board_listed_twice_is_searched_once():
    seen: list[httpx.Request] = []
    source = source_for(
        {
            JOBS.format(slug="acme"): (200, {"jobs": [job(1), job(2)]}),
            JOBS.format(slug="beta"): (200, {"jobs": [job(3)]}),
        },
        seen,
    )
    beta = BoardRef(ats="greenhouse", slug="beta", company="Beta")
    again = BoardRef(ats="greenhouse", slug="acme", company="Acme Again")

    jobs = list(source.search(criteria_for(ACME, beta, again, ACME)))

    assert sorted(raw.external_id for raw in jobs) == ["1", "2", "3"]
    assert {raw.company for raw in jobs if raw.external_id != "3"} == {"Acme Inc"}, "first wins"
    assert [r.url.path for r in seen] == [JOBS.format(slug="acme"), JOBS.format(slug="beta")]


@pytest.mark.parametrize(
    "bad_route",
    [
        (500, {"error": "boom"}),
        (404, {"error": "not found"}),
        (200, "<html>rate limited</html>"),
        (200, {"jobs": "nope"}),
        (200, [1, 2, 3]),
    ],
)
def test_one_failing_board_does_not_stop_the_others(caplog, bad_route):
    source = source_for(
        {
            JOBS.format(slug="broken"): bad_route,
            JOBS.format(slug="acme"): (200, {"jobs": [job(1), job(2)]}),
        }
    )
    crit = criteria_for(BoardRef(ats="greenhouse", slug="broken", company="Broken Co"), ACME)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        jobs = list(source.search(crit))

    assert sorted(raw.external_id for raw in jobs) == ["1", "2"]
    assert all(raw.company == "Acme Inc" for raw in jobs)
    assert any("'broken'" in record.getMessage() for record in caplog.records)


def test_a_connection_error_is_treated_like_a_failed_board():
    def handler(request: httpx.Request) -> httpx.Response:
        if "broken" in request.url.path:
            raise httpx.ConnectError("no route to host", request=request)
        return httpx.Response(200, json={"jobs": [job(1)]})

    source = GreenhouseSource(httpx.Client(transport=httpx.MockTransport(handler)))
    crit = criteria_for(BoardRef(ats="greenhouse", slug="broken", company="Broken Co"), ACME)
    jobs = list(source.search(crit))
    assert [raw.company for raw in jobs] == ["Acme Inc"]


@pytest.mark.parametrize(
    "slug",
    ["acme\n", "acme\x00", "a" * 70_000],
    ids=["newline", "nul", "url-too-long"],
)
def test_a_slug_that_makes_no_usable_url_is_a_failed_board_not_a_crash(caplog, slug):
    source = source_for({JOBS.format(slug="acme"): (200, {"jobs": [job(1)]})})
    crit = criteria_for(BoardRef(ats="greenhouse", slug=slug, company="Odd Co"), ACME)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        jobs = list(source.search(crit))

    assert [raw.company for raw in jobs] == ["Acme Inc"]
    assert any("could not be read" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize(
    ("slug", "quoted"),
    [("acme?x=1", "acme%3Fx%3D1"), ("acme#frag", "acme%23frag"), ("a/b", "a%2Fb")],
    ids=["query", "fragment", "slash"],
)
def test_the_slug_is_quoted_so_it_stays_on_the_boards_path(slug, quoted):
    seen: list[httpx.Request] = []
    source = source_for({}, seen)
    board = BoardRef(ats="greenhouse", slug=slug, company="Odd Co")

    assert list(source.search(criteria_for(board))) == []

    (request,) = seen
    assert request.url.raw_path.startswith(f"/v1/boards/{quoted}/jobs?".encode())
    assert dict(request.url.params) == {"content": "true"}


def test_malformed_postings_are_skipped_without_raising():
    good = job(7)
    source = source_for(
        {
            JOBS.format(slug="acme"): (
                200,
                {
                    "jobs": [
                        {"id": 1, "absolute_url": "https://boards.greenhouse.io/acme/jobs/1"},
                        {"id": 2, "title": "Software Engineer"},
                        {"id": 3, "title": "   ", "absolute_url": "https://x.example/3"},
                        {"id": 4, "title": "Software Engineer", "absolute_url": ""},
                        {"id": 5, "title": 42, "absolute_url": "https://x.example/5"},
                        None,
                        "junk",
                        [],
                        good,
                    ]
                },
            )
        }
    )
    jobs = list(source.search(criteria_for(ACME)))
    assert [raw.external_id for raw in jobs] == ["7"]


def test_a_posting_without_an_id_still_yields():
    entry = job()
    del entry["id"]
    source = source_for({JOBS.format(slug="acme"): (200, {"jobs": [entry]})})
    (raw,) = source.search(criteria_for(ACME))
    assert raw.external_id is None
    assert raw.title == "Software Engineer"


def test_boards_on_other_ats_are_ignored():
    seen: list[httpx.Request] = []
    source = source_for({JOBS.format(slug="acme"): (200, {"jobs": [job(1)]})}, seen)
    crit = criteria_for(BoardRef(ats="lever", slug="acme"), BoardRef(ats="ashby", slug="acme"))
    assert list(source.search(crit)) == []
    assert seen == []


def test_no_boards_means_no_requests():
    seen: list[httpx.Request] = []
    source = source_for({}, seen)
    assert list(source.search(SearchCriteria())) == []
    assert seen == []


def test_search_is_lazy():
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"jobs": [job(1)]})

    source = GreenhouseSource(httpx.Client(transport=httpx.MockTransport(handler)))
    crit = criteria_for(ACME, BoardRef(ats="greenhouse", slug="beta", company="Beta"))
    it = source.search(crit)
    assert calls == []
    next(it)
    assert calls == [JOBS.format(slug="acme")]
