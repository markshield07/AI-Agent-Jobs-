"""The fallback: any application form on any site, by what the page shows
rather than by what the site is.

It runs when no handler claims the URL, so it has no selectors of its own
beyond the ordinary ones, and one job the others do not have: deciding
whether what it found is an application at all. A careers page's search box
and a newsletter sign-up are forms too, and filling them with a name and a
phone number would be worse than doing nothing. A page counts as an
application when it takes a file, or when it asks for enough of the things an
application asks for (a name, an email, a phone number, a letter) that it
could not be anything else. Anything thinner comes back as `failed` with the
reason, for a person to look at.

The bar and the field list follow the generic fillers in the reference study
(job-agent E7, AutoApply's `apply_generic`), which fill by name, email,
phone, location, LinkedIn, portfolio and a cover-letter textarea.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from jobagent.apply.answering import field_section
from jobagent.apply.browser.dom import discover_fields, script
from jobagent.apply.browser.fill import click_first_visible, detect_login_wall, wait_settled
from jobagent.apply.handlers.base import COOKIE_BUTTON_SELECTORS, BaseHandler
from jobagent.apply.models import Fill, FormField, HandlerResult
from jobagent.discovery.ats import detect_ats

log = logging.getLogger(__name__)

APPLY_CONTROL_JS = script("apply_control.js")
# Sites with a handler of their own: an Apply press that lands on one hands over.
HANDED_OFF = frozenset({"workday", "greenhouse", "lever", "ashby"})

_WIDGET = re.compile(r"api_key|AwliWidget|apply-with-linkedin|linkedin\.com/|indeed-apply", re.I)
# The button a page draws over a hidden file box for the resume (Phenom).
UPLOAD_BUTTON = re.compile(
    r"^(upload|attach|add|choose)\s+(your\s+|a\s+)?(resume|cv|r[ée]sum[ée])\b", re.I
)

# What an application asks for. Two of these, or one file upload, and the page
# is taken to be an application.
_APPLICATION_SECTIONS = ("contact", "resume", "cover_letter", "links")
MIN_APPLICATION_FIELDS = 3
NOT_AN_APPLICATION = (
    "the page has a form, but not one that looks like an application: no file upload "
    "and too few of the things an application asks for"
)


class GenericHandler(BaseHandler):
    ats = "generic"
    hosts = ()
    # How many Apply presses it may take to reach the form: posting, then a
    # choice of how to apply or a sign-in with a guest option, then the form.
    max_hops = 3
    # How long a press gets to open a new tab before it is taken to have none.
    new_tab_ms = 2_500

    def __init__(self) -> None:
        # Where an Apply press landed on a site another handler fills.
        self._handoff: str | None = None
        self._resume = ""

    def matches(self, url: str) -> bool:
        """Never claims a URL: it is what `handler_for` falls back to, so a
        site with a handler of its own always gets that one."""
        return False

    def prepare(self, page: Any) -> None:
        """Press through to the application, a page at a time.

        Company career sites put the form one, two or three presses from the
        posting: an Apply link to another page or tab, then often a choice of
        how to apply, or a sign-in that also offers to apply as a guest. At
        each page, stop if an application is showing; otherwise press the
        best Apply control (guest options first, then Apply wording; never
        apply-with-LinkedIn and the like, sign-ins, or job alerts). A job-alert
        email box on the posting is not the form, so it no longer stops the
        press as it did when any email input counted.
        """
        _clear_consent(page)
        tried: list[str] = []
        for _ in range(self.max_hops):
            if self._handed_off(page) or self._application_showing(page):
                return
            walled = detect_login_wall(page)
            try:
                control = page.evaluate(APPLY_CONTROL_JS, {"guestOnly": walled, "tried": tried})
            except Exception as exc:
                log.debug("generic: could not look for an Apply control: %s", exc)
                return
            if not control:
                return
            tried.append(control["key"])
            log.info("generic: pressing %r on %s", control["text"], page.url)
            if not self._press(page, control):
                return
            wait_settled(page, self.settle_ms)
            _clear_consent(page)
        self._handed_off(page)

    def _handed_off(self, page: Any) -> bool:
        """True when the press led to Workday, Greenhouse, Lever or Ashby,
        whose own handler fills the form from here."""
        url = str(getattr(page, "url", "") or "")
        if detect_ats(url) in HANDED_OFF:
            self._handoff = url
        return self._handoff is not None

    def _application_showing(self, page: Any) -> bool:
        """An application on screen: a form still hidden behind Apply does not count."""
        try:
            fields = discover_fields(page, expand_comboboxes=False)
        except Exception:
            return False
        return looks_like_an_application([f for f in fields if _shown(page, f.selector)])

    def _press(self, page: Any, control: dict[str, Any]) -> bool:
        """Follow the control: a link to another page is opened in this tab;
        anything else is clicked, and a tab it opens is taken over."""
        href = str(control.get("href") or "")
        here = str(getattr(page, "url", "") or "")
        if href.split("#")[0] and href.split("#")[0] != here.split("#")[0] and _web_link(href):
            try:
                page.goto(href, wait_until="domcontentloaded", timeout=self.settle_ms * 3)
                return True
            except Exception as exc:
                log.info("generic: could not open %s: %s", href, exc)
                return False
        context = page.context
        before = list(context.pages)
        try:
            page.locator('[data-jobagent-apply="1"]').first.click(timeout=5000)
        except Exception as exc:
            log.info("generic: could not press %r: %s", control.get("text"), exc)
            return False
        opened = _new_tab(page, before, self.new_tab_ms)
        if opened is None:
            return True
        try:
            opened.wait_for_load_state("domcontentloaded", timeout=self.settle_ms)
            url = opened.url
        except Exception:
            url = ""
        finally:
            try:
                opened.close()
            except Exception:
                pass
        if not _web_link(url):
            return True
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=self.settle_ms * 3)
        except Exception as exc:
            log.info("generic: could not open the new tab's %s: %s", url, exc)
            return False
        return True

    def fill(self, page: Any, fields: list[FormField], plan: Any) -> tuple[list, list, list]:
        """The plan, then the resume through the page's own Upload Resume
        button when the form's file box is hidden where it could not be read."""
        filled, unfilled, notes = super().fill(page, fields, plan)
        resume = self._resume
        if resume and not any(f.file_path for f in filled) and _upload_by_button(page, resume):
            filled.append(Fill(key="resume", file_path=resume, source="resume", label="Resume"))
            notes.append("uploaded the resume through the page's Upload Resume button")
        return filled, unfilled, notes

    def discover(self, page: Any) -> list[FormField]:
        if self._handoff:
            return []
        fields = _the_application(page, discover_fields(page))
        if not fields or looks_like_an_application(fields):
            return fields
        log.info("generic: %s has a form, but not an application", page.url)
        return []

    def apply(self, page: Any, packet: Any, *args: Any, **kwargs: Any) -> Any:
        self._handoff = None
        self._resume = getattr(packet, "resume_path", "") or ""
        result = super().apply(page, packet, *args, **kwargs)
        board = str(packet.job.get("url") or "")
        if result.outcome == "failed" and detect_ats(board) == "indeed" and _indeed_apply(page):
            # The company's page applies only through Indeed Apply (Paycor):
            # Indeed's own posting has that application, and a handler for it.
            return HandlerResult(
                outcome="blocked",
                error="the company's page takes applications only through Indeed Apply",
                external_url=board,
                final_url=result.final_url,
                screenshot_path=result.screenshot_path,
            )
        if self._handoff and not result.wrong_place:
            ats = detect_ats(self._handoff)
            return HandlerResult(
                outcome="blocked",
                error=f"the company's Apply leads to its {ats} site",
                external_url=self._handoff,
                final_url=self._handoff,
                screenshot_path=result.screenshot_path,
            )
        if result.outcome == "failed" and result.error == "no application form found":
            result.error = NOT_AN_APPLICATION if _has_form(page) else result.error
        return result


def looks_like_an_application(fields: list[FormField]) -> bool:
    """True when the form asks for what an application asks for. A field's
    part is read from its label too: pages rarely say "contact" themselves."""
    if any(f.kind == "file" and not _widget(f) for f in fields):
        return True
    sections = [field_section(f) for f in fields]
    wanted = {s for s in sections if s in _APPLICATION_SECTIONS}
    if len(wanted) >= 2:
        return True
    return len([s for s in sections if s in _APPLICATION_SECTIONS]) >= MIN_APPLICATION_FIELDS


def _widget(field: FormField) -> bool:
    """Another site's apply widget (Apply with LinkedIn) hides a file box of its own."""
    return bool(_WIDGET.search(f"{field.label} {field.key} {field.selector}"))


def _the_application(page: Any, fields: list[FormField]) -> list[FormField]:
    """The fields of the one form that is the application, when the page has
    several (a job-alert box or a site search beside it): the form with a
    file upload, else the one asking most of what an application asks."""
    groups: dict[int, list[FormField]] = {}
    for f in fields:
        groups.setdefault(_form_index(page, f.selector), []).append(f)
    if len(groups) < 2:
        return fields

    def weight(group: list[FormField]) -> tuple[int, int, int]:
        shown = [f for f in group if not _widget(f) and _shown(page, f.selector)]
        files = sum(f.kind == "file" for f in shown)
        asked = sum(field_section(f) in _APPLICATION_SECTIONS for f in shown)
        return files, asked, len(shown)

    return max(groups.values(), key=weight)


def _form_index(page: Any, selector: str | None) -> int:
    """Which of the page's forms holds the control; -1 for none."""
    if not selector:
        return -1
    try:
        return int(
            page.locator(selector).first.evaluate(
                "el => el.form ? Array.from(document.forms).indexOf(el.form)"
                " : (el.closest('form') ? Array.from(document.forms).indexOf(el.closest('form'))"
                " : -1)"
            )
        )
    except Exception:
        return -1


def _shown(page: Any, selector: str | None) -> bool:
    if not selector:
        return False
    try:
        control = page.locator(selector).first
        # A file input is often hidden behind a styled button; its box still counts.
        return control.is_visible() or bool(
            control.evaluate("el => !!el.closest('form') && el.closest('form').offsetParent")
        )
    except Exception:
        return False


def _upload_by_button(page: Any, path: str) -> bool:
    """Press a visible "Upload Resume" button and give the file chooser it opens the file."""
    try:
        buttons = page.locator('button, [role="button"], label, a')
        for index in range(min(buttons.count(), 200)):
            button = buttons.nth(index)
            text = (button.inner_text(timeout=1000) or "").strip()
            if not UPLOAD_BUTTON.search(text) or not button.is_visible():
                continue
            with page.expect_file_chooser(timeout=5000) as chooser:
                button.click(timeout=5000)
            chooser.value.set_files(path)
            return True
    except Exception as exc:
        log.info("generic: could not upload the resume by its button: %s", exc)
    return False


def _clear_consent(page: Any) -> None:
    """Cookie banners away. OneTrust's preference centre can stay over the
    page after them, taking every click (EY); it is then hidden, which
    leaves cookies at the site's defaults."""
    click_first_visible(page, COOKIE_BUTTON_SELECTORS)
    try:
        page.evaluate(
            """() => {
                const sdk = document.getElementById('onetrust-consent-sdk');
                const shade = sdk && sdk.querySelector('.onetrust-pc-dark-filter');
                if (shade && getComputedStyle(shade).display !== 'none') sdk.style.display = 'none';
            }"""
        )
    except Exception:
        pass


def _indeed_apply(page: Any) -> bool:
    try:
        return (
            page.locator(
                'iframe[src*="smartapply.indeed.com"], iframe[src*="indeedapply"], '
                '[class*="indeed-apply" i], [id*="indeed-apply" i]'
            ).count()
            > 0
        )
    except Exception:
        return False


def _web_link(url: str) -> bool:
    return url.startswith(("http://", "https://", "file://"))


def _new_tab(page: Any, before: list[Any], wait_ms: int) -> Any | None:
    """A tab the last press opened, waited for up to `wait_ms`."""
    waited = 0
    while True:
        opened = [p for p in page.context.pages if p not in before and p is not page]
        if opened:
            return opened[-1]
        if waited >= wait_ms:
            return None
        page.wait_for_timeout(250)
        waited += 250


def _has_form(page: Any) -> bool:
    try:
        return page.locator("form").count() > 0
    except Exception:
        return False
