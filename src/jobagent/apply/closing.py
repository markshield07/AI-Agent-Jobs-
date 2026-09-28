"""Whether a posting's own application deadline has passed.

Some postings end with the date applications close ("Application Deadline:
9/27/2026"). A job queued before that date and applied to after it would be
turned down unread, or, as on ADP, go no further than a talent-community
sign-up, so it is skipped with the reason. Only a date the posting names as
its deadline counts; a posting date or a start date says nothing about it.
"""

from __future__ import annotations

import re
from datetime import date

_MONTHS = {
    name: number
    for number, names in enumerate(
        (
            ("jan", "january"),
            ("feb", "february"),
            ("mar", "march"),
            ("apr", "april"),
            ("may",),
            ("jun", "june"),
            ("jul", "july"),
            ("aug", "august"),
            ("sep", "sept", "september"),
            ("oct", "october"),
            ("nov", "november"),
            ("dec", "december"),
        ),
        start=1,
    )
    for name in names
}
_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_DATE = (
    r"(?P<date>\d{4}-\d{1,2}-\d{1,2}"
    r"|\d{1,2}/\d{1,2}(?:/\d{2,4})?"
    rf"|{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?,?(?:\s+\d{{4}})?"
    rf"|\d{{1,2}}(?:st|nd|rd|th)?\s+{_MONTH},?(?:\s+\d{{4}})?)"
)
_DEADLINE = re.compile(
    r"\b(?:application\s+deadline|deadline\s+(?:to|for)\s+appl(?:y|ications?)"
    r"|apply\s+(?:by|before)|applications?\s+(?:will\s+be\s+)?"
    r"(?:close[sd]?|closing|due|accepted\s+(?:until|through))(?:\s+(?:on|date))?"
    r"|closing\s+date|posting\s+(?:end|close|closing)s?(?:\s+date)?)"
    rf"\s*(?:is|on|:|-|–)?\s*(?:\w+day,?\s+)?{_DATE}",
    re.IGNORECASE,
)


def deadline_in(text: str, today: date) -> date | None:
    """The deadline the text names, or None. A date without a year is taken
    in whichever year puts it nearest `today`."""
    match = _DEADLINE.search(text or "")
    if not match:
        return None
    return _parse(match.group("date"), today)


def deadline_passed(text: str, today: date) -> str | None:
    """Why the posting is closed, or None when it names no deadline or one still to come."""
    closes = deadline_in(text, today)
    if closes is None or closes >= today:
        return None
    return f"the posting's application deadline ({closes.isoformat()}) has passed"


def _parse(text: str, today: date) -> date | None:
    text = text.strip().lower().rstrip(".,")
    year: int | None = None
    try:
        if re.fullmatch(r"\d{4}-\d{1,2}-\d{1,2}", text):
            y, m, d = (int(x) for x in text.split("-"))
            return date(y, m, d)
        if "/" in text:
            parts = [int(x) for x in text.split("/")]
            month, day = parts[0], parts[1]
            if month > 12 >= day:  # 27/9: the day first
                month, day = day, month
            if len(parts) == 3:
                year = parts[2] + (2000 if parts[2] < 100 else 0)
        else:
            words = re.findall(r"[a-z]+|\d+", text)
            month = next(_MONTHS[w[:3]] for w in words if w.isalpha() and w[:3] in _MONTHS)
            numbers = [int(w) for w in words if w.isdigit()]
            day = next(n for n in numbers if n <= 31)
            year = next((n for n in numbers if n > 31), None)
        if year is not None:
            return date(year, month, day)
        candidates = [date(today.year + delta, month, day) for delta in (-1, 0, 1)]
    except (ValueError, StopIteration):
        return None
    return min(candidates, key=lambda d: abs((d - today).days))
