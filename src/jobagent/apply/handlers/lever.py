"""Lever: the posting page and the form behind `/apply`, with a hidden submit
button in front of the real one.

The posting lives at jobs.lever.co/<company>/<posting> and the form at the
same URL with `/apply` on the end, which every reference repo appends rather
than hunting for the link (job-agent E3, AutoApply A3). Two things on the
page would otherwise go wrong. Lever renders a hidden `button[type=submit]`
before the fields, so a plain "first submit button" press hits nothing;
`click_first_visible` passes over it and the visible `.postings-btn` is
pressed instead. And the cover letter belongs in the "Additional
information" textarea (`textarea[name="comments"]`), which says nothing about
a cover letter; the handler marks it so the letter is what goes in it
(job-pilot B3, job-agent E3).

Attested by the reference study, sections A3 (AutoApply), B3 (job-pilot), E3
(job-agent) and the cross-repo summary: `/apply` appended, the field names
`name`, `email`, `phone`, `org`, `location`, `urls[LinkedIn]`,
`urls[GitHub]`, `urls[Portfolio]`, `textarea[name="comments"]`, custom
questions in `.application-question` blocks named `cards[<uuid>][fieldN]`,
the submit chain `button[type="submit"].template-btn-submit` and
`.postings-btn`, the hidden decoy submit, and errors in
`.application-error`. This module's own assumptions are marked where they
occur.
"""

from __future__ import annotations

import logging
import re
from typing import Any
from urllib.parse import urlparse, urlunparse

from jobagent.apply.handlers.base import BaseHandler
from jobagent.apply.models import Answerer, FillPlan, FormField, HandlerResult, Packet

log = logging.getLogger(__name__)

APPLY_SUFFIX = "/apply"
# "Additional information", "Anything else", a box named `comments`: where a
# Lever form takes a cover letter, since it has no field that says so
# (job-pilot B3 L199-201, job-agent E3).
_LETTER_BOX = re.compile(
    r"additional\s+information|anything\s+else|cover\s*letter|^comments$", re.IGNORECASE
)
# A posting URL is /<company>/<posting-id>; anything longer is already a
# sub-page (/apply, /thanks) and is left alone.
_POSTING_PATH = re.compile(r"^/[\w.-]+/[\w-]+/?$")


def apply_url_for(url: str) -> str:
    """The posting URL with `/apply` on the end, when it is a Lever posting.

    A company careers page that fronts Lever keeps its own URL shape, so the
    suffix is only ever added on a lever.co host.
    """
    parts = urlparse(url or "")
    host = (parts.netloc or "").lower()
    if host != "lever.co" and not host.endswith(".lever.co"):
        return url
    path = parts.path or ""
    if not _POSTING_PATH.match(path):
        return url
    return urlunparse(parts._replace(path=path.rstrip("/") + APPLY_SUFFIX))


def letter_aware(answerer: Answerer) -> Answerer:
    """Plan the "Additional information" box as the cover letter.

    The box is an ordinary textarea with a label that says nothing about a
    letter, so the planner would treat it as an open question and spend a
    model call writing one. The letter is already written. The fields are put
    back before they reach the filler, so a needed-input report keeps the
    field's own section.
    """

    def plan(fields: list[FormField]) -> FillPlan:
        moved = [
            f
            for f in fields
            if f.kind == "textarea"
            and f.section not in ("cover_letter", "resume")
            and (_LETTER_BOX.search(f.label or "") or _LETTER_BOX.search(f.name or ""))
        ]
        for field in moved:
            field.section = "cover_letter"
        try:
            return answerer(fields)
        finally:
            for field in moved:
                field.section = "questions"

    return plan


class LeverHandler(BaseHandler):
    ats = "lever"
    # Assumption: jobs.eu.lever.co mirrors jobs.lever.co for EU-hosted
    # accounts. The study covers jobs.lever.co and hire.lever.co only.
    hosts = ("jobs.lever.co", "hire.lever.co", "jobs.eu.lever.co", "lever.co")
    # Only needed when the posting page was opened instead of the form: the
    # posting's own apply link (A3, E3).
    apply_button_selectors = (
        'a.postings-btn[href*="/apply"]',
        'a[href$="/apply"]',
        'a:has-text("Apply for this job")',
        'a:has-text("Apply")',
    )
    # `form.application-form` is the form itself; the name box is on every
    # Lever form. The resume input is hidden behind its own label, so it is
    # no use as a readiness signal.
    form_ready_selectors = (
        "form.application-form",
        'input[name="name"]',
        'input[name="email"]',
    )
    # The visible button first. `click_first_visible` skips the hidden decoy
    # either way, but naming Lever's own classes keeps the press off any
    # other submit button the page may carry.
    submit_selectors = (
        "button.postings-btn.template-btn-submit",
        'button[type="submit"].template-btn-submit',
        ".postings-btn[type='submit']",
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Submit application")',
    )

    def application_url(self, url: str) -> str:
        return apply_url_for(url)

    def apply(
        self,
        page: Any,
        packet: Packet,
        answerer: Answerer,
        *,
        submit: bool,
        screenshot_path: str | None = None,
    ) -> HandlerResult:
        return super().apply(
            page, packet, letter_aware(answerer), submit=submit, screenshot_path=screenshot_path
        )
