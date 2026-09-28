"""The inbox log.

Every mail the poller reads gets a row here, matched or not, and that row is
what makes a second poll over the same window cheap and safe: `message_id` is
unique, so a mail already recorded is skipped before anything is classified or
appended. Unmatched mail is kept too, because the user attaching it by hand is
the correction path when matching abstains.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from jobagent.db.database import transaction, utcnow

from .models import InboxMessage, Match, Reading

READERS = ("rules", "model", "none", "manual")


def already_seen(conn: sqlite3.Connection, message_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM inbox_messages WHERE message_id = ?", (message_id,)
    ).fetchone()
    return row is not None


def seen_ids(conn: sqlite3.Connection) -> set[str]:
    """Every message id on file. One query beats one per message."""
    return {r["message_id"] for r in conn.execute("SELECT message_id FROM inbox_messages")}


def record(
    conn: sqlite3.Connection,
    message: InboxMessage,
    *,
    match: Match | None = None,
    reading: Reading | None = None,
    event_id: int | None = None,
) -> int:
    """Log one mail. Returns the row id; a mail already logged keeps its row."""
    with transaction(conn):
        cur = conn.execute(
            """INSERT OR IGNORE INTO inbox_messages
                   (message_id, application_id, subject, from_addr, from_name, received_at,
                    excerpt, label, confidence, reason, read_by, match_confidence,
                    match_reason, event_id, seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                message.message_id,
                match.application_id if match else None,
                message.subject,
                message.from_addr,
                message.from_name,
                message.received_at,
                message.excerpt(),
                reading.label if reading else "unknown",
                reading.confidence if reading else 0.0,
                reading.reason if reading else None,
                reading.source if reading else None,
                match.confidence if match else None,
                match.reason if match else None,
                event_id,
                utcnow(),
            ),
        )
        if cur.lastrowid and cur.rowcount:
            return int(cur.lastrowid)
    row = conn.execute(
        "SELECT id FROM inbox_messages WHERE message_id = ?", (message.message_id,)
    ).fetchone()
    return int(row["id"])


def set_event(conn: sqlite3.Connection, row_id: int, event_id: int) -> None:
    with transaction(conn):
        conn.execute("UPDATE inbox_messages SET event_id = ? WHERE id = ?", (event_id, row_id))


def attach(
    conn: sqlite3.Connection,
    row_id: int,
    application_id: int,
    *,
    label: str | None = None,
) -> dict[str, Any] | None:
    """File a message against an application by hand, which matching could not."""
    exists = conn.execute("SELECT 1 FROM applications WHERE id = ?", (application_id,)).fetchone()
    if exists is None:
        raise ValueError(f"no application {application_id}")
    with transaction(conn):
        cur = conn.execute(
            """UPDATE inbox_messages
                   SET application_id = ?,
                       label = COALESCE(?, label),
                       read_by = 'manual',
                       match_confidence = 1.0,
                       match_reason = 'attached by hand'
               WHERE id = ?""",
            (application_id, label, row_id),
        )
        if not cur.rowcount:
            return None
    return get(conn, row_id)


def get(conn: sqlite3.Connection, row_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM inbox_messages WHERE id = ?", (row_id,)).fetchone()
    return dict(row) if row else None


def list_messages(
    conn: sqlite3.Connection,
    *,
    application_id: int | None = None,
    matched: bool | None = None,
    label: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """The log, newest first."""
    where, params = [], []
    if application_id is not None:
        where.append("application_id = ?")
        params.append(application_id)
    elif matched is True:
        where.append("application_id IS NOT NULL")
    elif matched is False:
        where.append("application_id IS NULL")
    if label:
        where.append("label = ?")
        params.append(label)
    clause = ("WHERE " + " AND ".join(where) + " ") if where else ""
    rows = conn.execute(
        f"SELECT * FROM inbox_messages {clause}ORDER BY received_at DESC, id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [dict(r) for r in rows]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many messages per label, plus how many are still unattached."""
    found = {
        r["label"]: r["n"]
        for r in conn.execute("SELECT label, COUNT(*) AS n FROM inbox_messages GROUP BY label")
    }
    unmatched = conn.execute(
        "SELECT COUNT(*) AS n FROM inbox_messages WHERE application_id IS NULL"
    ).fetchone()
    found["unmatched"] = int(unmatched["n"])
    return found


def latest_received(conn: sqlite3.Connection) -> str | None:
    """When the newest logged mail arrived: where the next poll can start."""
    row = conn.execute("SELECT MAX(received_at) AS at FROM inbox_messages").fetchone()
    return row["at"] if row and row["at"] else None
