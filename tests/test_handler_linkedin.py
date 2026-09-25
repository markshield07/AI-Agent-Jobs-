"""The LinkedIn Easy Apply handler against a posting and its modal, in a real browser.

The fixture is the page the reference repos describe for a signed-in member:
a search box in the nav, the Easy Apply button, and a modal of steps (contact
info prefilled from the profile, resume, questions, review). Its variants
prove the stops: a signed-out visitor, a company-site posting, a question
nobody on file answers, a step that rejects its answers, a Next that does
nothing, a Submit with no confirmation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.apply.answering import make_answerer
from jobagent.apply.handlers import default_handlers, handler_for
from jobagent.apply.handlers.linkedin import LinkedInHandler
from jobagent.apply.models import Packet

FIXTURE = Path(__file__).parent / "fixtures" / "forms" / "linkedin.html"


def fixture_url(variant: str = "") -> str:
    return FIXTURE.as_uri() + (f"?variant={variant}" if variant else "")


@pytest.fixture
def packet(tmp_path) -> Packet:
    resume = tmp_path / "mark-shield.pdf"
    resume.write_bytes(b"%PDF-1.4 resume")
    return Packet(
        job={
            "id": "li1",
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
        cover_letter="I build control planes.",
        answers={"work_authorization": "Yes", "visa_sponsorship": "No"},
    )


def run(page, packet, *, submit=False, variant="", shot=None):
    packet.job["url"] = fixture_url(variant)
    answerer = make_answerer(packet, completer=None, allow_model=False)
    return LinkedInHandler().apply(page, packet, answerer, submit=submit, screenshot_path=shot)


def js(page, name):
    return page.evaluate(f"() => window.{name} === undefined ? null : window.{name}")


# ------------------------------------------------------------------- url --


def test_the_handler_claims_linkedin_postings_only():
    handler = LinkedInHandler()
    assert handler.matches("https://www.linkedin.com/jobs/view/4012345678/")
    assert handler.matches("https://www.linkedin.com/jobs/search/?currentJobId=4012345678")
    assert not handler.matches("https://www.linkedin.com/in/markshield")
    assert not handler.matches("https://boards.greenhouse.io/acme/jobs/1")


def test_a_search_result_url_opens_the_posting_page():
    handler = LinkedInHandler()
    assert (
        handler.application_url(
            "https://www.linkedin.com/jobs/search/?keywords=platform&currentJobId=4012345678"
        )
        == "https://www.linkedin.com/jobs/view/4012345678/"
    )
    view = "https://www.linkedin.com/jobs/view/4012345678/"
    assert handler.application_url(view) == view


def test_linkedin_jobs_get_this_handler_not_the_generic_one():
    handlers = default_handlers()
    picked = handler_for("https://www.linkedin.com/jobs/view/1/", "linkedin", handlers)
    assert picked is not None and picked.ats == "linkedin"
    picked = handler_for(None, "linkedin", handlers)
    assert picked is not None and picked.ats == "linkedin"


# --------------------------------------------------------------- dry run --


@pytest.mark.usefixtures("page")
def test_a_dry_run_walks_every_step_and_stops_at_submit(page, packet, tmp_path):
    shot = tmp_path / "shots" / "linkedin.png"
    result = run(page, packet, shot=str(shot))

    assert result.outcome == "dry_run", result.error
    assert js(page, "__steps") == ["contact", "resume", "questions", "review"]
    assert js(page, "__submitted") is None, "a dry run never presses Submit"
    assert shot.is_file()
    assert js(page, "__discarded") is True, "the draft is discarded, not left saved"


@pytest.mark.usefixtures("page")
def test_what_the_profile_prefilled_is_kept_and_the_gaps_are_filled(page, packet):
    packet.contact["first_name"] = "Marcus"  # would differ from the profile's own
    result = run(page, packet)

    by_key = {f.label: f for f in result.filled}
    assert by_key["First name"].source == "prefilled"
    assert by_key["First name"].value == "Mark", "the profile's value is not overwritten"
    assert by_key["Email address"].source == "prefilled"
    assert by_key["Mobile phone number"].value == "+1 555 0100"
    assert js(page, "__contact") == {
        "first": "Mark",
        "last": "Shield",
        "email": "mark@example.com",
        "phone": "+1 555 0100",
    }


@pytest.mark.usefixtures("page")
def test_the_tailored_resume_and_banked_answers_go_in(page, packet):
    result = run(page, packet)
    assert result.outcome == "dry_run", result.error
    assert js(page, "__resume") == "mark-shield.pdf"
    assert js(page, "__questions") == {"auth": "Yes", "sponsor": "No", "k8s": ""}


@pytest.mark.usefixtures("page")
def test_the_search_box_outside_the_modal_is_never_touched(page, packet):
    result = run(page, packet)
    assert all("global-nav-search" not in f.selector for f in result.fields)
    assert page.input_value("#global-nav-search") == ""


# ---------------------------------------------------------------- submit --


@pytest.mark.usefixtures("page")
def test_auto_submits_and_reads_the_confirmation(page, packet, tmp_path):
    result = run(page, packet, submit=True, shot=str(tmp_path / "sent.png"))
    assert result.outcome == "submitted", result.error
    assert js(page, "__submitted") is True
    assert "application was sent to Acme Robotics" in result.confirmation
    assert js(page, "__discarded") is None, "a sent application has no draft to discard"


@pytest.mark.usefixtures("page")
def test_submit_with_no_confirmation_is_unconfirmed(page, packet):
    result = run(page, packet, submit=True, variant="unconfirmed")
    assert js(page, "__submitted") is True
    assert result.outcome == "unconfirmed"
    assert "applied-jobs" in result.error


# ----------------------------------------------------------------- stops --


@pytest.mark.usefixtures("page")
def test_a_question_nobody_can_answer_stops_before_submit(page, packet):
    result = run(page, packet, submit=True, variant="needs_input")
    assert result.outcome == "needs_input"
    assert js(page, "__submitted") is None
    asked = [n for n in result.needed if n.required]
    assert len(asked) == 1 and "Kubernetes" in asked[0].label
    assert asked[0].answer_key
    assert js(page, "__discarded") is True


@pytest.mark.usefixtures("page")
def test_the_same_question_answered_goes_through(page, packet):
    packet.answers["q:how many years of work experience do you have with kubernetes"] = "4"
    result = run(page, packet, submit=True, variant="needs_input")
    assert result.outcome == "submitted", result.error
    assert js(page, "__questions")["k8s"] == "4"


@pytest.mark.usefixtures("page")
def test_a_visitor_who_is_not_signed_in_is_told_to_log_in(page, packet):
    result = run(page, packet, submit=True, variant="signed_out")
    assert result.outcome == "blocked"
    assert "jobagent login linkedin" in result.error
    assert js(page, "__steps") == []


@pytest.mark.usefixtures("page")
def test_a_company_site_posting_is_blocked_not_followed(page, packet):
    result = run(page, packet, submit=True, variant="external")
    assert result.outcome == "blocked"
    assert "company's own site" in result.error
    assert js(page, "__left") is None, "the company-site button is never pressed"


@pytest.mark.usefixtures("page")
def test_a_step_that_rejects_its_answers_stops_with_the_message(page, packet):
    result = run(page, packet, submit=True, variant="invalid")
    assert result.outcome == "blocked"
    assert "Please enter a valid answer" in result.error
    assert js(page, "__submitted") is None
    assert js(page, "__discarded") is True


@pytest.mark.usefixtures("page")
def test_a_next_that_does_nothing_stops_instead_of_looping(page, packet):
    result = run(page, packet, submit=True, variant="stuck")
    assert result.outcome == "blocked"
    assert "did not move on" in result.error
    assert js(page, "__steps") == ["contact", "resume", "questions"]
