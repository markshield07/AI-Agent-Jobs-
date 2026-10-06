"""The security code a board emails before it accepts an application.

Greenhouse, on some boards, answers the Submit button with a request for a
code it has just sent to the applicant ("Security code for your application
to Anthropic": "Copy and paste this code into the security code field on your
application: hWze7RPl"). lordvacuum/job-agent reads it from the mailbox when it
has one; so does this. The mailbox is the one the inbox poller already reads,
opened read-only, and the code is used once, on the page that asked for it.

A code is taken only from a mail that arrived after the button was pressed
(less a minute of clock difference), names the company when its subject names
one, and has not been used already in this run, so three applications to the
same company one after another each get their own.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from .mailbox import InboxNotConfigured, Mailbox, MailboxError, open_mailbox
from .models import InboxMessage

if TYPE_CHECKING:
    from jobagent.config import Settings

log = logging.getLogger(__name__)

CodeSource = Callable[[datetime], "str | None"]

_SUBJECT = re.compile(r"\b(?:security|verification|confirmation)\s+code\b", re.I)
_APPLICATION_TO = re.compile(r"\bapplication\s+(?:to|at|with)\s+(.+?)\s*$", re.I)
# "...the security code field on your application: hWze7RPl", "Your code is 482913".
_CODE = re.compile(
    r"\bcode\b[^:]{0,80}?(?::|\bis\b)\s*([A-Za-z0-9]{4,12})\b",
    re.I,
)
CLOCK_SKEW = timedelta(seconds=60)
# Codes handed out in this process, by message id: one mail, one application.
_USED: set[str] = set()


def _looks_like_code(token: str) -> bool:
    """`hWze7RPl` or `482913`, not `Thanks` or `please`."""
    if any(c.isdigit() for c in token):
        return True
    if token.isupper() and len(token) >= 6:
        return True
    return any(c.isupper() for c in token[1:])


def _same_company(company: str, named: str) -> bool:
    """ "Anthropic" in the subject for the job at "Anthropic, PBC"."""
    words = re.findall(r"[a-z0-9]+", company.lower())
    return bool(words) and words[0] in re.findall(r"[a-z0-9]+", named.lower())


def code_in(message: InboxMessage, company: str = "") -> str | None:
    """The code in a security-code mail for `company`, or None."""
    if not _SUBJECT.search(message.subject):
        return None
    named = _APPLICATION_TO.search(message.subject)
    if company and named and not _same_company(company, named.group(1)):
        return None
    for match in _CODE.finditer(message.body):
        if _looks_like_code(match.group(1)):
            return match.group(1)
    return None


def _received(message: InboxMessage) -> datetime | None:
    try:
        when = datetime.fromisoformat(message.received_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def find_code(
    messages: Iterable[InboxMessage], *, company: str = "", after: datetime
) -> tuple[str, str] | None:
    """(code, message id) from the newest unused code mail since `after`."""
    since = after - CLOCK_SKEW
    found: list[tuple[datetime, str, str]] = []
    for message in messages:
        if message.message_id in _USED:
            continue
        when = _received(message)
        if when is None or when < since:
            continue
        code = code_in(message, company)
        if code:
            found.append((when, code, message.message_id))
    if not found:
        return None
    _, code, message_id = max(found)
    return code, message_id


def wait_for_code(
    mailbox: Mailbox,
    *,
    company: str = "",
    after: datetime,
    timeout_s: float = 240,
    interval_s: float = 10,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> str | None:
    """Poll the mailbox until the code arrives, or None after `timeout_s`."""
    deadline = clock() + timeout_s
    # IMAP's SINCE counts whole days on the server's calendar: the day before
    # covers a submit just after midnight.
    since = (after - timedelta(days=1)).date().isoformat()
    while True:
        try:
            found = find_code(mailbox.fetch(since=since, limit=30), company=company, after=after)
        except MailboxError as exc:
            log.warning("could not read the mailbox for a security code: %s", exc)
            found = None
        if found:
            code, message_id = found
            _USED.add(message_id)
            return code
        if clock() >= deadline:
            return None
        sleep(interval_s)


def email_code_source(settings: Settings, company: str = "") -> CodeSource | None:
    """A function from the submit time to the emailed code, or None when no
    mailbox is set up."""
    try:
        mailbox = open_mailbox(settings)
    except InboxNotConfigured:
        return None

    def source(after: datetime) -> str | None:
        return wait_for_code(
            mailbox, company=company, after=after, timeout_s=settings.security_code_wait_s
        )

    return source
