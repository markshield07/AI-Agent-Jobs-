"""The dashboard's numbers, from a small history with known dates."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime

import pytest

from jobagent.discovery import store as jobs
from jobagent.discovery.models import RawJob
from jobagent.stats import dashboard_stats, recent_activity

NOW = datetime(2026, 9, 25, 18, 0, tzinfo=UTC)


def _job(conn, n, *, category="Backend Engineering", source="greenhouse", scraped="2026-09-20"):
    raw = RawJob(
        url=f"https://example.com/jobs/{n}", title=f"Job {n}", company=f"Co {n}", source=source
    )
    jid = jobs.upsert_jobs(conn, [raw]).new_ids[0]
    conn.execute(
        "UPDATE jobs SET category = ?, scraped_at = ? WHERE id = ?",
        (category, f"{scraped}T12:00:00+00:00", jid),
    )
    return jid


def _sent(conn, jid, at, *, ats="greenhouse"):
    cur = conn.execute(
        "INSERT INTO applications (job_id, mode, ats, submitted_at, created_at)"
        " VALUES (?, 'auto', ?, ?, ?)",
        (jid, ats, at, at),
    )
    app_id = cur.lastrowid
    jobs.set_status(conn, jid, "applied")
    _event(conn, app_id, at, to_status="applied", source="campaign")
    return app_id


def _event(conn, app_id, at, *, kind="status_change", to_status=None, source="manual"):
    conn.execute(
        "INSERT INTO application_events (application_id, kind, to_status, source, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (app_id, kind, to_status, source, at),
    )


_MESSAGE_IDS = itertools.count(1)


def _reply(conn, app_id, at, label="screening"):
    n = next(_MESSAGE_IDS)
    if app_id is not None:
        _event(conn, app_id, at, kind="email", source="email")
    conn.execute(
        "INSERT INTO inbox_messages (message_id, application_id, received_at, label, seen_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (f"<m{n}@x>", app_id, at, label, at),
    )


@pytest.fixture
def history(conn):
    """Five jobs, three sent; one ignored, one acknowledged, one interviewing."""
    a = _job(conn, 1, scraped="2026-09-20")
    b = _job(conn, 2, scraped="2026-09-21", category="Platform Engineering", source="linkedin")
    c = _job(conn, 3, scraped="2026-09-21", category="Platform Engineering", source="indeed")
    _job(conn, 4, scraped="2026-09-24", category="")
    _job(conn, 5, scraped="2026-08-01")
    jobs.set_status(conn, _job(conn, 6, scraped="2026-09-25"), "queued")

    first = _sent(conn, a, "2026-09-21T15:00:00+00:00")
    second = _sent(conn, b, "2026-09-22T09:00:00+00:00", ats="linkedin")
    third = _sent(conn, c, "2026-09-22T23:30:00+00:00", ats="indeed")
    _reply(conn, second, "2026-09-22T09:05:00+00:00", label="acknowledgement")
    _reply(conn, third, "2026-09-24T10:00:00+00:00", label="screening")
    _event(conn, third, "2026-09-24T10:00:01+00:00", to_status="screening", source="email")
    _event(conn, third, "2026-09-25T08:00:00+00:00", to_status="interviewing")
    _reply(conn, None, "2026-09-25T09:00:00+00:00", label="unknown")
    conn.commit()
    return {"first": first, "second": second, "third": third}


def test_totals_and_rates(conn, history):
    stats = dashboard_stats(conn, days=7, now=NOW)
    t = stats["totals"]
    assert t["discovered"] == 6 and t["queued"] == 1
    assert t["applied"] == 3
    assert t["responses"] == 2, "the acknowledgement and the screening reply"
    assert t["replies_from_a_person"] == 1
    assert t["interviews"] == 1 and t["offers"] == 0 and t["rejections"] == 0
    assert t["response_rate"] == pytest.approx(2 / 3, abs=1e-4)
    assert t["interview_rate"] == pytest.approx(1 / 3, abs=1e-4)
    # 5 minutes and about 1.4 days: the median of two is their mean.
    assert t["median_days_to_response"] == pytest.approx(0.7, abs=0.05)


def test_the_series_covers_every_day_in_the_window(conn, history):
    stats = dashboard_stats(conn, days=7, now=NOW)
    assert stats["window"] == {"days": 7, "from": "2026-09-19", "to": "2026-09-25"}
    days = {d["date"]: d for d in stats["per_day"]}
    assert list(days) == [f"2026-09-{d}" for d in range(19, 26)]
    assert days["2026-09-21"]["applied"] == 1
    assert days["2026-09-22"]["applied"] == 2
    assert days["2026-09-22"]["responses"] == 1
    assert days["2026-09-24"]["responses"] == 1
    assert days["2026-09-21"]["discovered"] == 2
    assert days["2026-09-19"] == {
        "date": "2026-09-19",
        "applied": 0,
        "responses": 0,
        "discovered": 0,
    }
    assert stats["window_totals"] == {"discovered": 5, "applied": 3, "responses": 2}


def test_days_follow_the_viewers_time_zone(conn, history):
    # In UTC+10, 15:00 UTC on the 21st is the 22nd and 23:30 UTC on the 22nd is the 23rd.
    east = {
        d["date"]: d
        for d in dashboard_stats(conn, days=7, tz_offset_minutes=600, now=NOW)["per_day"]
    }
    assert east["2026-09-21"]["applied"] == 0
    assert east["2026-09-22"]["applied"] == 2 and east["2026-09-23"]["applied"] == 1
    # And the window ends on the viewer's today: 04:00 on the 26th.
    assert "2026-09-26" in east


def test_categories_and_sites(conn, history):
    stats = dashboard_stats(conn, days=30, now=NOW)
    cats = {r["category"]: r for r in stats["by_category"]}
    assert cats["Platform Engineering"] == {
        "category": "Platform Engineering",
        "discovered": 2,
        "applied": 2,
        "responses": 2,
        "response_rate": 1.0,
    }
    assert cats["Backend Engineering"]["applied"] == 1
    assert cats["Backend Engineering"]["discovered"] == 3
    assert cats["Uncategorised"]["discovered"] == 1, "a blank category is not its own bucket"
    assert stats["by_category"][0]["category"] == "Platform Engineering", "most applied first"

    sites = {r["site"]: r for r in stats["by_site"]}
    assert sites["linkedin"]["applied"] == 1 and sites["linkedin"]["responses"] == 1
    assert sites["greenhouse"]["response_rate"] == 0.0


def test_funnel_and_what_needs_attention(conn, history):
    stats = dashboard_stats(conn, now=NOW)
    assert [s["count"] for s in stats["funnel"]] == [6, 4, 3, 2, 1, 0]
    assert stats["attention"]["unmatched_replies"] == 1
    assert stats["application_status"]["interviewing"] == 1


def test_an_empty_database_has_zeros_not_errors(conn):
    stats = dashboard_stats(conn, days=3, now=NOW)
    assert stats["totals"]["applied"] == 0
    assert stats["totals"]["response_rate"] is None
    assert stats["totals"]["median_days_to_response"] is None
    assert len(stats["per_day"]) == 3
    assert stats["by_category"] == [] and stats["by_site"] == []
    assert recent_activity(conn) == []


def test_days_are_clamped(conn):
    assert dashboard_stats(conn, days=0, now=NOW)["window"]["days"] == 1
    assert dashboard_stats(conn, days=5000, now=NOW)["window"]["days"] == 366


def test_recent_activity_newest_first(conn, history):
    feed = recent_activity(conn, limit=4)
    assert [(i["kind"], i["application_id"]) for i in feed] == [
        ("status", history["third"]),
        ("reply", history["third"]),
        ("applied", history["third"]),
        ("reply", history["second"]),
    ]
    assert feed[0]["detail"] == "interviewing", "set by hand, so shown on its own"
    assert feed[1]["moved_to"] == "screening", "the inbox's move rides on its reply"
    assert feed[2]["site"] == "indeed" and feed[2]["title"] == "Job 3"
    assert feed[3]["moved_to"] is None
