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
from typing import Any

from jobagent.apply.browser.dom import discover_fields
from jobagent.apply.handlers.base import BaseHandler
from jobagent.apply.models import FormField

log = logging.getLogger(__name__)

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
    apply_button_selectors = (
        'a:has-text("Apply for this Job")',
        'button:has-text("Apply for this Job")',
        'a:has-text("Apply Now")',
        'button:has-text("Apply Now")',
        'a:has-text("Apply")',
        'button:has-text("Apply")',
        '[data-testid="apply-button"]',
    )
    # A site search is a text input in a form, so a bare text box does not
    # count as an application form: the apply button would never be pressed.
    form_ready_selectors = (
        'input[type="file"]',
        'input[type="email"]',
        "form textarea",
    )

    def matches(self, url: str) -> bool:
        """Never claims a URL: it is what `handler_for` falls back to, so a
        site with a handler of its own always gets that one."""
        return False

    def discover(self, page: Any) -> list[FormField]:
        fields = discover_fields(page)
        if not fields or looks_like_an_application(fields):
            return fields
        log.info("generic: %s has a form, but not an application", page.url)
        return []

    def apply(self, page: Any, *args: Any, **kwargs: Any) -> Any:
        result = super().apply(page, *args, **kwargs)
        if result.outcome == "failed" and result.error == "no application form found":
            result.error = NOT_AN_APPLICATION if _has_form(page) else result.error
        return result


def looks_like_an_application(fields: list[FormField]) -> bool:
    """True when the form asks for what an application asks for."""
    if any(f.kind == "file" for f in fields):
        return True
    wanted = {f.section for f in fields if f.section in _APPLICATION_SECTIONS}
    if len(wanted) >= 2:
        return True
    return len([f for f in fields if f.section in _APPLICATION_SECTIONS]) >= MIN_APPLICATION_FIELDS


def _has_form(page: Any) -> bool:
    try:
        return page.locator("form").count() > 0
    except Exception:
        return False
