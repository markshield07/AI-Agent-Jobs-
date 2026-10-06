"""The security code a board emails before it accepts an application.

Greenhouse, on some boards, answers the Submit button with a request for a
code it has just sent to the applicant ("Security code for your application
to Anthropic": "Copy and paste this code into the security code field on your
application: hWze7RPl"). lordvacuum/job-agent reads it from the mailbox when it
has one; so does this. The mailbox is the one the inbox poller already reads,
opened read-only, and the code is used once, on the page that asked for it.

A code is taken only from a mail that was not in the inbox when the button
was pressed, arrived after it (less a minute of clock difference), names the
company when its subject names one, and has not been used before: the used
ones are kept in the data folder, so applications to the same company one
after another, each its own command, each get their own. When several new
mails qualify, the one naming the job's title wins, else the first to arrive.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
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
    messages: Iterable[InboxMessage],
    *,
    company: str = "",
    after: datetime,
    skip: Iterable[str] = (),
    title: str = "",
) -> tuple[str, str] | None:
    """(code, message id) from the code mail that answers a press at `after`.

    Mails in `skip` (already used, or already there before the press) never
    count. Of the rest, one naming the job's title wins; otherwise the first
    to arrive, since the press that asked for it came first.
    """
    since = after - CLOCK_SKEW
    skipped = set(skip) | _USED
    found: list[tuple[bool, datetime, str, str]] = []
    for message in messages:
        if message.message_id in skipped:
            continue
        when = _received(message)
        if when is None or when < since:
            continue
        code = code_in(message, company)
        if code:
            named = bool(title) and title.strip().lower() in message.body.lower()
            found.append((not named, when, code, message.message_id))
    if not found:
        return None
    _, _, code, message_id = min(found)
    return code, message_id


def code_mail_ids(messages: Iterable[InboxMessage], company: str = "") -> set[str]:
    """The ids of the code mails in `messages`."""
    return {m.message_id for m in messages if code_in(m, company)}


def wait_for_code(
    mailbox: Mailbox,
    *,
    company: str = "",
    after: datetime,
    skip: Iterable[str] = (),
    title: str = "",
    timeout_s: float = 240,
    interval_s: float = 10,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str, str] | None:
    """Poll the mailbox until the code arrives: (code, message id), or None
    after `timeout_s`."""
    deadline = clock() + timeout_s
    skip = set(skip)
    while True:
        try:
            found = find_code(
                mailbox.fetch(since=_imap_since(after), limit=30),
                company=company,
                after=after,
                skip=skip,
                title=title,
            )
        except MailboxError as exc:
            log.warning("could not read the mailbox for a security code: %s", exc)
            found = None
        if found:
            _USED.add(found[1])
            return found
        if clock() >= deadline:
            return None
        sleep(interval_s)


def _imap_since(after: datetime) -> str:
    # IMAP's SINCE counts whole days on the server's calendar: the day before
    # covers a submit just after midnight.
    return (after - timedelta(days=1)).date().isoformat()


class EmailCodes:
    """The emailed code for one application, from the configured mailbox.

    `mark()`, just before the button is pressed, notes the code mails already
    in the inbox; none of them is this press's. A code once handed out is
    written to `used_path`, so the next application, in this run or the next
    command, never takes it. Called again after a code was refused, it waits
    for the next new one.
    """

    def __init__(
        self,
        mailbox: Mailbox,
        *,
        company: str = "",
        title: str = "",
        used_path: Path | None = None,
        timeout_s: float = 240,
        interval_s: float = 10,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.mailbox = mailbox
        self.company = company
        self.title = title
        self.used_path = used_path
        self.timeout_s = timeout_s
        self.interval_s = interval_s
        self.sleep = sleep
        self.clock = clock
        self.before: set[str] = set()

    def mark(self) -> None:
        try:
            now = datetime.now(UTC)
            self.before = code_mail_ids(
                self.mailbox.fetch(since=_imap_since(now), limit=30), self.company
            )
        except MailboxError as exc:
            log.warning("could not read the mailbox before the press: %s", exc)

    def __call__(self, after: datetime) -> str | None:
        found = wait_for_code(
            self.mailbox,
            company=self.company,
            after=after,
            skip=self.before | self._used(),
            title=self.title,
            timeout_s=self.timeout_s,
            interval_s=self.interval_s,
            sleep=self.sleep,
            clock=self.clock,
        )
        if not found:
            return None
        self._remember(found[1])
        return found[0]

    def _used(self) -> set[str]:
        if self.used_path is None:
            return set()
        try:
            return set(json.loads(self.used_path.read_text()))
        except (OSError, ValueError, TypeError):
            return set()

    def _remember(self, message_id: str) -> None:
        if self.used_path is None:
            return
        kept = [i for i in self._used() if i != message_id][-199:] + [message_id]
        try:
            self.used_path.parent.mkdir(parents=True, exist_ok=True)
            self.used_path.write_text(json.dumps(kept))
        except OSError as exc:
            log.warning("could not record the used code: %s", exc)


def email_code_source(settings: Settings, company: str = "", title: str = "") -> EmailCodes | None:
    """The emailed code for an application to `company`, or None when no
    mailbox is set up."""
    try:
        mailbox = open_mailbox(settings)
    except InboxNotConfigured:
        return None
    return EmailCodes(
        mailbox,
        company=company,
        title=title,
        used_path=settings.data_dir / "security-codes-used.json",
        timeout_s=settings.security_code_wait_s,
    )
