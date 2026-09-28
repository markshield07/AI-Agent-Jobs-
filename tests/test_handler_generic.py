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
