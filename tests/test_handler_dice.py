"""The Dice Easy Apply handler against a posting and its wizard, in a real browser.

The fixture is the posting the Dice bots consulted describe for a signed-in
member: a search form in the header, the "Easy Apply" button, and the wizard
(the resume card with Replace, the employer's questions, a review). Its
variants prove the stops: a signed-out visitor, a company-site posting, a
posting applied to before, a question nobody on file answers, and a step
that rejects its answers.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from jobagent.apply.answering import make_answerer
from jobagent.apply.handlers import default_handlers, handler_for
from jobagent.apply.handlers.dice import REFRESH_URL, DiceHandler, refresh_sign_in
from jobagent.apply.models import Packet
from jobagent.apply.sessions import jwt_expiry
from jobagent.discovery.ats import detect_ats

FIXTURE = Path(__file__).parent / "fixtures" / "forms" / "dice.html"
POSTING = "https://www.dice.com/job-detail/0b6a2f52-4c1e-4f0e-9d7c-2a1b3c4d5e6f"


def fixture_url(variant: str = "") -> str:
    return FIXTURE.as_uri() + (f"?variant={variant}" if variant else "")


@pytest.fixture
def packet(tmp_path) -> Packet:
    resume = tmp_path / "mark-shield-acme.pdf"
    resume.write_bytes(b"%PDF-1.4 resume")
    return Packet(
        job={
            "id": "dice1",
            "title": "Senior Network Engineer",
            "company": "Acme Networks",
            "url": fixture_url(),
            "apply_url": None,
        },
        contact={
            "full_name": "Mark Shield",
            "first_name": "Mark",
            "last_name": "Shield",
            "email": "mark@example.com",
            "phone": "+1 555 0100",
            "location": "Menifee, CA",
        },
        resume_path=str(resume),
        answers={"work_authorization": "Yes", "visa_sponsorship": "No"},
    )


def run(page, packet, *, submit=False, variant="", shot=None):
    packet.job["url"] = fixture_url(variant)
    answerer = make_answerer(packet, completer=None, allow_model=False)
    return DiceHandler().apply(page, packet, answerer, submit=submit, screenshot_path=shot)


def js(page, name):
    return page.evaluate(f"() => window.{name} === undefined ? null : window.{name}")


# ------------------------------------------------------------------- url --


def test_the_handler_claims_dice_postings_only():
    handler = DiceHandler()
    assert handler.matches(POSTING)
    assert handler.matches(f"{POSTING}?searchlink=search%2F%3Fq%3Dnetwork")
    assert handler.matches("https://www.dice.com/job-applications/0b6a2f52/wizard")
    assert not handler.matches("https://www.dice.com/company-profile/acme")
    assert not handler.matches("https://www.linkedin.com/jobs/view/1/")


def test_a_search_link_opens_the_bare_posting_page():
    handler = DiceHandler()
    assert handler.application_url(f"{POSTING}?searchlink=search&searchId=abc") == POSTING
    assert handler.application_url(POSTING) == POSTING


def test_dice_jobs_get_this_handler_not_the_generic_one():
    assert detect_ats(POSTING) == "dice"
    picked = handler_for(POSTING, None, default_handlers())
    assert picked is not None and picked.ats == "dice"


# ------------------------------------------------------------- the flow --


@pytest.mark.usefixtures("page")
def test_a_dry_run_walks_every_step_and_stops_at_submit(page, packet, tmp_path):
    shot = tmp_path / "shots" / "dice.png"
    result = run(page, packet, shot=str(shot))

    assert result.outcome == "dry_run", result.error
    assert js(page, "__steps") == ["resume", "resume", "questions", "review"]
    assert js(page, "__submitted") is None, "a dry run never presses Submit"
    assert shot.is_file()
    assert js(page, "__resume") == "mark-shield-acme.pdf", "the tailored resume replaces the old"
    assert js(page, "__questions") == {"auth": "Yes", "sponsor": "No", "bgp": ""}


@pytest.mark.usefixtures("page")
def test_the_resume_is_replaced_through_the_cards_three_dot_menu(page, packet):
    """The live wizard's step 1 (Mark's PC, 2026-10-01): only a three-dot menu
    on the resume card, a cookie banner over Next, the route announcer as an
    alert, and Next spinning before the questions show."""
    result = run(page, packet)
    assert result.outcome == "dry_run", result.error
    assert js(page, "__resume") == "mark-shield-acme.pdf"
    assert js(page, "__deleted") is None, "Delete in the same menu is never pressed"
    assert js(page, "__cover") is None, "the cover letter's box is left empty"
    assert js(page, "__cookies") == "rejected"
    resume = next(f for f in result.filled if f.label == "Resume")
    assert resume.file_path == packet.resume_path


@pytest.mark.usefixtures("page")
def test_step_one_drawn_again_with_the_new_file_is_left_alone_and_updated(page, packet):
    """The PC's second dry run (2026-10-01): after Replace, step 1 came back
    naming the tailored file, with no Replace in the menu and "Update" for
    Next. The resume is done; Update goes on to the questions."""
    result = run(page, packet)
    assert result.outcome == "dry_run", result.error
    assert js(page, "__steps")[:3] == ["resume", "resume", "questions"]
    assert not [n for n in result.needed if n.required]


@pytest.mark.usefixtures("page")
def test_a_cookie_banner_drawn_late_in_a_frame_is_rejected(page, packet):
    result = run(page, packet, variant="cookie_frame")
    assert result.outcome == "dry_run", result.error
    assert js(page, "__cookies") == "rejected"


@pytest.mark.usefixtures("page")
def test_a_plain_replace_button_still_takes_the_resume(page, packet):
    result = run(page, packet, variant="replace_button")
    assert result.outcome == "dry_run", result.error
    assert js(page, "__resume") == "mark-shield-acme.pdf"


@pytest.mark.usefixtures("page")
def test_the_header_search_form_is_never_touched(page, packet):
    result = run(page, packet)
    assert result.outcome == "dry_run", result.error
    assert all("search" not in (f.selector or "") for f in result.fields)
    assert not any(f.label.startswith("Job title") for f in result.fields)


@pytest.mark.usefixtures("page")
def test_auto_submits_and_reads_the_confirmation(page, packet):
    result = run(page, packet, submit=True)
    assert result.outcome == "submitted", result.error
    assert js(page, "__submitted") is True
    assert "application submitted" in result.confirmation.lower()


@pytest.mark.usefixtures("page")
def test_a_replace_that_draws_a_file_box_still_takes_the_resume(page, packet):
    result = run(page, packet, variant="replace_box")
    assert result.outcome == "dry_run", result.error
    assert js(page, "__resume") == "mark-shield-acme.pdf"


@pytest.mark.usefixtures("page")
def test_a_question_nobody_can_answer_stops_before_submit(page, packet):
    result = run(page, packet, submit=True, variant="needs_input")
    assert result.outcome == "needs_input"
    assert js(page, "__submitted") is None
    asked = [n for n in result.needed if n.required]
    assert len(asked) == 1 and "BGP" in asked[0].label


@pytest.mark.usefixtures("page")
def test_a_visitor_who_is_not_signed_in_is_told_to_log_in(page, packet):
    result = run(page, packet, submit=True, variant="signed_out")
    assert result.outcome == "blocked"
    assert "jobagent login dice" in result.error
    assert result.sign_in == "dice"


@pytest.mark.usefixtures("page")
def test_a_company_site_posting_is_blocked_not_followed(page, packet):
    result = run(page, packet, submit=True, variant="external")
    assert result.outcome == "blocked"
    assert "company's own site" in result.error
    assert result.external_url == "https://careers.acme.example/apply/77"
    assert js(page, "__left") is None


@pytest.mark.usefixtures("page")
def test_a_posting_applied_to_before_is_not_applied_to_again(page, packet):
    result = run(page, packet, submit=True, variant="applied")
    assert result.outcome == "blocked"
    assert js(page, "__steps") is None


@pytest.mark.usefixtures("page")
def test_a_step_that_rejects_its_answers_stops_with_the_message(page, packet):
    result = run(page, packet, submit=True, variant="invalid")
    assert result.outcome == "blocked"
    assert "Please answer this question" in result.error
    assert js(page, "__submitted") is None


# -------------------------------------------------------------- sign-in --


def token(exp: float) -> str:
    def part(data: dict) -> str:
        raw = json.dumps(data).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{part({'alg': 'HS256'})}.{part({'sub': 'mark', 'exp': exp})}.sig"


def test_the_token_expiry_is_read_from_the_access_cookie():
    assert jwt_expiry(token(1_800_000_000)) == 1_800_000_000
    assert jwt_expiry("not-a-token") is None
    assert jwt_expiry("a.!!!.c") is None


class FakeContext:
    def __init__(self, exp: float | None):
        self.exp = exp

    def cookies(self, url=None):
        if self.exp is None:
            return []
        return [{"name": "access", "value": token(self.exp), "domain": ".dice.com"}]


class FakePage:
    def __init__(self, context: FakeContext, renews_to: float | None):
        self.context = context
        self.renews_to = renews_to
        self.visited: list[str] = []

    def goto(self, url, **kw):
        self.visited.append(url)

    def wait_for_timeout(self, ms):
        if self.visited and self.renews_to is not None:
            self.context.exp = self.renews_to


def test_a_fresh_sign_in_is_left_alone():
    page = FakePage(FakeContext(exp=10_000), renews_to=None)
    assert refresh_sign_in(page, now=1_000)
    assert page.visited == []


def test_a_stale_sign_in_is_renewed_on_the_home_feed():
    page = FakePage(FakeContext(exp=900), renews_to=10_000)
    assert refresh_sign_in(page, now=1_000)
    assert page.visited == [REFRESH_URL]


def test_a_sign_in_dice_will_not_renew_is_reported():
    page = FakePage(FakeContext(exp=900), renews_to=None)
    assert not refresh_sign_in(page, now=1_000)
