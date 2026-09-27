"""Workday: a company's own careers site, one account per company, a form in steps.

A posting lives at <company>.wd<N>.myworkdayjobs.com/<site>/job/<location>/<title>_<req>.
Its "Apply" button (data-automation-id="adventureButton") opens a choice:
"Autofill with Resume", "Apply Manually", sometimes "Use My Last
Application". Then comes a sign-in, since every company on Workday keeps its
own candidate accounts, and after it the steps: My Information, My
Experience, Application Questions, Voluntary Disclosures, Self Identify,
Review. Each step ends in "Save and Continue"; Review ends in "Submit", on
the same button (data-automation-id="bottom-navigation-next-button").

What this handler does about each part:

- The start choice is "Autofill with Resume" when offered: Workday reads the
  tailored resume into My Experience, which "Apply Manually" leaves for a
  person to type. What it read is kept like any prefilled value. The resume
  it shows again under My Experience is not uploaded twice.
- The sign-in is never typed. `jobagent login workday <posting URL>` opens a
  visible browser on that company's site once; the person signs in or
  creates the account there, and its cookies are kept, never the password.
  A run that meets a sign-in page stops as blocked and names that command.
- Workday draws most choices with its own controls: a dropdown is a button
  that opens a listbox, "How did you hear about us?" is a search box that
  picks items into pills (a "prompt"), and a date is three spin buttons.
  `workday_widgets.js` reads each as one field; filling it clicks and types
  the way a person would. An item that opens a further list (Job Board, then
  which board) takes the board the job was found on, when that list has it,
  and is asked about otherwise, never guessed.
- Two housekeeping answers are the same for everyone and are filled without
  asking: phone device type (Mobile), and the date on the self-identify form
  (today).
- Every "Save and Continue" saves a draft on the company's site. A dry run
  therefore leaves the application there unsent, at the Review step, where
  the person can read it and press Submit themselves.

Selectors are the data-automation-id values Workday has used for years and
that the reference repos drive (AutoApply `bot/apply/workday.py`); none of
them could be checked against a live site from where this was written, so
the fixture under tests/fixtures/forms/workday.html is the contract, and a
live dry run is the proof.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from urllib.parse import urlparse

from jobagent.apply.browser.dom import (
    _section,
    clean_label,
    current_values,
    discover_fields,
    script,
)
from jobagent.apply.browser.fill import (
    SETTLE_MS,
    click_first_visible,
    fill_field,
    fill_plan,
    page_text,
    wait_settled,
)
from jobagent.apply.handlers.wizard import WizardHandler
from jobagent.apply.models import Fill, FillPlan, FormField, HandlerResult, Packet
from jobagent.apply.sessions import WORKDAY_DOMAINS

log = logging.getLogger(__name__)

WIDGETS_JS = script("workday_widgets.js")

_COVERED_JS = r"""(selectors) => selectors.map((sel) => {
  let el = null;
  try { el = document.querySelector(sel); } catch (e) { return false; }
  return !!(el && el.closest('[data-jobagent-covered]'));
})"""

_ORDER_JS = r"""(selectors) => {
  const all = Array.from(document.querySelectorAll('*'));
  return selectors.map((sel) => {
    try { return all.indexOf(document.querySelector(sel)); } catch (e) { return -1; }
  });
}"""

_OPTION_TEXTS_JS = r"""() => {
  const txt = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const visible = (el) => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const found = Array.from(document.querySelectorAll(
    '[data-automation-id="promptOption"], [data-automation-id="promptLeafNode"], [role="option"]'
  )).filter(visible).map(txt).filter(Boolean);
  return Array.from(new Set(found)).slice(0, 200);
}"""

# Clicks the visible option that reads `wanted`: exactly, else by prefix, else
# as a whole word. Returns the text it clicked, or null.
_CLICK_JS = r"""(wanted) => {
  const norm = (s) => (s || '').toLowerCase().replace(/[^\w\s+]/g, ' ').replace(/\s+/g, ' ').trim();
  const visible = (el) => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  const want = norm(wanted);
  if (!want) return null;
  const options = Array.from(document.querySelectorAll(
    '[data-automation-id="promptOption"], [data-automation-id="promptLeafNode"], [role="option"]'
  )).filter(visible);
  const texts = options.map((o) => norm(o.innerText || o.textContent));
  let i = texts.findIndex((t) => t === want);
  if (i < 0) i = texts.findIndex((t) => t.startsWith(want) || (want.startsWith(t) && t.length > 2));
  if (i < 0) {
    const word = new RegExp('\\b' + want.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + '\\b');
    i = texts.findIndex((t) => word.test(t));
  }
  if (i < 0) return null;
  const el = options[i];
  el.scrollIntoView({ block: 'nearest' });
  for (const type of ['mousedown', 'mouseup', 'click']) {
    el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, view: window }));
  }
  return (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
}"""

_PICKED_JS = r"""(sel) => {
  const el = document.querySelector(sel);
  if (!el) return [];
  const box = el.closest('[data-automation-id^="formField-"]')
    || el.closest('[data-automation-id="multiselectInputContainer"]')
    || el.closest('[data-automation-id="multiSelectContainer"]');
  if (!box) return [];
  return Array.from(box.querySelectorAll('[data-automation-id="selectedItem"]'))
    .map((n) => (n.innerText || n.textContent || '').replace(/\s+/g, ' ').trim()).filter(Boolean);
}"""

# Housekeeping questions with one answer for everybody, filled when the site
# left them empty: (label pattern, the option wanted).
_DEFAULT_CHOICES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bphone device type\b|\bphone type\b", re.I), "Mobile"),
)
# The name a self-identification form is signed with, and the date it is signed on.
_SIGNATURE_LABEL = re.compile(r"^(your\s+)?(full\s+)?name$|^signature$", re.I)
_TODAY_LABEL = re.compile(r"^(today'?s\s+)?date$|^date\s+signed$|^signature\s+date$", re.I)

_EEO_LABEL = re.compile(
    r"gender|\brace\b|ethnic|hispanic|latin[ox]|veteran|disabilit|orientation|transgender"
    r"|self[- ]identif",
    re.I,
)

# The board a job was found on, as a "How did you hear about us?" list names it.
_SOURCE_LABELS = {
    "linkedin": "LinkedIn",
    "indeed": "Indeed",
    "glassdoor": "Glassdoor",
    "zip_recruiter": "ZipRecruiter",
    "ziprecruiter": "ZipRecruiter",
}

_CLOSED = (
    "no longer accepting applications",
    "job posting is no longer available",
    "this job is no longer available",
    "position has been filled",
    "the page you are looking for doesn't exist",
)


@dataclass(slots=True)
class _Widget:
    kind: str  # dropdown, prompt or date
    value: str
    parts: tuple[str | None, str | None, str | None] = (None, None, None)


class WorkdayHandler(WizardHandler):
    ats = "workday"
    site = "workday"
    hosts = WORKDAY_DOMAINS
    own_hosts = WORKDAY_DOMAINS
    easy_apply_selectors = (
        "a[data-automation-id='adventureButton']",
        "button[data-automation-id='adventureButton']",
        "a[role='button']:text-is('Apply')",
        "button:text-is('Apply')",
    )
    # The start choice, in the order preferred.
    start_selectors = (
        "a[data-automation-id='autofillWithResume']",
        "button[data-automation-id='autofillWithResume']",
        "a[data-automation-id='applyManually']",
        "button[data-automation-id='applyManually']",
    )
    signed_out_selectors = (
        "input[type='password']",
        "[data-automation-id='signInContent']",
        "[data-automation-id='createAccountContent']",
        "[data-automation-id='signInSubmitButton']",
        "[data-automation-id='createAccountSubmitButton']",
    )
    root_selectors = (
        "[data-automation-id='applyFlowPage']",
        "main",
        "[role='main']",
    )
    # Checked after the Submit button, which shares Next's automation id on
    # the Review step; the loop looks for Submit first, so Next is never
    # pressed when it reads Submit.
    next_selectors = (
        "button[data-automation-id='bottom-navigation-next-button']",
        "button[data-automation-id='pageFooterNextButton']",
        "button:text-is('Save and Continue')",
        "button:text-is('Continue')",
        "button:text-is('Next')",
    )
    submit_selectors = (
        "button[data-automation-id='bottom-navigation-next-button']:text-is('Submit')",
        "button[data-automation-id='pageFooterSubmitButton']",
        "button:text-is('Submit')",
        "button:text-is('Submit Application')",
    )
    step_error_selectors = (
        "[data-automation-id='errorMessage']",
        "[data-automation-id='errorBanner'] li",
        "[data-automation-id='inputAlert']",
    )
    success_signals = (
        "application submitted",
        "your application has been submitted",
        "application has been submitted",
        "thank you for applying",
        "thanks for applying",
        "successfully submitted",
        "we have received your application",
    )
    success_url = None
    refill_files = False
    # The wait after opening a list or picking from it, for Workday to draw it.
    pause_ms = SETTLE_MS
    max_steps = 10
    settle_ms = 8_000

    def __init__(self) -> None:
        self._posting = ""
        self._widgets: dict[str, _Widget] = {}
        self._uploaded: dict[str, str] = {}
        self._source = ""
        self._full_name = ""

    # -- the flow ------------------------------------------------------------

    def apply(
        self,
        page: Any,
        packet: Packet,
        answerer: Any,
        *,
        submit: bool,
        screenshot_path: str | None = None,
    ) -> HandlerResult:
        self._posting = self.application_url(
            packet.job.get("apply_url") or packet.job.get("url") or ""
        )
        self._source = _SOURCE_LABELS.get(str(packet.job.get("source") or "").lower(), "")
        self._full_name = str(packet.contact.get("full_name") or "").strip()
        return super().apply(page, packet, answerer, submit=submit, screenshot_path=screenshot_path)

    def open_flow(self, page: Any) -> Any:
        """Press Apply unless the page is already past it, then pick how to start."""
        flow = page
        if not self._any_visible(page, self.start_selectors) and not self._in_flow(page):
            flow = super().open_flow(page)
            if flow is None:
                return None
        if click_first_visible(flow, self.start_selectors):
            wait_settled(flow, self.settle_ms)
        return flow

    def _in_flow(self, page: Any) -> bool:
        return (
            self.signed_out(page)
            or self._any_visible(page, self.submit_selectors)
            or self._any_visible(page, self.next_selectors)
        )

    def login_hint(self) -> str:
        host = urlparse(self._posting).hostname or ""
        where = f"{host}'s Workday site" if host else "this company's Workday site"
        return (
            f"not signed in to {where}; each company on Workday has its own account. Run "
            f"`jobagent login workday {self._posting or '<posting URL>'}` on your own machine "
            "to sign in (or create the account) once, then try again"
        )

    def _no_button(self, page: Any, screenshot_path: str | None) -> HandlerResult:
        text = page_text(page).lower()
        if any(signal in text for signal in _CLOSED):
            error = "the Workday posting is closed"
        else:
            error = (
                "no Apply button on the Workday posting; it may be closed, or already "
                "applied to from this account"
            )
        return self._result(page, "blocked", screenshot_path, error=error)

    def _has_session(self, page: Any) -> bool:
        host = urlparse(getattr(page, "url", "") or "").hostname or ""
        try:
            cookies = page.context.cookies()
        except Exception:
            return False
        domains = {(c.get("domain") or "").lstrip(".").lower() for c in cookies}
        return any(d and (host == d or host.endswith("." + d)) for d in domains)

    # -- reading a step --------------------------------------------------------

    def discover_step(self, page: Any, root: str | None) -> list[FormField]:
        try:
            found = page.evaluate(WIDGETS_JS, root) or {}
        except Exception as exc:
            log.debug("could not read Workday's own controls: %s", exc)
            found = {}
        generic = discover_fields(page, root=root, expand_comboboxes=False)
        covered: list[bool] = []
        if generic:
            try:
                covered = list(page.evaluate(_COVERED_JS, [f.selector for f in generic]) or [])
            except Exception:
                covered = []
        covered += [False] * (len(generic) - len(covered))
        fields = [f for f, hidden in zip(generic, covered, strict=True) if not hidden]
        for field in fields:
            if field.kind in ("select", "multiselect") and not field.options:
                field.options = self._options(page, field.selector)
            # The terms box sits on the Voluntary Disclosures page, whose heading
            # would make it read as a demographic question with no way to decline.
            if field.kind == "checkbox" and not field.options and field.section == "eeo":
                if not _EEO_LABEL.search(field.label or ""):
                    field.section = "other"

        self._widgets = {}
        self._uploaded = dict(found.get("uploaded") or {})
        for raw in found.get("widgets") or []:
            kind = raw.get("widget")
            if kind not in ("dropdown", "prompt", "date") or not raw.get("selector"):
                continue
            label = clean_label(raw.get("label") or "")
            value = str(raw.get("value") or "")
            options: list[str] = []
            if kind != "date" and not value:
                options = self._options(page, raw["selector"])
            field = FormField(
                key=str(raw.get("key") or raw["selector"]),
                label=label,
                # A prompt searches far more than its first list shows (every
                # school, every board), so its answer is text to search for;
                # the first list goes along as options for whoever answers.
                kind={"date": "date", "prompt": "text"}.get(kind, "select"),
                required=bool(raw.get("required")),
                options=options,
                section=_section({"context": raw.get("context") or "", "legend": label}),
                selector=raw["selector"],
            )
            parts = tuple((raw.get("parts") or []) + [None, None, None])[:3]
            self._widgets[field.key] = _Widget(kind=kind, value=value, parts=parts)
            fields.append(field)
        return self._in_page_order(page, fields)

    def prefilled(self, page: Any, fields: list[FormField]) -> dict[str, str]:
        plain = [f for f in fields if f.key not in self._widgets]
        values = current_values(page, plain)
        for field in fields:
            widget = self._widgets.get(field.key)
            if widget is not None and widget.value:
                values[field.key] = widget.value
            if field.kind == "file" and field.selector in self._uploaded:
                values[field.key] = self._uploaded[field.selector]
        return values

    def _options(self, page: Any, selector: str) -> list[str]:
        """Open a dropdown or prompt, read what it offers, close it."""
        try:
            control = page.locator(selector).first
            control.scroll_into_view_if_needed(timeout=3000)
            control.click(timeout=3000)
            page.wait_for_timeout(self.pause_ms)
            options = [str(o) for o in page.evaluate(_OPTION_TEXTS_JS) or []]
        except Exception as exc:
            log.debug("options of %s: %s", selector, exc)
            options = []
        _escape(page)
        return [o for o in options if not re.match(r"^(select one|no items)\b", o, re.I)]

    @staticmethod
    def _in_page_order(page: Any, fields: list[FormField]) -> list[FormField]:
        """Filled in the order the page shows them: a country chosen after the
        address was typed can clear it."""
        try:
            order = list(page.evaluate(_ORDER_JS, [f.selector for f in fields]) or [])
        except Exception:
            return fields
        if len(order) != len(fields):
            return fields
        ranked = sorted(zip(order, range(len(fields)), fields, strict=True))
        return [field for _, _, field in ranked]

    # -- filling a step ----------------------------------------------------------

    def fill(self, page: Any, fields: list[FormField], plan: Any) -> tuple[list, list, list]:
        self._add_defaults(fields, plan)
        return fill_plan(page, fields, plan, fill_one=self._fill_one)

    def _add_defaults(self, fields: list[FormField], plan: FillPlan) -> None:
        """Answer the housekeeping questions the answerer would otherwise ask about."""
        planned = {f.key for f in plan.fills}
        for field in fields:
            if field.key in planned:
                continue
            value: str | None = None
            source = "default"
            if field.kind == "date" and _TODAY_LABEL.match(field.label or ""):
                value = date.today().strftime("%m/%d/%Y")
            elif field.kind == "text" and _SIGNATURE_LABEL.match(field.label or ""):
                value, source = self._full_name or None, "contact"
            elif field.kind == "select":
                for pattern, wanted in _DEFAULT_CHOICES:
                    if pattern.search(field.label or ""):
                        value = next(
                            (o for o in field.options if o.lower() == wanted.lower()), None
                        )
            if value is None:
                continue
            plan.fills.append(Fill(key=field.key, value=value, source=source, label=field.label))
            plan.needed[:] = [n for n in plan.needed if n.key != field.key]

    def _fill_one(self, page: Any, field: FormField, fill: Fill) -> str | None:
        widget = self._widgets.get(field.key)
        if widget is None:
            return fill_field(page, field, fill)
        value = fill.value[0] if isinstance(fill.value, list) and fill.value else fill.value
        text = str(value or "").strip()
        if not text:
            return "nothing to put there"
        try:
            if widget.kind == "dropdown":
                self._pick_dropdown(page, field, text)
            elif widget.kind == "prompt":
                self._pick_prompt(page, field, text)
            else:
                self._type_date(page, widget, text)
        except Exception as exc:
            first = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
            return f"{type(exc).__name__}: {first}"
        return None

    def _pick_dropdown(self, page: Any, field: FormField, value: str) -> None:
        control = page.locator(field.selector).first
        control.scroll_into_view_if_needed(timeout=3000)
        control.click(timeout=3000)
        page.wait_for_timeout(self.pause_ms)
        if not page.evaluate(_CLICK_JS, value):
            _escape(page)
            raise LookupError(f"no option matched {value!r}")
        page.wait_for_timeout(self.pause_ms)
        shown = (control.inner_text(timeout=2000) or "").strip()
        if re.fullmatch(r"select one|select|", shown, re.I):
            raise LookupError(f"{value!r} did not stay chosen")

    def _pick_prompt(self, page: Any, field: FormField, value: str) -> None:
        """Search the prompt for the item and click it, as a person would."""
        control = page.locator(field.selector).first
        control.scroll_into_view_if_needed(timeout=3000)
        control.click(timeout=3000)
        page.wait_for_timeout(self.pause_ms)
        clicked = page.evaluate(_CLICK_JS, value)
        if not clicked:
            control.fill(value, timeout=3000)
            page.keyboard.press("Enter")
            page.wait_for_timeout(self.pause_ms + 400)
            clicked = page.evaluate(_CLICK_JS, value)
        if not clicked:
            _escape(page)
            raise LookupError(f"no item matched {value!r}")
        page.wait_for_timeout(self.pause_ms)
        picked = list(page.evaluate(_PICKED_JS, field.selector) or [])
        if not picked and self._source and page.evaluate(_CLICK_JS, self._source):
            # "Job Board" opened the list of boards: the one the job was found on.
            page.wait_for_timeout(self.pause_ms)
            picked = list(page.evaluate(_PICKED_JS, field.selector) or [])
        _escape(page)
        if not picked:
            raise LookupError(f"{clicked!r} opens a further list here; choose the item yourself")

    def _type_date(self, page: Any, widget: _Widget, value: str) -> None:
        parsed = parse_date(value)
        if parsed is None:
            raise ValueError(f"{value!r} is not a date this form can take")
        month, day, year = parsed
        wanted = (
            f"{month:02d}" if month else None,
            f"{day:02d}" if day else None,
            f"{year:04d}" if year else None,
        )
        for selector, part, name in zip(
            widget.parts, wanted, ("month", "day", "year"), strict=True
        ):
            if selector is None:
                continue
            if part is None:
                raise ValueError(f"the form wants a {name} and {value!r} does not give one")
            control = page.locator(selector).first
            control.click(timeout=3000)
            page.keyboard.type(part)
            page.wait_for_timeout(100)


def parse_date(value: str) -> tuple[int | None, int | None, int] | None:
    """(month, day, year) from the shapes an answer comes in; day or month may be None."""
    text = re.sub(r"\s+", " ", (value or "").strip())
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m-%d-%Y", "%B %d, %Y", "%b %d, %Y", "%d %B %Y"):
        try:
            moment = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return moment.month, moment.day, moment.year
    for fmt in ("%Y-%m", "%m/%Y", "%B %Y", "%b %Y"):
        try:
            moment = datetime.strptime(text, fmt)
        except ValueError:
            continue
        return moment.month, None, moment.year
    if re.fullmatch(r"(19|20)\d\d", text):
        return None, None, int(text)
    return None


def _escape(page: Any) -> None:
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(100)
    except Exception:
        pass
