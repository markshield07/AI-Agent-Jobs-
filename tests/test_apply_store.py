"""Applications and attempts on disk: one row per job, one per try, status derived."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobagent.apply import store
from jobagent.apply.models import Fill, FormField, HandlerResult, NeededInput
from jobagent.db.database import utcnow
from jobagent.discovery import store as jobs
from jobagent.discovery.models import RawJob


@pytest.fixture
def job_id(conn) -> str:
    raw = RawJob(
        url="https://jobs.lever.co/acme/123",
        title="Engineer",
        company="Acme",
        source="lever",
        ats_type="lever",
    )
    jid = jobs.upsert_jobs(conn, [raw]).new_ids[0]
    jobs.set_status(conn, jid, "queued")
    return jid


def _result(outcome, **kw) -> HandlerResult:
    return HandlerResult(outcome=outcome, **kw)


def test_application_is_one_per_job_and_details_overwrite(conn, job_id):
    first = store.get_or_create_application(conn, job_id, mode="dry_run", ats="lever")
    second = store.get_or_create_application(conn, job_id, mode="auto", variant_id=None)
    assert first == second
    app = store.get_application(conn, first)
    assert app["mode"] == "auto" and app["ats"] == "lever", "unset details are kept"
    assert app["status"] == "pending" and app["attempts"] == 0
    assert app["job"] == {
        "id": job_id,
        "title": "Engineer",
        "company": "Acme",
        "url": "https://jobs.lever.co/acme/123",
        "apply_url": None,
        "status": "queued",
    }
    assert store.application_for_job(conn, job_id)["id"] == first
    assert store.application_for_job(conn, "nope") is None


def test_unknown_mode_or_outcome_is_refused(conn, job_id):
    with pytest.raises(ValueError):
        store.get_or_create_application(conn, job_id, mode="yolo")
    app_id = store.get_or_create_application(conn, job_id, mode="dry_run")
    with pytest.raises(ValueError):
        store.record_attempt(
            conn, app_id, _result("sent"), mode="dry_run", handler="lever", started_at=utcnow()
        )


def test_dry_run_attempt_is_kept_with_what_it_filled(conn, job_id):
    app_id = store.get_or_create_application(conn, job_id, mode="dry_run", ats="lever")
    result = _result(
        "dry_run",
        filled=[Fill(key="name", value="Mark Shield", source="contact", label="Full name")],
        fields=[FormField(key="name", label="Full name", kind="text", required=True)],
        screenshot_path="/tmp/shot.png",
        final_url="https://jobs.lever.co/acme/123/apply",
    )
    attempt_id = store.record_attempt(
        conn, app_id, result, mode="dry_run", handler="lever", started_at=utcnow()
    )

    app = store.get_application(conn, app_id)
    assert app["status"] == "dry_run"
    assert app["submitted_at"] is None
    assert app["screenshot_path"] == "/tmp/shot.png"
    assert app["attempts"] == 1 and app["last_attempt"]["id"] == attempt_id
    assert app["last_attempt"]["filled"] == [
        {
            "key": "name",
            "label": "Full name",
            "value": "Mark Shield",
            "file_path": None,
            "source": "contact",
        }
    ]
    assert app["last_attempt"]["fields"][0]["label"] == "Full name"
    assert store.list_events(conn, app_id) == [], "a dry run is not an application event"


def test_submitted_attempt_stamps_the_application_once_and_logs_applied(conn, job_id):
    app_id = store.get_or_create_application(conn, job_id, mode="auto", ats="lever")
    store.record_attempt(
        conn,
        app_id,
        _result("submitted", confirmation="Thanks for applying"),
        mode="auto",
        handler="lever",
        started_at=utcnow(),
    )
    app = store.get_application(conn, app_id)
    first_stamp = app["submitted_at"]
    assert first_stamp and app["status"] == "applied"
    events = store.list_events(conn, app_id)
    assert len(events) == 1
    assert events[0]["to_status"] == "applied" and events[0]["source"] == "campaign"
    assert events[0]["note"] == "Thanks for applying"
    assert store.job_is_applied(conn, job_id)

    # A second submitted result (a re-run by mistake) does not move the stamp.
    store.record_attempt(
        conn, app_id, _result("submitted"), mode="auto", handler="lever", started_at=utcnow()
    )
    assert store.get_application(conn, app_id)["submitted_at"] == first_stamp


def test_needs_input_keeps_the_questions(conn, job_id):
    app_id = store.get_or_create_application(conn, job_id, mode="auto", ats="lever")
    needed = [
        NeededInput(
            key="q1",
            label="Are you authorised to work in the US?",
            kind="select",
            required=True,
            options=["Yes", "No"],
            reason="no answer on file",
            answer_key="work_authorization",
        )
    ]
    store.record_attempt(
        conn,
        app_id,
        _result("needs_input", needed=needed),
        mode="auto",
        handler="lever",
        started_at=utcnow(),
    )
    assert store.get_application(conn, app_id)["status"] == "needs_input"
    assert store.needed_for(conn, app_id) == needed
    parked = store.needing_input(conn)
    assert [p["id"] for p in parked] == [app_id]
    assert parked[0]["needed"][0]["answer_key"] == "work_authorization"

    # Answering and trying again clears the parking.
    store.record_attempt(
        conn, app_id, _result("dry_run"), mode="dry_run", handler="lever", started_at=utcnow()
    )
    assert store.needed_for(conn, app_id) == []
    assert store.needing_input(conn) == []


def test_events_validate_and_record_the_previous_status(conn, job_id):
    app_id = store.get_or_create_application(conn, job_id, mode="auto")
    store.record_attempt(
        conn, app_id, _result("submitted"), mode="auto", handler="lever", started_at=utcnow()
    )
    store.add_event(conn, app_id, to_status="interviewing", note="phone screen", source="manual")
    events = store.list_events(conn, app_id)
    assert events[-1]["from_status"] == "applied" and events[-1]["to_status"] == "interviewing"
    assert store.get_application(conn, app_id)["status"] == "interviewing"

    store.add_event(conn, app_id, kind="note", note="sent a thank-you")
    assert store.get_application(conn, app_id)["status"] == "interviewing"

    with pytest.raises(ValueError):
        store.add_event(conn, app_id, to_status="hired")
    with pytest.raises(ValueError):
        store.add_event(conn, app_id, kind="status_change")
    with pytest.raises(ValueError):
        store.add_event(conn, app_id, to_status="rejected", source="carrier pigeon")
    with pytest.raises(ValueError):
        store.add_event(conn, 999, to_status="rejected")


def test_listing_filters_by_derived_status_and_counts(conn):
    ids = []
    for n in range(3):
        raw = RawJob(url=f"https://x/{n}", title=f"J{n}", company="C", source="lever")
        jid = jobs.upsert_jobs(conn, [raw]).new_ids[0]
        ids.append(store.get_or_create_application(conn, jid, mode="auto"))
    store.record_attempt(
        conn, ids[0], _result("submitted"), mode="auto", handler="lever", started_at=utcnow()
    )
    store.record_attempt(
        conn, ids[1], _result("failed", error="boom"), mode="auto", handler="x", started_at=utcnow()
    )

    assert [a["id"] for a in store.list_applications(conn)] == ids[::-1]
    assert [a["id"] for a in store.list_applications(conn, status="applied")] == [ids[0]]
    assert store.list_applications(conn, status="failed")[0]["last_attempt"]["error"] == "boom"
    assert store.count_applications(conn) == {"applied": 1, "failed": 1, "pending": 1}
    assert [a["id"] for a in store.list_applications(conn, limit=1, offset=1)] == [ids[1]]


def test_daily_cap_counts_the_trailing_day(conn):
    now = datetime(2026, 9, 21, 12, 0, tzinfo=UTC)
    for n, when in enumerate(
        (now - timedelta(hours=1), now - timedelta(hours=23), now - timedelta(hours=25))
    ):
        raw = RawJob(url=f"https://x/{n}", title="J", company="C", source="lever")
        jid = jobs.upsert_jobs(conn, [raw]).new_ids[0]
        app_id = store.get_or_create_application(conn, jid, mode="auto")
        conn.execute(
            "UPDATE applications SET submitted_at = ? WHERE id = ?",
            (when.isoformat(timespec="seconds"), app_id),
        )
    assert store.submitted_last_day(conn, now) == 2


def test_attempts_are_listed_in_order_and_cascade_with_the_job(conn, job_id):
    app_id = store.get_or_create_application(conn, job_id, mode="dry_run")
    for outcome in ("failed", "dry_run"):
        store.record_attempt(
            conn, app_id, _result(outcome), mode="dry_run", handler="lever", started_at=utcnow()
        )
    assert [a["outcome"] for a in store.list_attempts(conn, app_id)] == ["failed", "dry_run"]

    conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
    assert store.get_application(conn, app_id) is None
    assert store.list_attempts(conn, app_id) == []
