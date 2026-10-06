"""Reading the security code a board emails before it accepts an application."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from jobagent.inbox import codes
from jobagent.inbox.codes import code_in, find_code, wait_for_code
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


def test_the_newest_unused_code_is_taken():
    first = mail(message_id="a", at=PRESSED + timedelta(seconds=5))
    second = mail(
        message_id="b",
        at=PRESSED + timedelta(seconds=40),
        body=GREENHOUSE_BODY.replace("hWze7RPl", "Qp4rT9xZ"),
    )
    assert find_code([first, second], company="Anthropic", after=PRESSED) == ("Qp4rT9xZ", "b")


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
    assert got == "hWze7RPl" and box.calls == 3
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
