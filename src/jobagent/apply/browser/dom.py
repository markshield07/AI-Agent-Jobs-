"""Reading a form off a page: every control, its label, whether it is
required, and its options, as `FormField`s the answerer understands.

One script runs in the page and returns the raw controls; Python groups
radios and checkbox sets, names the kind, and opens each combobox once to read
its options, since a react-select list is not in the document until it opens
(job-agent, job-pilot). Selectors come back stable enough for the filler to
find the control again: its id, else its name, else a short path.
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any

from jobagent.apply.models import FieldKind, FormField, Section

log = logging.getLogger(__name__)

_JS = Path(__file__).parent / "js"


def script(name: str) -> str:
    """One of the scripts under js/, run in the page with `page.evaluate`."""
    return (_JS / name).read_text()


INVENTORY_JS = script("inventory.js")

COMBOBOX_OPTIONS_JS = script("combobox_options.js")

_EEO = re.compile(r"eeo|demograph|self[-_ ]?identif|voluntary", re.IGNORECASE)
_QUESTIONS = re.compile(
    r"custom[-_ ]?field|question|screening|additional|cards\[|application-question"
    r"|answers_attributes",
    re.IGNORECASE,
)
_COMBOBOX_CLASS = re.compile(r"select__input|autocomplete|typeahead|combobox", re.IGNORECASE)

_INPUT_KINDS: dict[str, FieldKind] = {
    "text": "text",
    "search": "text",
    "email": "email",
    "tel": "tel",
    "url": "url",
    "number": "number",
    "date": "date",
    "month": "date",
    "password": "unknown",
    "": "text",
}


def _is_combobox(control: dict[str, Any]) -> bool:
    return bool(
        control.get("role") == "combobox"
        or control.get("aria_haspopup") in ("listbox", "true")
        or control.get("aria_autocomplete") in ("list", "both")
        or _COMBOBOX_CLASS.search(control.get("classes") or "")
    )


def _kind(control: dict[str, Any]) -> FieldKind:
    tag, type_ = control["tag"], control.get("type") or ""
    role = control.get("role")
    if tag == "select":
        return "multiselect" if control.get("multiple") else "select"
    if tag == "textarea" or control.get("contenteditable"):
        return "textarea"
    if role in ("radio",):
        return "radio"
    if role in ("checkbox", "switch"):
        return "checkbox"
    if tag == "input":
        if type_ == "file":
            return "file"
        if type_ == "radio":
            return "radio"
        if type_ == "checkbox":
            return "checkbox"
        if _is_combobox(control):
            return "multiselect" if control.get("multiple") else "select"
        return _INPUT_KINDS.get(type_, "unknown")
    if _is_combobox(control):
        return "multiselect" if control.get("multiple") else "select"
    return "unknown"


def _section(control: dict[str, Any]) -> Section:
    context = " ".join(str(control.get(k) or "") for k in ("context", "name", "id"))
    if _EEO.search(context) or _EEO.search(control.get("legend") or ""):
        return "eeo"
    if _QUESTIONS.search(context):
        return "questions"
    return "other"


def _key(control: dict[str, Any], index: int) -> str:
    return control.get("name") or control.get("id") or f"{control['tag']}-{index}"


# The marks a form puts after a required field's label: a star in one of its
# several shapes, or the word itself. They are noise in a question put to the
# user, and `required` already carries what they mean.
_STARS = r"[\s*\u2217\u2731\u273b\uff0a]*"
_REQUIRED_MARK = re.compile(_STARS + r"(\(?required\)?)?" + _STARS + r"$", re.I)


def clean_label(label: str) -> str:
    text = _REQUIRED_MARK.sub("", (label or "").strip())
    return re.sub(r"\s+", " ", text).strip()


def _label(control: dict[str, Any], group: bool = False) -> str:
    """A group's question is its legend; a lone control's is its own label."""
    if group:
        return clean_label(control.get("legend") or control.get("label") or "")
    label = control.get("label") or ""
    if not label or (control.get("legend") and len(label) < 3):
        label = control.get("legend") or label
    return clean_label(label)


def _group_key(control: dict[str, Any]) -> str | None:
    """Radios and checkboxes that share a name are one question."""
    grouped = control["tag"] == "input" and control.get("type") in ("radio", "checkbox")
    if grouped and control.get("name"):
        return f"{control['type']}:{control['name']}"
    return None


def raw_controls(page: Any, root: str | None = None) -> list[dict[str, Any]]:
    """The inventory's raw records; `root` limits it to the controls inside one element."""
    return list(page.evaluate(INVENTORY_JS, root) or [])


def combobox_options(page: Any, selector: str, *, settle_ms: int = 400) -> list[str]:
    """Open the combobox at `selector`, read its options, close it. [] when none show."""
    try:
        control = page.locator(selector).first
        control.scroll_into_view_if_needed(timeout=3000)
        control.click(timeout=3000)
        page.wait_for_timeout(settle_ms)
        options = list(page.evaluate(COMBOBOX_OPTIONS_JS) or [])
    except Exception as exc:  # a combobox that will not open is not fatal
        log.debug("combobox %s did not open: %s", selector, exc)
        options = []
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(100)
    except Exception:
        pass
    return [o for o in options if o and not re.match(r"^(select|choose|please select)\b", o, re.I)]


def discover_fields(
    page: Any, *, expand_comboboxes: bool = True, root: str | None = None
) -> list[FormField]:
    """Every fillable control on the page, grouped and labelled, in document order.

    `root` is a selector for the element that holds the form, when the page
    around it has controls of its own (a modal over a job listing)."""
    controls = raw_controls(page, root)
    fields: list[FormField] = []
    groups: OrderedDict[str, FormField] = OrderedDict()
    for index, control in enumerate(controls):
        group = _group_key(control)
        if group is not None:
            existing = groups.get(group)
            option = control.get("option_label") or control.get("value") or ""
            if existing is None:
                kind: FieldKind = "radio" if control["type"] == "radio" else "checkbox"
                field = FormField(
                    key=control["name"],
                    label="",
                    kind=kind,
                    required=bool(control.get("required")),
                    options=[],
                    section=_section(control),
                    selector=f'input[type="{control["type"]}"][name="{control["name"]}"]',
                    name=control.get("name"),
                    help_text=control.get("help") or None,
                )
                field.label = _label(control, group=True)
                groups[group] = field
                fields.append(field)
                existing = field
            if option and option not in existing.options:
                existing.options.append(option)
            existing.required = existing.required or bool(control.get("required"))
            continue

        kind = _kind(control)
        field = FormField(
            key=_key(control, index),
            label=_label(control),
            kind=kind,
            required=bool(control.get("required")),
            options=list(control.get("options") or []),
            section=_section(control),
            selector=control.get("selector") or "",
            name=control.get("name"),
            accept=control.get("accept"),
            multiple=bool(control.get("multiple")),
            help_text=control.get("help") or None,
        )
        if kind in ("select", "multiselect") and not field.options and expand_comboboxes:
            field.options = combobox_options(page, field.selector)
        fields.append(field)

    # A lone checkbox is not a group: its question is the box's own text, and
    # the legend around it is context. A box whose only text is its value
    # ("1", "on") keeps the legend.
    for field in fields:
        if field.kind == "checkbox" and len(field.options) == 1:
            own = field.options[0]
            if own and not re.fullmatch(r"[\w-]{0,3}|on|true|yes", own, re.I):
                if field.label and field.label != own:
                    field.help_text = field.help_text or field.label
                field.label = own
            field.options = []
    return fields


_VALUES_JS = r"""(selectors) => selectors.map((sel) => {
  let el = null;
  try { el = document.querySelector(sel); } catch (e) { return null; }
  if (!el) return null;
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  if (type === 'file') return el.files && el.files.length ? el.files[0].name : null;
  if (type === 'radio' || type === 'checkbox') {
    const name = el.getAttribute('name');
    if (!name) return el.checked ? (el.value || 'on') : null;
    const on = Array.from(document.querySelectorAll('input[name="' + CSS.escape(name) + '"]'))
      .filter((n) => n.checked);
    return on.length ? on.map((n) => n.value).join(', ') : null;
  }
  if (tag === 'select') {
    const opt = el.options[el.selectedIndex];
    if (!opt || !opt.value) return null;
    const text = (opt.text || '').replace(/\s+/g, ' ').trim();
    return /^select\b|^choose\b|^please select/i.test(text) ? null : text;
  }
  if (el.isContentEditable) return (el.innerText || '').trim() || null;
  const v = (el.value || '').trim();
  return v || null;
})"""


def current_values(page: Any, fields: list[FormField]) -> dict[str, str]:
    """What each field already holds, keyed by field key; empty fields are left out.

    A site that prefills from the signed-in account (LinkedIn, Indeed) has
    already put the person's own details there, and overwriting them with a
    guess at the same thing only risks a mismatch.
    """
    if not fields:
        return {}
    try:
        values = page.evaluate(_VALUES_JS, [f.selector for f in fields]) or []
    except Exception as exc:
        log.debug("could not read current values: %s", exc)
        return {}
    return {f.key: str(v) for f, v in zip(fields, values, strict=False) if v}
