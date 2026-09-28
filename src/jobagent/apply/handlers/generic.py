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
from datetime import date
from pathlib import Path
from typing import Any

from jobagent.apply.answering import CONSENT_KEY, NEVER_GUESSED, SENSITIVE, field_section
from jobagent.apply.browser.dom import current_values, discover_fields, script
from jobagent.apply.browser.fill import (
    click_first_visible,
    detect_login_wall,
    fill_field,
    page_text,
    visible_errors,
    wait_settled,
)
from jobagent.apply.closing import deadline_passed
from jobagent.apply.handlers.base import COOKIE_BUTTON_SELECTORS, BaseHandler
from jobagent.apply.models import Fill, FormField, HandlerResult, NeededInput
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
# A form that signs the person up for something other than this job: a talent
# community or job alerts (ADP's "Join Our Talent Community" after its notice),
# or a check of an email or phone number by a code sent to it.
_SIGN_UP = re.compile(
    r"talent\s+(?:community|network|pool|pipeline)|join\s+our\s+(?:community|network)"
    r"|job\s+alerts?\b|stay\s+(?:connected|in\s+touch)",
    re.I,
)
_CODE_CHECK = re.compile(
    r"verification\s+(?:code|process|step|link)|one[-\s]time\s+(?:pass)?code|\bOTP\b"
    r"|verify\s+your\s+(?:email|phone|mobile|identity|account)|code\s+(?:we|we'll)\s+sen[dt]",
    re.I,
)
SIGN_UP_JS = script("sign_up.js")
STEP_CONTROL_JS = script("step_control.js")
# A page still busy with an upload: its spinner or progress bar is showing.
BUSY_JS = """() => Array.from(document.querySelectorAll(
    '[aria-busy="true"], [role="progressbar"], [class*="spinner" i], [class*="loader" i],'
    + ' [class*="loading" i]')).some((el) => {
  const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
  return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'
    && st.opacity !== '0';
})"""
_UPLOADED = re.compile(
    r"\b(?:uploaded|attached)\s+successfully\b|\bsuccessfully\s+(?:uploaded|attached)\b"
    r"|\b(?:remove|delete|replace)\s+(?:resume|cv|file)\b",
    re.I,
)
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
    # How long an uploaded resume gets to be read (the page's spinner) before
    # the form is filled around it.
    upload_wait_ms = 30_000
    # How many more looks at the form after filling it, for questions that
    # appear only once an answer is given (Serco's "Source" after "How did you hear").
    more_looks = 2
    # How many pages a multi-page application may run to (Phenom: My
    # Information, My Experience, Application Questions, Voluntary, Review).
    max_pages = 8
    # How long a list that appeared late gets to fill in its choices.
    options_wait_ms = 8_000
    # Where the posting names its place: schema.org markup, ADP's location line.
    place_selectors = ('[itemprop="jobLocation"]', ".job-description-location-item")

    def __init__(self) -> None:
        # Where an Apply press landed on a site another handler fills.
        self._handoff: str | None = None
        self._resume = ""
        self._answerer: Any = None
        self._wanted: list[str] = []
        # What the posting said before any press: closed, or somewhere not wanted.
        self._posting: str | None = None
        # Why the form found is a sign-up and not the application.
        self._sign_up: str | None = None
        # The fields of the page on screen, and whether the resume went on.
        self._page_fields: list[FormField] = []
        self._resume_sent = False

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
        self._posting = self._read_posting(page)
        if self._posting:
            return
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

    def _read_posting(self, page: Any) -> str | None:
        """Why the posting itself rules the job out, read before any press
        leaves it: a deadline that has passed, or a place not wanted."""
        closed = deadline_passed(page_text(page), date.today())
        if closed:
            return closed
        return super().wrong_place(page, self._wanted)

    def wrong_place(self, page: Any, wanted: Any) -> str | None:
        return self._posting or super().wrong_place(page, wanted)

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
        """This page of the application, then each page after it (Next), up
        to the one that sends it; the Submit button itself is left to the
        caller, which presses it only in auto mode."""
        filled, unfilled, notes = self._fill_page(page, fields, plan)
        self._page_fields = list(fields)
        self._next_pages(page, fields, plan, filled, unfilled, notes)
        return filled, unfilled, notes

    def _fill_page(
        self, page: Any, fields: list[FormField], plan: Any
    ) -> tuple[list[Fill], list[NeededInput], list[str]]:
        """The resume first when it goes through the page's own Upload Resume
        button (the form's file box hidden where it could not be read): a page
        that reads the resume rewrites the form while its spinner shows, so the
        plan goes on after. Then the plan; then what the page already held for
        a field the plan could not answer; then another look for questions an
        answer brought up. The resume goes by button once per application."""
        resume = "" if self._resume_sent else self._resume
        by_button: list[Fill] = []
        notes: list[str] = []
        if resume and not any(f.file_path for f in plan.fills):
            self._resume_by_button(page, resume, by_button, plan, notes)
        filled, unfilled, more = super().fill(page, fields, plan)
        filled[:0] = by_button
        notes.extend(more)
        if resume and not any(f.file_path for f in filled):
            self._resume_by_button(page, resume, filled, plan, notes)
        self._resume_sent = self._resume_sent or any(f.file_path for f in filled)
        self._keep_prefilled(page, fields, plan, filled)
        self._look_again(page, fields, plan, filled, unfilled, notes)
        return filled, unfilled, notes

    def _next_pages(
        self,
        page: Any,
        fields: list[FormField],
        plan: Any,
        filled: list[Fill],
        unfilled: list[NeededInput],
        notes: list[str],
    ) -> None:
        """Press Next while the page's last button is Next and every required
        question so far has an answer; fill each page it brings. A page that
        stays put after Next (the site found something wrong) stops it with
        what the page said."""
        for number in range(2, self.max_pages + 1):
            if any(n.required for n in [*plan.needed, *unfilled]) or self._answerer is None:
                return
            control = _page_end(page, self._page_fields)
            if not control or control["kind"] != "next":
                return
            before = _page_mark(page, self._page_fields)
            try:
                page.locator('[data-jobagent-step="next"]').first.click(timeout=5000)
            except Exception as exc:
                notes.append(f"could not press {control['text']!r}: {exc}")
                return
            wait_settled(page, self.settle_ms)
            _wait_idle(page, self.upload_wait_ms)
            try:
                found = [
                    f
                    for f in _the_application(page, discover_fields(page))
                    if not _widget(f) and _shown(page, f.selector)
                ]
            except Exception as exc:
                notes.append(f"could not read page {number}: {exc}")
                return
            if _page_mark(page, found) == before:
                said = "; ".join(visible_errors(page)[:3]) or "it gave no reason"
                unfilled.append(
                    NeededInput(
                        key=f"page-{number - 1}",
                        label=f"Page {number - 1} of the application",
                        kind="unknown",
                        required=True,
                        reason=f"the form stayed on page {number - 1} after "
                        f"{control['text']!r}: {said}",
                        answer_key=f"page-{number - 1}",
                    )
                )
                return
            notes.append(f"went on to page {number} with {control['text']!r}")
            self._page_fields = found
            if not found:
                continue
            extra = self._answerer(found)
            done, lost, more = self._fill_page(page, found, extra)
            fields.extend(found)
            filled.extend(done)
            unfilled.extend(lost)
            notes.extend(more)
            plan.needed.extend(extra.needed)
            plan.notes.extend(extra.notes)
            plan.input_tokens += extra.input_tokens
            plan.output_tokens += extra.output_tokens

    def submit_buttons(self, page: Any, fields: list[FormField]) -> list[str]:
        """The button ending the last page reached. A page still ending in
        Next was not the last one, and its Next is never pressed as Submit."""
        current = self._page_fields or fields
        control = _page_end(page, current)
        if control and control["kind"] == "next":
            return []
        first = ['[data-jobagent-step="submit"]'] if control else []
        return first + super().submit_buttons(page, current)

    def _resume_by_button(
        self, page: Any, resume: str, filled: list[Fill], plan: Any, notes: list[str]
    ) -> None:
        if not _upload_by_button(page, resume):
            return
        _wait_idle(page, self.upload_wait_ms)
        filled.append(Fill(key="resume", file_path=resume, source="resume", label="Resume"))
        if _resume_shown(page, resume):
            notes.append("uploaded the resume through the page's Upload Resume button")
            return
        plan.needed.append(
            NeededInput(
                key="resume",
                label="Resume",
                kind="file",
                required=True,
                reason="the resume went to the page's Upload Resume button, but the page "
                "never showed it attached; not sent without it",
                answer_key="resume",
            )
        )

    def _keep_prefilled(
        self, page: Any, fields: list[FormField], plan: Any, filled: list[Fill]
    ) -> None:
        """A field the plan had no answer for that the page already filled
        (Serco's Country of Residence, set to United States) keeps what it
        holds. Never for a question only the person answers."""
        by_key = {f.key: f for f in fields}
        open_ = [
            n
            for n in plan.needed
            if n.key in by_key
            and n.reason != NEVER_GUESSED
            and n.answer_key not in SENSITIVE
            and n.answer_key != CONSENT_KEY
            and field_section(by_key[n.key]) != "eeo"
        ]
        held = current_values(page, [by_key[n.key] for n in open_])
        for need in open_:
            value = held.get(need.key)
            if value and by_key[need.key].kind != "file":
                plan.needed.remove(need)
                filled.append(Fill(key=need.key, value=value, source="prefilled", label=need.label))
                plan.notes.append(f"kept {need.label!r} as the page had it: {value!r}")

    def _look_again(
        self,
        page: Any,
        fields: list[FormField],
        plan: Any,
        filled: list[Fill],
        unfilled: list[NeededInput],
        notes: list[str],
    ) -> None:
        """Fill what appeared since the plan was made: a question an answer
        brought up, or a box the page emptied again after it was filled."""
        if self._answerer is None:
            return
        quiet = 0
        for _ in range(self.more_looks + 1):
            try:
                # A second, longer wait before deciding nothing more is coming.
                page.wait_for_timeout(500 if not quiet else 1_500)
                found = _the_application(page, discover_fields(page))
            except Exception as exc:
                log.debug("generic: could not look at the form again: %s", exc)
                return
            new = [f for f in _unseen(found, fields) if not _widget(f) and _shown(page, f.selector)]
            if _options_loading(page, new):
                # A list that appeared with an answer fills its choices a
                # moment later (Serco's Source): wait for them, then read again.
                _wait_for_options(page, new, self.options_wait_ms)
                if _options_loading(page, new):
                    # Still none: the answer that brought the list up is
                    # chosen again, which asks the page for its choices again.
                    self._choose_again(page, new, fields, filled, notes)
                    _wait_for_options(page, new, self.options_wait_ms)
                found = _the_application(page, discover_fields(page))
                new = [
                    f for f in _unseen(found, fields) if not _widget(f) and _shown(page, f.selector)
                ]
            emptied = _emptied(page, fields, filled)
            if not new and not emptied:
                quiet += 1
                if quiet > 1:
                    return
                continue
            if emptied:
                again = type(plan)(fills=emptied)
                _, lost, more = super().fill(page, fields, again)
                unfilled.extend(lost)
                notes.extend(more)
                notes.append(f"filled {len(emptied)} field(s) again after the page emptied them")
            if new:
                extra = self._answerer(new)
                done, lost, more = super().fill(page, new, extra)
                fields.extend(new)
                filled.extend(done)
                unfilled.extend(lost)
                notes.extend(more)
                plan.needed.extend(extra.needed)
                plan.notes.extend(extra.notes)
                plan.input_tokens += extra.input_tokens
                plan.output_tokens += extra.output_tokens

    def _choose_again(
        self,
        page: Any,
        new: list[FormField],
        fields: list[FormField],
        filled: list[Fill],
        notes: list[str],
    ) -> None:
        """Choose again the list answer just above each new list that has no
        choices: first its prompt, then the answer, as a person would."""
        by_key = {f.key: f for f in fields}
        chosen = [
            (by_key[f.key], f)
            for f in filled
            if f.key in by_key and by_key[f.key].kind == "select" and by_key[f.key].selector
        ]
        for empty in _empty_selects(page, new):
            before = _nearest_before(page, empty.selector, [f.selector for f, _ in chosen])
            if before is None:
                notes.append(f"{empty.label!r} came up with no choices, and none came")
                continue
            field, fill = chosen[before]
            try:
                page.locator(field.selector).first.select_option(index=0, timeout=5000)
                page.wait_for_timeout(300)
            except Exception as exc:
                log.debug("generic: could not reset %s: %s", field.selector, exc)
            error = fill_field(page, field, fill)
            notes.append(
                f"{empty.label!r} came up with no choices; chose {field.label!r} again"
                + (f" (could not: {error})" if error else "")
            )

    def discover(self, page: Any) -> list[FormField]:
        if self._handoff:
            return []
        fields = _the_application(page, discover_fields(page))
        if fields and looks_like_an_application(fields):
            self._sign_up = _sign_up(page, fields)
            if self._sign_up:
                log.info("generic: %s: %s", page.url, self._sign_up)
                return []
        if not fields or looks_like_an_application(fields):
            return fields
        log.info("generic: %s has a form, but not an application", page.url)
        return []

    def apply(self, page: Any, packet: Any, *args: Any, **kwargs: Any) -> Any:
        self._handoff = None
        self._posting = None
        self._sign_up = None
        self._page_fields = []
        self._resume_sent = False
        self._resume = getattr(packet, "resume_path", "") or ""
        self._wanted = list(getattr(packet, "wanted_places", None) or [])
        self._answerer = args[0] if args else kwargs.get("answerer")
        result = super().apply(page, packet, *args, **kwargs)
        if self._sign_up and not result.wrong_place:
            return HandlerResult(
                outcome="blocked",
                error=self._sign_up,
                final_url=result.final_url,
                screenshot_path=result.screenshot_path,
            )
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


def _page_end(page: Any, fields: list[FormField]) -> dict[str, Any] | None:
    """The button ending the page on screen: {kind: submit|next, text}, or None."""
    try:
        return page.evaluate(STEP_CONTROL_JS, [f.selector for f in fields if f.selector][:5])
    except Exception:
        return None


def _page_mark(page: Any, fields: list[FormField]) -> tuple[str, ...]:
    """What tells one page of a form from the next: the address and its fields."""
    return (str(getattr(page, "url", "") or ""), *sorted(f"{f.selector}|{f.label}" for f in fields))


def _wait_idle(page: Any, wait_ms: int) -> None:
    """Wait, up to `wait_ms`, until no spinner or progress bar is showing."""
    waited = 0
    while waited < wait_ms:
        try:
            if not page.evaluate(BUSY_JS):
                return
        except Exception:
            return
        page.wait_for_timeout(500)
        waited += 500


def _resume_shown(page: Any, resume: str) -> bool:
    """The page shows the resume as attached: its file name, or a word that it went on."""
    text = page_text(page)
    name = Path(resume).name
    return (
        name.lower() in text.lower()
        or Path(resume).stem.lower() in text.lower()
        or bool(_UPLOADED.search(text))
    )


_EMPTY_SELECTS_JS = """(selectors) => selectors.filter((sel) => {
  let el = null;
  try { el = document.querySelector(sel); } catch (e) { return false; }
  // A choice is an option with a value and words that are not a placeholder
  // ("Please Select", "Loading...", "--"), the same test the field reader uses.
  const real = (o) => o.value && o.value !== '-1' && o.value !== '0'
    && !/^\s*$|^-+$|^select\b|^choose\b|^please\s+select|^loading/i.test(o.text || '');
  return !!el && el.tagName === 'SELECT' && !Array.from(el.options).some(real);
})"""


def _empty_selects(page: Any, fields: list[FormField]) -> list[FormField]:
    """The native selects among `fields` that have no choices yet."""
    lists = [f for f in fields if f.kind in ("select", "multiselect") and f.selector]
    try:
        empty = set(page.evaluate(_EMPTY_SELECTS_JS, [f.selector for f in lists]) or [])
    except Exception:
        return []
    return [f for f in lists if f.selector in empty]


def _nearest_before(page: Any, selector: str, candidates: list[str]) -> int | None:
    """Which of `candidates` comes last before `selector` in the page, or None."""
    try:
        index = page.evaluate(
            """([sel, cands]) => {
                const AFTER = Node.DOCUMENT_POSITION_FOLLOWING;
                const el = document.querySelector(sel);
                let best = -1;
                cands.forEach((c, i) => {
                    let other = null;
                    try { other = document.querySelector(c); } catch (e) { return; }
                    if (!el || !other || other === el) return;
                    if (!(other.compareDocumentPosition(el) & AFTER)) return;
                    if (best < 0) { best = i; return; }
                    const cur = document.querySelector(cands[best]);
                    if (cur.compareDocumentPosition(other) & AFTER) best = i;
                });
                return best;
            }""",
            [selector, candidates],
        )
    except Exception:
        return None
    return None if index is None or index < 0 else int(index)


def _options_loading(page: Any, fields: list[FormField]) -> bool:
    """A native select among `fields` that has no choices yet."""
    return bool(_empty_selects(page, fields))


def _wait_for_options(page: Any, fields: list[FormField], wait_ms: int) -> None:
    waited = 0
    while waited < wait_ms and _options_loading(page, fields):
        page.wait_for_timeout(250)
        waited += 250


def _unseen(found: list[FormField], fields: list[FormField]) -> list[FormField]:
    """The fields in `found` that are not among `fields`. A key made from a
    control's position shifts when a control is added above it, so a field
    counts as seen by its key, its selector, or its label and kind."""
    keys = {f.key for f in fields}
    selectors = {f.selector for f in fields if f.selector}
    labels = {(f.label, f.kind) for f in fields if f.label}
    return [
        f
        for f in found
        if f.key not in keys and f.selector not in selectors and (f.label, f.kind) not in labels
    ]


def _emptied(page: Any, fields: list[FormField], filled: list[Fill]) -> list[Fill]:
    """The typed-in answers the page has since emptied."""
    by_key = {f.key: f for f in fields}
    typed = [
        f
        for f in filled
        if f.key in by_key
        and isinstance(f.value, str)
        and f.value
        and by_key[f.key].kind in ("text", "textarea", "number", "email", "tel")
    ]
    if not typed:
        return []
    held = current_values(page, [by_key[f.key] for f in typed])
    return [f for f in typed if not held.get(f.key)]


def _sign_up(page: Any, fields: list[FormField]) -> str | None:
    """Why the form is a sign-up rather than the application, or None. A form
    that takes a resume is an application whatever is written around it."""
    if any(f.kind == "file" and not _widget(f) for f in fields):
        return None
    try:
        seen = page.evaluate(SIGN_UP_JS, [f.selector for f in fields if f.selector][:5]) or {}
    except Exception:
        return None
    heading = str(seen.get("heading") or "")
    if _SIGN_UP.search(heading):
        return (
            f"the form is a sign-up ({heading!r}), not an application; nothing was sent "
            "and nothing more can be done here"
        )
    if _CODE_CHECK.search(str(seen.get("text") or "")):
        return (
            "the form asks to verify an email or phone number with a code sent to it; apply by hand"
        )
    return None


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
