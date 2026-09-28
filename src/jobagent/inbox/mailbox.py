"""Reading the mailbox over IMAP, and turning raw mail into `InboxMessage`.

Parsing is a free function over bytes, so every test in this package works on
fixture mail and nothing but `ImapMailbox` needs a server. The credentials come
from the environment or `.env` and are never written to the database: what the
poller keeps is the subject, the sender and an excerpt, which is what the
dashboard shows.

The agent only ever reads. Nothing here deletes, moves or marks mail, and the
messages are fetched with BODY.PEEK so an unread mail stays unread.
"""

from __future__ import annotations

import email
import email.policy
import hashlib
import imaplib
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from typing import TYPE_CHECKING, Protocol

from .models import InboxMessage

if TYPE_CHECKING:
    from jobagent.config import Settings

log = logging.getLogger(__name__)

MAX_BODY_CHARS = 20_000


class MailboxError(RuntimeError):
    """The mailbox could not be read."""


class InboxNotConfigured(MailboxError):
    """No IMAP host, user or password is set."""


class Mailbox(Protocol):
    name: str

    def fetch(self, *, since: str | None = None, limit: int = 200) -> list[InboxMessage]: ...


# ---------------------------------------------------------------- parsing --


def _text_of(message: EmailMessage) -> str:
    """The plain-text body, falling back to the HTML part with its tags taken off."""
    body = message.get_body(preferencelist=("plain",))
    if body is not None:
        return str(body.get_content())

    html = message.get_body(preferencelist=("html",))
    if html is None:
        return ""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(str(html.get_content()), "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text(" ")


def _received_at(message: EmailMessage) -> str:
    raw = message.get("Date")
    if raw:
        try:
            parsed = parsedate_to_datetime(raw)
        except (TypeError, ValueError):
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed.astimezone(UTC).isoformat(timespec="seconds")
    return datetime.now(UTC).isoformat(timespec="seconds")


def _header(message: EmailMessage, name: str) -> str:
    value = message.get(name, "")
    return " ".join(str(value).split())


def parse_message(raw: bytes, *, folder: str = "INBOX") -> InboxMessage:
    """One raw RFC 822 mail, flattened into what the rest of the package reads."""
    message = email.message_from_bytes(raw, policy=email.policy.default)
    name, addr = parseaddr(_header(message, "From"))
    body = " ".join(_text_of(message).split("\n"))
    message_id = _header(message, "Message-ID").strip("<>")
    if not message_id:
        # Some senders omit it. A digest of what identifies the mail keeps the
        # poller idempotent anyway.
        seed = f"{_header(message, 'Date')}|{addr}|{_header(message, 'Subject')}"
        message_id = "sha:" + hashlib.sha256(seed.encode()).hexdigest()[:32]

    return InboxMessage(
        message_id=message_id,
        subject=_header(message, "Subject"),
        from_addr=addr.lower(),
        from_name=name,
        to_addr=parseaddr(_header(message, "To"))[1].lower(),
        received_at=_received_at(message),
        body=body[:MAX_BODY_CHARS],
        folder=folder,
    )


# ------------------------------------------------------------------- IMAP --


def _imap_date(since: str) -> str:
    """`SINCE` wants 01-Jan-2026, whatever shape the caller had."""
    text = since.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).strftime("%d-%b-%Y")
    except ValueError as exc:
        raise MailboxError(f"cannot read {since!r} as a date") from exc


_UID_SPLIT = re.compile(rb"\s+")


@dataclass(slots=True)
class ImapMailbox:
    """A mailbox on an IMAP server, read-only."""

    host: str
    user: str
    password: str
    port: int = 993
    folder: str = "INBOX"
    ssl: bool = True
    name: str = "imap"

    def _connect(self) -> imaplib.IMAP4:
        maker = imaplib.IMAP4_SSL if self.ssl else imaplib.IMAP4
        try:
            conn = maker(self.host, self.port)
            conn.login(self.user, self.password)
        except (OSError, imaplib.IMAP4.error) as exc:
            raise MailboxError(f"{self.host}: {exc}") from exc
        return conn

    def fetch(self, *, since: str | None = None, limit: int = 200) -> list[InboxMessage]:
        """The newest `limit` messages received since `since`, oldest first."""
        conn = self._connect()
        try:
            status, _ = conn.select(self.folder, readonly=True)
            if status != "OK":
                raise MailboxError(f"cannot open folder {self.folder!r}")
            criteria = ["ALL"] if since is None else ["SINCE", _imap_date(since)]
            status, data = conn.uid("SEARCH", None, *criteria)  # type: ignore[arg-type]
            if status != "OK":
                raise MailboxError(f"search failed on {self.folder!r}")
            uids = _UID_SPLIT.split(data[0].strip()) if data and data[0] else []
            uids = [u for u in uids if u][-limit:]

            messages: list[InboxMessage] = []
            for uid in uids:
                status, payload = conn.uid("FETCH", uid.decode(), "(BODY.PEEK[])")
                if status != "OK" or not payload or not isinstance(payload[0], tuple):
                    log.warning("could not fetch message %s", uid.decode())
                    continue
                try:
                    messages.append(parse_message(payload[0][1], folder=self.folder))
                except Exception:  # a single unreadable mail must not end the poll
                    log.exception("could not parse message %s", uid.decode())
            return messages
        except imaplib.IMAP4.error as exc:
            raise MailboxError(str(exc)) from exc
        finally:
            try:
                conn.logout()
            except (OSError, imaplib.IMAP4.error):
                pass


def open_mailbox(settings: Settings) -> Mailbox:
    """The configured mailbox, or `InboxNotConfigured` naming what is missing."""
    missing = [
        name
        for name in ("imap_host", "imap_user", "imap_password")
        if not getattr(settings, name, None)
    ]
    if missing:
        raise InboxNotConfigured(
            "Set " + ", ".join(f"JOBAGENT_{m.upper()}" for m in missing) + " to read replies."
        )
    return ImapMailbox(
        host=settings.imap_host or "",
        user=settings.imap_user or "",
        password=settings.imap_password or "",
        port=settings.imap_port,
        folder=settings.imap_folder,
        ssl=settings.imap_ssl,
    )


def default_since(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
