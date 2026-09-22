"""Applications and attempts on disk.

One `applications` row per job, however many times the form was tried; one
`submission_attempts` row per try. `submitted_at` is set by the attempt that
saw the confirmation and is never cleared, and `application_events` stays
append-only: the agent adds the 'applied' event, phase 5 adds the replies,
and the status view derives the rest.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from jobagent.db.database import transaction, utcnow
from jobagent.discovery import store as jobs

from .models import MODES, OUTCOMES, HandlerResult, NeededInput

EVENT_STATUSES = ("applied", "screening", "interviewing", "offer", "rejected", "withdrawn")
EVENT_SOURCES = ("manual", "email", "campaign")


def get_or_create_application(
    conn: sqlite3.Connection,
    job_id: str,
    *,
    mode: str,
    variant_id: int | None = None,
    ats: str | None = None,
    cover_letter_path: str | None = None,
) -> int:
    """The application row for `job_id`, made if absent. Newer details overwrite."""
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    with transaction(conn):
        row = conn.execute("SELECT id FROM applications WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            cur = conn.execute(
                """INSERT INTO applications
                       (job_id, variant_id, cover_letter_path, mode, ats, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (job_id, variant_id, cover_letter_path, mode, ats, utcnow()),
            )
            return int(cur.lastrowid)
        conn.execute(
            """UPDATE applications SET
                   mode = ?,
                   variant_id = COALESCE(?, variant_id),
                   ats = COALESCE(?, ats),
                   cover_letter_path = COALESCE(?, cover_letter_path)
               WHERE id = ?""",
            (mode, variant_id, ats, cover_letter_path, row["id"]),
        )
        return int(row["id"])


def application_for_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT id FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    return get_application(conn, int(row["id"])) if row else None


def record_attempt(
    conn: sqlite3.Connection,
    application_id: int,
    result: HandlerResult,
    *,
    mode: str,
    handler: str | None,
    started_at: str,
) -> int:
    """Keep what one try did. A `submitted` result also stamps the application
    and appends its 'applied' event, in the same transaction."""
    if result.outcome not in OUTCOMES:
        raise ValueError(f"unknown outcome {result.outcome!r}")
    if mode not in MODES:
        raise ValueError(f"unknown mode {mode!r}")
    ended = utcnow()
    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO submission_attempts
                   (application_id, mode, outcome, handler, filled, needed, fields,
                    confirmation, error, screenshot_path, final_url, tokens_in, tokens_out,
                    started_at, ended_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                application_id,
                mode,
                result.outcome,
                handler,
                json.dumps([f.as_dict() for f in result.filled]),
                json.dumps([n.as_dict() for n in result.needed]),
                json.dumps([_field_dict(f) for f in result.fields]),
                result.confirmation,
                result.error,
                result.screenshot_path,
                result.final_url,
                result.input_tokens,
                result.output_tokens,
                started_at,
                ended,
            ),
        )
        attempt_id = int(cur.lastrowid)
        if result.screenshot_path:
            conn.execute(
                "UPDATE applications SET screenshot_path = ? WHERE id = ?",
                (result.screenshot_path, application_id),
            )
        if result.outcome == "submitted":
            conn.execute(
                "UPDATE applications SET submitted_at = COALESCE(submitted_at, ?) WHERE id = ?",
                (ended, application_id),
            )
            conn.execute(
                """INSERT INTO application_events
                       (application_id, kind, from_status, to_status, note, source, created_at)
                   VALUES (?, 'status_change', NULL, 'applied', ?, 'campaign', ?)""",
                (application_id, result.confirmation or f"submitted via {handler}", ended),
            )
    return attempt_id


def add_event(
    conn: sqlite3.Connection,
    application_id: int,
    *,
    kind: str = "status_change",
    to_status: str | None = None,
    note: str | None = None,
    source: str = "manual",
) -> int:
    """Append one event. Status events carry `to_status`; notes and emails need not."""
    if kind not in ("status_change", "note", "email"):
        raise ValueError(f"unknown event kind {kind!r}")
    if to_status is not None and to_status not in EVENT_STATUSES:
        raise ValueError(f"unknown status {to_status!r}")
    if source not in EVENT_SOURCES:
        raise ValueError(f"unknown source {source!r}")
    if kind == "status_change" and to_status is None:
        raise ValueError("a status change needs to_status")
    current = conn.execute(
        "SELECT status FROM application_status WHERE application_id = ?", (application_id,)
    ).fetchone()
    if current is None:
        raise ValueError(f"no application {application_id}")
    with transaction(conn):
        cur = conn.execute(
            """INSERT INTO application_events
                   (application_id, kind, from_status, to_status, note, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                application_id,
                kind,
                current["status"] if to_status else None,
                to_status,
                note,
                source,
                utcnow(),
            ),
        )
    return int(cur.lastrowid)


_APPLICATION_SQL = """
SELECT a.*, s.status, s.first_reply_at,
       j.title AS job_title, j.company AS job_company, j.url AS job_url,
       j.apply_url AS job_apply_url, j.status AS job_status,
       (SELECT COUNT(*) FROM submission_attempts t WHERE t.application_id = a.id) AS attempts,
       (SELECT t.id FROM submission_attempts t WHERE t.application_id = a.id
        ORDER BY t.id DESC LIMIT 1) AS last_attempt_id
FROM applications a
JOIN application_status s ON s.application_id = a.id
JOIN jobs j ON j.id = a.job_id
"""


def get_application(conn: sqlite3.Connection, application_id: int) -> dict[str, Any] | None:
    row = conn.execute(_APPLICATION_SQL + "WHERE a.id = ?", (application_id,)).fetchone()
    return _application_dict(conn, row) if row else None


def list_applications(
    conn: sqlite3.Connection,
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    where, params = "", []
    if status:
        where, params = "WHERE s.status = ? ", [status]
    rows = conn.execute(
        _APPLICATION_SQL + where + "ORDER BY a.id DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [_application_dict(conn, r) for r in rows]


def count_applications(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM application_status GROUP BY status"
    ).fetchall()
    return {r["status"]: r["n"] for r in rows}


def needing_input(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Applications parked on a question, newest first, each with its questions."""
    return list_applications(conn, status="needs_input")


def submitted_since(conn: sqlite3.Connection, since: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM applications WHERE submitted_at >= ?", (since,)
    ).fetchone()
    return int(row["n"])


def submitted_last_day(conn: sqlite3.Connection, now: datetime | None = None) -> int:
    """Submissions in the trailing 24 hours: what the daily cap counts."""
    now = now or datetime.now(UTC)
    return submitted_since(conn, (now - timedelta(hours=24)).isoformat(timespec="seconds"))


def list_attempts(conn: sqlite3.Connection, application_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM submission_attempts WHERE application_id = ? ORDER BY id",
        (application_id,),
    ).fetchall()
    return [_attempt_dict(r) for r in rows]


def get_attempt(conn: sqlite3.Connection, attempt_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM submission_attempts WHERE id = ?", (attempt_id,)).fetchone()
    return _attempt_dict(row) if row else None


def list_events(conn: sqlite3.Connection, application_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM application_events WHERE application_id = ? ORDER BY created_at, id",
        (application_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def needed_for(conn: sqlite3.Connection, application_id: int) -> list[NeededInput]:
    """The questions the newest attempt could not answer; [] when it could."""
    row = conn.execute(
        """SELECT needed FROM submission_attempts WHERE application_id = ?
           ORDER BY id DESC LIMIT 1""",
        (application_id,),
    ).fetchone()
    if row is None or not row["needed"]:
        return []
    return [NeededInput(**n) for n in json.loads(row["needed"])]


# ---------------------------------------------------------------- shaping --


def _field_dict(f: Any) -> dict[str, Any]:
    return {
        "key": f.key,
        "label": f.label,
        "kind": f.kind,
        "required": f.required,
        "options": list(f.options),
        "section": f.section,
    }


def _attempt_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    for key in ("filled", "needed", "fields"):
        d[key] = json.loads(d[key]) if d.get(key) else []
    return d


def _application_dict(conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    last = get_attempt(conn, d["last_attempt_id"]) if d.get("last_attempt_id") else None
    d["last_attempt"] = last
    d["needed"] = last["needed"] if last else []
    d["job"] = {
        "id": d.pop("job_id"),
        "title": d.pop("job_title"),
        "company": d.pop("job_company"),
        "url": d.pop("job_url"),
        "apply_url": d.pop("job_apply_url"),
        "status": d.pop("job_status"),
    }
    d.pop("last_attempt_id", None)
    return d


def job_is_applied(conn: sqlite3.Connection, job_id: str) -> bool:
    row = conn.execute(
        "SELECT submitted_at FROM applications WHERE job_id = ?", (job_id,)
    ).fetchone()
    return bool(row and row["submitted_at"])


def mark_job(conn: sqlite3.Connection, job_id: str, status: str) -> None:
    """Mirror the outcome onto the jobs table so discovery and the dashboard see it."""
    jobs.set_status(conn, job_id, status)
