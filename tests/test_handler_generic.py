"""The generic fallback against a company's own application page.

It runs where no handler claims the site, so the thing to prove is that it
fills a real application and leaves anything that is not one alone: the site
search box on the same page is a form too.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.apply.answering import make_answerer
from jobagent.apply.handlers import default_handlers, handler_for
from jobagent.apply.handlers.generic import (
    NOT_AN_APPLICATION,
    GenericHandler,
    looks_like_an_application,
)
from jobagent.apply.models import FormField, Packet

FIXTURE = Path(__file__).parent / "fixtures" / "forms" / "careers.html"

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
            "title": "Instrumentation Engineer",
            "company": "Marlow Instruments",
            "url": fixture_url(),
            "apply_url": None,
        },
        contact={
            "full_name": "Mark Shield",
            "first_name": "Mark",
            "last_name": "Shield",
            "email": "mark@example.com",
            "phone": "+1 555 0100",
        },
        resume_path=str(resume),
        cover_letter="I have built field instruments.",
        links={"linkedin": "https://www.linkedin.com/in/markshield"},
    )


def run(page, packet, *, submit=False, variant=""):
    packet.job["url"] = fixture_url(variant)
    return GenericHandler().apply(
        page,
        packet,
        make_answerer(packet, completer=None, allow_model=False),
        submit=submit,
    )


# ------------------------------------------------------------ what it is --


def test_it_claims_no_url_and_is_only_the_fallback():
    handlers = default_handlers()
    generic = GenericHandler()
    assert generic.matches("https://example.com/careers/1") is False
    assert generic.matches("https://boards.greenhouse.io/x/jobs/1") is False
    assert handler_for("https://example.com/careers/1", None, handlers).ats == "generic"
    assert handler_for("https://jobs.lever.co/acme/1", None, handlers).ats == "lever"
    assert handler_for("https://example.com/x", None, [generic]).ats == "generic"
    assert handler_for("https://example.com/x", None, []) is None


def test_what_counts_as_an_application():
    def field(key, section, kind="text"):
        return FormField(key=key, label=key, kind=kind, section=section)

    assert looks_like_an_application([field("cv", "resume", "file")])
    assert looks_like_an_application([field("name", "contact"), field("cv", "links")])
    assert looks_like_an_application(
        [field("name", "contact"), field("email", "contact"), field("phone", "contact")]
    )
    assert not looks_like_an_application([field("q", "other")])
    assert not looks_like_an_application([field("name", "contact"), field("q", "other")])
    assert not looks_like_an_application([])


# ---------------------------------------------------------------- filling --


def test_it_fills_a_company_application_and_leaves_the_search_box_alone(page, packet):
    result = run(page, packet)

    assert result.outcome == "dry_run", result.error
    values = {f.key: f.value for f in result.filled}
    assert values["applicant_name"] == "Mark Shield"
    assert values["applicant_email"] == "mark@example.com"
    assert values["applicant_phone"] == "+1 555 0100"
    assert values["applicant_linkedin"] == packet.links["linkedin"]
    assert values["cover_letter"] == packet.cover_letter
    assert next(f for f in result.filled if f.key == "cv").file_path == packet.resume_path
    assert page.input_value("#q") == "", "the site search is not an application field"
    assert "q" not in values


def test_it_opens_a_form_behind_an_apply_button(page, packet):
    result = run(page, packet, variant="hidden_form")
    assert result.outcome == "dry_run", result.error
    assert page.input_value("#applicant-name") == "Mark Shield"


def test_submitting_reads_the_confirmation(page, packet):
    result = run(page, packet, submit=True)
    assert result.outcome == "submitted", result.error
    assert "received your application" in (result.confirmation or "").lower()
    record = page.evaluate("() => window.__submitted")
    assert record["applicant_name"] == "Mark Shield" and record["cv"] == "mark-shield.pdf"
    assert page.evaluate("() => window.__searched || null") is None


def test_a_page_with_only_a_search_box_is_not_an_application(page, packet):
    result = run(page, packet, submit=True, variant="search_only")
    assert result.outcome == "failed" and result.error == NOT_AN_APPLICATION
    assert page.input_value("#q") == ""
    assert page.evaluate("() => window.__searched || null") is None


# ------------------------------------------------ pressing through to it --

FLOW = Path(__file__).parent / "fixtures" / "forms" / "careers_flow.html"


def run_flow(page, packet, variant, *, submit=False):
    packet.job["url"] = f"{FLOW.as_uri()}?variant={variant}"
    handler = GenericHandler()
    handler.settle_ms = 1_500
    handler.new_tab_ms = 1_500
    result = handler.apply(
        page, packet, make_answerer(packet, completer=None, allow_model=False), submit=submit
    )
    return result


@pytest.mark.parametrize(
    "variant", ["link", "newtab", "choice", "guest", "reveal", "consent", "filter", "onetrust"]
)
def test_it_presses_through_to_the_form_past_alerts_and_other_sites(page, packet, variant):
    result = run_flow(page, packet, variant)
    assert result.outcome == "dry_run", result.error
    values = {f.key: f.value for f in result.filled}
    assert values["first_name"] == "Mark" and values["email"] == "mark@example.com"
    assert "alert_email" not in values, "the talent-community box is not the application"
    assert page.evaluate("() => window.__opened") in ([], None), "no LinkedIn or Indeed apply"


def test_a_pressed_through_form_is_submitted(page, packet):
    result = run_flow(page, packet, "choice", submit=True)
    assert result.outcome == "submitted", result.error
    assert page.evaluate("() => window.__submitted")["first_name"] == "Mark"
    assert page.evaluate("() => window.__subscribed || null") is None


def test_a_sign_in_with_no_guest_way_is_a_login_wall(page, packet):
    result = run_flow(page, packet, "wall")
    assert result.outcome == "blocked"
    assert result.error == "the page wants a login before the form"


def test_an_apply_that_lands_on_workday_hands_over(monkeypatch):
    from jobagent.apply.handlers.generic import HANDED_OFF

    class Page:
        url = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/Remote/Engineer_R1"

    handler = GenericHandler()
    assert "workday" in HANDED_OFF and handler._handed_off(Page())
    assert handler.discover(Page()) == [], "nothing is filled by the generic handler there"
    Page.url = "https://careers.example.com/job/1"
    assert not GenericHandler()._handed_off(Page())


def test_a_search_filters_apply_is_not_the_jobs(page, packet):
    result = run_flow(page, packet, "filter")
    assert result.outcome == "dry_run", result.error
    assert page.evaluate("() => window.__filtered || null") is None


def test_a_page_that_applies_only_through_indeed_goes_to_indeeds_posting(page, packet):
    packet.job["url"] = "https://www.indeed.com/viewjob?jk=abc123"
    packet.job["apply_url"] = f"{FLOW.as_uri()}?variant=indeedapply"
    handler = GenericHandler()
    handler.settle_ms = 1_500
    result = handler.apply(
        page, packet, make_answerer(packet, completer=None, allow_model=False), submit=False
    )
    assert result.outcome == "blocked" and "Indeed Apply" in result.error
    assert result.external_url == "https://www.indeed.com/viewjob?jk=abc123"


def test_a_phenom_step_takes_the_fields_and_the_resume_by_its_button(page, packet):
    packet.job["source"] = "indeed"
    result = run_flow(page, packet, "phenom")
    assert result.outcome == "dry_run", (result.error, result.needed)
    values = {f.key: f.value for f in result.filled}
    assert values["firstName"] == "Mark" and values["email"] == "mark@example.com"
    assert not any("api_key" in f.key for f in result.filled), "not LinkedIn's widget box"
    first = page.evaluate("() => window.__page1")
    assert first["resume"] == "mark-shield.pdf"
    # The resume went first and the page finished reading it before the form
    # was filled: the Last Name it emptied was filled after.
    assert first["lastName"] == "Shield"
    assert "homePhone" not in values and first["homePhone"] == "", "not the mobile again"
    # What the page chose itself stays; a question an answer brought up is filled.
    assert values["division"] == "Networks" and first["division"] == "Networks"
    assert {f.key: f.source for f in result.filled}["division"] == "prefilled"
    assert values["sourceType"] == "Job Board"
    assert values["source"] == "Indeed" and first["source"] == "Indeed", "the job's board"
    assert values["cust_Preferred"] == "Email"
    # Page 2 filled after Next; the dry run stops at Review without sending.
    assert values["li"] == "https://www.linkedin.com/in/markshield"
    assert values["gender"] == "I decline to self-identify"
    assert page.is_visible("#submit-app")
    assert page.evaluate("() => window.__submitted || null") is None


def test_a_multi_page_application_is_sent_from_its_last_page(page, packet):
    packet.job["source"] = "indeed"
    result = run_flow(page, packet, "phenom", submit=True)
    assert result.outcome == "submitted", (result.error, result.needed)
    sent = page.evaluate("() => window.__submitted")
    assert sent["page1"]["firstName"] == "Mark" and sent["page1"]["source"] == "Indeed"
    assert sent["page2"]["gender"] == "I decline to self-identify"


def test_a_page_that_will_not_go_on_stops_with_what_it_said(page, packet):
    packet.job["source"] = "indeed"
    result = run_flow(page, packet, "phenomstuck", submit=True)
    assert result.outcome == "needs_input"
    stuck = [n for n in result.needed if n.required]
    assert [n.key for n in stuck] == ["page-1"]
    assert "Please enter a valid postal code" in stuck[0].reason
    assert page.evaluate("() => window.__submitted || null") is None


def _work_history(packet):
    from jobagent.resume.facts import Fact

    packet.contact["location"] = "Austin, Texas, United States"
    packet.facts = [
        Fact(
            id=1,
            kind="role",
            text="Ran the network operations center at Globex",
            detail={
                "employer": "Globex Corporation",
                "title": "Network Operations Manager",
                "start": "April 2018",
                "end": "Present",
            },
        ),
        Fact(
            id=2,
            kind="role",
            text="Built the campus network at Initech",
            detail={
                "employer": "Initech",
                "title": "Network Engineer",
                "start": "Sep 2013",
                "end": "Jun 2015",
                "location": "Denver, CO",
            },
        ),
    ]


def test_work_history_dates_go_through_the_calendar_and_the_home_town_is_not_a_job_location(
    page, packet
):
    packet.job["source"] = "indeed"
    _work_history(packet)
    result = run_flow(page, packet, "phenomwork", submit=True)
    assert result.outcome == "needs_input", result.error
    picked = page.evaluate("() => window.__picked")
    assert picked == {
        "experienceData[0].fromTo.startDate": "04/01/2018",
        "experienceData[1].fromTo.startDate": "09/01/2013",
        "experienceData[1].fromTo.endDate": "06/01/2015",
    }, "each date set through its calendar; the job still held has no end"
    assert page.locator(".react-datepicker-popper").count() == 0, "no calendar left open"
    ticked = page.evaluate(
        "() => [0, 1].map((n) => document.getElementById("
        "'experienceData[' + n + '].fromTo.currentlyWorkHere').checked)"
    )
    assert ticked == [True, False], "only the job that runs to the present"
    held = page.evaluate(
        "() => [0, 1].map((n) => document.getElementById("
        "'experienceData[' + n + '].jobLocation').value)"
    )
    assert held == ["", "Denver, CO"], "the job's own location, never the home town"
    # The calendar's own lists are not questions; the location with none on
    # file is asked, once for that employer, and stops the page going on.
    asked = [n for n in result.needed if n.required]
    assert [n.key for n in asked] == ["experienceData[0].jobLocation"]
    assert asked[0].answer_key == "location_at:globex_corporation"
    assert not any(not (n.label or "").strip() for n in result.needed)
    values = {f.key: f.value for f in result.filled}
    assert values["experienceData[0].title"] == "Network Operations Manager"
    assert page.evaluate("() => window.__submitted || null") is None


def test_a_job_location_on_file_for_the_employer_lets_the_page_go_on(page, packet):
    packet.job["source"] = "indeed"
    _work_history(packet)
    packet.answers["location_at:globex_corporation"] = "Remote"
    result = run_flow(page, packet, "phenomwork")
    assert result.outcome == "dry_run", (result.error, result.needed)
    assert page.is_visible("#submit-app"), "went on to Review"


def test_a_resume_the_page_never_shows_is_not_taken_as_attached(page, packet):
    result = run_flow(page, packet, "phenomsilent", submit=True)
    assert result.outcome == "needs_input"
    assert [n.key for n in result.needed if n.required] == ["resume"]
    assert "never showed it attached" in result.needed[0].reason


@pytest.mark.parametrize(
    ("variant", "why"),
    [("signup", "verify an email or phone"), ("community", "talent community")],
)
def test_a_sign_up_in_place_of_the_application_is_stopped(page, packet, variant, why):
    result = run_flow(page, packet, variant, submit=True)
    assert result.outcome == "blocked"
    assert why in result.error
    assert not result.filled
    assert page.input_value("input[type=email]") == "", "nothing typed into it"


def test_a_posting_past_its_deadline_is_not_applied_to(page, packet):
    result = run_flow(page, packet, "closed", submit=True)
    assert result.outcome == "blocked"
    assert result.wrong_place and "deadline (2020-09-27) has passed" in result.wrong_place
    assert page.evaluate("() => window.__submitted || null") is None


def test_a_form_with_nothing_filled_is_never_a_success(page, packet, monkeypatch):
    monkeypatch.setattr(GenericHandler, "fill", lambda self, page, fields, plan: ([], [], []))
    result = run_flow(page, packet, "link", submit=True)
    assert result.outcome == "failed" and "could fill none of it" in result.error
    assert page.evaluate("() => window.__submitted || null") is None
