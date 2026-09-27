"""The Workday handler against a company's posting and its steps, in a real browser.

The fixture draws the posting and the application with the data-automation-id
values Workday uses, and with its own controls: dropdowns that are buttons, a
"How did you hear about us?" prompt whose categories open further lists, and
a date in three spin buttons. Its variants prove the stops: a sign-in page, a
question nobody on file answers, a step that will not save, a closed posting,
a Submit with no confirmation.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from jobagent.apply.answering import make_answerer, question_key
from jobagent.apply.handlers import default_handlers, handler_for
from jobagent.apply.handlers.workday import WorkdayHandler, parse_date
from jobagent.apply.models import Packet

FIXTURE = Path(__file__).parent / "fixtures" / "forms" / "workday.html"
POSTING = (
    "https://acme.wd5.myworkdayjobs.com/acmecareers/job/USA-Remote/"
    "Network-Engineering-Manager_R12345"
)


def fixture_url(variant: str = "") -> str:
    return FIXTURE.as_uri() + (f"?variant={variant}" if variant else "")


@pytest.fixture
def packet(tmp_path) -> Packet:
    resume = tmp_path / "mark-shield.pdf"
    resume.write_bytes(b"%PDF-1.4 resume")
    return Packet(
        job={
            "id": "wd1",
            "title": "Network Engineering Manager",
            "company": "Acme Robotics",
            "source": "linkedin",
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
        cover_letter="I run networks.",
        answers={
            "work_authorization": "Yes",
            "visa_sponsorship": "No",
            question_key("Have you previously worked for Acme Robotics?"): "No",
        },
    )


def run(page, packet, *, submit=False, variant="", shot=None):
    packet.job["url"] = fixture_url(variant)
    answerer = make_answerer(packet, completer=None, allow_model=False)
    handler = WorkdayHandler()
    handler.settle_ms = 500
    handler.pause_ms = 100
    return handler.apply(page, packet, answerer, submit=submit, screenshot_path=shot)


def js(page, name):
    return page.evaluate(f"() => window.{name} === undefined ? null : window.{name}")


# ------------------------------------------------------------------- url --


def test_the_handler_claims_workday_postings():
    handler = WorkdayHandler()
    assert handler.matches(POSTING)
    assert handler.matches("https://acme.wd1.myworkdaysite.com/recruiting/acme/careers/job/1")
    assert not handler.matches("https://www.linkedin.com/jobs/view/1/")
    picked = handler_for(POSTING, None, default_handlers())
    assert picked is not None and picked.ats == "workday"


def test_dates_are_read_in_the_shapes_answers_come_in():
    assert parse_date("2026-09-27") == (9, 27, 2026)
    assert parse_date("9/27/2026") == (9, 27, 2026)
    assert parse_date("September 27, 2026") == (9, 27, 2026)
    assert parse_date("09/2026") == (9, None, 2026)
    assert parse_date("2019") == (None, None, 2019)
    assert parse_date("two weeks after an offer") is None


# --------------------------------------------------------------- dry run --


@pytest.mark.usefixtures("page")
def test_a_dry_run_walks_every_step_and_stops_at_submit(page, packet, tmp_path):
    shot = tmp_path / "shots" / "workday.png"
    result = run(page, packet, shot=str(shot))

    assert result.outcome == "dry_run", result.error
    assert js(page, "__start") == "autofill", "Autofill with Resume is the start chosen"
    assert js(page, "__steps") == [
        "autofill",
        "info",
        "experience",
        "questions",
        "disclosures",
        "selfid",
        "review",
    ]
    assert js(page, "__submitted") is None, "a dry run never presses Submit"
    assert shot.is_file()


@pytest.mark.usefixtures("page")
def test_workdays_own_controls_are_filled_like_a_person_would(page, packet):
    result = run(page, packet)
    assert result.outcome == "dry_run", result.error
    answers = js(page, "__answers")
    assert answers["info"]["phoneType"] == "Mobile", "phone device type is housekeeping"
    assert answers["info"]["phone"] == "+1 555 0100"
    assert answers["info"]["previous"] == "false"
    assert answers["questions"] == {"auth": "Yes", "sponsor": "No", "teams": ""}
    assert answers["disclosures"]["gender"] == "I do not wish to self-identify"
    assert answers["disclosures"]["veteran"] == "I don't wish to answer"
    assert answers["disclosures"]["terms"] is True
    today = date.today()
    assert answers["selfid"] == {
        "name": "Mark Shield",
        "month": f"{today.month:02d}",
        "day": f"{today.day:02d}",
        "year": str(today.year),
        "disability": ["decline"],
    }


@pytest.mark.usefixtures("page")
def test_how_did_you_hear_follows_the_board_the_job_came_from(page, packet):
    result = run(page, packet)
    assert js(page, "__answers")["info"]["source"] == "LinkedIn"
    by_label = {f.label: f for f in result.filled}
    assert by_label["How Did You Hear About Us?"].value == "Job Board"


@pytest.mark.usefixtures("page")
def test_a_category_with_no_known_board_is_asked_not_guessed(page, packet):
    packet.job["source"] = "greenhouse"
    result = run(page, packet, submit=True)
    assert result.outcome == "needs_input"
    asked = [n for n in result.needed if n.required]
    assert [n.label for n in asked] == ["How Did You Hear About Us?"]
    assert "further list" in asked[0].reason
    assert js(page, "__steps") == ["autofill", "info"]


@pytest.mark.usefixtures("page")
def test_what_autofill_read_is_kept_and_the_resume_goes_up_once(page, packet):
    packet.contact["first_name"] = "Marcus"
    result = run(page, packet)
    by_label = {f.label: f for f in result.filled}
    assert by_label["Given Name(s)"].source == "prefilled"
    assert js(page, "__answers")["info"]["first"] == "Mark"
    assert by_label["Country"].value == "United States of America"
    assert js(page, "__answers")["autofillResume"] == "mark-shield.pdf"
    assert js(page, "__uploads") == 1, "the resume shown under My Experience is not sent again"
    assert js(page, "__answers")["experienceResume"] is None


@pytest.mark.usefixtures("page")
def test_apply_manually_uploads_the_resume_under_my_experience(page, packet):
    result = run(page, packet, variant="manual")
    assert result.outcome == "dry_run", result.error
    assert js(page, "__start") == "manual"
    assert js(page, "__answers")["experienceResume"] == "mark-shield.pdf"
    assert js(page, "__uploads") == 1


@pytest.mark.usefixtures("page")
def test_the_job_search_box_in_the_header_is_never_touched(page, packet):
    result = run(page, packet)
    assert all("job-search" not in f.selector for f in result.fields)
    assert page.input_value("#job-search") == ""


# ---------------------------------------------------------------- submit --


@pytest.mark.usefixtures("page")
def test_auto_submits_and_reads_the_confirmation(page, packet, tmp_path):
    result = run(page, packet, submit=True, shot=str(tmp_path / "sent.png"))
    assert result.outcome == "submitted", result.error
    assert js(page, "__submitted") is True
    assert "Application Submitted" in result.confirmation


@pytest.mark.usefixtures("page")
def test_submit_with_no_confirmation_is_unconfirmed(page, packet):
    result = run(page, packet, submit=True, variant="unconfirmed")
    assert js(page, "__submitted") is True
    assert result.outcome == "unconfirmed"


# ----------------------------------------------------------------- stops --


@pytest.mark.usefixtures("page")
def test_a_sign_in_page_stops_and_names_the_login_command(page, packet):
    packet.job["apply_url"] = None
    result = run(page, packet, submit=True, variant="signed_out")
    assert result.outcome == "blocked"
    assert "jobagent login workday" in result.error
    assert "own account" in result.error
    assert page.input_value("#si-password") == "", "a password is never typed"
    assert js(page, "__submitted") is None


def test_the_login_hint_names_the_companys_site():
    handler = WorkdayHandler()
    handler._posting = POSTING
    hint = handler.login_hint()
    assert "acme.wd5.myworkdayjobs.com" in hint
    assert f"jobagent login workday {POSTING}" in hint


@pytest.mark.usefixtures("page")
def test_a_question_nobody_can_answer_stops_before_submit(page, packet):
    result = run(page, packet, submit=True, variant="needs_input")
    assert result.outcome == "needs_input"
    asked = [n for n in result.needed if n.required]
    assert len(asked) == 1 and "network engineering teams" in asked[0].label
    assert js(page, "__submitted") is None


@pytest.mark.usefixtures("page")
def test_the_same_question_answered_goes_through(page, packet):
    key = question_key("How many years have you managed network engineering teams?")
    packet.answers[key] = "6"
    result = run(page, packet, submit=True, variant="needs_input")
    assert result.outcome == "submitted", result.error
    assert js(page, "__answers")["questions"]["teams"] == "6"


@pytest.mark.usefixtures("page")
def test_a_step_that_will_not_save_stops_with_its_message(page, packet):
    result = run(page, packet, submit=True, variant="invalid")
    assert result.outcome == "blocked"
    assert "could not be saved" in result.error
    assert js(page, "__submitted") is None


@pytest.mark.usefixtures("page")
def test_a_closed_posting_says_so(page, packet):
    result = run(page, packet, submit=True, variant="closed")
    assert result.outcome == "blocked"
    assert result.error == "the Workday posting is closed"
