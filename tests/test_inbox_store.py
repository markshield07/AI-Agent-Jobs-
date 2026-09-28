"""The inbox log: what is kept about each mail, and what is deliberately not."""

from __future__ import annotations

import pytest

from jobagent.apply import store as applications
from jobagent.discovery import store as jobs
from jobagent.discovery.models import RawJob
from jobagent.inbox import store
from jobagent.inbox.models import InboxMessage, Match, Reading


def message(message_id: str = "m1", **kwargs) -> InboxMessage:
    base = {
        "subject": "Update on your application",
        "from_addr": "dana@acmerobotics.com",
        "from_name": "Dana Okafor",
        "received_at": "2026-09-21T09:00:00+00:00",
        "body": "  a body   with    spacing\nand a newline ",
    }
    return InboxMessage(message_id=message_id, **{**base, **kwargs})


@pytest.fixture
def application(conn) -> int:
    raw = RawJob(
        url="https://boards.greenhouse.io/acme/jobs/1",
        title="Senior Backend Engineer",
        company="Acme Robotics",
        source="greenhouse",
    )
    job_id = jobs.upsert_jobs(conn, [raw]).new_ids[0]
    return applications.get_or_create_application(conn, job_id, mode="auto")


def test_a_message_is_recorded_with_what_was_read(conn, application):
    row_id = store.record(
        conn,
        message(),
        match=Match(application_id=application, confidence=0.8, reasons=["sender domain"]),
        reading=Reading(label="rejected", confidence=0.95, reason="said no", source="rules"),
    )
    row = store.get(conn, row_id)
    assert row is not None
    assert row["application_id"] == application
    assert (row["label"], row["confidence"], row["read_by"]) == ("rejected", 0.95, "rules")
    assert row["match_reason"] == "sender domain"


def test_only_an_excerpt_of_the_body_is_kept(conn):
    row = store.get(conn, store.record(conn, message()))
    assert row["excerpt"] == "a body with spacing and a newline"


def test_recording_the_same_message_twice_keeps_one_row(conn):
    first = store.record(conn, message("m1"))
    second = store.record(conn, message("m1", subject="changed"))
    assert first == second
    assert len(store.list_messages(conn)) == 1
    assert store.get(conn, first)["subject"] == "Update on your application"


def test_seen_ids_and_already_seen_agree(conn):
    store.record(conn, message("m1"))
    assert store.already_seen(conn, "m1") and not store.already_seen(conn, "m2")
    assert store.seen_ids(conn) == {"m1"}


def test_the_log_comes_back_newest_first(conn):
    store.record(conn, message("old", received_at="2026-09-01T09:00:00+00:00"))
    store.record(conn, message("new", received_at="2026-09-21T09:00:00+00:00"))
    assert [r["message_id"] for r in store.list_messages(conn)] == ["new", "old"]


def test_the_log_can_be_narrowed(conn, application):
    store.record(conn, message("unmatched"))
    store.record(
        conn,
        message("matched"),
        match=Match(application_id=application, confidence=0.9),
        reading=Reading(label="offer", confidence=0.9),
    )
    assert [r["message_id"] for r in store.list_messages(conn, matched=False)] == ["unmatched"]
    assert [r["message_id"] for r in store.list_messages(conn, matched=True)] == ["matched"]
    assert [r["message_id"] for r in store.list_messages(conn, label="offer")] == ["matched"]
    by_app = store.list_messages(conn, application_id=application)
    assert [r["message_id"] for r in by_app] == ["matched"]


def test_counts_are_by_label_and_by_whether_anything_was_matched(conn, application):
    store.record(conn, message("a"))
    store.record(
        conn,
        message("b"),
        match=Match(application_id=application, confidence=0.9),
        reading=Reading(label="rejected", confidence=0.9),
    )
    counts = store.counts(conn)
    assert counts["unknown"] == 1 and counts["rejected"] == 1 and counts["unmatched"] == 1


def test_attaching_a_message_files_it_and_marks_who_did(conn, application):
    row_id = store.record(conn, message())
    attached = store.attach(conn, row_id, application, label="rejected")
    assert attached["application_id"] == application
    assert attached["read_by"] == "manual" and attached["match_confidence"] == 1.0
    assert attached["label"] == "rejected"


def test_attaching_to_an_application_that_is_not_there_is_refused(conn):
    row_id = store.record(conn, message())
    with pytest.raises(ValueError, match="no application"):
        store.attach(conn, row_id, 999)


def test_attaching_a_row_that_is_not_there_returns_nothing(conn, application):
    assert store.attach(conn, 404, application) is None


def test_the_newest_arrival_is_where_the_next_poll_starts(conn):
    assert store.latest_received(conn) is None
    store.record(conn, message("old", received_at="2026-09-01T09:00:00+00:00"))
    store.record(conn, message("new", received_at="2026-09-21T09:00:00+00:00"))
    assert store.latest_received(conn) == "2026-09-21T09:00:00+00:00"


def test_an_event_can_be_linked_back_to_the_message_that_caused_it(conn, application):
    row_id = store.record(conn, message())
    event_id = applications.add_event(conn, application, kind="note", note="hello")
    store.set_event(conn, row_id, event_id)
    assert store.get(conn, row_id)["event_id"] == event_id
