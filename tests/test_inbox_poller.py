"""One pass over the mailbox, end to end against fixture mail.

The append-only event log is what makes these assertions worth making: a status
is never written, it is derived from the newest event, so what the poller has
to get right is which events it appends and which it declines to.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.apply import store as applications
from jobagent.config import Settings
from jobagent.discovery import store as jobs
from jobagent.discovery.models import RawJob
from jobagent.inbox import store as inbox
from jobagent.inbox.mailbox import InboxNotConfigured, MailboxError, parse_message
from jobagent.inbox.models import InboxMessage
from jobagent.inbox.poller import attach_message, poll_inbox, should_advance

EMAILS = Path(__file__).parent / "fixtures" / "emails"


def fixture(name: str) -> InboxMessage:
    return parse_message((EMAILS / f"{name}.eml").read_bytes())


class FakeMailbox:
    """A mailbox holding whatever the test put in it."""

    name = "fake"

    def __init__(self, *names: str, error: Exception | None = None):
        self.messages = [fixture(n) for n in names]
        self.error = error
        self.since: str | None = None
        self.limit: int | None = None

    def fetch(self, *, since=None, limit=200):
        self.since, self.limit = since, limit
        if self.error:
            raise self.error
        return list(self.messages)


@pytest.fixture
def settings(tmp_path) -> Settings:
    """Model triage off: these tests are about what the poller records."""
    return Settings(data_dir=tmp_path, inbox_model_triage=False)


@pytest.fixture
def application(conn) -> int:
    raw = RawJob(
        url="https://boards.greenhouse.io/acme/jobs/1",
        title="Senior Backend Engineer",
        company="Acme Robotics",
        source="greenhouse",
        ats_type="greenhouse",
    )
    job_id = jobs.upsert_jobs(conn, [raw]).new_ids[0]
    app_id = applications.get_or_create_application(conn, job_id, mode="auto", ats="greenhouse")
    conn.execute(
        "UPDATE applications SET submitted_at = ? WHERE id = ?",
        ("2026-09-20T12:00:00+00:00", app_id),
    )
    conn.execute(
        """INSERT INTO application_events
               (application_id, kind, to_status, source, created_at)
           VALUES (?, 'status_change', 'applied', 'campaign', '2026-09-20T12:00:00+00:00')""",
        (app_id,),
    )
    return app_id


def status_of(conn, application_id: int) -> str:
    return conn.execute(
        "SELECT status FROM application_status WHERE application_id = ?", (application_id,)
    ).fetchone()["status"]


# ----------------------------------------------------------- advancing --


@pytest.mark.parametrize(
    ("current", "to_status", "moves"),
    [
        ("applied", "screening", True),
        ("applied", "rejected", True),
        ("screening", "interviewing", True),
        ("interviewing", "offer", True),
        ("interviewing", "screening", False),
        ("offer", "interviewing", False),
        ("screening", "screening", False),
        ("rejected", "interviewing", False),
        ("rejected", "rejected", False),
        ("offer", "rejected", True),
        ("withdrawn", "screening", False),
        (None, "screening", True),
    ],
)
def test_a_reply_only_moves_an_application_forward(current, to_status, moves):
    assert should_advance(current, to_status) is moves


# -------------------------------------------------------------- polling --


def test_a_reply_is_filed_read_and_recorded(conn, settings, application):
    report = poll_inbox(conn, settings, mailbox=FakeMailbox("interview"))

    assert (report.fetched, report.matched, report.unmatched, report.advanced) == (1, 1, 0, 1)
    assert status_of(conn, application) == "interviewing"

    events = applications.list_events(conn, application)
    assert [e["kind"] for e in events] == ["status_change", "email", "status_change"]
    assert events[1]["source"] == "email"
    assert "Senior Backend Engineer" in events[1]["note"]


def test_the_same_mail_is_never_recorded_twice(conn, settings, application):
    mailbox = FakeMailbox("interview")
    poll_inbox(conn, settings, mailbox=mailbox)
    again = poll_inbox(conn, settings, mailbox=mailbox)

    assert (again.skipped, again.matched, again.advanced) == (1, 0, 0)
    assert len(applications.list_events(conn, application)) == 3
    assert len(inbox.list_messages(conn)) == 1


def test_an_acknowledgement_is_logged_and_moves_nothing(conn, settings, application):
    report = poll_inbox(conn, settings, mailbox=FakeMailbox("acknowledgement"))

    assert report.matched == 1 and report.advanced == 0
    assert status_of(conn, application) == "applied"
    kinds = [e["kind"] for e in applications.list_events(conn, application)]
    assert kinds.count("email") == 1, "an acknowledgement still counts as a reply"


def test_an_acknowledgement_sets_the_first_reply_time(conn, settings, application):
    poll_inbox(conn, settings, mailbox=FakeMailbox("acknowledgement"))
    row = conn.execute(
        "SELECT first_reply_at FROM application_status WHERE application_id = ?", (application,)
    ).fetchone()
    assert row["first_reply_at"], "time-to-first-reply is measured from the email events"


def test_a_rejection_ends_the_application(conn, settings, application):
    poll_inbox(conn, settings, mailbox=FakeMailbox("rejection"))
    assert status_of(conn, application) == "rejected"


def test_mail_after_a_decision_is_kept_as_a_note_and_changes_nothing(conn, settings, application):
    poll_inbox(conn, settings, mailbox=FakeMailbox("rejection"))
    poll_inbox(conn, settings, mailbox=FakeMailbox("interview"))

    assert status_of(conn, application) == "rejected"
    assert len(inbox.list_messages(conn)) == 2


def test_a_newsletter_is_kept_unmatched_and_touches_no_application(conn, settings, application):
    report = poll_inbox(conn, settings, mailbox=FakeMailbox("newsletter"))

    assert (report.matched, report.unmatched) == (0, 1)
    assert len(applications.list_events(conn, application)) == 1
    unmatched = inbox.list_messages(conn, matched=False)
    assert len(unmatched) == 1 and unmatched[0]["application_id"] is None
    assert unmatched[0]["label"] == "unknown"


def test_what_was_read_is_kept_beside_the_message(conn, settings, application):
    poll_inbox(conn, settings, mailbox=FakeMailbox("rejection"))
    row = inbox.list_messages(conn)[0]

    assert row["application_id"] == application
    assert row["label"] == "rejected" and row["read_by"] == "rules"
    assert row["match_confidence"] and row["match_reason"]
    assert row["event_id"], "the status event it caused is linked back"
    assert "decided to move forward" in row["reason"]


def test_a_dry_run_reads_everything_and_records_nothing(conn, settings, application):
    report = poll_inbox(conn, settings, mailbox=FakeMailbox("interview"), write=False)

    assert (report.matched, report.advanced) == (1, 1)
    assert status_of(conn, application) == "applied"
    assert inbox.list_messages(conn) == []
    assert len(applications.list_events(conn, application)) == 1


def test_several_messages_are_read_in_one_pass(conn, settings, application):
    report = poll_inbox(
        conn, settings, mailbox=FakeMailbox("acknowledgement", "newsletter", "interview")
    )
    assert (report.fetched, report.matched, report.unmatched) == (3, 2, 1)
    assert status_of(conn, application) == "interviewing"


# ------------------------------------------------------------- the since --


def test_the_first_poll_reads_back_as_far_as_the_settings_say(conn, settings):
    mailbox = FakeMailbox()
    poll_inbox(conn, settings, mailbox=mailbox)
    assert mailbox.since and len(mailbox.since) == len("2026-09-01")
    assert mailbox.limit == settings.inbox_poll_limit


def test_a_later_poll_starts_where_the_last_one_ended(conn, settings, application):
    poll_inbox(conn, settings, mailbox=FakeMailbox("acknowledgement"))
    mailbox = FakeMailbox()
    poll_inbox(conn, settings, mailbox=mailbox)
    assert mailbox.since == "2026-09-21T09:14:02+00:00"


def test_an_explicit_since_wins(conn, settings, application):
    mailbox = FakeMailbox()
    poll_inbox(conn, settings, mailbox=mailbox, since="2026-01-01", limit=5)
    assert (mailbox.since, mailbox.limit) == ("2026-01-01", 5)


# ------------------------------------------------------------ not working --


def test_an_unconfigured_mailbox_is_reported_not_raised(conn, settings):
    report = poll_inbox(conn, settings)
    assert report.error and "JOBAGENT_IMAP_HOST" in report.error
    assert report.fetched == 0


def test_a_mailbox_that_will_not_open_is_reported(conn, settings):
    report = poll_inbox(conn, settings, mailbox=FakeMailbox(error=MailboxError("no route to host")))
    assert report.error == "no route to host"


def test_a_model_that_cannot_be_reached_is_a_note_not_a_failure(conn, tmp_path, application):
    """Without a model the rules still run; the report says the model was missing."""
    wants_model = Settings(data_dir=tmp_path, llm_backend="api", inbox_model_triage=True)
    report = poll_inbox(conn, wants_model, mailbox=FakeMailbox("rejection"))

    assert report.error is None
    assert report.matched == 1 and status_of(conn, application) == "rejected"
    assert any("rules only" in note for note in report.notes)


# --------------------------------------------------------------- attaching --


def test_a_message_can_be_filed_by_hand(conn, settings, application):
    poll_inbox(conn, settings, mailbox=FakeMailbox("newsletter"))
    row_id = inbox.list_messages(conn, matched=False)[0]["id"]

    done = attach_message(conn, row_id, application, to_status="screening", settings=settings)

    assert done["advanced"] is True
    assert status_of(conn, application) == "screening"
    assert done["message"]["application_id"] == application
    assert done["message"]["read_by"] == "manual"


def test_filing_a_message_without_a_status_records_the_reply_only(conn, settings, application):
    poll_inbox(conn, settings, mailbox=FakeMailbox("newsletter"))
    row_id = inbox.list_messages(conn, matched=False)[0]["id"]

    done = attach_message(conn, row_id, application, settings=settings)

    assert done["advanced"] is False
    assert status_of(conn, application) == "applied"
    kinds = [e["kind"] for e in applications.list_events(conn, application)]
    assert kinds.count("email") == 1


def test_filing_against_an_application_that_is_not_there_says_so(conn, settings, application):
    poll_inbox(conn, settings, mailbox=FakeMailbox("newsletter"))
    row_id = inbox.list_messages(conn, matched=False)[0]["id"]
    with pytest.raises(ValueError, match="no application"):
        attach_message(conn, row_id, 999, settings=settings)


def test_filing_a_message_that_is_not_there_says_so(conn, settings, application):
    with pytest.raises(ValueError, match="no message"):
        attach_message(conn, 404, application, settings=settings)


def test_an_unconfigured_inbox_never_reaches_the_mailbox(conn, settings):
    with pytest.raises(InboxNotConfigured):
        from jobagent.inbox.mailbox import open_mailbox

        open_mailbox(settings)
