"""Dice Easy Apply: a page of steps on dice.com, for a signed-in member.

The posting is dice.com/job-detail/<guid>. A member sees "Easy Apply" (the
`data-testid="apply-button"` control, once drawn inside the `apply-button-wc`
web component, which Playwright's selectors reach into) when the employer
takes applications through Dice, and "Apply Now" that leaves for the
company's site otherwise; "Applied" when an application went in before. Easy
Apply opens dice.com/job-applications/<guid>/wizard: the resume (the one on
the Dice profile, with "Replace" to send another) and an optional cover
letter, the employer's questions when there are any, and a review, moved
through with "Next" and finished with "Submit". Dice then shows
"Application submitted" (`data-testid="job-application-success-card"`).

Attested by the Dice bots studied for this handler, both changed in
September 2026: kristipatithoyajakshakashyap/DiceAutoApply `dice_bot.py`
(the search URL and its `filters.*` parameters, the result links'
`job-search-job-detail-link`, the apply button's data-testid and its
disabled state once applied, Next and Submit by their text, and the sent
wording) and thandava34/auto-apply-dice-jobs `core/main_script.py` (Easy
Apply told from Apply Now by its text or its /job-applications/ link, the
wizard URL, "Replace" on the resume card to send a different file, and the
success card). Neither was checked against the live site from here; the
assumptions of this module are marked where they occur.

What those bots do and this module does not: mouse curves, typing delays and
a stealth script to look less like a program. Dice is used as it presents
itself, at the pace the daily cap sets.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from jobagent.apply.browser.fill import click_first_visible, fill_field, fill_plan, wait_settled
from jobagent.apply.handlers.wizard import ROOT, WizardHandler
from jobagent.apply.models import Fill, FormField
from jobagent.apply.sessions import jwt_expiry

log = logging.getLogger(__name__)

# The resume card with no file box: its "Replace" (or "Upload resume")
# button, which opens the file chooser or draws the box, stands in for one.
# On the live wizard (Mark's PC, 2026-10-01) the card shows only a three-dot
# menu at its right, and Replace is inside it: then the menu button is
# marked "resume-menu", and Replace is pressed once the menu is open.
UPLOAD_KEY = "dice-resume-upload"
UPLOAD_ATTR = "data-jobagent-upload"
_MARK_UPLOAD_JS = r"""([root, attr]) => {
  document.querySelectorAll('[' + attr + ']').forEach((el) => el.removeAttribute(attr));
  const scope = document.querySelector(root) || document;
  const shown = (el) => {
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const text = (el) => (el.innerText || el.getAttribute('aria-label') || '').trim();
  const label = (el) => (el.getAttribute('aria-label') || el.title || '').trim();
  const buttons = Array.from(scope.querySelectorAll('button, [role="button"]')).filter(shown);
  const replace = buttons.find((b) =>
    /^(replace( resume)?|upload( a)? (new )?resume)$/i.test(text(b)));
  if (replace) { replace.setAttribute(attr, 'resume'); return true; }
  // The three-dot menu on the card that names a resume file (not the cover
  // letter's): dots for text, or labelled as a menu. Never a delete button.
  const menu = buttons.find((b) => {
    if (/delete|remove/i.test(label(b) + ' ' + (b.innerText || ''))) return false;
    const dots = /^\s*[\u22ee\u22ef\u2026.]{1,3}\s*$/.test(b.innerText || '')
      || /more|option|menu|action/i.test(label(b))
      || /^(menu|true)$/.test(b.getAttribute('aria-haspopup') || '')
      || (!(b.innerText || '').trim() && !!b.querySelector('svg'));
    if (!dots) return false;
    let card = b.parentElement;
    for (let i = 0; i < 5 && card && card !== scope; i++, card = card.parentElement) {
      const text = card.innerText || '';
      if (/\.(pdf|docx?)\b/i.test(text)) return !/cover letter/i.test(text) || /resume/i.test(text);
    }
    return false;
  });
  if (!menu) return false;
  menu.setAttribute(attr, 'resume-menu');
  return true;
}"""
# A file box Replace drew: the first one not under the cover letter's heading.
_MARK_RESUME_BOX_JS = r"""([root, attr]) => {
  const scope = document.querySelector(root) || document;
  for (const box of scope.querySelectorAll('input[type="file"]')) {
    let near = box.parentElement, words = '';
    for (let i = 0; i < 3 && near; i++, near = near.parentElement) {
      words += ' ' + (near.innerText || '');
    }
    if (/cover letter/i.test(words) && !/resume/i.test(words)) continue;
    box.setAttribute(attr, 'resume-box');
    return true;
  }
  return false;
}"""
# Replace (or Upload) in the menu the three dots opened.
REPLACE_IN_MENU = (
    "[role='menu'] :text-matches('^\\s*replace', 'i')",
    "[role='menuitem']:has-text('Replace')",
    "[role='menuitem']:has-text('Upload')",
    "li:has-text('Replace')",
    "button:has-text('Replace')",
    "a:has-text('Replace')",
)
# Dice's cookie banner covers the bottom of the page, Next included.
COOKIE_REJECT = (
    "#onetrust-reject-all-handler",
    "button:has-text('Reject all')",
    "button:has-text('Reject All')",
)
# Shown on or near Next while Dice loads the following step.
BUSY = (
    "[aria-busy='true']",
    "[role='progressbar']",
    "[class*='spinner' i]",
    "[class*='animate-spin']",
    "[data-testid*='spinner' i]",
    "[data-testid*='loading' i]",
)
_COVER = re.compile(r"cover\s*letter", re.IGNORECASE)
_ROOT_TEXT = "(sel) => (document.querySelector(sel) || document.body).innerText || ''"


class DiceHandler(WizardHandler):
    ats = "dice"
    site = "dice"
    hosts = ("dice.com",)
    own_hosts = ("dice.com",)
    # Easy Apply only: "Apply Now" carries the same data-testid and leaves Dice.
    easy_apply_selectors = (
        "[data-testid='apply-button']:has-text('Easy Apply')",
        "a[data-testid='apply-button'][href*='/job-applications/']",
        "apply-button-wc button:has-text('Easy Apply')",
        "a[href*='/job-applications/'][href*='wizard']",
        "button:has-text('Easy Apply')",
        "a:has-text('Easy Apply')",
    )
    external_apply_selectors = (
        "a[data-testid='apply-button'][href^='http']:not([href*='dice.com'])",
        "[data-testid*='external-apply']",
        "[data-testid='apply-button']:has-text('Apply Now')",
        "apply-button-wc button:has-text('Apply Now')",
        "a:has-text('Apply on company site')",
    )
    # Assumption: a visitor who is not signed in sees a link to Dice's sign-in
    # page (dice.com/dashboard/login) in the header; a member does not.
    signed_out_selectors = (
        "header a[href*='/dashboard/login']",
        "button:has-text('Login to apply')",
        "a:has-text('Login to apply')",
    )
    # Partway through the steps (the PC's first live run, 2026-10-02) Dice
    # sent the browser to /dashboard/login/password: "Welcome / Log in to
    # continue" with only a Password box, for an account it knows.
    relogin_url = re.compile(r"/dashboard/login|/login\b|/signin\b", re.IGNORECASE)
    relogin_selectors = (
        "input[type='password']",
        "text=/log in to continue/i",
    )
    # The live wizard (the PC's probe, 2026-10-01) has no `main` and no
    # data-testid: its steps are a plain <form> a few divs under <body>. The
    # site header's search form is never the root.
    root_selectors = (
        "[data-testid*='wizard' i]",
        "main form",
        "main",
        "form:has(input[type='file'])",
        "form:not([role='search']):not(:has(input[name='q'])):has(button)",
    )
    # After the resume is replaced, step 1's button reads "Update" (the PC,
    # 2026-10-01); pressing it goes on as Next does.
    next_selectors = (
        "button:has-text('Next')",
        "button[aria-label*='Next']",
        "button:has-text('Continue')",
        "button:text-is('Update')",
    )
    submit_selectors = (
        "button:has-text('Submit Application')",
        "button[type='submit']:has-text('Submit')",
        "button:has-text('Submit')",
    )
    # Assumption: field errors use role=alert or an "error" data-testid, as
    # the success card's data-testid suggests Dice names its parts.
    # Not aria-live regions as such: Next.js's route announcer is one, always
    # on the page, and it read as an error on the live wizard.
    step_error_selectors = (
        "[data-testid*='error' i]",
        "[role='alert']:not(#__next-route-announcer__)",
    )
    success_signals = (
        "application submitted",
        "your application has been submitted",
        "successfully applied",
        "application sent",
    )
    success_url = re.compile(r"/success\b|/post-apply\b|/confirmation\b", re.IGNORECASE)
    max_steps = 8
    settle_ms = 6_000
    # How long a step may take to follow Next (Dice spins on the button meanwhile).
    step_wait_ms = 20_000

    def matches(self, url: str) -> bool:
        # Only postings and their wizard: a company profile is not an application.
        return super().matches(url) and bool(
            re.search(r"/job-detail/|/jobs/detail/|/job-applications/", url or "")
        )

    def application_url(self, url: str) -> str:
        """The posting page, without the search's tracking parameters."""
        found = re.search(r"dice\.com/job-detail/([0-9a-zA-Z-]+)", url or "")
        if found:
            return f"https://www.dice.com/job-detail/{found.group(1)}"
        return url

    def apply(self, page: Any, packet: Any, answerer: Any, **kwargs: Any) -> Any:
        url = self.application_url(packet.job.get("apply_url") or packet.job.get("url") or "")
        if url.startswith("https://www.dice.com/"):
            # In the browser the steps run in, right before the posting opens.
            if not refresh_sign_in(page):
                return self._result(
                    page,
                    "blocked",
                    kwargs.get("screenshot_path"),
                    error=f"Dice's sign-in could not be renewed ({token_note(page)}); "
                    + self.login_hint(),
                    sign_in=self.sign_in_target(),
                )
            log.info("dice: signed in, %s", token_note(page))
        return super().apply(page, packet, answerer, **kwargs)

    def sign_in_asked(self, page: Any) -> str | None:
        try:
            path = urlparse(page.url or "").path or ""
        except Exception:
            path = ""
        if self.relogin_url.search(path):
            shown = path
        elif self._any_visible(page, self.relogin_selectors):
            shown = "a Log in / Password box"
        else:
            return None
        return f"Dice asked to sign in again ({shown}; {token_note(page)})"

    def open_flow(self, page: Any) -> Any:
        reject_cookies(page)
        return super().open_flow(page)

    def step_marker(self, page: Any) -> str:
        # Called just before Next is pressed: the cookie banner, which can
        # arrive late and covers Next, goes first.
        reject_cookies(page)
        return self._signature(page, self._mark_root(page))

    def click_next(self, page: Any, root: str | None) -> bool:
        """Next inside the step's form, else any visible Next on the page.
        When none can be pressed, `next_problem` says why (covered, disabled,
        missing), for the error the run stops with."""
        if super().click_next(page, root):
            return True
        problem = "no visible Next or Update button"
        for selector in self.next_selectors:
            try:
                found = page.locator(selector)
                for index in range(min(found.count(), 5)):
                    button = found.nth(index)
                    if not button.is_visible():
                        continue
                    if not button.is_enabled():
                        problem = f"{selector} is disabled"
                        continue
                    try:
                        button.scroll_into_view_if_needed(timeout=3000)
                        button.click(timeout=5000)
                        return True
                    except Exception as exc:
                        problem = f"{selector} could not be pressed: {str(exc).splitlines()[0]}"
            except Exception as exc:
                problem = f"{selector}: {str(exc).splitlines()[0]}"
        self.next_problem = problem[:300]
        log.warning("dice: %s", self.next_problem)
        return False

    def wait_for_step(self, page: Any, before: str) -> None:
        """After Next, wait while Dice shows it is loading (a spinner on Next)
        and the step on screen is still the one left, up to `step_wait_ms`."""
        waited = 0
        while waited < self.step_wait_ms:
            busy = self._busy(page)
            if not busy and self._signature(page, self._mark_root(page)) != before:
                wait_settled(page, 1_500)  # the new step's fields arrive just after
                return
            if not busy and waited >= 3_000:
                return
            page.wait_for_timeout(250)
            waited += 250

    def _busy(self, page: Any) -> bool:
        return self._any_visible(page, BUSY)

    def _step_errors(self, page: Any, root: str | None) -> list[str]:
        """The step's error messages; an alert with no words is not one."""
        return [e for e in super()._step_errors(page, root) if re.search(r"[A-Za-z]{3}", e)]

    def discover_step(self, page: Any, root: str | None) -> list[FormField]:
        reject_cookies(page)
        return self._discover_step(page, root)

    def _discover_step(self, page: Any, root: str | None) -> list[FormField]:
        """The step's fields; on the resume step with no file box, the resume
        card's "Replace" button as the resume's upload, so the tailored
        resume goes instead of the one on the profile."""
        fields = super().discover_step(page, root)
        # A file box for the resume is used as it is; the cover letter's
        # dashed box (left empty) is not the resume's.
        if any(f.kind == "file" and not _COVER.search(f.label or "") for f in fields):
            return fields
        try:
            found = page.evaluate(_MARK_UPLOAD_JS, [root or "body", UPLOAD_ATTR])
        except Exception:
            found = False
        if found:
            fields.append(
                FormField(
                    key=UPLOAD_KEY,
                    label="Resume",
                    kind="file",
                    required=True,
                    section="resume",
                    selector=f"[{UPLOAD_ATTR}^='resume']",
                    accept=".pdf,.doc,.docx",
                )
            )
        return fields

    def prefilled(self, page: Any, fields: list[FormField]) -> dict[str, str]:
        return super().prefilled(page, [f for f in fields if f.key != UPLOAD_KEY])

    def fill(self, page: Any, fields: list[FormField], plan: Any) -> tuple[list, list, list]:
        return fill_plan(page, fields, plan, fill_one=self._fill_one)

    def _fill_one(self, page: Any, field: FormField, fill: Fill) -> str | None:
        if field.key != UPLOAD_KEY:
            return fill_field(page, field, fill)
        if not fill.file_path:
            return "no resume file to upload"
        name = Path(fill.file_path).name
        if name.lower() in self._root_text(page).lower():
            # Replaced already: the card names the tailored file. Going round
            # the menu again finds no Replace, and there is nothing to do.
            return None
        control = page.locator(field.selector).first
        try:
            in_menu = control.get_attribute(UPLOAD_ATTR, timeout=2000) == "resume-menu"
        except Exception:
            in_menu = False
        try:
            if in_menu:
                control.click(timeout=5000)
                page.wait_for_timeout(500)
                with page.expect_file_chooser(timeout=5000) as chooser:
                    if not click_first_visible(page, REPLACE_IN_MENU):
                        page.keyboard.press("Escape")
                        raise LookupError("no Replace in the resume's menu")
            else:
                with page.expect_file_chooser(timeout=5000) as chooser:
                    control.click(timeout=5000)
            chooser.value.set_files(fill.file_path)
        except LookupError as exc:
            return str(exc)
        except Exception:
            # Replace may draw a file box instead of opening the chooser:
            # the one for the resume, never the cover letter's.
            try:
                found = page.evaluate(_MARK_RESUME_BOX_JS, [ROOT, UPLOAD_ATTR])
                if not found:
                    raise LookupError("no resume file box")
                page.locator(f"[{UPLOAD_ATTR}='resume-box']").first.set_input_files(
                    fill.file_path, timeout=5000
                )
            except Exception as exc:
                where = "the resume's menu" if in_menu else "Replace"
                return f"{where} gave neither a file chooser nor a file box: {type(exc).__name__}"
        for _ in range(20):  # the new file's name shows on the card within seconds
            page.wait_for_timeout(500)
            if name.lower() in self._root_text(page).lower():
                return None
        return "Dice never showed the uploaded resume"

    @staticmethod
    def _root_text(page: Any) -> str:
        try:
            return str(page.evaluate(_ROOT_TEXT, ROOT) or "")
        except Exception:
            return ""


_REJECT_TEXT = re.compile(r"^\s*reject all\s*$", re.IGNORECASE)


def reject_cookies(page: Any) -> bool:
    """Press Reject all on Dice's cookie banner, in the page or any frame it
    is drawn in. True when one was pressed."""
    if click_first_visible(page, COOKIE_REJECT):
        return True
    try:
        frames = list(page.frames)
    except Exception:
        frames = []
    for frame in frames:
        try:
            button = frame.get_by_role("button", name=_REJECT_TEXT).first
            if button.count() and button.is_visible():
                button.click(timeout=3000)
                return True
        except Exception:
            continue
    return False


# Dice signs a member in with an `access` cookie holding a token that lasts an
# hour, though the cookie itself is kept 30 days, and a `refreshToken` cookie
# good for 30 days. A visit to a member page while the token is stale makes
# Dice issue a new one (the PC, 2026-10-01: /home-feed, about 8 seconds).
REFRESH_URL = "https://www.dice.com/home-feed"
REFRESH_WAIT_MS = 15_000
FRESH_FOR_S = 300


def refresh_sign_in(page: Any, *, now: float | None = None) -> bool:
    """Make sure the browser's Dice sign-in token has a few minutes left,
    visiting Dice's home feed to have it renewed when it has not. True when
    the token is fresh afterwards. The run's browser state, saved when it
    closes, keeps the new token for the next run."""
    context = getattr(page, "context", None)
    if context is None or _access_fresh(context, now=now):
        return True
    try:
        page.goto(REFRESH_URL, wait_until="domcontentloaded", timeout=30_000)
    except Exception as exc:
        log.info("dice: could not open %s to renew the sign-in: %s", REFRESH_URL, exc)
        return False
    waited = 0
    while waited < REFRESH_WAIT_MS:
        page.wait_for_timeout(500)
        waited += 500
        if _access_fresh(context, now=now):
            return True
    log.info("dice: the sign-in token was not renewed; signing in again may be needed")
    return False


def token_note(page: Any, *, now: float | None = None) -> str:
    """How long the browser's Dice sign-in token has left, in words."""
    try:
        cookies = page.context.cookies("https://www.dice.com")
    except Exception:
        return "sign-in token unreadable"
    now = time.time() if now is None else now
    for cookie in cookies:
        if cookie.get("name") == "access":
            expires = jwt_expiry(str(cookie.get("value") or ""))
            if expires is None:
                return "sign-in token unreadable"
            left = int((expires - now) // 60)
            return (
                f"sign-in token good for {left} more min" if left > 0 else "sign-in token expired"
            )
    return "no sign-in token"


def _access_fresh(context: Any, *, now: float | None = None) -> bool:
    try:
        cookies = context.cookies("https://www.dice.com")
    except Exception:
        return False
    now = time.time() if now is None else now
    for cookie in cookies:
        if cookie.get("name") == "access":
            expires = jwt_expiry(str(cookie.get("value") or ""))
            return expires is not None and expires > now + FRESH_FOR_S
    return False
