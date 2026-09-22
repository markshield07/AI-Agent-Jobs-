"""Greenhouse: one page, one form, and ids that three of the reference repos
agree on.

The posting page is the form page (boards.greenhouse.io/<board>/jobs/<id>,
job-boards.greenhouse.io on the newer layout), so `application_url` leaves
the URL alone. The older layout keeps the form below the posting behind an
"Apply" anchor. A company site that fronts Greenhouse with its own page holds
the form in an iframe, or nothing fillable at all; the plain embed form at
boards.greenhouse.io/embed/job_app?for=<board>&token=<id> is then the page to
fill (job-agent). After the button some boards ask for a code they emailed
before they accept the application: a block, not a failure, which a person
finishes in a visible browser (job-pilot, job-agent).

Attested by the reference study, sections A2 (AutoApply), B2 (job-pilot), E2
(job-agent) and the cross-repo summary: the canonical ids `#first_name
#last_name #email #phone #resume #cover_letter #candidate-location #country
#gender #hispanic_ethnicity #veteran_status #disability_status #submit_app`,
custom questions named `job_application[answers_attributes][N][...]`, yes/no
groups as `fieldset.checkbox`, the apply anchor `a#apply_button` /
`a[href*='#app']`, the CTA texts "Apply Now" / "Apply for this Job", the
`#grnhse_iframe` embed and its URL shape, the security modal
`#security-input-0` / `input[id^="security-input"]`, and an invisible
reCAPTCHA (never a wall by itself). This module's own assumptions are marked
where they occur.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from jobagent.apply.browser.dom import discover_fields
from jobagent.apply.browser.fill import click_first_visible, wait_settled
from jobagent.apply.handlers.base import COOKIE_BUTTON_SELECTORS, BaseHandler
from jobagent.apply.models import Answerer, FillPlan, FormField, HandlerResult, Packet

log = logging.getLogger(__name__)

EMBED_PATH = "/embed/job_app"
# The Greenhouse iframe a company page carries (job-agent E2: `#grnhse_iframe,
# iframe[src*="greenhouse.io"]`); its src is the embed form itself.
EMBED_IFRAME_SELECTORS: tuple[str, ...] = (
    "#grnhse_iframe",
    'iframe[src*="greenhouse.io/embed/job_app"]',
    'iframe[src*="greenhouse.io"]',
)
_JOB_PATH = re.compile(r"^/(?P<board>[\w.-]+)/jobs/(?P<token>\d+)/?$")
# Assumption: EU-hosted boards (job-boards.eu.greenhouse.io) serve the embed
# from boards.eu.greenhouse.io. The study only covers the US host.
_EU_HOST = re.compile(r"(?:^|\.)eu\.greenhouse\.io$")


def embed_url_for(url: str) -> str | None:
    """boards.greenhouse.io/embed/job_app?for=<board>&token=<id> for a posting
    URL on any Greenhouse host, or None when the URL does not name a job."""
    parts = urlparse(url or "")
    host = (parts.netloc or "").lower()
    if host != "greenhouse.io" and not host.endswith(".greenhouse.io"):
        return None
    board = token = ""
    match = _JOB_PATH.match(parts.path or "")
    if match:
        board, token = match.group("board"), match.group("token")
    elif (parts.path or "").rstrip("/") == EMBED_PATH:
        query = parse_qs(parts.query)
        board = (query.get("for") or [""])[0]
        token = (query.get("token") or [""])[0]
    if not board or not token:
        return None
    subdomain = "boards.eu" if _EU_HOST.search(host) else "boards"
    return f"https://{subdomain}.greenhouse.io{EMBED_PATH}?for={board}&token={token}"


def typeahead_aware(answerer: Answerer) -> Answerer:
    """Plan a typeahead as free text, fill it as a combobox.

    Greenhouse's location box (#candidate-location, react-select) lists nothing
    until something is typed, so discovery reads it as a select with no
    options and the planner, matching the contact's city against an empty
    list, would report it as a question nobody can answer. The city is on
    file; what the planner needs is to hand it over as text. The filler still
    sees a select, types the city and takes the suggestion it brings up
    (fill._combobox_pick). The fields are restored before they reach the
    filler, so a needed-input report keeps the field's real kind.
    """

    def plan(fields: list[FormField]) -> FillPlan:
        typeaheads = [f for f in fields if f.kind == "select" and not f.options]
        for field in typeaheads:
            field.kind = "text"
        try:
            return answerer(fields)
        finally:
            for field in typeaheads:
                field.kind = "select"

    return plan


class GreenhouseHandler(BaseHandler):
    ats = "greenhouse"
    hosts = ("boards.greenhouse.io", "job-boards.greenhouse.io", "greenhouse.io", "grnh.se")
    # AutoApply A2 (the first four) and job-agent E2's CTA texts for company
    # fronts. A bare "Apply" button is left out: on a careers site it also
    # names filters and unrelated actions.
    apply_button_selectors = (
        "a#apply_button",
        "a[href*='#app']",
        "button[id*='apply']",
        "a.btn[href*='apply']",
        'a:has-text("Apply for this Job")',
        'button:has-text("Apply for this Job")',
        'a:has-text("Apply Now")',
        'button:has-text("Apply Now")',
    )
    # job-agent E2 `_form_present`: the first-name box is on every Greenhouse
    # form, old layout and new. A bare `form` would also match a site search.
    form_ready_selectors = (
        "input#first_name",
        'input[name*="first_name"]',
        'input[autocomplete="given-name"]',
        'input[placeholder*="First" i]',
    )
    # A2, E2: `#submit_app` on both layouts (a button on the new one, an
    # input[type=submit] on the old), then the generic fallbacks.
    submit_selectors = (
        "#submit_app",
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Submit Application")',
        'button:has-text("Submit")',
    )
    # B2 (`#security-input-0`), E2 (`input[id^="security-input"]`).
    security_code_selectors = ("#security-input-0", 'input[id^="security-input"]')
    # How long to give a React-rendered form after the page has settled
    # before concluding there is none (job-agent waits 8s; the form renders
    # well within a settled page's networkidle in practice).
    form_wait_ms: int = 3_000

    # -- what differs on Greenhouse ---------------------------------------

    def application_url(self, url: str) -> str:
        """The posting page carries the form on every Greenhouse layout, and a
        grnh.se short link redirects to it in the browser, so the URL is used
        as given."""
        return url

    def prepare(self, page: Any) -> None:
        """Banner away, the apply anchor pressed; then, when the page still
        shows no form, the plain embed form for this job."""
        super().prepare(page)
        if self._form_visible(page) or self._wait_for_form(page):
            return
        embed = self.embed_url(page)
        if not embed or embed == page.url:
            return
        log.info("greenhouse: no form on %s, opening the embed form %s", page.url, embed)
        page.goto(embed, wait_until="domcontentloaded", timeout=self.settle_ms * 3)
        wait_settled(page, self.settle_ms)
        click_first_visible(page, COOKIE_BUTTON_SELECTORS)

    def discover(self, page: Any) -> list[FormField]:
        """Only a Greenhouse form is read: a company front's own inputs (a job
        search, a newsletter box) are not an application."""
        if not self._form_visible(page):
            return []
        return discover_fields(page)

    def apply(
        self,
        page: Any,
        packet: Packet,
        answerer: Answerer,
        *,
        submit: bool,
        screenshot_path: str | None = None,
    ) -> HandlerResult:
        return super().apply(
            page,
            packet,
            typeahead_aware(answerer),
            submit=submit,
            screenshot_path=screenshot_path,
        )

    # -- helpers ---------------------------------------------------------

    def embed_url(self, page: Any) -> str | None:
        """The embed form for the job on `page`: the src of a Greenhouse iframe
        when the page has one, else built from the page's own URL."""
        for selector in EMBED_IFRAME_SELECTORS:
            try:
                frame = page.locator(selector).first
                if frame.count() == 0:
                    continue
                src = str(frame.evaluate("el => el.src") or "")
            except Exception as exc:
                log.debug("greenhouse: iframe %s unreadable: %s", selector, exc)
                continue
            if src and not src.startswith("about:"):
                return src
        return embed_url_for(page.url or "")

    def _wait_for_form(self, page: Any) -> bool:
        try:
            page.wait_for_selector(
                ", ".join(self.form_ready_selectors),
                state="visible",
                timeout=self.form_wait_ms,
            )
            return True
        except Exception:
            return False
