"""The numbers the dashboard shows, read straight from the database.

Everything here is derived at read time from the tables the agent already
keeps: `jobs` for what was found, `applications.submitted_at` for what was
sent, and the append-only `application_events` for what came back. Nothing
is stored twice, so a number on the dashboard can never disagree with the
log it came from.

A **response** is the first sign an employer read the application: an email
the inbox filed against it, or a status you or the inbox moved it to
(screening, interviewing, offer, rejected). An automatic "we received your
application" is an email too, and counts; `acknowledgement` replies are
reported separately so the rate can be read both ways.

Days are counted in the viewer's time zone: `tz_offset_minutes` is what
JavaScript's `-new Date().getTimezoneOffset()` gives (minutes east of UTC),
and every timestamp is shifted by it before its date is taken.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, date, datetime, timedelta
from typing import Any

RESPONSE_STATUSES = ("screening", "interviewing", "offer", "rejected")
UNCATEGORISED = "Uncategorised"

# One row per submitted application: when it went, which site, which
# category, and when (if ever) the first response came.
_SENT_SQL = """
SELECT a.id,
       a.submitted_at,
       COALESCE(a.ats, j.source) AS site,
       COALESCE(NULLIF(TRIM(j.category), ''), :uncat) AS category,
       s.status,
       (SELECT MIN(t) FROM (
            SELECT MIN(e.created_at) AS t FROM application_events e
             WHERE e.application_id = a.id AND e.kind = 'email'
            UNION ALL
            SELECT MIN(e.created_at) FROM application_events e
             WHERE e.application_id = a.id
               AND e.to_status IN ('screening','interviewing','offer','rejected')
       )) AS responded_at,
       EXISTS (SELECT 1 FROM application_events e
                WHERE e.application_id = a.id AND e.to_status IN ('interviewing','offer'))
           AS interviewed,
       EXISTS (SELECT 1 FROM application_events e
                WHERE e.application_id = a.id AND e.to_status = 'offer') AS offered,
       EXISTS (SELECT 1 FROM application_events e
                WHERE e.application_id = a.id AND e.to_status = 'rejected') AS rejected,
       EXISTS (SELECT 1 FROM inbox_messages m
                WHERE m.application_id = a.id AND m.label <> 'acknowledgement')
           AS human_reply
FROM applications a
JOIN jobs j ON j.id = a.job_id
JOIN application_status s ON s.application_id = a.id
WHERE a.submitted_at IS NOT NULL
"""


def _shift(ts: str | None, offset: timedelta) -> date | None:
    if not ts:
        return None
    try:
        moment = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return (moment.astimezone(UTC) + offset).date()


def _days_between(start: str, end: str) -> float | None:
    try:
        a, b = datetime.fromisoformat(start), datetime.fromisoformat(end)
    except (TypeError, ValueError):
        return None
    if a.tzinfo is None:
        a = a.replace(tzinfo=UTC)
    if b.tzinfo is None:
        b = b.replace(tzinfo=UTC)
    return max(0.0, (b - a).total_seconds() / 86400)


def _rate(part: int, whole: int) -> float | None:
    return round(part / whole, 4) if whole else None


def dashboard_stats(
    conn: sqlite3.Connection,
    *,
    days: int = 30,
    tz_offset_minutes: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Totals, a per-day series over the last `days` days, and breakdowns by
    category and site. Totals are all-time; `window` totals cover the series."""
    days = max(1, min(int(days), 366))
    offset = timedelta(minutes=int(tz_offset_minutes))
    now = now or datetime.now(UTC)
    today = (now.astimezone(UTC) + offset).date()
    first = today - timedelta(days=days - 1)

    sent = [dict(r) for r in conn.execute(_SENT_SQL, {"uncat": UNCATEGORISED}).fetchall()]
    jobs = [
        dict(r)
        for r in conn.execute(
            "SELECT scraped_at, status,"
            " COALESCE(NULLIF(TRIM(category), ''), ?) AS category FROM jobs",
            (UNCATEGORISED,),
        ).fetchall()
    ]

    series = {
        first + timedelta(days=i): {"applied": 0, "responses": 0, "discovered": 0}
        for i in range(days)
    }
    for job in jobs:
        day = _shift(job["scraped_at"], offset)
        if day in series:
            series[day]["discovered"] += 1
    for app in sent:
        day = _shift(app["submitted_at"], offset)
        if day in series:
            series[day]["applied"] += 1
        replied = _shift(app["responded_at"], offset)
        if replied in series:
            series[replied]["responses"] += 1

    responded = [a for a in sent if a["responded_at"]]
    waits = [
        w
        for w in (_days_between(a["submitted_at"], a["responded_at"]) for a in responded)
        if w is not None
    ]
    in_window = [a for a in sent if (_shift(a["submitted_at"], offset) or date.min) >= first]

    categories: dict[str, dict[str, Any]] = {}
    for job in jobs:
        row = categories.setdefault(
            job["category"],
            {"category": job["category"], "discovered": 0, "applied": 0, "responses": 0},
        )
        row["discovered"] += 1
    for app in sent:
        row = categories.setdefault(
            app["category"],
            {"category": app["category"], "discovered": 0, "applied": 0, "responses": 0},
        )
        row["applied"] += 1
        row["responses"] += 1 if app["responded_at"] else 0
    by_category = sorted(
        categories.values(), key=lambda r: (-r["applied"], -r["discovered"], r["category"])
    )
    for row in by_category:
        row["response_rate"] = _rate(row["responses"], row["applied"])

    sites: dict[str, dict[str, Any]] = {}
    for app in sent:
        name = app["site"] or "other"
        row = sites.setdefault(name, {"site": name, "applied": 0, "responses": 0})
        row["applied"] += 1
        row["responses"] += 1 if app["responded_at"] else 0
    by_site = sorted(sites.values(), key=lambda r: (-r["applied"], r["site"]))
    for row in by_site:
        row["response_rate"] = _rate(row["responses"], row["applied"])

    job_status = {
        r["status"]: r["n"]
        for r in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
    }
    app_status = {
        r["status"]: r["n"]
        for r in conn.execute(
            "SELECT status, COUNT(*) AS n FROM application_status GROUP BY status"
        )
    }
    unmatched = conn.execute(
        "SELECT COUNT(*) AS n FROM inbox_messages WHERE application_id IS NULL"
    ).fetchone()["n"]

    applied = len(sent)
    interviews = sum(1 for a in sent if a["interviewed"])
    offers = sum(1 for a in sent if a["offered"])
    shortlisted = sum(job_status.get(s, 0) for s in ("queued", "applied", "failed", "needs_review"))
    return {
        "generated_at": now.astimezone(UTC).isoformat(timespec="seconds"),
        "window": {"days": days, "from": first.isoformat(), "to": today.isoformat()},
        "totals": {
            "discovered": len(jobs),
            "queued": job_status.get("queued", 0),
            "applied": applied,
            "responses": len(responded),
            "replies_from_a_person": sum(1 for a in sent if a["human_reply"]),
            "interviews": interviews,
            "offers": offers,
            "rejections": sum(1 for a in sent if a["rejected"]),
            "response_rate": _rate(len(responded), applied),
            "interview_rate": _rate(interviews, applied),
            "median_days_to_response": _median(waits),
        },
        "window_totals": {
            "discovered": sum(d["discovered"] for d in series.values()),
            "applied": len(in_window),
            "responses": sum(d["responses"] for d in series.values()),
        },
        "per_day": [{"date": day.isoformat(), **counts} for day, counts in series.items()],
        "funnel": [
            {"stage": "Found", "count": len(jobs)},
            {"stage": "Shortlisted", "count": shortlisted},
            {"stage": "Applied", "count": applied},
            {"stage": "Heard back", "count": len(responded)},
            {"stage": "Interviewing", "count": interviews},
            {"stage": "Offer", "count": offers},
        ],
        "by_category": by_category,
        "by_site": by_site,
        "application_status": app_status,
        "attention": {
            "needs_input": app_status.get("needs_input", 0),
            "to_approve": app_status.get("review", 0),
            "to_check": app_status.get("blocked", 0) + app_status.get("unconfirmed", 0),
            "unmatched_replies": int(unmatched),
        },
    }


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    mid = len(ordered) // 2
    value = ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2
    return round(value, 1)


_ACTIVITY_SQL = """
SELECT * FROM (
    SELECT a.submitted_at AS at, 'applied' AS kind, a.id AS application_id,
           j.title, j.company, COALESCE(a.ats, j.source) AS site,
           NULL AS detail, NULL AS moved_to, 0 AS seq
      FROM applications a JOIN jobs j ON j.id = a.job_id
     WHERE a.submitted_at IS NOT NULL
    UNION ALL
    -- A reply, with the status it moved the application to when the inbox
    -- moved one: that status event is the next one written for the application.
    SELECT e.created_at, 'reply', a.id, j.title, j.company, COALESCE(a.ats, j.source),
           e.note,
           (SELECT n.to_status FROM application_events n
             WHERE n.id = (SELECT MIN(x.id) FROM application_events x
                            WHERE x.application_id = e.application_id AND x.id > e.id)
               AND n.kind = 'status_change' AND n.source = 'email'),
           e.id
      FROM application_events e
      JOIN applications a ON a.id = e.application_id
      JOIN jobs j ON j.id = a.job_id
     WHERE e.kind = 'email'
    UNION ALL
    -- A status set by hand; the inbox's own moves are shown on their reply.
    SELECT e.created_at, 'status', a.id, j.title, j.company, COALESCE(a.ats, j.source),
           e.to_status, e.to_status, e.id
      FROM application_events e
      JOIN applications a ON a.id = e.application_id
      JOIN jobs j ON j.id = a.job_id
     WHERE e.kind = 'status_change' AND e.to_status IS NOT NULL AND e.to_status <> 'applied'
       AND COALESCE(e.source, '') <> 'email'
)
ORDER BY at DESC, seq DESC
LIMIT ?
"""


def recent_activity(conn: sqlite3.Connection, limit: int = 20) -> list[dict[str, Any]]:
    """The newest submissions, replies and status moves, newest first."""
    limit = max(1, min(int(limit), 200))
    rows = [dict(r) for r in conn.execute(_ACTIVITY_SQL, (limit,)).fetchall()]
    for row in rows:
        row.pop("seq", None)
    return rows
