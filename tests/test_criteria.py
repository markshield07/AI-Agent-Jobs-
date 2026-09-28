"""SearchCriteria defaults, validation bounds and the settings-table round trip."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from jobagent.discovery.criteria import (
    SETTINGS_KEY,
    BoardRef,
    SearchCriteria,
    dump_criteria,
    load_criteria,
    save_criteria,
)


def test_defaults():
    criteria = SearchCriteria()
    assert criteria.titles == []
    assert criteria.locations == ["Remote"]
    assert criteria.remote_ok is True
    assert criteria.keywords == []
    assert criteria.exclude_keywords == []
    assert criteria.company_blacklist == []
    assert criteria.salary_min is None
    assert criteria.min_score == 60
    assert criteria.max_tier == 2
    assert criteria.max_age_hours == 72
    assert criteria.results_per_query == 50
    assert criteria.jobspy_sites == ["indeed", "linkedin"]
    assert criteria.boards == []


def test_default_lists_are_not_shared_between_instances():
    one, two = SearchCriteria(), SearchCriteria()
    one.titles.append("SRE")
    one.locations.append("Berlin")
    assert two.titles == []
    assert two.locations == ["Remote"]


def test_load_returns_defaults_when_nothing_is_saved(conn):
    assert load_criteria(conn) == SearchCriteria()


def test_save_and_load_round_trip(conn):
    criteria = SearchCriteria(
        titles=["Platform Engineer", "SRE"],
        locations=["Berlin", "Remote"],
        remote_ok=False,
        keywords=["kubernetes"],
        exclude_keywords=["clearance"],
        company_blacklist=["Initech"],
        salary_min=90_000,
        min_score=75,
        max_tier=3,
        max_age_hours=24,
        results_per_query=20,
        jobspy_sites=["indeed"],
        boards=[
            BoardRef(ats="greenhouse", slug="stripe", company="Stripe"),
            BoardRef(ats="lever", slug="acme"),
        ],
    )
    save_criteria(conn, criteria)

    loaded = load_criteria(conn)
    assert loaded == criteria
    assert loaded.boards[1].company is None


def test_save_overwrites_the_single_settings_row(conn):
    save_criteria(conn, SearchCriteria(titles=["A"]))
    save_criteria(conn, SearchCriteria(titles=["B"]))

    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    assert [r["key"] for r in rows] == [SETTINGS_KEY]
    assert json.loads(rows[0]["value"])["titles"] == ["B"]
    assert load_criteria(conn).titles == ["B"]


def test_load_rejects_a_corrupt_document(conn):
    conn.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)",
        (SETTINGS_KEY, '{"min_score": 500}', "2024-01-01T00:00:00+00:00"),
    )
    with pytest.raises(ValidationError):
        load_criteria(conn)


def test_dump_is_plain_json_data():
    dumped = dump_criteria(SearchCriteria(boards=[BoardRef(ats="ashby", slug="linear")]))
    assert dumped["boards"] == [{"ats": "ashby", "slug": "linear", "company": None}]
    assert dumped["min_score"] == 60
    json.dumps(dumped)


def test_board_ref_company_is_optional():
    assert BoardRef(ats="lever", slug="acme").company is None
    assert BoardRef(ats="lever", slug="acme", company="Acme Inc").company == "Acme Inc"


def test_board_ref_rejects_an_unknown_ats():
    with pytest.raises(ValidationError):
        BoardRef(ats="workday", slug="acme")


@pytest.mark.parametrize("value", [0, 60, 100])
def test_min_score_accepts_its_bounds(value):
    assert SearchCriteria(min_score=value).min_score == value


@pytest.mark.parametrize("value", [-1, 101])
def test_min_score_rejects_out_of_range(value):
    with pytest.raises(ValidationError):
        SearchCriteria(min_score=value)


@pytest.mark.parametrize("value", [1, 4])
def test_max_tier_accepts_its_bounds(value):
    assert SearchCriteria(max_tier=value).max_tier == value


@pytest.mark.parametrize("value", [0, 5])
def test_max_tier_rejects_out_of_range(value):
    with pytest.raises(ValidationError):
        SearchCriteria(max_tier=value)


@pytest.mark.parametrize(
    "field, bad",
    [
        ("max_age_hours", 0),
        ("results_per_query", 0),
        ("results_per_query", 201),
        ("jobspy_sites", ["monster"]),
        ("boards", [{"ats": "greenhouse"}]),
    ],
)
def test_other_fields_are_validated(field, bad):
    with pytest.raises(ValidationError):
        SearchCriteria(**{field: bad})


def test_normalised_strips_lowercases_and_drops_blanks():
    criteria = SearchCriteria()
    values = ["  Senior Engineer ", "", "   ", "SRE", "\tPlatform\n"]
    assert criteria.normalised(values) == ["senior engineer", "sre", "platform"]
    assert criteria.normalised([]) == []
    assert values[0] == "  Senior Engineer ", "the input list is left alone"
