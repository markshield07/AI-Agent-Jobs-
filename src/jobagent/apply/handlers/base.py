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
from datetime import UTC, datetime
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
from jobagent.apply.place import read_place, wrong_place

log = logging.getLogger(__name__)

COOKIE_BUTTON_SELECTORS: tuple[str, ...] = (
    "#onetrust-accept-btn-handler",
    'button[id*="accept" i][id*="cookie" i]',
    '[class*="cookie" i] button:has-text("Accept")',
    '[id*="cookie" i] button:has-text("Accept")',
    '[class*="consent" i] button:has-text("Accept")',
    'button:has-text("Accept all cookies")',
    'button:has-text("Accept Cookies")',
    # OneTrust's preference centre, when it is what shows (EY): save as is.
    "#onetrust-pc-sdk .save-preference-btn-handler",
    "#onetrust-pc-sdk #accept-recommended-btn-handler",
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
    # Pressed after the code is typed, before the submit button is tried again.
    code_submit_selectors: tuple[str, ...] = ()
    # Beyond the settle after the button: how long to wait for any answer.
    submit_wait_ms: int = 0
    # How long to give the page after each navigation or click.
    settle_ms: int = 10_000
    # Where the site's own posting states its location and remote type, beside
    # the schema.org JobPosting most career pages embed (see apply/place.py).
    place_selectors: tuple[str, ...] = ()
    remote_type_selectors: tuple[str, ...] = ()
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
        elsewhere = self.wrong_place(page, packet.wanted_places)
        if elsewhere:
            return HandlerResult(outcome="blocked", error=elsewhere, wrong_place=elsewhere)

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
        if not filled:
            # Nothing went on the form: pressing its button would send it empty.
            return self._result(
                page,
                "failed",
                screenshot_path,
                needed=needed,
                error="found the form but could fill none of it; not sent",
                **common,
            )
        if not submit:
            return self._result(page, "dry_run", screenshot_path, needed=needed, **common)

        before_url, before_text = page.url, page_text(page)
        pressed_at = datetime.now(UTC)
        if not click_first_visible(page, self.submit_buttons(page, fields)):
            return self._result(
                page, "failed", screenshot_path, needed=needed, error="no submit button", **common
            )
        wait_settled(page, self.submit_settle_ms)
        self._await_answer(page, before_url, before_text)
        code_sent = False
        if self.security_code_selectors and self._any_visible(page, self.security_code_selectors):
            pending = self.enter_emailed_code(page, packet, fields, pressed_at)
            if pending:
                return self._result(
                    page, "blocked", screenshot_path, needed=needed, error=pending, **common
                )
            # The code boxes may stay drawn while the page answers the second press.
            code_sent = True
            self._await_answer(page, before_url, before_text, code_boxes_answer=False)
        answered = code_sent and (
            check_outcome(page, before_url=before_url, before_text=before_text)[0] != "unconfirmed"
        )
        blocked = None if answered else self.after_submit(page)
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

    # -- the emailed security code -----------------------------------------

    def _await_answer(
        self, page: Any, before_url: str, before_text: str, *, code_boxes_answer: bool = True
    ) -> None:
        """Give a slow page up to `submit_wait_ms` more to show its answer: a
        confirmation, an error, or the box for an emailed code. A button left
        spinning is otherwise read as no answer at all."""
        waited = 0
        while waited < self.submit_wait_ms:
            if (
                code_boxes_answer
                and self.security_code_selectors
                and self._any_visible(page, self.security_code_selectors)
            ):
                return
            if check_outcome(page, before_url=before_url, before_text=before_text)[0] != (
                "unconfirmed"
            ):
                return
            page.wait_for_timeout(1_000)
            waited += 1_000

    def enter_emailed_code(
        self, page: Any, packet: Packet, fields: list[FormField], pressed_at: datetime
    ) -> str | None:
        """Read the code the site emailed, type it, press the button again.

        Returns None once the code is in and the button pressed, else the
        reason the application waits on the code.
        """
        if packet.security_code is None:
            return (
                "the site emailed a verification code and waits for it before it accepts "
                "the application; no mailbox is set up to read it (JOBAGENT_IMAP_*), so "
                "enter it in a visible browser (run with --headed) or apply by hand"
            )
        log.info("%s: the site asks for the code it emailed; reading the inbox", self.ats)
        code = packet.security_code(pressed_at)
        if not code:
            return (
                "the site emailed a verification code and waits for it, but no code mail "
                "reached the inbox in time; nothing was sent. Try again, or enter the code "
                "in a visible browser (run with --headed)"
            )
        if not self._type_code(page, code):
            return "the site asked for an emailed code, but its code box would not take it"
        page.wait_for_timeout(500)
        buttons = [*self.code_submit_selectors, *self.submit_buttons(page, fields)]
        if not click_first_visible(page, buttons):
            return "the emailed code is in, but there was no button to send it with"
        wait_settled(page, self.submit_settle_ms)
        return None

    def _type_code(self, page: Any, code: str) -> bool:
        """One box per character (Greenhouse draws eight), or one box for all."""
        boxes = []
        for selector in self.security_code_selectors:
            try:
                found = page.locator(selector)
                boxes = [found.nth(i) for i in range(found.count()) if found.nth(i).is_visible()]
            except Exception:
                continue
            if boxes:
                break
        if not boxes:
            return False
        try:
            if len(boxes) == len(code):
                for box, char in zip(boxes, code, strict=True):
                    box.fill(char, timeout=3_000)
            else:
                boxes[0].click(timeout=3_000)
                boxes[0].fill("", timeout=3_000)
                page.keyboard.type(code, delay=60)
        except Exception as exc:
            log.info("%s: could not type the emailed code: %s", self.ats, exc)
            return False
        return True

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

    def wrong_place(self, page: Any, wanted: Sequence[str]) -> str | None:
        """Why the posting's own location rules it out, or None to go ahead."""
        if not wanted:
            return None
        try:
            found = read_place(page, self.place_selectors, self.remote_type_selectors)
        except Exception as exc:  # reading the place must never stop an application
            log.debug("could not read the posting's place: %s", exc)
            return None
        return wrong_place(found, wanted)

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
