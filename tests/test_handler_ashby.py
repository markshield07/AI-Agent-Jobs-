"""The Ashby handler against an Ashby application page, in a real browser.

The page's resume parser is the point: attaching the resume makes Ashby write
the contact boxes itself, from what it made of the file, behind an
"Autofilling" banner. The fixture's parser gets it wrong on purpose, so a
handler that fills before the file or does not wait for the banner submits
the parser's version instead of what is on file.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.apply.answering import make_answerer
from jobagent.apply.handlers.ashby import AshbyHandler, application_url_for
from jobagent.apply.models import Packet

FIXTURE = Path(__file__).parent / "fixtures" / "forms" / "ashby.html"

pytestmark = pytest.mark.usefixtures("page")


def fixture_url(variant: str = "") -> str:
    return FIXTURE.as_uri() + (f"?variant={variant}" if variant else "")


@pytest.fixture
def packet(tmp_path) -> Packet:
    resume = tmp_path / "mark-shield.pdf"
    resume.write_bytes(b"%PDF-1.4 resume")
    return Packet(
        job={
            "id": "j1",
            "title": "Backend Engineer",
            "company": "Helios Systems",
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
        cover_letter="I have built control planes.",
        links={"linkedin": "https://www.linkedin.com/in/markshield"},
        answers={"work_authorization": "Yes"},
    )


def run(page, packet, *, submit=False, variant="", shot=None):
    packet.job["url"] = fixture_url(variant)
    return AshbyHandler().apply(
        page,
        packet,
        make_answerer(packet, completer=None, allow_model=False),
        submit=submit,
        screenshot_path=shot,
    )


def submitted(page):
    return page.evaluate("() => window.__submitted || null")


# ------------------------------------------------------------------- url --


def test_application_is_appended_to_a_posting_url_only():
    assert application_url_for("https://jobs.ashbyhq.com/helios/8f2c4d1e") == (
        "https://jobs.ashbyhq.com/helios/8f2c4d1e/application"
    )
    assert application_url_for("https://jobs.ashbyhq.com/helios/8f2c4d1e/") == (
        "https://jobs.ashbyhq.com/helios/8f2c4d1e/application"
    )
    for unchanged in (
        "https://jobs.ashbyhq.com/helios/8f2c4d1e/application",
        "https://jobs.ashbyhq.com/helios",
        "https://example.com/careers/1",
        "",
    ):
        assert application_url_for(unchanged) == unchanged


def test_the_handler_claims_ashby_urls_and_no_others():
    handler = AshbyHandler()
    assert handler.matches("https://jobs.ashbyhq.com/helios/1")
    assert not handler.matches("https://jobs.lever.co/acme/1")
    assert not handler.matches("https://notashbyhq.com/x")


# ------------------------------------------------------- the resume parser --


def test_what_is_on_file_wins_over_what_the_resume_parser_wrote(page, packet):
    result = run(page, packet)

    assert result.outcome == "dry_run", result.error
    assert page.evaluate("() => window.__autofillRan || false") is True
    assert page.evaluate("() => window.__autofillDone || false") is True
    assert page.input_value('input[name="_systemfield_name"]') == "Mark Shield"
    assert page.input_value('input[name="_systemfield_email"]') == "mark@example.com"
    assert page.input_value('input[name="_systemfield_phone"]') == "+1 555 0100"
    assert page.evaluate("() => document.getElementById('file-resume').files[0].name") == (
        "mark-shield.pdf"
    )


def test_the_wait_is_reported_and_skipped_when_there_is_no_banner(page, packet):
    handler = AshbyHandler()
    page.goto(fixture_url(), wait_until="domcontentloaded")
    assert handler.wait_for_autofill(page) is False, "no banner, no wait"

    page.evaluate("() => document.getElementById('autofill-banner').classList.remove('hidden')")
    page.evaluate(
        "() => setTimeout(() => document.getElementById('autofill-banner')"
        ".classList.add('hidden'), 400)"
    )
    assert handler.wait_for_autofill(page) is True
    assert page.locator("#autofill-banner").is_visible() is False


def test_a_banner_that_never_goes_does_not_hang_the_run(page, packet):
    handler = AshbyHandler()
    handler.autofill_wait_ms = 600
    page.goto(fixture_url(), wait_until="domcontentloaded")
    page.evaluate("() => document.getElementById('autofill-banner').classList.remove('hidden')")
    assert handler.wait_for_autofill(page) is True
    assert page.locator("#autofill-banner").is_visible() is True


# --------------------------------------------------------------- the form --


def test_the_form_behind_the_apply_button_is_opened(page, packet):
    result = run(page, packet, variant="apply_button")
    assert result.outcome == "dry_run", result.error
    assert page.input_value('input[name="_systemfield_name"]') == "Mark Shield"


def test_a_uuid_named_question_is_matched_by_its_label(page, packet):
    result = run(page, packet)
    by_label = {f.label: f for f in result.fields}
    assert by_label["LinkedIn"].key.startswith("a3f19c7e")
    assert by_label["Cover letter"].kind == "textarea"
    auth = next(f for f in result.fields if f.label.startswith("Are you legally"))
    assert auth.kind == "radio" and auth.options == ["Yes", "No"]

    values = {f.key: f.value for f in result.filled}
    assert values[by_label["LinkedIn"].key] == packet.links["linkedin"]
    assert values[by_label["Cover letter"].key] == packet.cover_letter
    assert values[auth.key] == "Yes"


def test_a_required_question_nobody_can_answer_stops_before_the_button(page, packet):
    result = run(page, packet, submit=True, variant="needs_input")
    assert result.outcome == "needs_input"
    assert submitted(page) is None
    asked = [n for n in result.needed if n.required]
    assert len(asked) == 1 and asked[0].answer_key == "start_date"


# ----------------------------------------------------------------- send --


def test_submitting_sends_what_is_on_file_and_reads_the_confirmation(page, packet):
    result = run(page, packet, submit=True)
    assert result.outcome == "submitted", result.error
    assert "application received" in (result.confirmation or "").lower()
    record = submitted(page)
    assert record["_systemfield_name"] == "Mark Shield"
    assert record["_systemfield_email"] == "mark@example.com"
    assert record["_systemfield_resume"] == "mark-shield.pdf"


def test_a_turnstile_after_the_button_is_blocked(page, packet):
    result = run(page, packet, submit=True, variant="turnstile")
    assert result.outcome == "blocked"
    assert "Turnstile" in result.error and "--headed" in result.error
    assert submitted(page) is None
