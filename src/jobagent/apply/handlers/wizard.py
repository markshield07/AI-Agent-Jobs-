"""The flow LinkedIn Easy Apply and Indeed's own application share: a form in
steps, inside a signed-in account.

    open the posting -> signed in? -> press the site's apply button
    -> for each step: read the fields in the step, keep what the site
       prefilled from the account, plan and fill the rest -> Next / Review
    -> the step with the Submit button: stop (dry run) or press it and read
       the confirmation

Both sites differ from a career-page form in the same ways. The form is not
on the posting; it opens in a modal (LinkedIn) or a new page (Indeed) after a
button that only a signed-in visitor sees. It arrives in steps, three to ten
of them, and a step cannot be left with a required field empty. And the
account has already filled much of it: name, email, phone, sometimes a
resume. What the site prefilled is left alone and recorded as `prefilled`,
since overwriting the person's own details with a guess at the same thing
can only introduce a mismatch.

The step loop follows AutoApply (`bot/apply/linkedin.py`,
`bot/apply/indeed.py`): press Next, Continue or Review until a Submit button
shows, at most a fixed number of steps. job-agent (`linkedin_easy_apply.py`)
adds two rules kept here: a step that did not advance is a stop, never a
loop, and the application counts as sent only when the page says it was.

Every stop that is not a submission closes the form without sending it. On
LinkedIn that means Dismiss, then Discard, so no half-filled draft is left
in the account.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlparse

from jobagent.apply.browser.dom import current_values, discover_fields
from jobagent.apply.browser.fill import (
    click_first_visible,
    detect_captcha,
    page_text,
    wait_settled,
)
from jobagent.apply.handlers.base import COOKIE_BUTTON_SELECTORS, BaseHandler, _brief
from jobagent.apply.models import (
    Answerer,
    Fill,
    FormField,
    HandlerResult,
    NeededInput,
    Packet,
)
from jobagent.apply.sessions import site as site_info

log = logging.getLogger(__name__)

ROOT_ATTR = "data-jobagent-root"
ROOT = f'[{ROOT_ATTR}="1"]'

# Marks the first visible element matching one of the selectors as the root
# the steps are read inside, and clears any earlier mark.
_MARK_ROOT_JS = r"""([selectors, attr]) => {
  document.querySelectorAll('[' + attr + ']').forEach((el) => el.removeAttribute(attr));
  const visible = (el) => { const r = el.getBoundingClientRect(); const st = getComputedStyle(el);
    return r.width > 0 && r.height > 0 && st.visibility !== 'hidden' && st.display !== 'none'; };
  for (const sel of selectors) {
    let found = [];
    try { found = Array.from(document.querySelectorAll(sel)); } catch (e) { continue; }
    const el = found.find(visible);
    if (el) { el.setAttribute(attr, '1'); return true; }
  }
  return false;
}"""
_ROOT_TEXT_JS = (
    "(sel) => { const el = document.querySelector(sel) || document.body;"
    " return (el ? el.innerText : '').slice(0, 4000); }"
)


class WizardHandler(BaseHandler):
    """Subclasses name the site and its selectors; the loop is shared."""

    site: str = ""  # the key in `sessions.SITES`
    # The button on the posting that opens the site's own application.
    easy_apply_selectors: tuple[str, ...] = ()
    # Present on a posting that sends the applicant to the company's site.
    external_apply_selectors: tuple[str, ...] = ()
    # Present when the page is shown to a visitor who is not signed in.
    signed_out_selectors: tuple[str, ...] = ()
    signed_out_url = re.compile(r"/(login|signin|uas/login|authwall|auth)\b", re.IGNORECASE)
    # The element the steps are drawn in, when the page around it has
    # controls of its own. Empty means the whole page.
    root_selectors: tuple[str, ...] = ()
    # Next, Continue, Review: anything that moves to the following step.
    next_selectors: tuple[str, ...] = ()
    # Messages a step shows beside a field it will not accept.
    step_error_selectors: tuple[str, ...] = ()
    # Text on the page after a sent application, and URL fragments that mean the same.
    success_signals: tuple[str, ...] = ()
    success_url: re.Pattern[str] | None = None
    # Hosts that are still the site's own flow after the apply button.
    own_hosts: tuple[str, ...] = ()
    max_steps: int = 12

    # -- what subclasses may override --------------------------------------

    def open_flow(self, page: Any) -> Any:
        """Press the apply button; return the page the steps are on.

        A site that opens its application in a new tab gets that tab back.
        """
        context = _context(page)
        before = len(context.pages) if context is not None else 0
        if not click_first_visible(page, self.easy_apply_selectors):
            return None
        wait_settled(page, self.settle_ms)
        if context is not None and len(context.pages) > before:
            flow = context.pages[-1]
            wait_settled(flow, self.settle_ms)
            return flow
        return page

    def discard(self, page: Any) -> None:
        """Close the form without sending it. The default leaves the page as it is."""

    # -- the shared flow ---------------------------------------------------

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
        click_first_visible(page, COOKIE_BUTTON_SELECTORS)

        if self.signed_out(page):
            return self._result(page, "blocked", screenshot_path, error=self.login_hint())
        flow = self.open_flow(page)
        if flow is None:
            return self._no_button(page, screenshot_path)
        if flow is not page:
            try:
                return self._steps(flow, answerer, submit=submit, screenshot_path=screenshot_path)
            finally:
                _close(flow)
        return self._steps(page, answerer, submit=submit, screenshot_path=screenshot_path)

    def _steps(
        self, page: Any, answerer: Answerer, *, submit: bool, screenshot_path: str | None
    ) -> HandlerResult:
        if self._left_site(page):
            return self._stop(
                page,
                "blocked",
                screenshot_path,
                error=f"the apply button leads to the company's own site ({page.url}); "
                "apply there by hand or add it as a career-page job",
            )
        if self.signed_out(page):
            return self._stop(page, "blocked", screenshot_path, error=self.login_hint())

        filled: list[Fill] = []
        needed: list[NeededInput] = []
        seen: list[FormField] = []
        tokens = {"input_tokens": 0, "output_tokens": 0}

        def common() -> dict[str, Any]:
            return {"filled": list(filled), "fields": list(seen), "needed": list(needed), **tokens}

        for step in range(1, self.max_steps + 1):
            root = self._mark_root(page)
            try:
                fields = discover_fields(page, root=root)
            except Exception as exc:
                return self._stop(
                    page,
                    "failed",
                    screenshot_path,
                    error=f"could not read step {step}: {_brief(exc)}",
                    **common(),
                )
            seen.extend(fields)
            prefilled = current_values(page, fields)
            for field in fields:
                if field.key in prefilled and field.kind != "file":
                    filled.append(
                        Fill(
                            key=field.key,
                            value=prefilled[field.key],
                            source="prefilled",
                            label=field.label,
                        )
                    )
            todo = [f for f in fields if f.key not in prefilled or f.kind == "file"]
            if todo:
                plan = answerer(todo)
                done, unfilled, notes = self.fill(page, todo, plan)
                filled.extend(done)
                needed.extend([*plan.needed, *unfilled])
                tokens["input_tokens"] += plan.input_tokens
                tokens["output_tokens"] += plan.output_tokens
                for note in [*plan.notes, *notes]:
                    log.info("%s: %s", self.ats, note)
            if any(n.required for n in needed):
                return self._stop(page, "needs_input", screenshot_path, **common())

            if self._submit_visible(page, root):
                if not submit:
                    return self._stop(page, "dry_run", screenshot_path, **common())
                return self._send(page, root, screenshot_path, common())

            before = self._signature(page, root)
            if not click_first_visible(page, self._scoped(root, self.next_selectors)):
                captcha = detect_captcha(page)
                error = (
                    f"{captcha} stands in the way at step {step}"
                    if captcha
                    else f"step {step} has neither a Next nor a Submit button"
                )
                return self._stop(page, "blocked", screenshot_path, error=error, **common())
            wait_settled(page, self.settle_ms)
            errors = self._step_errors(page, root)
            if errors:
                return self._stop(
                    page,
                    "blocked",
                    screenshot_path,
                    error=f"step {step} would not accept the answers: {'; '.join(errors[:3])}",
                    **common(),
                )
            if self._signature(page, self._mark_root(page)) == before:
                return self._stop(
                    page,
                    "blocked",
                    screenshot_path,
                    error=f"step {step} did not move on after Next; check the screenshot",
                    **common(),
                )
        return self._stop(
            page,
            "failed",
            screenshot_path,
            error=f"no Submit button after {self.max_steps} steps",
            **common(),
        )

    def _send(
        self, page: Any, root: str | None, screenshot_path: str | None, common: dict[str, Any]
    ) -> HandlerResult:
        before_url, before_text = page.url, page_text(page).lower()
        if not click_first_visible(page, self._scoped(root, self.submit_selectors)):
            return self._stop(page, "failed", screenshot_path, error="no submit button", **common)
        wait_settled(page, self.submit_settle_ms)
        confirmation = self._confirmation(page, before_url, before_text)
        if confirmation:
            return self._result(
                page, "submitted", screenshot_path, confirmation=confirmation, **common
            )
        errors = self._step_errors(page, self._mark_root(page))
        if errors:
            return self._stop(
                page,
                "failed",
                screenshot_path,
                error=f"the form said: {'; '.join(errors[:5])}",
                **common,
            )
        captcha = detect_captcha(page)
        if captcha:
            return self._result(
                page,
                "blocked",
                screenshot_path,
                error=f"{captcha} after the submit button; solve it in a visible browser",
                **common,
            )
        return self._result(
            page,
            "unconfirmed",
            screenshot_path,
            error="Submit was pressed but the page showed neither a confirmation nor an "
            "error; check the screenshot and the site's applied-jobs list before trying again",
            **common,
        )

    # -- checks ------------------------------------------------------------

    def signed_out(self, page: Any) -> bool:
        if self.signed_out_url.search(urlparse(page.url or "").path or ""):
            return True
        return self._any_visible(page, self.signed_out_selectors)

    def login_hint(self) -> str:
        label = site_info(self.site).label
        return (
            f"not signed in to {label}; run `jobagent login {self.site}` on your own "
            "machine to sign in once, then try again"
        )

    def _no_button(self, page: Any, screenshot_path: str | None) -> HandlerResult:
        label = site_info(self.site).label
        if self._any_visible(page, self.external_apply_selectors):
            error = (
                f"this posting applies on the company's own site, not through {label}; "
                "apply there by hand or add it as a career-page job"
            )
        elif not self._has_session(page):
            error = self.login_hint()
        else:
            error = f"no {label} apply button on the posting; it may be closed or already applied"
        return self._result(page, "blocked", screenshot_path, error=error)

    def _has_session(self, page: Any) -> bool:
        from jobagent.apply.sessions import signed_in

        context = _context(page)
        if context is None:
            return False
        try:
            return signed_in(context.cookies(), self.site)
        except Exception:
            return False

    def _left_site(self, page: Any) -> bool:
        host = (urlparse(page.url or "").netloc or "").lower()
        if not host or not self.own_hosts:
            return False
        return not any(host == h or host.endswith("." + h) for h in self.own_hosts)

    def _confirmation(self, page: Any, before_url: str, before_text: str) -> str | None:
        url = page.url or ""
        text = page_text(page)
        lower = text.lower()
        for signal in self.success_signals:
            if signal in lower and signal not in before_text:
                return _sentence(text, signal)
        if self.success_url and self.success_url.search(url):
            if not self.success_url.search(before_url or ""):
                return f"confirmation page {url}"
        return None

    def _submit_visible(self, page: Any, root: str | None) -> bool:
        return self._any_visible(page, self._scoped(root, self.submit_selectors))

    def _step_errors(self, page: Any, root: str | None) -> list[str]:
        out: list[str] = []
        for selector in self._scoped(root, self.step_error_selectors):
            try:
                found = page.locator(selector)
                for index in range(min(found.count(), 8)):
                    item = found.nth(index)
                    if not item.is_visible():
                        continue
                    text = re.sub(r"\s+", " ", item.inner_text()).strip()
                    if text and text not in out:
                        out.append(text[:300])
            except Exception:
                continue
        return out

    # -- helpers -----------------------------------------------------------

    def _mark_root(self, page: Any) -> str | None:
        if not self.root_selectors:
            return None
        try:
            found = page.evaluate(_MARK_ROOT_JS, [list(self.root_selectors), ROOT_ATTR])
        except Exception as exc:
            log.debug("could not mark the form root: %s", exc)
            return None
        return ROOT if found else None

    @staticmethod
    def _scoped(root: str | None, selectors: Sequence[str]) -> list[str]:
        if not root:
            return list(selectors)
        return [f"{root} {s}" for s in selectors]

    @staticmethod
    def _signature(page: Any, root: str | None) -> str:
        """What the step looks like, to tell whether Next moved anything."""
        try:
            text = str(page.evaluate(_ROOT_TEXT_JS, root or "body") or "")
        except Exception:
            text = ""
        # Values typed into a step do not show in innerText, so the text of
        # the step before and after a Next that did nothing is the same.
        return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()

    def _stop(
        self, page: Any, outcome: str, screenshot_path: str | None, **kwargs: Any
    ) -> HandlerResult:
        """The result, with its screenshot of the form as it was, then the form closed unsent."""
        result = self._result(page, outcome, screenshot_path, **kwargs)
        try:
            self.discard(page)
        except Exception as exc:
            log.debug("could not close the %s form: %s", self.ats, exc)
        return result


def _context(page: Any) -> Any:
    try:
        return page.context
    except Exception:
        return None


def _close(page: Any) -> None:
    try:
        page.close()
    except Exception:
        pass


def _sentence(text: str, signal: str) -> str:
    index = text.lower().find(signal)
    if index < 0:
        return signal
    start = max(text.rfind(".", 0, index), text.rfind("\n", 0, index)) + 1
    ends = [i for i in (text.find(".", index), text.find("!", index), text.find("\n", index))]
    ends = [i for i in ends if i > 0]
    end = min(ends) if ends else min(len(text), index + 160)
    return text[start:end].strip()[:200]
