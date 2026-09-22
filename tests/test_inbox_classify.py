"""Reading what a reply says.

The rules are asked to be right about the mail an applicant tracking system
actually sends, and to abstain everywhere else. The tests that matter are the
abstentions: a message that thanks the candidate is not a rejection, and a
message that could be read two ways is not read at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobagent.inbox.classify import ReplyReading, read_by_rules, read_message, score_labels
from jobagent.inbox.mailbox import parse_message
from jobagent.inbox.models import InboxMessage
from jobagent.llm.backend import Completion, LLMError

EMAILS = Path(__file__).parent / "fixtures" / "emails"


def message(subject: str = "", body: str = "") -> InboxMessage:
    return InboxMessage(message_id="m1", subject=subject, body=body)


def fixture(name: str) -> InboxMessage:
    return parse_message((EMAILS / f"{name}.eml").read_bytes())


class FakeCompleter:
    """A model that answers whatever it was told to, and counts the asking."""

    name = "fake"

    def __init__(self, reading: ReplyReading | None = None, error: Exception | None = None):
        self.reading, self.error, self.calls = reading, error, 0

    def complete(self, *, system, prompt, output, **kwargs) -> Completion:
        self.calls += 1
        self.prompt = prompt
        if self.error:
            raise self.error
        return Completion(result=self.reading, input_tokens=10, output_tokens=5, backend="fake")


# ------------------------------------------------------------- the rules --


@pytest.mark.parametrize(
    ("name", "label"),
    [
        ("acknowledgement", "acknowledgement"),
        ("rejection", "rejected"),
        ("interview", "interviewing"),
        ("screening_html", "screening"),
    ],
)
def test_the_mail_an_ats_actually_sends_is_read_correctly(name, label):
    reading = read_by_rules(fixture(name))
    assert reading.label == label
    assert reading.confidence >= 0.6
    assert reading.reason.startswith("said ")


def test_an_offer_is_read_as_an_offer():
    reading = read_by_rules(
        message("Offer from Acme Robotics", "We are pleased to offer you the position.")
    )
    assert reading.label == "offer" and reading.status == "offer"


def test_a_withdrawal_is_read_as_one():
    reading = read_by_rules(message("Application withdrawn", "Your application has been withdrawn"))
    assert reading.label == "withdrawn"


def test_a_newsletter_is_not_read_as_anything():
    reading = read_by_rules(fixture("newsletter"))
    assert reading.label == "unknown" and reading.status is None


def test_unfortunately_on_its_own_is_not_a_rejection():
    reading = read_by_rules(
        message(
            "Your interview",
            "Unfortunately Dana is travelling, so we will start with the technical interview "
            "on Thursday instead.",
        )
    )
    assert reading.label != "rejected"


def test_thanking_the_candidate_is_an_acknowledgement_not_a_decision():
    reading = read_by_rules(
        message("Thanks!", "Thank you for applying. We will be in touch if there is a fit.")
    )
    assert reading.label == "acknowledgement" and reading.status is None


def test_a_rejection_that_opens_with_thanks_is_still_a_rejection():
    reading = read_by_rules(
        message(
            "Update",
            "Thank you for your interest. We have decided to move forward with other candidates.",
        )
    )
    assert reading.label == "rejected"


def test_a_message_that_says_two_contradictory_things_is_not_read():
    reading = read_by_rules(
        message(
            "Update",
            "We will not be moving forward with this role, but we would like to schedule an "
            "interview for a different one.",
        )
    )
    assert reading.label == "unknown"
    assert "says both" in reading.reason


def test_every_matched_phrase_adds_to_the_score():
    one = score_labels(message("Update", "we regret to inform you"))["rejected"][0]
    two = score_labels(
        message("Update", "we regret to inform you that we will not be moving forward")
    )["rejected"][0]
    assert two > one


def test_the_words_that_decided_it_are_kept():
    reading = read_by_rules(message("Update", "we have decided not to move forward at this time"))
    assert "decided not to move forward" in reading.reason


# ------------------------------------------------------------- the model --


def test_the_model_is_not_asked_when_the_rules_are_sure():
    completer = FakeCompleter(ReplyReading(label="offer", confidence=1.0, reason="never asked"))
    reading = read_message(fixture("rejection"), completer=completer)
    assert completer.calls == 0
    assert reading.label == "rejected"


def test_the_model_reads_what_the_rules_could_not():
    completer = FakeCompleter(
        ReplyReading(label="interviewing", confidence=0.9, reason="asks for times")
    )
    reading = read_message(
        message("Thursday?", "Are Thursday or Friday any good for the loop?"), completer=completer
    )
    assert completer.calls == 1
    assert reading.label == "interviewing" and reading.source == "model"
    assert reading.status == "interviewing"


def test_the_model_sees_the_sender_the_subject_and_the_body():
    completer = FakeCompleter(ReplyReading(label="unknown", confidence=0.0, reason=""))
    read_message(fixture("no_message_id"), completer=completer)
    assert "hello@acmerobotics.com" in completer.prompt
    assert "Senior Backend Engineer at Acme Robotics" in completer.prompt
    assert "no Message-ID header" in completer.prompt


def test_a_model_that_is_unsure_labels_nothing_but_says_what_it_thought():
    completer = FakeCompleter(ReplyReading(label="offer", confidence=0.2, reason="a guess"))
    reading = read_message(message("Hello", "Just checking in."), completer=completer)
    assert reading.label == "unknown" and reading.status is None
    assert "closest is offer" in reading.reason


def test_a_model_that_fails_is_not_an_error():
    completer = FakeCompleter(error=LLMError("no backend today"))
    reading = read_message(message("Hello", "Just checking in."), completer=completer)
    assert reading.label == "unknown"


def test_without_a_model_the_rules_answer_alone():
    reading = read_message(message("Hello", "Just checking in."), completer=None)
    assert reading.label == "unknown" and reading.source == "none"


def test_a_rules_reading_under_the_bar_goes_to_the_model():
    completer = FakeCompleter(
        ReplyReading(label="rejected", confidence=0.95, reason="says the role is filled")
    )
    reading = read_message(
        message("Update", "The position has been filled."), completer=completer, min_confidence=0.6
    )
    assert completer.calls == 1 and reading.label == "rejected" and reading.source == "model"
