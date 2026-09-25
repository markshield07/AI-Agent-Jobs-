"""LinkedIn Easy Apply: a modal of steps over the posting, for a signed-in member.

The posting is linkedin.com/jobs/view/<id>. A member sees an "Easy Apply"
button when the company takes applications through LinkedIn, and a plain
"Apply" that opens the company's site otherwise; a visitor who is not signed
in sees "Sign in" and is sent through /authwall or /login. The Easy Apply
button opens a `.jobs-easy-apply-modal` dialog whose steps are contact info,
resume, questions and review, moved through with "Continue to next step",
"Review your application" and finally "Submit application". LinkedIn
prefills contact details from the profile and offers the resumes uploaded
before; the tailored resume is uploaded anyway, since it is the one written
for this job.

Attested by the reference study: AutoApply `bot/apply/linkedin.py` (the
button, Next and Submit selector chains, a ten-step limit) and job-agent
`linkedin_easy_apply.py` (the aria-labels "Continue to next step", "Review
your application" and "Submit application", the form-element classes, the
sent text "application was sent to", and signing in from a saved browser
state rather than a password). Assumptions of this module are marked where
they occur.
"""

from __future__ import annotations

import re
from typing import Any

from jobagent.apply.browser.fill import click_first_visible, wait_settled
from jobagent.apply.handlers.wizard import WizardHandler


class LinkedInHandler(WizardHandler):
    ats = "linkedin"
    site = "linkedin"
    hosts = ("linkedin.com",)
    own_hosts = ("linkedin.com",)
    easy_apply_selectors = (
        "button.jobs-apply-button[aria-label*='Easy Apply']",
        "button[aria-label*='Easy Apply']",
        ".jobs-apply-button--top-card button:has-text('Easy Apply')",
        "button:has-text('Easy Apply')",
    )
    # The company-site button is the same `.jobs-apply-button`, labelled
    # "Apply" and marked as leaving LinkedIn (assumption: the aria-label reads
    # "Apply to <title> on company website", as it did when last seen).
    external_apply_selectors = (
        "button.jobs-apply-button[aria-label*='company website']",
        "a.jobs-apply-button[href]:not([href*='linkedin.com'])",
        "button[role='link'].jobs-apply-button",
    )
    signed_out_selectors = (
        "a.nav__button-secondary[href*='login']",
        "button:has-text('Sign in to apply')",
        "a:has-text('Sign in to apply')",
        "form.join-form",
    )
    root_selectors = (
        ".jobs-easy-apply-modal",
        "[data-test-modal][role='dialog']",
        "div[role='dialog']",
    )
    next_selectors = (
        "button[aria-label='Continue to next step']",
        "button[aria-label='Review your application']",
        "button[aria-label*='Continue']",
        "button[aria-label*='Next']",
        "button[aria-label*='Review']",
    )
    submit_selectors = (
        "button[aria-label='Submit application']",
        "button[aria-label*='Submit application']",
    )
    step_error_selectors = (
        ".artdeco-inline-feedback--error",
        ".fb-dash-form-element-error",
        "[data-test-form-element-error-messages]",
    )
    success_signals = (
        "application was sent",
        "your application was sent",
        "application submitted",
    )
    success_url = re.compile(r"/post-apply\b|[?&]postApply", re.IGNORECASE)
    max_steps = 12
    settle_ms = 6_000

    def matches(self, url: str) -> bool:
        # Only postings: a company page or profile link is not an application.
        return super().matches(url) and "/jobs/" in (url or "")

    def application_url(self, url: str) -> str:
        """The posting page, from any of the URLs LinkedIn uses for one job.

        Search results link to /jobs/search/?currentJobId=<id> or
        /jobs/collections/...?currentJobId=<id>; the Easy Apply button is on
        /jobs/view/<id>/ either way, and opening that page directly keeps the
        search list's own controls out of the way.
        """
        found = re.search(r"[?&]currentJobId=(\d+)", url or "")
        if found and "linkedin.com" in url:
            return f"https://www.linkedin.com/jobs/view/{found.group(1)}/"
        return url

    def discard(self, page: Any) -> None:
        """Close the modal and discard the draft, so nothing half-filled stays saved."""
        if not click_first_visible(
            page, ("button[aria-label='Dismiss']", ".artdeco-modal__dismiss")
        ):
            return
        wait_settled(page, 2_000)
        click_first_visible(
            page,
            (
                "button[data-control-name='discard_application_confirm_btn']",
                "button[data-test-dialog-secondary-btn]:has-text('Discard')",
                "button:has-text('Discard')",
            ),
        )
