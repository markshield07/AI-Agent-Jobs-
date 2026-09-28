from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from jobagent.discovery.criteria import BoardRef, SearchCriteria
from jobagent.discovery.sources.lever import LeverSource

# 2023-11-14T22:13:20Z, in milliseconds as Lever sends it.
FIXED_MS = 1_700_000_000_000
FOREVER = 1_000_000  # hours; makes the age prefilter a no-op


def ms_ago(hours: float) -> int:
    return int((datetime.now(UTC) - timedelta(hours=hours)).timestamp() * 1000)


def posting(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "abc-123",
        "text": "Senior Software Engineer",
        "hostedUrl": "https://jobs.lever.co/acme/abc-123",
        "applyUrl": "https://jobs.lever.co/acme/abc-123/apply",
        "createdAt": FIXED_MS,
        "categories": {
            "location": "Berlin",
            "team": "Platform",
            "department": "Engineering",
            "commitment": "Full-time",
            "allLocations": ["Berlin"],
        },
        "workplaceType": "onsite",
        "descriptionPlain": "We build things.",
        "description": "<p>We build things.</p>",
        "lists": [],
        "additionalPlain": "",
    }
    base.update(overrides)
    return base


def make_source(boards: dict[str, Any], seen: list[httpx.Request] | None = None) -> LeverSource:
    """A source whose client answers `/v0/postings/<slug>` from `boards`.

    A value that is a list is served as JSON; an int is served as that HTTP
    status with an empty body; a str is served verbatim (for broken JSON).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        slug = request.url.path.rsplit("/", 1)[-1]
        body = boards.get(slug)
        if body is None:
            return httpx.Response(404)
        if isinstance(body, int):
            return httpx.Response(body)
        if isinstance(body, str):
            return httpx.Response(200, text=body)
        return httpx.Response(200, json=body)

    return LeverSource(client=httpx.Client(transport=httpx.MockTransport(handler)))


def criteria(**overrides: Any) -> SearchCriteria:
    values: dict[str, Any] = {
        "titles": [],
        "max_age_hours": FOREVER,
        "boards": [BoardRef(ats="lever", slug="acme", company="Acme")],
    }
    values.update(overrides)
    return SearchCriteria(**values)


def test_maps_fields_and_converts_millisecond_epoch():
    seen: list[httpx.Request] = []
    source = make_source({"acme": [posting()]}, seen)

    jobs = list(source.search(criteria()))

    assert len(jobs) == 1
    job = jobs[0]
    assert job.url == "https://jobs.lever.co/acme/abc-123"
    assert job.apply_url == "https://jobs.lever.co/acme/abc-123/apply"
    assert job.title == "Senior Software Engineer"
    assert job.company == "Acme"
    assert job.source == "lever"
    assert job.ats_type == "lever"
    assert job.external_id == "abc-123"
    assert job.location == "Berlin"
    assert job.posted_at == "2023-11-14T22:13:20+00:00"
    assert job.description == "We build things."

    assert seen[0].url.host == "api.lever.co"
    assert seen[0].url.path == "/v0/postings/acme"
    assert seen[0].url.params["mode"] == "json"


def test_company_defaults_to_the_slug_title_cased():
    source = make_source({"acme-robotics": [posting()]})
    boards = [BoardRef(ats="lever", slug="acme-robotics")]

    [job] = source.search(criteria(boards=boards))

    assert job.company == "Acme Robotics"


def test_apply_url_falls_back_to_hosted_url():
    source = make_source({"acme": [posting(applyUrl=None)]})

    [job] = source.search(criteria())

    assert job.apply_url == job.url == "https://jobs.lever.co/acme/abc-123"


def test_description_is_assembled_from_lists_and_additional_text():
    source = make_source(
        {
            "acme": [
                posting(
                    descriptionPlain="We build things.\n\nIt is fun.",
                    lists=[
                        {"text": "Requirements", "content": "<li>Python</li><li>SQL</li>"},
                        {"text": "Nice to have", "content": ""},
                        {"content": "<li>Headless section</li>"},
                        "not a list section",
                    ],
                    additionalPlain="Equal opportunity employer.",
                )
            ]
        }
    )

    [job] = source.search(criteria())

    # html_to_text keeps a paragraph break between block elements, list items included.
    assert job.description == (
        "We build things.\n\nIt is fun."
        "\n\nRequirements\nPython\n\nSQL"
        "\n\nHeadless section"
        "\n\nEqual opportunity employer."
    )


def test_description_falls_back_to_html_when_plain_is_absent():
    html = "<p>First paragraph.</p><p>Second &amp; last.</p>"
    source = make_source(
        {"acme": [posting(descriptionPlain=None, description=html, additionalPlain=None)]}
    )

    [job] = source.search(criteria())

    assert job.description == "First paragraph.\n\nSecond & last."


def test_description_is_none_when_nothing_is_provided():
    source = make_source(
        {"acme": [posting(descriptionPlain="", description="", lists=None, additionalPlain=None)]}
    )

    [job] = source.search(criteria())

    assert job.description is None


@pytest.mark.parametrize(
    ("workplace", "location", "expected"),
    [
        ("remote", "Berlin", True),
        ("Remote", "Berlin", True),
        ("hybrid", "Remote - US", True),
        (None, "Remote (EMEA)", True),
        ("onsite", "Berlin", False),
        ("hybrid", "Berlin", False),
        ("unspecified", "Berlin", None),
        (None, "Berlin", None),
        (None, None, None),
    ],
)
def test_remote_is_inferred_from_workplace_type_and_location(workplace, location, expected):
    categories = {"location": location} if location else {}
    source = make_source({"acme": [posting(workplaceType=workplace, categories=categories)]})

    [job] = source.search(criteria())

    assert job.remote is expected
    assert job.location == location


def test_location_falls_back_to_all_locations():
    categories = {"allLocations": ["Berlin", "Munich", ""]}
    source = make_source({"acme": [posting(categories=categories)]})

    [job] = source.search(criteria())

    assert job.location == "Berlin, Munich"


@pytest.mark.parametrize(
    ("salary_range", "expected"),
    [
        (
            {"min": 120000, "max": 160000, "currency": "USD", "interval": "per-year-salary"},
            (120000, 160000),
        ),
        ({"min": 90000, "max": 110000, "currency": "EUR", "interval": "yearly"}, (90000, 110000)),
        ({"min": 90000, "max": 110000, "currency": "EUR", "interval": "year"}, (90000, 110000)),
        ({"min": 90000, "max": 110000, "currency": "EUR"}, (90000, 110000)),
        ({"min": "90000", "max": 110000.0, "currency": "EUR"}, (90000, 110000)),
        ({"min": 40, "max": 60, "currency": "USD", "interval": "per-hour-wage"}, (None, None)),
        ({"min": 40, "max": 60, "currency": "USD", "interval": "hourly"}, (None, None)),
        ({"currency": "USD", "interval": "yearly"}, (None, None)),
        ("120k-160k", (None, None)),
        (None, (None, None)),
    ],
)
def test_salary_keeps_yearly_ranges_only(salary_range, expected):
    body = posting()
    if salary_range is not None:
        body["salaryRange"] = salary_range
    source = make_source({"acme": [body]})

    [job] = source.search(criteria())

    assert (job.salary_min, job.salary_max) == expected


def test_title_prefilter_drops_unwanted_roles():
    source = make_source(
        {
            "acme": [
                posting(id="1", text="Senior Software Engineer, Payments", hostedUrl="https://x/1"),
                posting(id="2", text="Software Sales Lead", hostedUrl="https://x/2"),
                posting(id="3", text="Staff software engineer", hostedUrl="https://x/3"),
            ]
        }
    )

    jobs = list(source.search(criteria(titles=["Software Engineer"])))

    assert [job.external_id for job in jobs] == ["1", "3"]


def test_age_prefilter_drops_old_postings_and_keeps_undated_ones():
    source = make_source(
        {
            "acme": [
                posting(id="fresh", hostedUrl="https://x/fresh", createdAt=ms_ago(1)),
                posting(id="stale", hostedUrl="https://x/stale", createdAt=ms_ago(100)),
                posting(id="undated", hostedUrl="https://x/undated", createdAt=None),
                posting(id="odd", hostedUrl="https://x/odd", createdAt={"weird": True}),
            ]
        }
    )

    jobs = list(source.search(criteria(max_age_hours=24)))

    assert [job.external_id for job in jobs] == ["fresh", "undated", "odd"]
    assert jobs[1].posted_at is None
    assert jobs[2].posted_at is None


def test_one_failing_board_does_not_stop_the_others():
    seen: list[httpx.Request] = []
    source = make_source(
        {
            "broken": 500,
            "garbled": "<html>not json</html>",
            "wrong-shape": {"postings": []},
            "missing": None,
            "acme": [posting()],
        },
        seen,
    )
    boards = [
        BoardRef(ats="lever", slug="broken"),
        BoardRef(ats="lever", slug="garbled"),
        BoardRef(ats="lever", slug="wrong-shape"),
        BoardRef(ats="lever", slug="missing"),
        BoardRef(ats="greenhouse", slug="not-for-us"),
        BoardRef(ats="lever", slug="acme", company="Acme"),
    ]

    jobs = list(source.search(criteria(boards=boards)))

    assert [job.company for job in jobs] == ["Acme"]
    requested = [request.url.path.rsplit("/", 1)[-1] for request in seen]
    assert requested == ["broken", "garbled", "wrong-shape", "missing", "acme"]


def test_malformed_postings_are_skipped_not_raised():
    source = make_source(
        {
            "acme": [
                "just a string",
                42,
                None,
                {"id": "no-title", "hostedUrl": "https://x/no-title"},
                {"id": "blank-title", "text": "   ", "hostedUrl": "https://x/blank"},
                {"id": "no-url", "text": "Engineer"},
                {"id": "bad-url", "text": "Engineer", "hostedUrl": 12345},
                {"id": 7, "text": "Engineer", "hostedUrl": "https://x/minimal"},
            ]
        }
    )

    jobs = list(source.search(criteria()))

    assert len(jobs) == 1
    job = jobs[0]
    assert job.external_id == "7"
    assert job.title == "Engineer"
    assert job.url == "https://x/minimal"
    assert job.apply_url == "https://x/minimal"
    assert job.location is None
    assert job.description is None
    assert job.remote is None
    assert job.posted_at is None
    assert (job.salary_min, job.salary_max) == (None, None)


def test_search_is_lazy_and_yields_nothing_without_lever_boards():
    seen: list[httpx.Request] = []
    source = make_source({"acme": [posting()]}, seen)

    jobs = list(source.search(criteria(boards=[BoardRef(ats="ashby", slug="acme")])))

    assert jobs == []
    assert seen == []


def test_raw_response_round_trips_through_json():
    """Guard against the handler serialising something the real API would not."""
    body = json.dumps([posting()])
    source = make_source({"acme": body})

    [job] = source.search(criteria())

    assert job.title == "Senior Software Engineer"
