"""The mapping between the model's output and fact-base rows.

The model call itself is not exercised here — this is the part that must stay
correct when the schema changes.
"""

from __future__ import annotations

from jobagent.resume.parser import (
    ParsedContact,
    ParsedFact,
    ParsedResume,
    contact_answers,
    to_facts,
)


def test_detail_drops_fields_the_resume_did_not_state():
    parsed = ParsedResume(
        contact=ParsedContact(),
        facts=[
            ParsedFact(
                kind="role",
                text="Led the billing rewrite",
                employer="Acme",
                title="Staff Engineer",
                start="Mar 2021",
                end=None,
                tags=["python"],
            )
        ],
    )
    fact = to_facts(parsed, base_id=7)[0]

    assert fact.detail == {"employer": "Acme", "title": "Staff Engineer", "start": "Mar 2021"}
    assert "end" not in fact.detail, "an unstated date must not become an empty string"
    assert fact.source == "parsed"
    assert fact.base_id == 7


def test_contact_answers_skip_missing_fields():
    answers = contact_answers(
        ParsedContact(name="Mark Shield", email="mark@example.com", links=["github.com/m"])
    )
    assert answers == {
        "full_name": "Mark Shield",
        "email": "mark@example.com",
        "links": "github.com/m",
    }


def test_contact_with_nothing_stated_yields_no_answers():
    assert contact_answers(ParsedContact()) == {}
