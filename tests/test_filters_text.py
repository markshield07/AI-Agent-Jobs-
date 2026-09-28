"""Source prefilters (title words, posting age) and the shared HTML-to-text step."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from jobagent.discovery.criteria import SearchCriteria
from jobagent.discovery.sources.filters import parse_when, title_matches, within_age
from jobagent.discovery.text import html_to_text

# ---------------------------------------------------------- title_matches --


def _criteria(*titles: str, **extra) -> SearchCriteria:
    return SearchCriteria(titles=list(titles), **extra)


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer",
        "Senior Software Engineer, Payments",
        "Engineer, Software (Remote)",
        "SOFTWARE engineer II",
        "Software-Engineer",
    ],
)
def test_title_matches_when_every_wanted_word_is_present(title):
    assert title_matches(title, _criteria("Software Engineer")) is True


@pytest.mark.parametrize(
    "title",
    ["Software Sales", "Engineer", "Software", "Softwareengineer", "Hardware Engineer", ""],
)
def test_title_does_not_match_when_a_wanted_word_is_missing(title):
    assert title_matches(title, _criteria("Software Engineer")) is False


def test_title_matches_any_of_several_wanted_titles():
    criteria = _criteria("Platform Engineer", "SRE")
    assert title_matches("Senior SRE", criteria) is True
    assert title_matches("Platform Engineer II", criteria) is True
    assert title_matches("Data Engineer", criteria) is False


def test_title_matches_everything_when_no_titles_are_configured():
    assert title_matches("Head of Legal", _criteria()) is True
    assert title_matches("Head of Legal", _criteria("  ", "")) is True


def test_title_matches_normalises_the_wanted_titles():
    assert title_matches("Data Engineer", _criteria("  DATA  engineer ")) is True


def test_title_matches_keeps_symbols_that_belong_to_language_names():
    assert title_matches("Senior C++ Developer", _criteria("C++ Developer")) is True
    assert title_matches("Senior C Developer", _criteria("C++ Developer")) is False
    assert title_matches("C# / .NET Engineer", _criteria("C# Engineer")) is True
    assert title_matches("Node.js Engineer", _criteria("node.js engineer")) is True


@pytest.mark.parametrize(
    "title, wanted",
    [
        ("Software Engineer.", "Software Engineer"),
        ("Sr Engineer", "Sr. Engineer"),
        ("Sr. Engineer", "Sr Engineer"),
        (".NET Engineer", "net engineer"),
        ("Engineer (Node.js).", "node.js engineer"),
        ("Engineer...", "engineer"),
    ],
)
def test_title_matches_ignores_dots_at_the_edges_of_words(title, wanted):
    assert title_matches(title, _criteria(wanted)) is True


def test_title_matches_does_not_turn_a_bare_dot_into_a_word():
    assert title_matches("Software . Engineer", _criteria("Software Engineer")) is True
    assert title_matches("...", _criteria("Engineer")) is False


def test_title_matches_treats_a_missing_title_as_empty():
    assert title_matches(None, _criteria("Engineer")) is False  # type: ignore[arg-type]
    assert title_matches(None, _criteria()) is True  # type: ignore[arg-type]


# ------------------------------------------------------------- parse_when --


def test_parse_when_iso_with_z_is_utc():
    assert parse_when("2024-05-01T12:30:00Z") == datetime(2024, 5, 1, 12, 30, tzinfo=UTC)


def test_parse_when_iso_with_an_offset_keeps_the_instant():
    parsed = parse_when("2024-05-01T14:30:00+02:00")
    assert parsed == datetime(2024, 5, 1, 12, 30, tzinfo=UTC)
    assert parsed.utcoffset() == timedelta(hours=2)


def test_parse_when_naive_iso_is_taken_as_utc():
    assert parse_when("2024-05-01T12:30:00") == datetime(2024, 5, 1, 12, 30, tzinfo=UTC)
    assert parse_when("2024-05-01") == datetime(2024, 5, 1, tzinfo=UTC)
    assert parse_when(" 2024-05-01 12:30:00 ") == datetime(2024, 5, 1, 12, 30, tzinfo=UTC)


def test_parse_when_iso_with_fractional_seconds():
    assert parse_when("2024-05-01T12:30:00.250Z") == datetime(
        2024, 5, 1, 12, 30, 0, 250_000, tzinfo=UTC
    )


def test_parse_when_epoch_seconds_and_milliseconds():
    expected = datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)
    assert parse_when(1_700_000_000) == expected
    assert parse_when(1_700_000_000.0) == expected
    assert parse_when(1_700_000_000_000) == expected
    assert parse_when(1_700_000_000_500) == expected.replace(microsecond=500_000)


@pytest.mark.parametrize(
    "garbage", ["yesterday", "2024-13-45", "1700000000abc", "Posted 3 days ago", "NaT"]
)
def test_parse_when_garbage_strings_are_none(garbage):
    assert parse_when(garbage) is None


@pytest.mark.parametrize("empty", [None, ""])
def test_parse_when_empty_is_none(empty):
    assert parse_when(empty) is None


@pytest.mark.parametrize("number", [float("nan"), 1e20, -1e18])
def test_parse_when_numbers_that_are_not_dates_are_none(number):
    assert parse_when(number) is None


# ------------------------------------------------------------- within_age --

NOW = datetime(2024, 6, 1, 12, 0, tzinfo=UTC)


def test_within_age_boundaries():
    criteria = _criteria(max_age_hours=72)
    limit = NOW - timedelta(hours=72)

    assert within_age(limit.isoformat(), criteria, now=NOW) is True
    assert within_age((limit + timedelta(seconds=1)).isoformat(), criteria, now=NOW) is True
    assert within_age((limit - timedelta(seconds=1)).isoformat(), criteria, now=NOW) is False
    assert within_age(NOW.isoformat(), criteria, now=NOW) is True
    assert within_age((NOW + timedelta(days=1)).isoformat(), criteria, now=NOW) is True


def test_within_age_respects_the_configured_window():
    posted = (NOW - timedelta(hours=30)).isoformat()
    assert within_age(posted, _criteria(max_age_hours=24), now=NOW) is False
    assert within_age(posted, _criteria(max_age_hours=48), now=NOW) is True


def test_within_age_compares_instants_across_offsets():
    posted = (NOW - timedelta(hours=73)).astimezone(timezone(timedelta(hours=5)))
    assert within_age(posted.isoformat(), _criteria(max_age_hours=72), now=NOW) is False
    assert within_age(int(posted.timestamp()) * 1000, _criteria(max_age_hours=72), now=NOW) is False


def test_within_age_accepts_epoch_values():
    posted = NOW - timedelta(hours=1)
    assert within_age(int(posted.timestamp()), _criteria(max_age_hours=2), now=NOW) is True
    assert within_age(int(posted.timestamp()), _criteria(max_age_hours=1), now=NOW) is True


@pytest.mark.parametrize("unknown", [None, "", "a while ago", float("nan")])
def test_within_age_lets_unknown_dates_through(unknown):
    assert within_age(unknown, _criteria(max_age_hours=1), now=NOW) is True


def test_within_age_defaults_now_to_the_current_time():
    fresh = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    stale = (datetime.now(UTC) - timedelta(days=30)).isoformat()
    assert within_age(fresh, _criteria(max_age_hours=1)) is True
    assert within_age(stale, _criteria(max_age_hours=1)) is False


# ----------------------------------------------------------- html_to_text --


@pytest.mark.parametrize("empty", [None, "", "   \n\t "])
def test_html_to_text_empty_input(empty):
    assert html_to_text(empty) == ""


def test_html_to_text_unescapes_greenhouse_style_content():
    escaped = "&lt;p&gt;We use &amp;amp; love Python&lt;/p&gt;&lt;p&gt;Remote&lt;/p&gt;"
    assert html_to_text(escaped) == "We use & love Python\n\nRemote"


def test_html_to_text_unescapes_plain_text_too():
    text = "Fish &amp; chips &gt; pizza&nbsp;&nbsp;always"
    assert html_to_text(text) == "Fish & chips > pizza always"


def test_html_to_text_breaks_on_block_elements_but_not_inline_ones():
    markup = (
        "<h2>About</h2><p>We build <b>fast</b> <em>systems.</em></p>"
        "<ul><li>Python</li><li>Go</li></ul><div>Apply <a href='#'>here</a></div>"
    )
    assert html_to_text(markup) == "About\n\nWe build fast systems.\n\nPython\n\nGo\n\nApply here"


def test_html_to_text_keeps_punctuation_attached_after_inline_tags():
    assert html_to_text("<p>We build <em>systems</em>.</p>") == "We build systems."
    assert html_to_text("<p>Apply <a href='#'>here</a>, now.</p>") == "Apply here, now."
    assert html_to_text("<li>Know <b>Node.js</b>.</li>") == "Know Node.js."
    assert html_to_text("<p>Hello<span>world</span></p>") == "Helloworld"


def test_html_to_text_separates_table_cells_with_a_space_and_rows_with_a_break():
    markup = (
        "<table><tr><th>Salary</th><th>Remote</th></tr><tr><td>100k</td><td>Yes</td></tr></table>"
    )
    assert html_to_text(markup) == "Salary Remote\n\n100k Yes"


def test_html_to_text_treats_br_as_a_break():
    assert html_to_text("line one<br>line two<br/>line three") == (
        "line one\n\nline two\n\nline three"
    )


def test_html_to_text_removes_scripts_styles_and_noscript():
    markup = (
        "<div>Intro</div><script>window.x = 1;</script><style>p { color: red }</style>"
        "<noscript>Enable JS</noscript><p>Body</p>"
    )
    assert html_to_text(markup) == "Intro\n\nBody"


def test_html_to_text_collapses_whitespace():
    markup = "<p>Too    many\t\tspaces</p>\n\n\n\n<p>and&nbsp;a&nbsp;non-breaking one</p>"
    assert html_to_text(markup) == "Too many spaces\n\nand a non-breaking one"

    plain = "  first  \n\n\n\n   second\t line \n\n"
    assert html_to_text(plain) == "first\n\nsecond line"


def test_html_to_text_keeps_at_most_one_blank_line_between_blocks():
    markup = "<p>a</p><p></p><p></p><div>   </div><p>b</p>"
    assert html_to_text(markup) == "a\n\nb"
