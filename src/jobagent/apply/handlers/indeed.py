"""Indeed's own application: a page of steps at smartapply.indeed.com.

The posting is indeed.com/viewjob?jk=<key>. When the employer takes
applications through Indeed, a signed-in visitor sees "Apply now" (the
`#indeedApplyButton`), which opens the steps on smartapply.indeed.com, in the
same tab or a new one. Otherwise the button reads "Apply on company site" and
leaves Indeed. The steps are contact information, the resume (one saved on
the Indeed profile, or an upload), the employer's questions, relevant
experience and a review page, moved through with "Continue" and finished
with "Submit your application". Indeed prefills contact details from the
profile.

Attested by the reference study: AutoApply `bot/apply/indeed.py` (the apply
button chain, the `ia-continueButton` class, the Submit chain, and treating a
redirect off indeed.com as needing a person). Indeed's error and success
wording are this module's assumptions, marked where they occur.
"""

from __future__ import annotations

import re

from jobagent.apply.handlers.wizard import WizardHandler


class IndeedHandler(WizardHandler):
    ats = "indeed"
    site = "indeed"
    hosts = ("indeed.com",)
    own_hosts = ("indeed.com",)
    easy_apply_selectors = (
        "button#indeedApplyButton",
        "button[id*='indeedApply']",
        ".jobsearch-IndeedApplyButton-newDesign",
        "button[aria-label*='Apply now']",
        "button:has-text('Apply now')",
    )
    external_apply_selectors = (
        "button[aria-label*='company site']",
        "a[aria-label*='company site']",
        "button:has-text('Apply on company site')",
        "a:has-text('Apply on company site')",
    )
    signed_out_selectors = (
        "a[href*='secure.indeed.com/auth']:has-text('Sign in')",
        "button:has-text('Sign in to apply')",
    )
    # Assumption: the steps render inside `main` (or the `ia-container` the
    # older flow used); the site header with its search box is outside it.
    root_selectors = ("#ia-container", "main", "form")
    next_selectors = (
        "button.ia-continueButton:not([type='submit'])",
        "button[data-testid*='continue' i]",
        "button:has-text('Continue')",
        "button:has-text('Review your application')",
        "button[aria-label*='Continue']",
        "button[id*='continue']",
    )
    submit_selectors = (
        "button:has-text('Submit your application')",
        "button[aria-label*='Submit']",
        "button.ia-continueButton[type='submit']",
        "button[id*='submit']",
    )
    # Assumption: errors carry "error" in a class or data-testid, as
    # Indeed's design system names them.
    step_error_selectors = (
        "[data-testid*='error' i]",
        "[class*='ErrorMessage']",
        "[class*='error-text' i]",
        "[role='alert']",
    )
    success_signals = (
        "your application has been submitted",
        "application has been submitted",
        "application submitted",
    )
    success_url = re.compile(r"/post-apply\b|/apply/confirmation\b", re.IGNORECASE)
    max_steps = 12
    settle_ms = 6_000

    def matches(self, url: str) -> bool:
        return super().matches(url) and bool(
            re.search(r"/viewjob|/rc/clk|/pagead/clk|smartapply|[?&]jk=|[?&]vjk=", url or "")
        )

    def application_url(self, url: str) -> str:
        """The posting page, from a search result's `vjk=` or `jk=` link."""
        found = re.search(r"[?&]v?jk=([0-9a-f]+)", url or "")
        if found and "indeed.com" in url and "/viewjob" not in url:
            return f"https://www.indeed.com/viewjob?jk={found.group(1)}"
        return url
