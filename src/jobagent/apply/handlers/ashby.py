"""Ashby: the form behind `/application`, and a resume parser that writes the
boxes itself.

Two things make Ashby its own handler. The form lives at
jobs.ashbyhq.com/<company>/<job-id>/application, which every reference repo
appends rather than hunting for the link (job-agent E4). And Ashby reads the
resume as soon as it is attached and fills the contact boxes from what it
found, showing an "Autofilling" banner while it works (job-pilot B4 waits for
that banner to go). Anything typed before it finishes is overwritten, so this
handler puts the resume on first, waits for the banner, and fills the rest
after. Ashby also fronts some boards with a Cloudflare Turnstile; that is a
block for a person to clear, never something to work around.

Attested by the reference study, sections A4 (AutoApply), B4 (job-pilot) and
E4 (job-agent): the `/application` suffix, the apply CTA
`[data-testid="apply-button"]` / "Apply for this Job", the field names
`_systemfield_name` and `_systemfield_email`, the "Autofilling" banner, the
Turnstile frame at challenges.cloudflare.com, and the submit chain. This
module's own assumptions are marked where they occur.
"""

from __future__ import annotations

import logging
import re
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse, urlunparse

from jobagent.apply.browser.fill import detect_captcha, fill_plan
from jobagent.apply.handlers.base import BaseHandler
from jobagent.apply.models import FillPlan, FormField

log = logging.getLogger(__name__)

APPLICATION_SUFFIX = "/application"
# jobs.ashbyhq.com/<company>/<job-id>. A longer path is already a sub-page.
_POSTING_PATH = re.compile(r"^/[\w.-]+/[\w-]+/?$")
# The banner Ashby shows while it reads the attached resume (job-pilot B4).
AUTOFILL_TEXT = re.compile(r"autofill", re.IGNORECASE)


def application_url_for(url: str) -> str:
    """The posting URL with `/application` on the end, on an Ashby host."""
    parts = urlparse(url or "")
    host = (parts.netloc or "").lower()
    if host != "ashbyhq.com" and not host.endswith(".ashbyhq.com"):
        return url
    path = parts.path or ""
    if not _POSTING_PATH.match(path):
        return url
    return urlunparse(parts._replace(path=path.rstrip("/") + APPLICATION_SUFFIX))


class AshbyHandler(BaseHandler):
    ats = "ashby"
    hosts = ("jobs.ashbyhq.com", "ashbyhq.com")
    apply_button_selectors = (
        '[data-testid="apply-button"]',
        'a:has-text("Apply for this Job")',
        'button:has-text("Apply for this Job")',
        'a:has-text("Apply")',
        'button:has-text("Apply")',
    )
    form_ready_selectors = (
        'input[name="_systemfield_name"]',
        'input[name="_systemfield_email"]',
        "form input[type='email']",
        "form",
    )
    submit_selectors = (
        'button:has-text("Submit Application")',
        'button[type="submit"]',
        'button:has-text("Submit")',
    )
    # How long to let the resume parser finish before filling the rest. The
    # banner usually goes well inside this; the wait ends when it does.
    autofill_wait_ms: int = 8_000
    autofill_poll_ms: int = 200

    def application_url(self, url: str) -> str:
        return application_url_for(url)

    def fill(self, page: Any, fields: list[FormField], plan: FillPlan) -> tuple[list, list, list]:
        """The resume first, then whatever Ashby wrote from it is overwritten
        by what is actually on file."""
        uploads = [f for f in plan.fills if f.file_path]
        rest = [f for f in plan.fills if not f.file_path]
        if not uploads:
            return fill_plan(page, fields, plan)
        filled, needed, notes = fill_plan(page, fields, replace(plan, fills=uploads))
        if self.wait_for_autofill(page):
            notes.append("waited for Ashby to read the resume before filling the rest")
        more, more_needed, more_notes = fill_plan(page, fields, replace(plan, fills=rest))
        return [*filled, *more], [*needed, *more_needed], [*notes, *more_notes]

    def after_submit(self, page: Any) -> str | None:
        """Turnstile stands between the button and the application."""
        blocked = super().after_submit(page)
        if blocked:
            return blocked
        captcha = detect_captcha(page)
        if captcha:
            return (
                f"{captcha} stands between the form and the application; clear it in a visible "
                "browser (run with --headed) or apply by hand"
            )
        return None

    # -- helpers ---------------------------------------------------------

    def wait_for_autofill(self, page: Any) -> bool:
        """Wait while the "Autofilling" banner is up. True when one was seen."""
        seen = False
        waited = 0
        while waited <= self.autofill_wait_ms:
            if not self._autofilling(page):
                if seen:
                    page.wait_for_timeout(self.autofill_poll_ms)
                return seen
            seen = True
            page.wait_for_timeout(self.autofill_poll_ms)
            waited += self.autofill_poll_ms
        log.info("ashby: the autofill banner was still up after %sms", self.autofill_wait_ms)
        return True

    @staticmethod
    def _autofilling(page: Any) -> bool:
        try:
            return bool(page.evaluate(_AUTOFILL_JS))
        except Exception:
            return False


_AUTOFILL_JS = """() => {
  const text = document.body ? document.body.innerText : '';
  if (!/autofill/i.test(text)) return false;
  return Array.from(document.querySelectorAll('*')).some(el => {
    if (el.children.length) return false;
    if (!/autofill/i.test(el.textContent || '')) return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  });
}"""
