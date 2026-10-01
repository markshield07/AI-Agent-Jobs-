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

import re
from pathlib import Path
from typing import Any

from jobagent.apply.browser.fill import fill_field, fill_plan
from jobagent.apply.handlers.wizard import ROOT, WizardHandler
from jobagent.apply.models import Fill, FormField

# The resume card with no file box: its "Replace" (or "Upload resume")
# button, which opens the file chooser or draws the box, stands in for one.
UPLOAD_KEY = "dice-resume-upload"
UPLOAD_ATTR = "data-jobagent-upload"
_MARK_UPLOAD_JS = r"""([root, attr]) => {
  document.querySelectorAll('[' + attr + ']').forEach((el) => el.removeAttribute(attr));
  const scope = document.querySelector(root) || document;
  if (scope.querySelector('input[type="file"]')) return false;
  const button = Array.from(scope.querySelectorAll('button, [role="button"]')).find((b) => {
    const r = b.getBoundingClientRect();
    const text = (b.innerText || b.getAttribute('aria-label') || '').trim();
    return r.width > 0 && /^(replace( resume)?|upload( a)? (new )?resume)$/i.test(text);
  });
  if (!button) return false;
  button.setAttribute(attr, 'resume');
  return true;
}"""
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
    # Assumption: the wizard draws its steps inside `main`; the site header
    # with its search box is outside it.
    root_selectors = ("[data-testid*='wizard' i]", "main form", "main")
    next_selectors = (
        "button:has-text('Next')",
        "button[aria-label*='Next']",
        "button:has-text('Continue')",
    )
    submit_selectors = (
        "button:has-text('Submit Application')",
        "button[type='submit']:has-text('Submit')",
        "button:has-text('Submit')",
    )
    # Assumption: field errors use role=alert or an "error" data-testid, as
    # the success card's data-testid suggests Dice names its parts.
    step_error_selectors = (
        "[data-testid*='error' i]",
        "[role='alert']",
        "[aria-live='assertive']",
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

    def discover_step(self, page: Any, root: str | None) -> list[FormField]:
        """The step's fields; on the resume step with no file box, the resume
        card's "Replace" button as the resume's upload, so the tailored
        resume goes instead of the one on the profile."""
        fields = super().discover_step(page, root)
        if any(f.kind == "file" for f in fields):
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
                    selector=f'[{UPLOAD_ATTR}="resume"]',
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
        try:
            with page.expect_file_chooser(timeout=5000) as chooser:
                page.locator(field.selector).first.click(timeout=5000)
            chooser.value.set_files(fill.file_path)
        except Exception:
            # Replace may draw a file box instead of opening the chooser.
            box = page.locator(f"{ROOT} input[type='file'], input[type='file']").first
            try:
                box.set_input_files(fill.file_path, timeout=5000)
            except Exception as exc:
                return f"Replace gave neither a file chooser nor a file box: {type(exc).__name__}"
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
