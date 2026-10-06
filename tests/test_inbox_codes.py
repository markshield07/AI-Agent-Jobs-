"""Reading the security code a board emails before it accepts an application."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobagent.inbox import codes
from jobagent.inbox.codes import EmailCodes, code_in, find_code, wait_for_code
from jobagent.inbox.models import InboxMessage

PRESSED = datetime(2026, 10, 6, 20, 21, 30, tzinfo=UTC)
GREENHOUSE_BODY = (
    "Hi Mark, Copy and paste this code into the security code field on your "
    "application: hWze7RPl After you enter the code, resubmit your application. "
    "Thanks, Anthropic"
)


def mail(
    *,
    subject="Security code for your application to Anthropic",
    body=GREENHOUSE_BODY,
    at=PRESSED + timedelta(seconds=20),
    message_id="m1",
) -> InboxMessage:
    return InboxMessage(
        message_id=message_id,
        subject=subject,
        from_addr="no-reply@us.greenhouse-mail.io",
        from_name="Anthropic",
        to_addr="mark@example.com",
        received_at=at.isoformat(timespec="seconds"),
        body=body,
    )


@pytest.fixture(autouse=True)
def fresh():
    codes._USED.clear()
    yield
    codes._USED.clear()


def test_the_greenhouse_code_is_read():
    assert code_in(mail(), "Anthropic") == "hWze7RPl"


@pytest.mark.parametrize(
    ("subject", "body", "company", "expected"),
    [
        ("Your verification code", "Your code is 482913.", "", "482913"),
        (
            "Security code for your application to Anthropic",
            GREENHOUSE_BODY,
            "Anthropic, PBC",
            "hWze7RPl",
        ),
        ("Security code for your application to Netflix", GREENHOUSE_BODY, "Anthropic", None),
        ("Thanks for applying to Anthropic", GREENHOUSE_BODY, "Anthropic", None),
        ("Security code", "Use the code: Thanks for applying", "", None),
    ],
)
def test_only_a_code_mail_for_the_company_gives_a_code(subject, body, company, expected):
    assert code_in(mail(subject=subject, body=body), company) == expected


def test_a_code_mailed_before_the_press_is_not_this_ones():
    old = mail(at=PRESSED - timedelta(minutes=3), message_id="old")
    assert find_code([old], company="Anthropic", after=PRESSED) is None


def test_the_first_new_code_is_taken_unless_another_names_the_job():
    first = mail(message_id="a", at=PRESSED + timedelta(seconds=5))
    second = mail(
        message_id="b",
        at=PRESSED + timedelta(seconds=40),
        body=GREENHOUSE_BODY.replace("hWze7RPl", "Qp4rT9xZ") + " DC Operations Lead",
    )
    assert find_code([first, second], company="Anthropic", after=PRESSED) == ("hWze7RPl", "a")
    assert find_code(
        [first, second], company="Anthropic", after=PRESSED, title="DC Operations Lead"
    ) == ("Qp4rT9xZ", "b")
    assert find_code([first, second], company="Anthropic", after=PRESSED, skip={"a"}) == (
        "Qp4rT9xZ",
        "b",
    )


class FakeMailbox:
    name = "fake"

    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = 0

    def fetch(self, *, since=None, limit=200):
        self.calls += 1
        return self.batches.pop(0) if self.batches else []


def test_the_mailbox_is_polled_until_the_code_arrives_and_used_once():
    box = FakeMailbox([[], [], [mail()]])
    ticks = iter(range(0, 1000, 10))
    got = wait_for_code(
        box,
        company="Anthropic",
        after=PRESSED,
        timeout_s=240,
        sleep=lambda s: None,
        clock=lambda: next(ticks),
    )
    assert got == ("hWze7RPl", "m1") and box.calls == 3
    again = FakeMailbox([[mail()]])
    assert (
        wait_for_code(again, company="Anthropic", after=PRESSED, timeout_s=0, sleep=lambda s: None)
        is None
    ), "one mail, one application"


def test_no_code_in_time_is_none():
    ticks = iter(range(0, 1000, 100))
    got = wait_for_code(
        FakeMailbox([]),
        after=PRESSED,
        timeout_s=240,
        sleep=lambda s: None,
        clock=lambda: next(ticks),
    )
    assert got is None


def test_a_code_mail_already_there_at_the_press_is_not_this_ones(tmp_path):
    """The previous application's mail lands seconds before this press."""
    previous = mail(message_id="prev", at=PRESSED - timedelta(seconds=10))
    mine = mail(
        message_id="mine",
        at=PRESSED + timedelta(seconds=30),
        body=GREENHOUSE_BODY.replace("hWze7RPl", "Mine2345"),
    )

    class Inbox:
        name = "fake"
        arrived = [previous]

        def fetch(self, *, since=None, limit=200):
            return list(self.arrived)

    inbox = Inbox()
    codes_for_job = EmailCodes(inbox, company="Anthropic", timeout_s=0, sleep=lambda s: None)
    codes_for_job.mark()
    inbox.arrived = [previous, mine]
    assert codes_for_job(PRESSED) == "Mine2345"


def test_a_used_code_is_remembered_by_the_next_command(tmp_path):
    used = tmp_path / "used.json"
    one = EmailCodes(FakeMailbox([[mail()]]), company="Anthropic", used_path=used, timeout_s=0)
    assert one(PRESSED) == "hWze7RPl"
    codes._USED.clear()  # a new process
    two = EmailCodes(FakeMailbox([[mail()]]), company="Anthropic", used_path=used, timeout_s=0)
    assert two(PRESSED) is None
