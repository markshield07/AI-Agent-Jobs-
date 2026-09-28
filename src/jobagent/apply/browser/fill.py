"""Putting a plan on the page, and reading what the page says back.

Every operation here is best effort and reports rather than raises: a control
that will not take its value comes back as an error string, and the handler
decides whether that stops the submission. Outcome detection follows
job-agent's rule that a submission counts only when the page says so, in its
URL or in text that was not there before the button was pressed; anything
else is unconfirmed, never assumed.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from jobagent.apply.browser.dom import COMBOBOX_OPTIONS_JS, script
from jobagent.apply.models import Fill, FillPlan, FormField, NeededInput

log = logging.getLogger(__name__)

SETTLE_MS = 400

SUCCESS_TEXT_SIGNALS: tuple[str, ...] = (
    "application received",
    "application was received",
    "application has been received",
    "we have received your application",
    "we've received your application",
    "thank you for applying",
    "thanks for applying",
    "thank you for your application",
    "successfully submitted",
    "application submitted",
    "application has been submitted",
    "application was submitted",
    "your application was sent",
    "application complete",
    "we'll be in touch",
    "we will be in touch",
)
SUCCESS_URL_PATTERNS: tuple[str, ...] = (
    r"/confirmation\b",
    r"thank[-_]?you",
    r"application[-_](submitted|received|complete)\b",
    r"[?&]submitted=",
    r"/applied\b",
    r"/success\b",
)
ERROR_SELECTOR = (
    '[role="alert"], .field_with_errors, #application_errors, .application-error, '
    '.error-message, .form-error, .field-error, .error-text, [class*="error" i]'
    ':not([class*="error-boundary" i]):not(script):not(style), [aria-invalid="true"]'
)
CAPTCHA_JS = script("captcha.js")
LOGIN_WALL_JS = script("login_wall.js")
ERRORS_JS = script("errors.js")
CLICK_OPTION_JS = script("click_option.js")
MARK_FORM_JS = script("mark_form.js")
FORM_SCOPE = '[data-jobagent-form="1"]'
BODY_TEXT_JS = "() => (document.body ? document.body.innerText : '').slice(0, 40000)"


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s+]", " ", str(value or "").lower())).strip()


def _values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value if str(v).strip()]
    return [str(value)] if value not in (None, "") else []


# ------------------------------------------------------------- controls --


def _type_text(page: Any, field: FormField, value: str) -> None:
    control = page.locator(field.selector).first
    control.scroll_into_view_if_needed(timeout=3000)
    try:
        control.fill(value, timeout=5000)
    except Exception:
        control.click(timeout=3000)
        page.keyboard.press("Control+A")
        page.keyboard.type(value)


_SELECT_OPTIONS_JS = (
    "el => Array.from(el.options).map((o, i) => "
    "({index: i, text: (o.text || '').replace(/\\s+/g, ' ').trim(), value: o.value}))"
)


def _native_select(page: Any, field: FormField, values: list[str]) -> None:
    """Pick by index, after matching the wanted text against the options the
    select actually has.

    Handing an unknown label straight to `select_option` costs the full
    timeout before it gives up, and a form with a few of those would stall an
    apply run for minutes. Reading the options first makes a wrong answer
    immediate, and lets the same matching the planner uses decide what counts
    (`Yes` for `yes`, `Bananas` for `bananas`).
    """
    from jobagent.apply.answering import pick_option

    control = page.locator(field.selector).first
    options = list(control.evaluate(_SELECT_OPTIONS_JS) or [])
    # A prompt ("Please select") is not an answer: it is the empty value.
    choosable = [o for o in options if o["value"] != "" and o["text"]]
    texts = [o["text"] for o in choosable]
    indices: list[int] = []
    for wanted in values:
        match = pick_option(wanted, texts)
        if match is None:
            raise LookupError(f"no option matched {wanted!r} among {texts[:12]}")
        index = choosable[texts.index(match)]["index"]
        if index not in indices:
            indices.append(index)
    control.select_option(
        index=indices if field.kind == "multiselect" else indices[0], timeout=5000
    )


def _combobox_pick(page: Any, field: FormField, value: str) -> None:
    """Open the combobox, narrow it by typing, click the matching option."""
    control = page.locator(field.selector).first
    control.scroll_into_view_if_needed(timeout=3000)
    control.click(timeout=3000)
    page.wait_for_timeout(SETTLE_MS)
    if page.evaluate(CLICK_OPTION_JS, [value, "exact"]):
        return
    typed = False
    try:
        control.fill(value, timeout=2000)
        typed = True
    except Exception:
        try:
            page.keyboard.type(value)
            typed = True
        except Exception:
            pass
    if typed:
        page.wait_for_timeout(SETTLE_MS + 300)
        if page.evaluate(CLICK_OPTION_JS, [value, "loose"]):
            return
        # A typeahead (a location box) offers what it found: take the first hit
        # when the typed text is a prefix of it.
        options = list(page.evaluate(COMBOBOX_OPTIONS_JS) or [])
        if options and _norm(options[0]).startswith(_norm(value)[: max(3, len(_norm(value)) // 2)]):
            if page.evaluate(CLICK_OPTION_JS, [options[0], "exact"]):
                return
    else:
        if page.evaluate(CLICK_OPTION_JS, [value, "loose"]):
            return
    page.keyboard.press("Escape")
    raise LookupError(f"no option matched {value!r}")


def _is_native_select(page: Any, field: FormField) -> bool:
    try:
        return bool(page.locator(field.selector).first.evaluate("el => el.tagName === 'SELECT'"))
    except Exception:
        return False


def _select(page: Any, field: FormField, value: Any) -> None:
    values = _values(value)
    if not values:
        raise ValueError("nothing to select")
    if _is_native_select(page, field):
        _native_select(page, field, values)
        return
    for item in values if field.kind == "multiselect" else values[:1]:
        _combobox_pick(page, field, item)
    if field.kind == "multiselect":
        page.keyboard.press("Escape")


def _group_inputs(page: Any, field: FormField) -> list[Any]:
    return page.locator(field.selector).all()


def _input_label(page: Any, control: Any) -> str:
    return control.evaluate(script("input_label.js"))


def _click_input(page: Any, control: Any) -> None:
    """Check a radio or box, going through its label when the input itself is styled away."""
    try:
        control.check(timeout=2000)
        return
    except Exception:
        pass
    label = control.evaluate(script("label_selector.js"))
    if label:
        page.locator(label).first.click(timeout=3000)
    else:
        control.click(timeout=3000, force=True)


def _choose(page: Any, field: FormField, wanted: list[str], *, exclusive: bool) -> None:
    inputs = _group_inputs(page, field)
    if not inputs:
        raise LookupError("no inputs in the group")
    labels = [(control, _input_label(page, control)) for control in inputs]
    for want in wanted:
        target = None
        for control, label in labels:
            if _norm(label) == _norm(want):
                target = control
                break
        if target is None:
            for control, label in labels:
                if _norm(label).startswith(_norm(want)) or _norm(want) in _norm(label).split():
                    target = control
                    break
        if target is None:
            raise LookupError(f"no choice matched {want!r}")
        _click_input(page, target)
        if exclusive:
            return


def _set_checkbox(page: Any, field: FormField, flag: bool) -> None:
    control = page.locator(field.selector).first
    try:
        control.set_checked(flag, timeout=2000)
        return
    except Exception:
        pass
    try:
        checked = bool(control.is_checked())
    except Exception:
        checked = False
    if checked != flag:
        _click_input(page, control)


def fill_field(page: Any, field: FormField, fill: Fill) -> str | None:
    """Put `fill` on `field`. Returns why it could not, or None when it did."""
    try:
        kind = field.kind
        if kind == "file":
            if not fill.file_path or not Path(fill.file_path).is_file():
                return f"no file at {fill.file_path!r}"
            page.locator(field.selector).first.set_input_files(fill.file_path, timeout=5000)
        elif kind == "radio":
            _choose(page, field, _values(fill.value)[:1], exclusive=True)
        elif kind == "checkbox" and field.options:
            _choose(page, field, _values(fill.value), exclusive=False)
        elif kind == "checkbox":
            _set_checkbox(page, field, bool(fill.value))
        elif kind in ("select", "multiselect"):
            _select(page, field, fill.value)
        else:
            values = _values(fill.value)
            if not values:
                return "nothing to type"
            _type_text(page, field, values[0])
    except Exception as exc:
        first = str(exc).splitlines()[0][:200] if str(exc) else type(exc).__name__
        log.debug("fill %s failed: %s", field.key, first)
        return f"{type(exc).__name__}: {first}"
    return None


def fill_plan(
    page: Any, fields: Sequence[FormField], plan: FillPlan
) -> tuple[list[Fill], list[NeededInput], list[str]]:
    """Apply every fill. Returns what went on, what a required field still
    needs (with the page's reason), and notes about optional fields skipped."""
    by_key = {f.key: f for f in fields}
    done: list[Fill] = []
    needed: list[NeededInput] = []
    notes: list[str] = []
    for fill in plan.fills:
        field = by_key.get(fill.key)
        if field is None:
            notes.append(f"plan names {fill.key!r}, which the form does not have")
            continue
        error = fill_field(page, field, fill)
        if error is None:
            done.append(fill)
            continue
        reason = f"could not set it on the form: {error}"
        if field.required:
            needed.append(
                NeededInput(
                    key=field.key,
                    label=field.label,
                    kind=field.kind,
                    required=True,
                    options=list(field.options),
                    reason=reason,
                    answer_key=_answer_key(field),
                )
            )
        else:
            notes.append(f"{field.label or field.key}: {reason}")
    return done, needed, notes


def _answer_key(field: FormField) -> str:
    from jobagent.apply.answering import answer_key_for

    return answer_key_for(field)


# ----------------------------------------------------------------- page --


def wait_settled(page: Any, timeout_ms: int = 10_000) -> None:
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
    except Exception:
        pass
    try:
        page.wait_for_timeout(SETTLE_MS)
    except Exception:
        pass


def page_text(page: Any) -> str:
    try:
        return str(page.evaluate(BODY_TEXT_JS) or "")
    except Exception:
        return ""


def detect_captcha(page: Any) -> str | None:
    try:
        found = page.evaluate(CAPTCHA_JS)
    except Exception:
        return None
    return str(found) if found else None


def detect_login_wall(page: Any) -> bool:
    try:
        if page.evaluate(LOGIN_WALL_JS):
            return True
    except Exception:
        return False
    return bool(re.search(r"/(login|signin|sign-in|sign_in|auth)\b", page.url or "", re.I))


def visible_errors(page: Any) -> list[str]:
    try:
        return [str(e) for e in page.evaluate(ERRORS_JS, ERROR_SELECTOR) or []]
    except Exception:
        return []


def mark_form(page: Any, fields: Sequence[FormField]) -> str | None:
    """Tag the form most of `fields` belong to and return a selector for it.

    A page often carries more than one form: a site search above the
    application, a newsletter box below it. Pressing the first submit button
    on such a page runs a search instead of sending the application, so the
    button is looked for inside the form the fields came from.
    """
    selectors = [f.selector for f in fields if f.selector]
    if not selectors:
        return None
    try:
        found = bool(page.evaluate(MARK_FORM_JS, selectors))
    except Exception as exc:
        log.debug("could not mark the form: %s", exc)
        return None
    return FORM_SCOPE if found else None


def click_first_visible(page: Any, selectors: Sequence[str]) -> bool:
    """Click the first visible match. Hidden decoy buttons (Lever) are passed over."""
    for selector in selectors:
        try:
            candidates = page.locator(selector)
            for index in range(min(candidates.count(), 10)):
                button = candidates.nth(index)
                if button.is_visible():
                    button.scroll_into_view_if_needed(timeout=3000)
                    button.click(timeout=5000)
                    return True
        except Exception as exc:
            log.debug("submit selector %s: %s", selector, exc)
    return False


def _sentence_with(text: str, signal: str) -> str:
    index = text.lower().find(signal)
    if index < 0:
        return signal
    start = max(text.rfind(".", 0, index), text.rfind("\n", 0, index)) + 1
    end_candidates = [i for i in (text.find(".", index), text.find("\n", index)) if i > 0]
    end = min(end_candidates) if end_candidates else min(len(text), index + 160)
    return text[start:end].strip()[:200]


def check_outcome(page: Any, *, before_url: str, before_text: str = "") -> tuple[str, str | None]:
    """('submitted', confirmation) when the page says so; ('failed', errors)
    when it shows errors; else ('unconfirmed', None)."""
    url = page.url or ""
    text = page_text(page)
    lower, before = text.lower(), before_text.lower()
    for pattern in SUCCESS_URL_PATTERNS:
        if re.search(pattern, url, re.I) and not re.search(pattern, before_url or "", re.I):
            signal = next((s for s in SUCCESS_TEXT_SIGNALS if s in lower), None)
            return "submitted", _sentence_with(
                text, signal
            ) if signal else f"confirmation page {url}"
    for signal in SUCCESS_TEXT_SIGNALS:
        if signal in lower and signal not in before:
            return "submitted", _sentence_with(text, signal)
    errors = visible_errors(page)
    if errors:
        return "failed", "; ".join(errors[:5])
    return "unconfirmed", None


def take_screenshot(page: Any, path: str | None) -> str | None:
    if not path:
        return None
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=path, full_page=True)
        return path
    except Exception as exc:
        log.debug("screenshot failed: %s", exc)
        return None
