"""The guardrail: every reason a plan may not be rendered.

The model may select, reorder and rephrase facts; it may never add one. This
module is what makes that a rule rather than a hope. It is deterministic and
makes no model call: given the plan and the fact base it returns every `Issue`
that makes the plan unshippable, and an empty list means the plan may be
rendered.

Two families of rules. STRUCTURE rules check the ids: every cited fact exists
and is active, has the kind its section expects, entries do not mix employers,
nothing is cited twice. CONTENT rules check the words: a bullet is compared
against the facts of its own entry, the summary and a cover letter against
every active fact. A never-claim term anywhere, a number the cited fact does
not state, or a name-like term (a technology, an acronym, a capitalised word,
a posting keyword) the facts do not contain each produce one issue. Term
matching goes through `terms.contains_term` everywhere, so "postgres" is not
supported by a fact that says "PostgreSQL": a rephrasing must keep the fact's
own names.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from jobagent.resume.facts import Fact
from jobagent.tailor.models import CoverLetter, Entry, Issue, TailoredResume
from jobagent.tailor.terms import (
    STOPWORDS,
    contains_term,
    find_terms,
    keyword_like,
    numbers_in,
    tokens,
)

MAX_BULLET_CHARS = 300
MAX_SUMMARY_CHARS = 600
MAX_PARAGRAPH_CHARS = 900
MIN_PARAGRAPHS = 2
MAX_PARAGRAPHS = 6

UNKNOWN_FACT = "unknown fact"
INACTIVE_FACT = "inactive fact"
NEVER_CLAIM = "on the never-claim list"
NUMBER_NOT_IN_FACT = "number not in the fact"
TERM_NOT_IN_CITED_FACT = "term not supported by the cited fact"
TERM_NOT_IN_ANY_FACT = "term not supported by any fact"

# Dates are rendered from the facts, but a bullet may still say "since March
# 2019" or "to present" without any fact spelling the month out.
_DATE_WORDS: frozenset[str] = frozenset(
    """
    january february march april may june july august september october november december
    jan feb mar apr jun jul aug sep sept oct nov dec present current
    """.split()
)

# A letter opens and closes with words no fact could contain.
_LETTER_WORDS: frozenset[str] = frozenset(
    "dear hiring manager recruiter sincerely regards thank thanks best".split()
)

# `terms.numbers_in` refuses a "." right after a number, so "latency 40%." reads
# as "40" and "saving $1.2M." as nothing at all. A space before a full stop that
# follows a number (or its %, +, k, M, B suffix) but not a digit puts them back.
_GLUED_STOP = re.compile(r"([\d%+kKmMbB])\.(?!\d)")


class _Report:
    """Collects issues without repeating one: a term is reported once per place."""

    def __init__(self) -> None:
        self.items: list[Issue] = []
        self._seen: set[tuple[Any, ...]] = set()

    def add(
        self, where: str, message: str, *, fact_id: int | None = None, term: str | None = None
    ) -> None:
        key = (where, term.lower()) if term else (where, message, fact_id)
        if key in self._seen:
            return
        self._seen.add(key)
        self.items.append(Issue(where=where, message=message, fact_id=fact_id, term=term))


# ------------------------------------------------------------------- pool --


def fact_pool_text(facts: Iterable[Fact]) -> str:
    """Everything the facts say, one string: text, tags and every detail value.

    This is the text a claim is checked against with `contains_term`.
    """
    lines: list[str] = []
    for fact in facts:
        lines.append(fact.text)
        lines.extend(fact.tags)
        lines.extend(_detail_values(fact.detail))
    return "\n".join(line for line in lines if line and line.strip())


def _detail_values(value: Any) -> list[str]:
    """Every scalar in a detail dict as text, however it is nested."""
    if value is None or isinstance(value, bool):
        return []
    if isinstance(value, str):
        return [value.strip()]
    if isinstance(value, (int, float)):
        return [str(value)]
    if isinstance(value, Mapping):
        return [text for item in value.values() for text in _detail_values(item)]
    if isinstance(value, (list, tuple, set)):
        return [text for item in value for text in _detail_values(item)]
    return [str(value)]


# ----------------------------------------------------------------- resume --


def validate_resume(
    plan: TailoredResume,
    facts: Mapping[int, Fact],
    never_claim: Iterable[str],
    posting_terms: Iterable[str] = (),
) -> list[Issue]:
    """Every issue that makes `plan` unshippable, in page order; [] means render it.

    `facts` maps id -> Fact for every fact, active or not: an inactive fact is
    reported as such rather than as unknown. `never_claim` are the user's
    forbidden terms and `posting_terms` the posting's keywords, the terms the
    model is most tempted to lift.
    """
    report = _Report()
    forbidden = _distinct(never_claim)
    lifted = _posting_terms(posting_terms)
    active_pool = fact_pool_text(fact for fact in facts.values() if fact.active)

    summary = plan.summary.strip()
    if len(summary) > MAX_SUMMARY_CHARS:
        report.add("summary", f"summary longer than {MAX_SUMMARY_CHARS} characters")
    if summary:
        _check_text(
            report,
            "summary",
            summary,
            allowed=active_pool,
            numbers=_numbers(active_pool),
            words=_DATE_WORDS,
            forbidden=forbidden,
            lifted=lifted,
            unsupported=TERM_NOT_IN_ANY_FACT,
        )

    _check_id_list(report, "skills", plan.skills, "skill", facts)

    used: dict[int, str] = {}
    for index, entry in enumerate(plan.experience):
        _check_entry(report, f"experience[{index}]", entry, "role", facts, used, forbidden, lifted)
    for index, entry in enumerate(plan.projects):
        _check_entry(report, f"projects[{index}]", entry, "project", facts, used, forbidden, lifted)

    _check_id_list(report, "education", plan.education, "education", facts)
    _check_id_list(report, "credentials", plan.credentials, "credential", facts)
    return report.items


def _lookup(report: _Report, where: str, fact_id: int, facts: Mapping[int, Fact]) -> Fact | None:
    """S1: the fact behind an id, reporting an unknown or inactive one."""
    fact = facts.get(fact_id)
    if fact is None:
        report.add(where, UNKNOWN_FACT, fact_id=fact_id)
        return None
    if not fact.active:
        report.add(where, INACTIVE_FACT, fact_id=fact_id)
    return fact


def _check_id_list(
    report: _Report, section: str, ids: list[int], kind: str, facts: Mapping[int, Fact]
) -> None:
    seen: set[int] = set()
    for index, fact_id in enumerate(ids):
        where = f"{section}[{index}]"
        if fact_id in seen:
            report.add(where, "fact repeated", fact_id=fact_id)
        seen.add(fact_id)
        fact = _lookup(report, where, fact_id, facts)
        if fact is not None and fact.kind != kind:
            report.add(where, f"expected a {kind} fact, got {fact.kind}", fact_id=fact_id)


def _check_entry(
    report: _Report,
    where: str,
    entry: Entry,
    kind: str,
    facts: Mapping[int, Fact],
    used: dict[int, str],
    forbidden: list[str],
    lifted: list[str],
) -> None:
    if not entry.fact_ids:
        report.add(where, "entry has no facts")

    members: dict[int, Fact] = {}
    for fact_id in entry.fact_ids:
        if fact_id in members:
            report.add(where, "fact repeated", fact_id=fact_id)
        elif fact_id in used:
            report.add(where, f"fact already used in {used[fact_id]}", fact_id=fact_id)
        else:
            used[fact_id] = where
        fact = _lookup(report, where, fact_id, facts)
        if fact is None:
            continue
        if fact.kind != kind:
            report.add(where, f"expected a {kind} fact, got {fact.kind}", fact_id=fact_id)
        members[fact_id] = fact

    _check_heading(report, where, list(members.values()))

    if not entry.bullets:
        report.add(where, "entry has no bullets")

    entry_pool = fact_pool_text(members.values())
    words = _DATE_WORDS | _heading_words(members.values())
    for index, bullet in enumerate(entry.bullets):
        bullet_where = f"{where}.bullets[{index}]"
        text = bullet.text.strip()
        if not text:
            report.add(bullet_where, "blank bullet")
            continue
        if len(text) > MAX_BULLET_CHARS:
            report.add(bullet_where, f"bullet longer than {MAX_BULLET_CHARS} characters")

        cited = members.get(bullet.fact_id)
        allowed = entry_pool
        if bullet.fact_id not in entry.fact_ids:
            report.add(
                bullet_where, "bullet cites a fact outside its entry", fact_id=bullet.fact_id
            )
            cited = _lookup(report, bullet_where, bullet.fact_id, facts)
            if cited is not None:
                allowed = fact_pool_text([*members.values(), cited])
        if cited is None:
            continue
        _check_text(
            report,
            bullet_where,
            text,
            allowed=allowed,
            numbers=_numbers(fact_pool_text([cited])),
            words=words,
            forbidden=forbidden,
            lifted=lifted,
            unsupported=TERM_NOT_IN_CITED_FACT,
        )


def _check_heading(report: _Report, where: str, members: list[Fact]) -> None:
    """S3: the facts of one entry describe one employer and one title."""
    if not members:
        return
    employers = {_norm(fact.detail.get("employer")) for fact in members}
    titles = {_norm(fact.detail.get("title")) for fact in members}
    if len(employers) > 1:
        report.add(where, "entry mixes facts from different employers")
    if len(titles) > 1:
        report.add(where, "entry mixes facts with different titles")
    if employers == {""} and titles == {""} and len(members) > 1:
        report.add(where, "facts without an employer or title cannot share an entry")


def _heading_words(members: Iterable[Fact]) -> frozenset[str]:
    words: set[str] = set()
    for fact in members:
        for key in ("employer", "title"):
            words.update(token.lower() for token in tokens(_norm(fact.detail.get(key))))
    return frozenset(words)


# ----------------------------------------------------------- cover letter --


def validate_cover_letter(
    letter: CoverLetter,
    facts: Mapping[int, Fact],
    never_claim: Iterable[str],
    posting_terms: Iterable[str] = (),
    allow: Iterable[str] = (),
) -> list[Issue]:
    """Every issue that makes `letter` unshippable; [] means it may be rendered.

    A letter is checked against every active fact plus the strings in `allow`:
    the company, the job title and the candidate's name, which a letter must
    be able to say although no fact states them.
    """
    report = _Report()
    forbidden = _distinct(never_claim)
    lifted = _posting_terms(posting_terms)
    pool = fact_pool_text(fact for fact in facts.values() if fact.active)
    allowed = "\n".join([pool, *_distinct(allow)])
    numbers = _numbers(allowed)
    words = _DATE_WORDS | _LETTER_WORDS

    count = len(letter.paragraphs)
    if not MIN_PARAGRAPHS <= count <= MAX_PARAGRAPHS:
        report.add(
            "cover_letter",
            f"cover letter needs {MIN_PARAGRAPHS} to {MAX_PARAGRAPHS} paragraphs, got {count}",
        )
    for index, paragraph in enumerate(letter.paragraphs):
        where = f"cover_letter[{index}]"
        text = paragraph.strip()
        if not text:
            report.add(where, "blank paragraph")
            continue
        if len(text) > MAX_PARAGRAPH_CHARS:
            report.add(where, f"paragraph longer than {MAX_PARAGRAPH_CHARS} characters")
        _check_text(
            report,
            where,
            text,
            allowed=allowed,
            numbers=numbers,
            words=words,
            forbidden=forbidden,
            lifted=lifted,
            unsupported=TERM_NOT_IN_ANY_FACT,
        )
    return report.items


def validate_text(
    text: str,
    facts: Mapping[int, Fact],
    never_claim: Iterable[str],
    *,
    allow: Iterable[str] = (),
    where: str = "answer",
) -> list[Issue]:
    """C1 to C3 on one piece of free text, such as an answer to a form question.

    Checked the way a cover letter paragraph is: against every active fact plus
    the strings in `allow`. [] means every term and number in it is on file.
    """
    report = _Report()
    pool = fact_pool_text(fact for fact in facts.values() if fact.active)
    allowed = "\n".join([pool, *_distinct(allow)])
    _check_text(
        report,
        where,
        text.strip(),
        allowed=allowed,
        numbers=_numbers(allowed),
        words=_DATE_WORDS | _LETTER_WORDS,
        forbidden=_distinct(never_claim),
        lifted=[],
        unsupported=TERM_NOT_IN_ANY_FACT,
    )
    return report.items


# ---------------------------------------------------------------- content --


def _check_text(
    report: _Report,
    where: str,
    text: str,
    *,
    allowed: str,
    numbers: set[str],
    words: frozenset[str],
    forbidden: list[str],
    lifted: list[str],
    unsupported: str,
) -> None:
    """C1, C2 and C3 on one piece of text.

    `allowed` is the text a term must appear in, `numbers` the numbers it may
    use and `words` the terms allowed regardless. The never-claim check runs
    first so a forbidden term is reported as forbidden, not merely unsupported.
    """
    for term in forbidden:
        if contains_term(text, term):
            report.add(where, NEVER_CLAIM, term=term)

    for number in sorted(_numbers(text)):
        if number not in numbers:
            report.add(where, NUMBER_NOT_IN_FACT, term=number)

    for term in _distinct([*keyword_like(text), *find_terms(text, lifted)]):
        # A bare number is C2's business; reporting it here too would say the
        # same thing twice about "40%".
        if term in words or term in _numbers(term):
            continue
        if not contains_term(allowed, term):
            report.add(where, unsupported, term=term)


# ---------------------------------------------------------------- helpers --


def _numbers(text: str) -> set[str]:
    """`numbers_in`, with a number that ends a sentence keeping its suffix."""
    return numbers_in(_GLUED_STOP.sub(r"\1 .", text))


def _norm(value: Any) -> str:
    return "" if value is None else str(value).strip().lower()


def _distinct(values: Iterable[str]) -> list[str]:
    """Stripped, non-blank, first spelling of each term kept, ignoring case."""
    seen: dict[str, str] = {}
    for value in values:
        cleaned = (value or "").strip()
        if cleaned:
            seen.setdefault(cleaned.lower(), cleaned)
    return list(seen.values())


def _posting_terms(terms: Iterable[str]) -> list[str]:
    """Posting keywords worth catching: a stopword claims nothing, so it is never lifted."""
    return [term for term in _distinct(terms) if term.lower() not in STOPWORDS]
