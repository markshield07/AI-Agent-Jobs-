"""LinkedIn Easy Apply: a modal of steps over the posting, for a signed-in member.

The posting is linkedin.com/jobs/view/<id>. A member sees an "Easy Apply"
button when the company takes applications through LinkedIn, and a plain
"Apply" that opens the company's site otherwise; a visitor who is not signed
in sees "Sign in" and is sent through /authwall or /login. The Easy Apply
button opens a `.jobs-easy-apply-modal` dialog whose steps are contact info,
resume, questions and review, moved through with "Continue to next step",
"Review your application" and finally "Submit application". LinkedIn
prefills contact details from the profile and offers the resumes uploaded
before; the tailored resume is uploaded anyway, since it is the one written
for this job.

Attested by the reference study: AutoApply `bot/apply/linkedin.py` (the
button, Next and Submit selector chains, a ten-step limit) and job-agent
`linkedin_easy_apply.py` (the aria-labels "Continue to next step", "Review
your application" and "Submit application", the form-element classes, the
sent text "application was sent to", and signing in from a saved browser
state rather than a password). Assumptions of this module are marked where
they occur.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from jobagent.apply.browser.fill import click_first_visible, fill_field, fill_plan, wait_settled
from jobagent.apply.handlers.wizard import ROOT, WizardHandler
from jobagent.apply.models import Fill, FormField

# The resume step with no file box: LinkedIn's "Upload resume" button, which
# opens the file chooser itself, stands in for one.
UPLOAD_KEY = "linkedin-resume-upload"
UPLOAD_ATTR = "data-jobagent-upload"
_MARK_UPLOAD_JS = r"""([root, attr]) => {
  document.querySelectorAll('[' + attr + ']').forEach((el) => el.removeAttribute(attr));
  const scope = document.querySelector(root) || document;
  if (scope.querySelector('input[type="file"]')) return false;
  const button = Array.from(scope.querySelectorAll('button')).find((b) => {
    const r = b.getBoundingClientRect();
    return r.width > 0 && /^\s*upload\s+(a\s+)?(resume|cv)\s*$/i.test(b.innerText || '');
  });
  if (!button) return false;
  button.setAttribute(attr, 'resume');
  return true;
}"""
_ROOT_TEXT = "(sel) => (document.querySelector(sel) || document.body).innerText || ''"
# Whether the step shows the file as the chosen resume: its name, or a
# checked choice in the resume list.
_RESUME_SHOWN_JS = r"""([root, name]) => {
  const scope = document.querySelector(root) || document;
  const text = (scope.innerText || '').toLowerCase();
  if (name && text.includes(name.toLowerCase())) return true;
  const ref = scope.querySelector('#easyApplyUploadedResumeRef, [role="radiogroup"]');
  const picked = 'input:checked, [aria-checked="true"], [aria-selected="true"]';
  return !!(ref && ref.querySelector(picked));
}"""


class LinkedInHandler(WizardHandler):
    ats = "linkedin"
    site = "linkedin"
    hosts = ("linkedin.com",)
    own_hosts = ("linkedin.com",)
    easy_apply_selectors = (
        "button.jobs-apply-button[aria-label*='Easy Apply']",
        "button[aria-label*='Easy Apply']",
        ".jobs-apply-button--top-card button:has-text('Easy Apply')",
        "button:has-text('Easy Apply')",
        "a[aria-label*='Easy Apply']",
    )
    # The company-site button is the same `.jobs-apply-button`, labelled
    # "Apply" and marked as leaving LinkedIn (assumption: the aria-label reads
    # "Apply to <title> on company website", as it did when last seen). The
    # 2026 layout draws it as a link labelled "Apply on company website" whose
    # href is linkedin.com/safety/go/?url=<the company's page>.
    external_apply_selectors = (
        "a[aria-label*='company website'][href]",
        "a[aria-label^='Apply on'][href]",
        "button.jobs-apply-button[aria-label*='company website']",
        "a.jobs-apply-button[href]:not([href*='linkedin.com'])",
        "button[role='link'].jobs-apply-button",
    )
    signed_out_selectors = (
        "a.nav__button-secondary[href*='login']",
        "button:has-text('Sign in to apply')",
        "a:has-text('Sign in to apply')",
        "form.join-form",
    )
    # The 2026 layout (the Mac's capture of Stand8's, 2026-09-28): a native
    # <dialog data-testid="dialog" aria-labelledby="dialog-header">, obfuscated
    # class names, and buttons that carry only their text ("Next", "Review",
    # "Submit application"), so those are matched by text inside the dialog.
    root_selectors = (
        "dialog[open][aria-labelledby='dialog-header']",
        "dialog[open][data-testid='dialog']",
        ".jobs-easy-apply-modal",
        "[data-test-modal][role='dialog']",
        "div[role='dialog']",
    )
    next_selectors = (
        "button[aria-label='Continue to next step']",
        "button[aria-label='Review your application']",
        "button[aria-label*='Continue']",
        "button[aria-label*='Next']",
        "button[aria-label*='Review']",
        "footer button:has-text('Next')",
        "footer button:has-text('Review')",
        "footer button:has-text('Continue')",
        "button:has-text('Next')",
        "button:has-text('Review')",
    )
    submit_selectors = (
        "button[aria-label='Submit application']",
        "button[aria-label*='Submit application']",
        "button:has-text('Submit application')",
    )
    step_error_selectors = (
        ".artdeco-inline-feedback--error",
        ".fb-dash-form-element-error",
        "[data-test-form-element-error-messages]",
        "[componentkey^='easyApplyFieldFocus'] [role='alert']",
        "[componentkey^='easyApplyFieldFocus'] [data-testid*='error' i]",
    )
    success_signals = (
        "application was sent",
        "your application was sent",
        "application submitted",
    )
    success_url = re.compile(r"/post-apply\b|[?&]postApply", re.IGNORECASE)
    max_steps = 12
    settle_ms = 6_000

    def matches(self, url: str) -> bool:
        # Only postings: a company page or profile link is not an application.
        return super().matches(url) and "/jobs/" in (url or "")

    def application_url(self, url: str) -> str:
        """The posting page, from any of the URLs LinkedIn uses for one job.

        Search results link to /jobs/search/?currentJobId=<id> or
        /jobs/collections/...?currentJobId=<id>; the Easy Apply button is on
        /jobs/view/<id>/ either way, and opening that page directly keeps the
        search list's own controls out of the way.
        """
        found = re.search(r"[?&]currentJobId=(\d+)", url or "")
        if found and "linkedin.com" in url:
            return f"https://www.linkedin.com/jobs/view/{found.group(1)}/"
        return url

    def review_problem(self, page: Any, root: str | None) -> str | None:
        """An answer shown as LinkedIn's own id ("urn:li:geo:103033862") where
        its name should be: sent, the employer would read the id."""
        try:
            text = str(page.evaluate(_ROOT_TEXT, root or "body") or "")
        except Exception:
            return None
        found = re.search(r"urn:li:[a-z_]+:\S+", text, re.IGNORECASE)
        if not found:
            return None
        return (
            f"the review shows {found.group(0)!r} where an answer should be; "
            "not sent. Check the screenshot"
        )

    def discover_step(self, page: Any, root: str | None) -> list[FormField]:
        """The step's fields; on the resume step with no file box, the
        "Upload resume" button as the resume's upload."""
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
        if field.kind == "select" and not field.options and isinstance(fill.value, str):
            return _typeahead(page, field, fill.value)
        if field.key != UPLOAD_KEY:
            return fill_field(page, field, fill)
        if not fill.file_path:
            return "no resume file to upload"
        name = Path(fill.file_path).name
        try:
            with page.expect_file_chooser(timeout=8000) as chooser:
                page.locator(field.selector).first.click(timeout=5000)
            chooser.value.set_files(fill.file_path)
        except Exception as exc:
            return f"the Upload resume button gave no file chooser: {type(exc).__name__}"
        for _ in range(30):  # the upload shows within seconds
            page.wait_for_timeout(500)
            try:
                if page.evaluate(_RESUME_SHOWN_JS, [ROOT, name]):
                    return None
            except Exception:
                continue
        return "LinkedIn never showed the uploaded resume"

    def discard(self, page: Any) -> None:
        """Close the modal and discard the draft, so nothing half-filled stays saved."""
        if not click_first_visible(
            page, ("button[aria-label='Dismiss']", ".artdeco-modal__dismiss")
        ):
            return
        wait_settled(page, 2_000)
        click_first_visible(
            page,
            (
                "button[data-control-name='discard_application_confirm_btn']",
                "dialog[open] button:has-text('Discard')",
                "button[data-test-dialog-secondary-btn]:has-text('Discard')",
                "button:has-text('Discard')",
            ),
        )


def _typeahead(page: Any, field: FormField, value: str) -> str | None:
    """A LinkedIn typeahead ("Location (city)"): type the first part of the
    value, then press the suggestion that matches it the way a person would.
    A click the page does not take as a real one leaves the place's id in the
    box ("urn:li:geo:..."), which then goes out as the answer; a box that ends
    up holding one, or nothing, is emptied and reported."""
    box = page.locator(field.selector).first
    typed = value.split(",")[0].strip() or value
    try:
        box.click(timeout=3000)
        box.fill("", timeout=3000)
        box.press_sequentially(typed, delay=60, timeout=10_000)
        options = page.locator("[role='option']")
        for _ in range(12):
            page.wait_for_timeout(250)
            if options.count() and options.first.is_visible():
                break
        texts = [t.strip() for t in options.all_inner_texts()]
        want, start = _plain(value), _plain(typed)
        pick = next((i for i, t in enumerate(texts) if _plain(t) == want), None)
        if pick is None:
            pick = next((i for i, t in enumerate(texts) if _plain(t).startswith(start)), None)
        if pick is not None:
            options.nth(pick).click(timeout=3000)
        else:
            box.press("ArrowDown")
            box.press("Enter")
        page.wait_for_timeout(300)
        got = (box.input_value(timeout=2000) or "").strip()
    except Exception as exc:
        first = str(exc).splitlines()[0][:150] if str(exc) else ""
        got, error = "", f"{type(exc).__name__}: {first}"
    else:
        error = None
    if got and not got.lower().startswith("urn:") and _plain(typed) in _plain(got):
        return None
    try:
        box.fill("", timeout=2000)
    except Exception:
        pass
    return error or f"LinkedIn did not take {value!r} from its list (the box showed {got!r})"


def _plain(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()
