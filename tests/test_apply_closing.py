"""A posting's own application deadline."""

from __future__ import annotations

from datetime import date

import pytest

from jobagent.apply.closing import deadline_in, deadline_passed

TODAY = date(2026, 9, 28)


@pytest.mark.parametrize(
    "text, closes",
    [
        ("EOE. Application Deadline: 9/27/2026 Back Apply", date(2026, 9, 27)),
        ("Apply by September 30, 2026.", date(2026, 9, 30)),
        ("Applications close on 27 Sept", date(2026, 9, 27)),
        ("Closing date: 2026-09-20", date(2026, 9, 20)),
        ("Applications accepted until Oct 5", date(2026, 10, 5)),
        ("Application Deadline: Friday, September 25", date(2026, 9, 25)),
        ("Apply by 1/5", date(2027, 1, 5)),
        ("Posted 9/1/2026. Start date 10/1.", None),
        ("No deadline here.", None),
    ],
)
def test_the_deadline_a_posting_names(text, closes):
    assert deadline_in(text, TODAY) == closes


def test_only_a_deadline_before_today_has_passed():
    assert deadline_passed("Application Deadline: 9/27/2026", TODAY) == (
        "the posting's application deadline (2026-09-27) has passed"
    )
    assert deadline_passed("Application Deadline: 9/28/2026", TODAY) is None
    assert deadline_passed("Nothing about closing.", TODAY) is None
