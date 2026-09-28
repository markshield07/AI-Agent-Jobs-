"""The Lever handler against a Lever application page, in a real browser.

The fixture is the page the reference repos describe: hidden decoy submit
before the fields, `cards[<uuid>][fieldN]` custom questions, the location
typeahead, EEO selects, an invisible hCaptcha and errors in
`.application-error`. Its variants turn on the cases worth proving: a
question nobody on file can answer, a server error, a captcha after the
button.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.apply.answering import make_answerer
from jobagent.apply.handlers.lever import LeverHandler, apply_url_for
from jobagent.apply.models import Packet

FIXTURE = Path(__file__).parent / "fixtures" / "forms" / "lever.html"

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
            "title": "Senior Platform Engineer",
            "company": "Acme Robotics",
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
        cover_letter="I build control planes.\n\nI would like to do it at Acme.",
        cover_letter_path=str(letter),
        links={
            "linkedin": "https://www.linkedin.com/in/markshield",
            "github": "https://github.com/markshield07",
            "website": "https://markshield.dev",
        },
        answers={
            "work_authorization": "Yes",
            "visa_sponsorship": "No",
            "current_company": "Northwind Robotics",
            "q:why do you want to work at acme robotics": "Warehouse robotics is my field.",
        },
    )


def run(page, packet, *, submit=False, variant="", shot=None):
    packet.job["url"] = fixture_url(variant)
    handler = LeverHandler()
    answerer = make_answerer(packet, completer=None, allow_model=False)
    return handler.apply(page, packet, answerer, submit=submit, screenshot_path=shot)


def submitted(page):
    return page.evaluate("() => window.__submitted || null")


# ------------------------------------------------------------------- url --


def test_apply_is_appended_to_a_posting_url_only():
    assert apply_url_for("https://jobs.lever.co/acme/8f2c-4d1e") == (
        "https://jobs.lever.co/acme/8f2c-4d1e/apply"
    )
    assert apply_url_for("https://jobs.lever.co/acme/8f2c-4d1e/") == (
        "https://jobs.lever.co/acme/8f2c-4d1e/apply"
    )
    for unchanged in (
        "https://jobs.lever.co/acme/8f2c-4d1e/apply",
        "https://jobs.lever.co/acme/8f2c-4d1e/thanks",
        "https://jobs.lever.co/acme",
        "https://example.com/careers/1",
        "",
    ):
        assert apply_url_for(unchanged) == unchanged


def test_the_handler_claims_lever_urls_and_no_others():
    handler = LeverHandler()
    assert handler.matches("https://jobs.lever.co/acme/1")
    assert handler.matches("https://hire.lever.co/acme/1")
    assert not handler.matches("https://boards.greenhouse.io/acme/jobs/1")
    assert not handler.matches("https://notlever.co/acme/1")


# --------------------------------------------------------------- dry run --


def test_a_dry_run_fills_the_form_and_leaves_it_unsent(page, packet, tmp_path):
    shot = tmp_path / "shots" / "lever.png"
    result = run(page, packet, shot=str(shot))

    assert result.outcome == "dry_run", result.error
    assert result.error is None and [n for n in result.needed if n.required] == []
    assert submitted(page) is None, "a dry run never presses the button"
    assert Path(result.screenshot_path).is_file() and shot.is_file()
    assert result.final_url.startswith("file://")

    filled = {f.key: f for f in result.filled}
    values = {key: f.value for key, f in filled.items()}
    assert values["name"] == "Mark Shield"
    assert values["email"] == "mark@example.com"
    assert values["phone"] == "+1 555 0100"
    assert values["urls[LinkedIn]"] == "https://www.linkedin.com/in/markshield"
    assert values["urls[GitHub]"] == "https://github.com/markshield07"
    assert filled["resume"].file_path == packet.resume_path
    assert filled["resume"].source == "resume"

    # What the page itself holds, not just what the plan intended.
    on_page = page.evaluate(
        """() => {
            const form = document.getElementById('application-form');
            const out = {};
            form.querySelectorAll('input, select, textarea').forEach(el => {
                if (el.type === 'file') out[el.name] = el.files.length ? el.files[0].name : '';
                else if (el.type === 'radio' || el.type === 'checkbox') {
                    if (el.checked) out[el.name] = el.value;
                } else if (el.value) out[el.name] = el.value;
            });
            return out;
        }"""
    )
    assert on_page["name"] == "Mark Shield"
    assert on_page["resume"] == "mark-shield.pdf"
    assert on_page["consent[privacy]"] == "on", "a required consent box is ticked"
    assert on_page["eeo[gender]"] == "Decline to self identify"
    assert on_page["eeo[race]"] == "Decline to self identify"


def test_the_additional_information_box_takes_the_cover_letter(page, packet):
    result = run(page, packet)
    letter = next(f for f in result.filled if f.key == "comments")
    assert letter.value == packet.cover_letter and letter.source == "cover_letter"
    assert page.input_value('textarea[name="comments"]') == packet.cover_letter

    fields = {f.key: f for f in result.fields}
    assert fields["comments"].section == "questions", "the field's own section is restored"


def test_a_banked_answer_fills_a_custom_question(page, packet):
    result = run(page, packet)
    values = {f.key: f.value for f in result.filled}
    why = next(k for k in values if k.startswith("cards[3f7a2c1e"))
    assert values[why] == "Warehouse robotics is my field."
    auth = next(k for k in values if k.startswith("cards[9b8c7d6e"))
    assert values[auth] == "Yes"
    assert {f.source for f in result.filled if f.key in (why, auth)} == {"answer_bank"}


def test_an_optional_question_with_no_answer_is_left_alone(page, packet):
    result = run(page, packet)
    heard = next((f for f in result.fields if f.label.startswith("How did you hear")), None)
    assert heard is not None
    assert page.input_value(heard.selector) in ("", "Job board", "Other")
    assert not any(n.required for n in result.needed)


# ---------------------------------------------------------- needs input --


def test_a_question_nobody_can_answer_stops_before_the_button(page, packet):
    packet.answers.pop("visa_sponsorship")
    result = run(page, packet, submit=True, variant="needs_input")

    assert result.outcome == "needs_input"
    assert submitted(page) is None, "the form is never sent with a gap in it"
    asked = [n for n in result.needed if n.required]
    assert len(asked) == 1
    assert "sponsorship" in asked[0].label.lower()
    assert asked[0].options == ["Yes", "No"] and asked[0].answer_key
    assert result.filled, "what could be filled was still filled"


def test_the_same_question_answered_goes_through(page, packet):
    packet.answers["visa_sponsorship"] = "No"
    result = run(page, packet, submit=True, variant="needs_input")
    assert result.outcome == "submitted", result.error
    record = submitted(page)
    sponsorship = next(k for k in record if k.startswith("cards[c1d2e3f4"))
    assert record[sponsorship] == "No"


# ----------------------------------------------------------------- send --


def test_submitting_presses_the_visible_button_and_reads_the_confirmation(page, packet):
    result = run(page, packet, submit=True)

    assert result.outcome == "submitted", result.error
    assert "application submitted" in (result.confirmation or "").lower()
    record = submitted(page)
    assert record["submitter"] == "btn-submit", "the hidden decoy is passed over"
    assert page.evaluate("() => window.__decoyClicked || false") is False
    assert record["name"] == "Mark Shield" and record["resume"] == "mark-shield.pdf"
    assert record["comments"] == packet.cover_letter
    assert record["location"].startswith("Austin")


def test_a_server_error_is_a_failure_with_what_the_page_said(page, packet):
    result = run(page, packet, submit=True, variant="error")
    assert result.outcome == "failed"
    assert "error processing your application" in result.error.lower()
    assert submitted(page) is None and result.filled


def test_a_captcha_after_the_button_is_blocked_not_failed(page, packet):
    result = run(page, packet, submit=True, variant="captcha")
    assert result.outcome == "blocked"
    assert "captcha" in result.error.lower()
    assert "visible browser" in result.error
    assert submitted(page) is None


# ------------------------------------------------------------ no form --


def test_a_page_with_no_form_is_a_failure_not_a_crash(page, packet, tmp_path):
    blank = tmp_path / "blank.html"
    blank.write_text("<html><body><h1>This job is closed</h1></body></html>")
    packet.job["url"] = blank.as_uri()
    handler = LeverHandler()
    result = handler.apply(page, packet, make_answerer(packet, allow_model=False), submit=True)
    assert result.outcome == "failed" and "form" in result.error


def test_a_url_that_will_not_open_is_a_failure(page, packet):
    packet.job["url"] = "file:///nowhere/definitely-missing.html"
    handler = LeverHandler()
    result = handler.apply(page, packet, make_answerer(packet, allow_model=False), submit=True)
    assert result.outcome == "failed" and "could not open" in result.error


def test_a_job_with_no_url_is_a_failure(page, packet):
    packet.job["url"] = packet.job["apply_url"] = None
    handler = LeverHandler()
    result = handler.apply(page, packet, make_answerer(packet, allow_model=False), submit=True)
    assert result.outcome == "failed" and result.error == "the job has no URL"
