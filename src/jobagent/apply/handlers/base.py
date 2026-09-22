"""The flow every handler shares; an ATS handler only names what differs.

    open the application page -> prepare (apply button, banners)
    -> discover the form -> plan -> fill -> screenshot
    -> stop (dry run / review / a question to ask) or submit -> read the outcome

A handler never raises for an ordinary failure. A page that will not open, a
form that is not there, a login wall, a captcha, a verification-code prompt
after the button: each comes back as a `HandlerResult` with the outcome that
fits and a reason a person can act on. `submitted` is claimed only when the
page confirms it (job-agent's rule); a press with no confirmation and no
error is `unconfirmed`, and the job waits for a person to look.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

from jobagent.apply.browser.dom import discover_fields
from jobagent.apply.browser.fill import (
    check_outcome,
    click_first_visible,
    detect_captcha,
    detect_login_wall,
    fill_plan,
    mark_form,
    page_text,
    take_screenshot,
    wait_settled,
)
from jobagent.apply.models import Answerer, FormField, HandlerResult, Packet

log = logging.getLogger(__name__)

COOKIE_BUTTON_SELECTORS: tuple[str, ...] = (
    "#onetrust-accept-btn-handler",
    'button[id*="accept" i][id*="cookie" i]',
    '[class*="cookie" i] button:has-text("Accept")',
    '[id*="cookie" i] button:has-text("Accept")',
    '[class*="consent" i] button:has-text("Accept")',
    'button:has-text("Accept all cookies")',
    'button:has-text("Accept Cookies")',
)


class BaseHandler:
    """Subclasses set `ats`, `hosts`, and whichever hooks their form needs."""

    ats: str = "base"
    hosts: tuple[str, ...] = ()
    # Clicked before the form is read, when present: an "Apply" button that
    # reveals the form, or a link to the application page.
    apply_button_selectors: tuple[str, ...] = ()
    # The form is on the page once one of these is visible.
    form_ready_selectors: tuple[str, ...] = ("form", 'input[type="file"]', "textarea")
    submit_selectors: tuple[str, ...] = (
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Submit application")',
        'button:has-text("Submit Application")',
        'button:has-text("Submit")',
    )
    # Visible after the button: the site wants a code from the inbox first.
    security_code_selectors: tuple[str, ...] = ()
    # How long to give the page after each navigation or click.
    settle_ms: int = 10_000
    submit_settle_ms: int = 20_000

    # -- what subclasses override -----------------------------------------

    def matches(self, url: str) -> bool:
        host = (urlparse(url).netloc or "").lower()
        return any(host == h or host.endswith("." + h) for h in self.hosts)

    def application_url(self, url: str) -> str:
        """The page that carries the form, from the posting's URL."""
        return url

    def prepare(self, page: Any) -> None:
        """Get the form on screen: banners away, the apply button pressed."""
        click_first_visible(page, COOKIE_BUTTON_SELECTORS)
        if self.apply_button_selectors and not self._form_visible(page):
            if click_first_visible(page, self.apply_button_selectors):
                wait_settled(page, self.settle_ms)

    def discover(self, page: Any) -> list[FormField]:
        return discover_fields(page)

    def fill(self, page: Any, fields: list[FormField], plan: Any) -> tuple[list, list, list]:
        """Put the plan on the page. Overridden where the order matters: a form
        that parses the resume and writes the boxes itself (Ashby) must take
        the file first and be left to finish."""
        return fill_plan(page, fields, plan)

    def submit_buttons(self, page: Any, fields: list[FormField]) -> list[str]:
        """The submit selectors, inside the form that was filled first.

        A site search or a newsletter box on the same page has a submit button
        too, and it usually comes first in the document.
        """
        scope = mark_form(page, fields)
        if not scope:
            return list(self.submit_selectors)
        return [f"{scope} {s}" for s in self.submit_selectors] + list(self.submit_selectors)

    def after_submit(self, page: Any) -> str | None:
        """A reason the submission is blocked after the button, if the site raised one."""
        if self.security_code_selectors and self._any_visible(page, self.security_code_selectors):
            return (
                "the site asks for a verification code sent to your email before it accepts "
                "the application; enter it in a visible browser (run with --headed) or apply "
                "by hand"
            )
        return None

    # -- the shared flow -------------------------------------------------

    def apply(
        self,
        page: Any,
        packet: Packet,
        answerer: Answerer,
        *,
        submit: bool,
        screenshot_path: str | None = None,
    ) -> HandlerResult:
        url = self.application_url(packet.job.get("apply_url") or packet.job.get("url") or "")
        if not url:
            return HandlerResult(outcome="failed", error="the job has no URL")
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.settle_ms * 3)
        except Exception as exc:
            return HandlerResult(outcome="failed", error=f"could not open {url}: {_brief(exc)}")
        wait_settled(page, self.settle_ms)
        try:
            self.prepare(page)
        except Exception as exc:
            log.debug("prepare failed on %s: %s", url, exc)

        if detect_login_wall(page):
            return self._result(
                page, "blocked", screenshot_path, error="the page wants a login before the form"
            )
        try:
            fields = self.discover(page)
        except Exception as exc:
            return self._result(
                page, "failed", screenshot_path, error=f"could not read the form: {_brief(exc)}"
            )
        if not fields:
            captcha = detect_captcha(page)
            if captcha:
                return self._result(
                    page, "blocked", screenshot_path, error=f"{captcha} stands before the form"
                )
            return self._result(page, "failed", screenshot_path, error="no application form found")

        plan = answerer(fields)
        filled, unfilled, notes = self.fill(page, fields, plan)
        needed = [*plan.needed, *unfilled]
        for note in [*plan.notes, *notes]:
            log.info("%s: %s", self.ats, note)
        tokens = {"input_tokens": plan.input_tokens, "output_tokens": plan.output_tokens}
        common = {"filled": filled, "fields": fields, **tokens}

        if any(n.required for n in needed):
            return self._result(page, "needs_input", screenshot_path, needed=needed, **common)
        if not submit:
            return self._result(page, "dry_run", screenshot_path, needed=needed, **common)

        before_url, before_text = page.url, page_text(page)
        if not click_first_visible(page, self.submit_buttons(page, fields)):
            return self._result(
                page, "failed", screenshot_path, needed=needed, error="no submit button", **common
            )
        wait_settled(page, self.submit_settle_ms)
        blocked = self.after_submit(page)
        if blocked:
            return self._result(
                page, "blocked", screenshot_path, needed=needed, error=blocked, **common
            )
        outcome, text = check_outcome(page, before_url=before_url, before_text=before_text)
        if outcome == "unconfirmed":
            captcha = detect_captcha(page)
            if captcha:
                return self._result(
                    page,
                    "blocked",
                    screenshot_path,
                    needed=needed,
                    error=f"{captcha} after the submit button; solve it in a visible browser",
                    **common,
                )
            return self._result(
                page,
                "unconfirmed",
                screenshot_path,
                needed=needed,
                error="the button was pressed but the page showed neither a confirmation "
                "nor an error; check the screenshot and your inbox before trying again",
                **common,
            )
        if outcome == "failed":
            return self._result(
                page,
                "failed",
                screenshot_path,
                needed=needed,
                error=f"the form said: {text}",
                **common,
            )
        return self._result(
            page, "submitted", screenshot_path, needed=needed, confirmation=text, **common
        )

    # -- helpers ---------------------------------------------------------

    def _form_visible(self, page: Any) -> bool:
        return self._any_visible(page, self.form_ready_selectors)

    @staticmethod
    def _any_visible(page: Any, selectors: Sequence[str]) -> bool:
        for selector in selectors:
            try:
                if page.locator(selector).first.is_visible():
                    return True
            except Exception:
                continue
        return False

    def _result(
        self, page: Any, outcome: str, screenshot_path: str | None, **kwargs: Any
    ) -> HandlerResult:
        shot = take_screenshot(page, screenshot_path)
        try:
            final_url = page.url
        except Exception:
            final_url = None
        return HandlerResult(outcome=outcome, screenshot_path=shot, final_url=final_url, **kwargs)


def _brief(exc: BaseException) -> str:
    text = str(exc).splitlines()[0] if str(exc) else type(exc).__name__
    return text[:200]
