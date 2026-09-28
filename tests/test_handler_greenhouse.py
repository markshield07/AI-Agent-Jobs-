"""The Greenhouse handler against a Greenhouse application page, in a real browser.

The fixture is the form the reference repos agree on: the canonical ids, the
`#candidate-location` typeahead that lists nothing until something is typed,
custom questions named `job_application[answers_attributes][N][...]`, the EEO
selects, a marketing box and `#submit_app`. Its variants turn on the cases
worth proving: the older layout behind an apply anchor, the verification code
some boards ask for after the button, a required question nothing answers,
and a server error.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from jobagent.apply.answering import make_answerer
from jobagent.apply.handlers.greenhouse import GreenhouseHandler, embed_url_for, typeahead_aware
from jobagent.apply.models import FillPlan, FormField, Packet

FIXTURE = Path(__file__).parent / "fixtures" / "forms" / "greenhouse.html"

pytestmark = pytest.mark.usefixtures("page")


def fixture_url(variant: str = "") -> str:
    return FIXTURE.as_uri() + (f"?variant={variant}" if variant else "")


@pytest.fixture
def packet(tmp_path) -> Packet:
    resume = tmp_path / "mark-shield.pdf"
    resume.write_bytes(b"%PDF-1.4 resume")
    letter = tmp_path / "mark-shield-letter.pdf"
    letter.write_bytes(b"%PDF-1.4 letter")
    return Packet(
        job={
            "id": "j1",
            "title": "Staff Data Engineer",
            "company": "Northwind Analytics",
            "url": fixture_url(),
            "apply_url": None,
        },
        contact={
            "full_name": "Mark Shield",
            "first_name": "Mark",
            "last_name": "Shield",
            "email": "mark@example.com",
            "phone": "+1 555 0100",
            "location": "Austin",
        },
        resume_path=str(resume),
        cover_letter="I keep warehouses honest.",
        cover_letter_path=str(letter),
        links={
            "linkedin": "https://www.linkedin.com/in/markshield",
            "website": "https://markshield.dev",
        },
        answers={
            "work_authorization": "Yes",
            "q:what draws you to northwind analytics": "Your warehouse work is the reason.",
        },
    )


def run(page, packet, *, submit=False, variant="", shot=None):
    packet.job["url"] = fixture_url(variant)
    return GreenhouseHandler().apply(
        page,
        packet,
        make_answerer(packet, completer=None, allow_model=False),
        submit=submit,
        screenshot_path=shot,
    )


def submitted(page):
    return page.evaluate("() => window.__submitted || null")


# ------------------------------------------------------------------- url --


def test_the_embed_form_url_is_built_from_a_posting_url():
    assert embed_url_for("https://boards.greenhouse.io/northwind/jobs/4102938") == (
        "https://boards.greenhouse.io/embed/job_app?for=northwind&token=4102938"
    )
    assert embed_url_for("https://job-boards.greenhouse.io/northwind/jobs/4102938/") == (
        "https://boards.greenhouse.io/embed/job_app?for=northwind&token=4102938"
    )
    assert (
        embed_url_for(
            "https://boards.greenhouse.io/embed/job_app?for=northwind&token=4102938&gh_src=x"
        )
        == "https://boards.greenhouse.io/embed/job_app?for=northwind&token=4102938"
    )
    assert embed_url_for("https://job-boards.eu.greenhouse.io/northwind/jobs/7") == (
        "https://boards.eu.greenhouse.io/embed/job_app?for=northwind&token=7"
    )
    for none in (
        "https://boards.greenhouse.io/northwind",
        "https://example.com/northwind/jobs/4102938",
        "",
    ):
        assert embed_url_for(none) is None


def test_the_handler_claims_greenhouse_urls_and_no_others():
    handler = GreenhouseHandler()
    assert handler.matches("https://boards.greenhouse.io/northwind/jobs/1")
    assert handler.matches("https://job-boards.greenhouse.io/northwind/jobs/1")
    assert handler.matches("https://grnh.se/abc123")
    assert not handler.matches("https://jobs.lever.co/acme/1")
    assert not handler.matches("https://notgreenhouse.io/x")


def test_a_typeahead_is_planned_as_text_and_the_field_is_put_back():
    seen: list[FormField] = []

    def answerer(fields):
        seen.extend(dataclasses.replace(f) for f in fields)
        return FillPlan()

    location = FormField(key="candidate-location", label="Location", kind="select", options=[])
    country = FormField(key="country", label="Country", kind="select", options=["US", "DE"])
    typeahead_aware(answerer)([location, country])

    assert [f.kind for f in seen] == ["text", "select"], "only an empty select is a typeahead"
    assert location.kind == "select", "the field keeps its real kind for the filler"
    assert country.kind == "select"


# --------------------------------------------------------------- dry run --


def test_a_dry_run_fills_the_form_and_leaves_it_unsent(page, packet, tmp_path):
    shot = tmp_path / "shots" / "greenhouse.png"
    result = run(page, packet, shot=str(shot))

    assert result.outcome == "dry_run", result.error
    assert [n for n in result.needed if n.required] == []
    assert submitted(page) is None
    assert Path(result.screenshot_path).is_file()

    values = {f.key: f.value for f in result.filled}
    assert values["job_application[first_name]"] == "Mark"
    assert values["job_application[last_name]"] == "Shield"
    assert values["job_application[email]"] == "mark@example.com"
    files = {f.key: f.file_path for f in result.filled if f.file_path}
    assert files["job_application[resume]"] == packet.resume_path
    assert files["job_application[cover_letter]"] == packet.cover_letter_path

    on_page = page.evaluate(
        """() => ({
            first: document.getElementById('first_name').value,
            location: document.getElementById('candidate-location').value,
            resume: document.getElementById('resume').files[0].name,
            cover: document.getElementById('cover_letter').files[0].name,
            auth: document.querySelector('[name$="[3][boolean_value]"]').value,
            gender: document.getElementById('gender').value,
            marketing: document.getElementById('marketing_opt_in').checked,
        })"""
    )
    assert on_page["first"] == "Mark"
    assert on_page["location"] == "Austin, TX, USA", "the typeahead's own suggestion is taken"
    assert on_page["resume"] == "mark-shield.pdf" and on_page["cover"].endswith("letter.pdf")
    assert on_page["auth"] == "1", "the option's value, not its text, goes to the form"
    assert on_page["gender"] == "Decline To Self Identify"
    assert on_page["marketing"] is False, "a marketing box is left unticked"


def test_the_older_layout_behind_the_apply_anchor_is_opened(page, packet):
    result = run(page, packet, variant="apply_button")
    assert result.outcome == "dry_run", result.error
    assert page.locator("#application_form").is_visible()
    assert page.input_value("#first_name") == "Mark"


def test_a_banked_answer_fills_a_custom_question(page, packet):
    result = run(page, packet)
    values = {f.key: f.value for f in result.filled}
    draws = "job_application[answers_attributes][2][text_value]"
    assert values[draws] == "Your warehouse work is the reason."
    assert page.input_value(f'[name="{draws}"]') == values[draws]


def test_an_unanswerable_optional_question_is_left_alone(page, packet):
    result = run(page, packet)
    skills = next(f for f in result.fields if f.label.startswith("Which of these"))
    assert skills.kind == "checkbox" and skills.options == ["Python", "Kubernetes", "dbt"]
    assert skills.key not in {f.key for f in result.filled}
    assert not any(n.required for n in result.needed)


# ----------------------------------------------------------- needs input --


def test_a_required_question_nobody_can_answer_stops_before_the_button(page, packet):
    result = run(page, packet, submit=True, variant="needs_input")

    assert result.outcome == "needs_input"
    assert submitted(page) is None
    asked = [n for n in result.needed if n.required]
    assert len(asked) == 1 and "salary" in asked[0].label.lower()
    assert asked[0].answer_key == "salary_expectation"
    assert asked[0].reason, "the report says why it was not answered"


def test_the_same_question_answered_goes_through(page, packet):
    packet.answers["salary_expectation"] = "$210,000"
    result = run(page, packet, submit=True, variant="needs_input")
    assert result.outcome == "submitted", result.error
    assert submitted(page)["job_application[answers_attributes][4][text_value]"] == "$210,000"


# ----------------------------------------------------------------- send --


def test_submitting_reads_the_confirmation(page, packet):
    result = run(page, packet, submit=True)
    assert result.outcome == "submitted", result.error
    assert "thank you for applying" in (result.confirmation or "").lower()
    record = submitted(page)
    assert record["job_application[first_name]"] == "Mark"
    assert record["job_application[resume]"] == "mark-shield.pdf"
    assert record["job_application[answers_attributes][3][boolean_value]"] == "1"


def test_a_verification_code_after_the_button_is_blocked(page, packet):
    result = run(page, packet, submit=True, variant="security")
    assert result.outcome == "blocked"
    assert "verification code" in result.error
    assert "--headed" in result.error, "the reason says what to do about it"


def test_a_server_error_is_a_failure_with_what_the_page_said(page, packet):
    result = run(page, packet, submit=True, variant="error")
    assert result.outcome == "failed"
    assert "problem with your application" in result.error.lower()
    assert submitted(page) is None


# ------------------------------------------------------------- no form --


def test_a_company_page_with_no_greenhouse_form_is_not_filled(page, packet, tmp_path):
    """A careers page of its own is not an application: its search box is left alone."""
    front = tmp_path / "front.html"
    front.write_text(
        "<html><body><h1>Careers at Northwind</h1>"
        '<form><label for="q">Search jobs</label>'
        '<input id="q" name="q" type="text"></form></body></html>'
    )
    packet.job["url"] = front.as_uri()
    result = GreenhouseHandler().apply(
        page, packet, make_answerer(packet, allow_model=False), submit=True
    )
    assert result.outcome == "failed" and "no application form" in result.error
    assert page.input_value("#q") == ""
