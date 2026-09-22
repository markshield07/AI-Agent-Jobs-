"""One pass over the mailbox.

Fetch, match, read, append. The order matters and so does what each step is
allowed to do:

- A mail already in the inbox log is skipped before anything else, so polling
  the same window twice appends nothing twice.
- A mail that matches no application is logged unmatched and left for the user.
  Filing it against the nearest application would put a rejection on the wrong
  job, and nothing here is worth that.
- Every matched mail appends an `email` event, whatever it says, because
  time-to-first-reply is measured from those.
- A status change is appended only when the reading is confident and the status
  actually moves the application forward. `rejected` and `withdrawn` end it from
  anywhere; nothing moves an application that has already ended.

Nothing is ever updated in place: the event log is append-only and the status
view derives the rest.
"""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from jobagent.apply import store as applications
from jobagent.config import Settings
from jobagent.llm.backend import Completer, LLMUnavailable, resolve_backend

from . import store
from .classify import read_message
from .mailbox import InboxNotConfigured, Mailbox, MailboxError, default_since, open_mailbox
from .match import candidates, match_message
from .models import STATUS_RANK, InboxMessage, PollReport, Reading, is_terminal

log = logging.getLogger(__name__)


def should_advance(current: str | None, to_status: str) -> bool:
    """Whether a reply saying `to_status` moves an application that is `current`."""
    if current == to_status:
        return False
    if is_terminal(current):
        return False  # a decision has already been recorded; later mail is a note
    if is_terminal(to_status):
        return True
    return STATUS_RANK.get(to_status, -1) > STATUS_RANK.get(current or "", -1)


def _completer(settings: Settings, given: Completer | None, notes: list[str]) -> Completer | None:
    if given is not None or not settings.inbox_model_triage:
        return given
    try:
        return resolve_backend(settings)
    except LLMUnavailable as exc:
        notes.append(f"rules only, no model to read unclear replies: {exc}")
        return None


def _current_status(conn: sqlite3.Connection, application_id: int) -> str | None:
    row = conn.execute(
        "SELECT status FROM application_status WHERE application_id = ?", (application_id,)
    ).fetchone()
    return row["status"] if row else None


def record_reply(
    conn: sqlite3.Connection,
    application_id: int,
    message: InboxMessage,
    reading: Reading,
    *,
    min_confidence: float = 0.6,
) -> tuple[int, int | None]:
    """Append the mail and, when it says so clearly enough, the status change.

    Returns the email event's id and the status event's id, the second None
    when nothing moved.
    """
    note = f"{message.from_name or message.from_addr}: {message.subject}".strip()
    email_event = applications.add_event(
        conn, application_id, kind="email", note=note, source="email"
    )
    status = reading.status
    if status is None or reading.confidence < min_confidence:
        return email_event, None
    current = _current_status(conn, application_id)
    if not should_advance(current, status):
        return email_event, None
    status_event = applications.add_event(
        conn,
        application_id,
        kind="status_change",
        to_status=status,
        note=f"{message.subject} ({reading.reason})".strip(),
        source="email",
    )
    return email_event, status_event


def poll_inbox(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    mailbox: Mailbox | None = None,
    completer: Completer | None = None,
    since: str | None = None,
    limit: int | None = None,
    write: bool = True,
) -> PollReport:
    """Read the mailbox once and record what it says.

    `write=False` reads and classifies everything and touches no table, which
    is what `--dry-run` uses to show what a first poll would do.
    """
    report = PollReport()
    try:
        box = mailbox or open_mailbox(settings)
    except InboxNotConfigured as exc:
        report.error = str(exc)
        return report

    start = since or store.latest_received(conn) or default_since(settings.inbox_lookback_days)
    try:
        messages = box.fetch(since=start, limit=limit or settings.inbox_poll_limit)
    except MailboxError as exc:
        report.error = str(exc)
        return report

    report.fetched = len(messages)
    seen = store.seen_ids(conn)
    reader = _completer(settings, completer, report.notes)
    pool = candidates(conn)

    for message in messages:
        if message.message_id in seen:
            report.skipped += 1
            continue
        seen.add(message.message_id)

        match = match_message(message, pool)
        if match is None:
            report.unmatched += 1
            if write:
                store.record(conn, message)
            report.results.append(
                {"message": message.as_dict(), "match": None, "reading": None, "advanced": False}
            )
            continue

        reading = read_message(
            message, completer=reader, min_confidence=settings.inbox_min_confidence
        )
        report.matched += 1
        status_event: int | None = None
        if write:
            _, status_event = record_reply(
                conn,
                match.application_id,
                message,
                reading,
                min_confidence=settings.inbox_min_confidence,
            )
            store.record(conn, message, match=match, reading=reading, event_id=status_event)
        elif reading.status and should_advance(
            _current_status(conn, match.application_id), reading.status
        ):
            status_event = -1  # a dry run: what would have moved
        if status_event is not None:
            report.advanced += 1
        report.results.append(
            {
                "message": message.as_dict(),
                "match": match.as_dict(),
                "reading": reading.as_dict(),
                "advanced": status_event is not None,
            }
        )

    log.info(report.summary())
    return report


def attach_message(
    conn: sqlite3.Connection,
    row_id: int,
    application_id: int,
    *,
    to_status: str | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """File an unmatched message by hand, and move the status if asked.

    The mail's own reading is honoured when no status is given, so attaching a
    rejection the matcher could not place records the rejection too.
    """
    row = store.get(conn, row_id)
    if row is None:
        raise ValueError(f"no message {row_id}")
    attached = store.attach(conn, row_id, application_id)
    assert attached is not None

    message = InboxMessage(
        message_id=row["message_id"],
        subject=row["subject"] or "",
        from_addr=row["from_addr"] or "",
        from_name=row["from_name"] or "",
        received_at=row["received_at"],
        body=row["excerpt"] or "",
    )
    reading = Reading(
        label=to_status or row["label"],  # type: ignore[arg-type]
        confidence=1.0,
        reason="attached by hand",
        source="manual",
    )
    minimum = settings.inbox_min_confidence if settings else 0.6
    _, status_event = record_reply(conn, application_id, message, reading, min_confidence=minimum)
    if status_event is not None:
        store.set_event(conn, row_id, status_event)
    return {
        "message": store.get(conn, row_id),
        "application": applications.get_application(conn, application_id),
        "advanced": status_event is not None,
    }
