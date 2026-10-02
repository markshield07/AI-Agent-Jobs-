"""Applications and attempts on disk.

One `applications` row per job, however many times the form was tried; one
`submission_attempts` row per try. `submitted_at` is set by the attempt that
saw the confirmation and is never cleared, and `application_events` stays
append-only: the agent adds the 'applied' event, phase 5 adds the replies,
and the status view derives the rest.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import Any

from jobagent.db.database import transaction, utcnow
from jobagent.discovery import brief
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
        if to_status == "applied":
            # Confirmed by hand (an unconfirmed send that did go in): it now
            # counts as sent, in the stats and the daily caps alike.
            conn.execute(
                "UPDATE applications SET submitted_at = COALESCE(submitted_at, ?) WHERE id = ?",
                (utcnow(), application_id),
            )
    return int(cur.lastrowid)


_APPLICATION_SQL = """
SELECT a.*, s.status, s.first_reply_at,
       j.title AS job_title, j.company AS job_company, j.url AS job_url,
       j.apply_url AS job_apply_url, j.status AS job_status,
       j.location AS job_location, j.remote AS job_remote, j.source AS job_source,
       j.category AS job_category, j.score AS job_score, j.posted_at AS job_posted_at,
       j.salary_min AS job_salary_min, j.salary_max AS job_salary_max,
       j.description AS job_description,
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
    submitted: bool = False,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Newest first; with `submitted`, only those sent, most recently sent first."""
    clauses, params = [], []
    if status:
        clauses.append("s.status = ?")
        params.append(status)
    if submitted:
        clauses.append("a.submitted_at IS NOT NULL")
    where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
    order = "ORDER BY a.submitted_at DESC, a.id DESC " if submitted else "ORDER BY a.id DESC "
    rows = conn.execute(
        _APPLICATION_SQL + where + order + "LIMIT ? OFFSET ?",
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


def submitted_since(conn: sqlite3.Connection, since: str, *, ats: str | None = None) -> int:
    """Submissions at or after `since`; only through one site's handler when `ats` is given."""
    if ats is None:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM applications WHERE submitted_at >= ?", (since,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM applications WHERE submitted_at >= ? AND ats = ?",
            (since, ats),
        ).fetchone()
    return int(row["n"])


def submitted_last_day(
    conn: sqlite3.Connection, now: datetime | None = None, *, ats: str | None = None
) -> int:
    """Submissions in the trailing 24 hours: what the daily caps count."""
    now = now or datetime.now(UTC)
    since = (now - timedelta(hours=24)).isoformat(timespec="seconds")
    return submitted_since(conn, since, ats=ats)


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
    remote = d.pop("job_remote")
    job = {
        "id": d.pop("job_id"),
        "title": d.pop("job_title"),
        "company": d.pop("job_company"),
        "url": d.pop("job_url"),
        "apply_url": d.pop("job_apply_url"),
        "status": d.pop("job_status"),
        "location": d.pop("job_location"),
        "remote": None if remote is None else bool(remote),
        "source": d.pop("job_source"),
        "category": d.pop("job_category"),
        "score": d.pop("job_score"),
        "posted_at": d.pop("job_posted_at"),
        "salary_min": d.pop("job_salary_min"),
        "salary_max": d.pop("job_salary_max"),
        "description": d.pop("job_description"),
    }
    # Lists carry the short form; the one-application route adds the full text back.
    d["job"] = brief.card(job)
    d.pop("last_attempt_id", None)
    return d


def withdraw_unsent(conn: sqlite3.Connection, job_id: str, note: str) -> None:
    """Stop an application to this job that was never sent: removed when nothing
    was ever tried, else marked withdrawn with the note, so it asks nothing more."""
    app = application_for_job(conn, job_id)
    if app is None or app.get("submitted_at"):
        return
    tried = conn.execute(
        "SELECT 1 FROM submission_attempts WHERE application_id = ? LIMIT 1", (app["id"],)
    ).fetchone()
    if tried is None:
        with transaction(conn):
            conn.execute("DELETE FROM application_events WHERE application_id = ?", (app["id"],))
            conn.execute("DELETE FROM applications WHERE id = ?", (app["id"],))
        return
    if app.get("status") != "withdrawn":
        add_event(conn, app["id"], to_status="withdrawn", note=note, source="campaign")


def _same_role(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def _same_company(value: str | None) -> str:
    """The company for matching twins: "Acme, Inc." is Acme. A board's
    placeholder for a company it did not name ("Unknown (Dice)") is no company."""
    name = _same_role(value)
    if name.startswith("unknown"):
        return ""
    return re.sub(r"(?:\s+(?:inc|llc|l l c|ltd|corp|corporation|co|company|plc|lp))+$", "", name)


def twin_application(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    """An earlier application to the same role at the same company (the same
    title reposted under another posting id), sent or still under way; None
    when there is none. The earlier one is the one kept, so of two twins exactly
    one goes on."""
    job = conn.execute("SELECT title, company FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if job is None or not _same_role(job["title"]) or not _same_company(job["company"]):
        return None
    own = conn.execute("SELECT id FROM applications WHERE job_id = ?", (job_id,)).fetchone()
    rows = conn.execute(
        """SELECT a.id, j.title, j.company FROM applications a JOIN jobs j ON j.id = a.job_id
           WHERE a.job_id != ? ORDER BY a.id""",
        (job_id,),
    ).fetchall()
    for row in rows:
        if own is not None and row["id"] > own["id"]:
            break
        if (_same_role(row["title"]), _same_company(row["company"])) != (
            _same_role(job["title"]),
            _same_company(job["company"]),
        ):
            continue
        app = get_application(conn, int(row["id"]))
        # One that never got through (blocked, failed) or was stopped leaves
        # the repost free to try.
        if app and app.get("status") not in ("withdrawn", "failed", "blocked"):
            return app
    return None


def job_is_applied(conn: sqlite3.Connection, job_id: str) -> bool:
    row = conn.execute(
        "SELECT submitted_at FROM applications WHERE job_id = ?", (job_id,)
    ).fetchone()
    return bool(row and row["submitted_at"])


def mark_job(conn: sqlite3.Connection, job_id: str, status: str) -> None:
    """Mirror the outcome onto the jobs table so discovery and the dashboard see it."""
    jobs.set_status(conn, job_id, status)
