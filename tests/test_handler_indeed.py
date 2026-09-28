"""The Indeed handler against a posting and the application it opens, in a real browser.

The fixture is the posting with its header search boxes and "Apply now", and
the smartapply flow: contact information prefilled from the profile, resume,
employer questions, review. Its variants prove the flow in a new tab, a
signed-out visitor, a company-site posting, a question nobody on file
answers, and a step that rejects its answers.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from jobagent.apply.answering import make_answerer
from jobagent.apply.handlers import default_handlers, handler_for
from jobagent.apply.handlers.indeed import IndeedHandler
from jobagent.apply.models import Packet

FIXTURE = Path(__file__).parent / "fixtures" / "forms" / "indeed.html"


def fixture_url(variant: str = "") -> str:
    return FIXTURE.as_uri() + (f"?variant={variant}" if variant else "")


@pytest.fixture
def packet(tmp_path) -> Packet:
    resume = tmp_path / "mark-shield.pdf"
    resume.write_bytes(b"%PDF-1.4 resume")
    return Packet(
        job={
            "id": "in1",
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
        answers={"work_authorization": "Yes", "visa_sponsorship": "No"},
    )


def run(page, packet, *, submit=False, variant="", shot=None):
    packet.job["url"] = fixture_url(variant)
    answerer = make_answerer(packet, completer=None, allow_model=False)
    return IndeedHandler().apply(page, packet, answerer, submit=submit, screenshot_path=shot)


def js(page, name):
    return page.evaluate(f"() => window.{name} === undefined ? null : window.{name}")


# ------------------------------------------------------------------- url --


def test_the_handler_claims_indeed_postings_only():
    handler = IndeedHandler()
    assert handler.matches("https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6789")
    assert handler.matches("https://www.indeed.com/jobs?q=platform&vjk=0a1b2c3d4e5f6789")
    assert handler.matches("https://smartapply.indeed.com/beta/indeedapply/form/contact-info")
    assert not handler.matches("https://www.indeed.com/cmp/Acme-Robotics")
    assert not handler.matches("https://jobs.lever.co/acme/1")


def test_a_search_result_url_opens_the_posting_page():
    handler = IndeedHandler()
    assert (
        handler.application_url("https://www.indeed.com/jobs?q=platform&vjk=0a1b2c3d4e5f6789")
        == "https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6789"
    )
    view = "https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6789"
    assert handler.application_url(view) == view


def test_indeed_jobs_get_this_handler_not_the_generic_one():
    picked = handler_for(
        "https://www.indeed.com/viewjob?jk=0a1b2c3d4e5f6789", None, default_handlers()
    )
    assert picked is not None and picked.ats == "indeed"


def test_a_flow_that_lands_off_indeed_counts_as_the_company_site():
    handler = IndeedHandler()
    assert handler._left_site(SimpleNamespace(url="https://careers.acme.example/apply/12"))
    assert not handler._left_site(SimpleNamespace(url="https://smartapply.indeed.com/form/1"))
    assert not handler._left_site(SimpleNamespace(url="https://www.indeed.com/viewjob?jk=1"))
    assert not handler._left_site(SimpleNamespace(url="file:///tmp/indeed.html"))


# ------------------------------------------------------------- the flow --


@pytest.mark.usefixtures("page")
def test_a_dry_run_walks_every_step_and_stops_at_submit(page, packet, tmp_path):
    shot = tmp_path / "shots" / "indeed.png"
    result = run(page, packet, shot=str(shot))

    assert result.outcome == "dry_run", result.error
    assert js(page, "__steps") == ["contact", "resume", "questions", "review"]
    assert js(page, "__submitted") is None
    assert shot.is_file()
    assert js(page, "__contact") == {
        "first": "Mark",
        "last": "Shield",
        "phone": "+1 555 0100",
        "city": "Austin, TX",
    }
    assert js(page, "__resume") == "mark-shield.pdf"
    assert js(page, "__questions") == {"auth": "Yes", "sponsor": "No", "k8s": ""}
    sources = {f.label: f.source for f in result.filled}
    assert sources["City, State"] == "prefilled"
    assert sources["First name"] == "prefilled"


@pytest.mark.usefixtures("page")
def test_the_header_search_boxes_are_never_touched(page, packet):
    result = run(page, packet)
    assert all("text-input" not in f.selector for f in result.fields)


@pytest.mark.usefixtures("page")
def test_auto_submits_and_reads_the_confirmation(page, packet):
    result = run(page, packet, submit=True)
    assert result.outcome == "submitted", result.error
    assert js(page, "__submitted") is True
    assert "application has been submitted" in result.confirmation.lower()


@pytest.mark.usefixtures("page")
def test_a_flow_in_a_new_tab_is_followed_and_closed(page, packet):
    result = run(page, packet, submit=True, variant="newtab")
    assert result.outcome == "submitted", result.error
    assert "flow=1" in result.final_url, "the result is read from the application's tab"
    assert len(page.context.pages) == 1, "the application's tab is closed afterwards"


@pytest.mark.usefixtures("page")
def test_a_question_nobody_can_answer_stops_before_submit(page, packet):
    result = run(page, packet, submit=True, variant="needs_input")
    assert result.outcome == "needs_input"
    assert js(page, "__submitted") is None
    asked = [n for n in result.needed if n.required]
    assert len(asked) == 1 and "Kubernetes" in asked[0].label


@pytest.mark.usefixtures("page")
def test_a_visitor_who_is_not_signed_in_is_told_to_log_in(page, packet):
    result = run(page, packet, submit=True, variant="signed_out")
    assert result.outcome == "blocked"
    assert "jobagent login indeed" in result.error


@pytest.mark.usefixtures("page")
def test_a_company_site_posting_is_blocked_not_followed(page, packet):
    result = run(page, packet, submit=True, variant="external")
    assert result.outcome == "blocked"
    assert "company's own site" in result.error
    assert js(page, "__left") is None


@pytest.mark.usefixtures("page")
def test_a_step_that_rejects_its_answers_stops_with_the_message(page, packet):
    result = run(page, packet, submit=True, variant="invalid")
    assert result.outcome == "blocked"
    assert "Answer this question to continue" in result.error
    assert js(page, "__submitted") is None
