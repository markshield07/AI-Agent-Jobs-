"""The free pass: a deterministic 0-100 score for every discovered posting.

Adapted from AutoApply's filter module. Three hard disqualifiers (an excluded
keyword, a blacklisted company, a stated salary under the floor) zero a posting
outright. Everything else is the sum of four components, title, salary,
location and keywords, each scoring in the middle when the criteria leave it
unconstrained, so an empty setting never drags every posting under the
threshold. The model only sees postings that clear `criteria.min_score`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from jobagent.discovery.criteria import SearchCriteria
from jobagent.discovery.sources.filters import title_matches

_MAX = {"title": 35, "salary": 20, "location": 20, "keywords": 25}

# Six distinct keyword hits is full marks; fewer scale linearly.
_KEYWORD_FULL_HITS = 6
_REASON_TERMS = 8

_WORD = re.compile(r"[a-z0-9+#.]+")


@dataclass(slots=True)
class RuleScore:
    score: int
    disqualified: bool
    reason: str
    breakdown: dict[str, int] = field(default_factory=dict)


def score_rules(
    job: Mapping[str, Any], criteria: SearchCriteria, profile_tags: set[str]
) -> RuleScore:
    """Score one jobs-table row, as a dict, against the criteria.

    Missing or null fields are tolerated: a posting with no salary or no
    location scores in the middle of that component rather than failing.
    """
    title = _text(job, "title")
    haystack = f"{title}\n{_text(job, 'description')}".lower()

    rejected = _disqualify(job, criteria, haystack)
    if rejected:
        return RuleScore(score=0, disqualified=True, reason=rejected)

    keyword_points, matched = _score_keywords(haystack, criteria, profile_tags)
    breakdown = {
        "title": _score_title(title, criteria),
        "salary": _score_salary(job, criteria),
        "location": _score_location(job, title, criteria),
        "keywords": keyword_points,
    }
    return RuleScore(
        score=sum(breakdown.values()),
        disqualified=False,
        reason=_reason(breakdown, matched),
        breakdown=breakdown,
    )


def passes(result: RuleScore, criteria: SearchCriteria) -> bool:
    return not result.disqualified and result.score >= criteria.min_score


# ----------------------------------------------------------- disqualifiers --


def _disqualify(job: Mapping[str, Any], criteria: SearchCriteria, haystack: str) -> str | None:
    for term in _clean(criteria.exclude_keywords):
        if _contains(haystack, term.lower()):
            return f"excluded keyword: {term}"

    company = _text(job, "company").strip().lower()
    if company:
        for entry in _clean(criteria.company_blacklist):
            wanted = entry.lower()
            if wanted in company or company in wanted:
                return f"blacklisted company: {entry}"

    floor = criteria.salary_min
    salary_max = _int(job.get("salary_max"))
    if floor is not None and salary_max is not None and salary_max < floor:
        return f"salary {salary_max} below minimum {floor}"
    return None


# -------------------------------------------------------------- components --


def _score_title(title: str, criteria: SearchCriteria) -> int:
    wanted = criteria.normalised(criteria.titles)
    if not wanted:
        return 25
    if title_matches(title, criteria):
        return 35
    have = set(_words(title))
    for want in wanted:
        words = set(_words(want))
        if words and 2 * len(have & words) >= len(words):
            return 20
    return 0


def _score_salary(job: Mapping[str, Any], criteria: SearchCriteria) -> int:
    floor = criteria.salary_min
    if floor is None:
        return 15
    low, high = _int(job.get("salary_min")), _int(job.get("salary_max"))
    if low is None and high is None:
        return 10
    if (high is not None and high >= floor) or (low is not None and low >= floor):
        return 20
    return 0


def _score_location(job: Mapping[str, Any], title: str, criteria: SearchCriteria) -> int:
    have = _text(job, "location").strip().lower()
    is_remote = _flag(job.get("remote")) or _contains(have, "remote") or _contains(title, "remote")
    if is_remote and criteria.remote_ok:
        return 20

    wanted = criteria.normalised(criteria.locations)
    if not wanted:
        return 15
    have_words = set(_words(have))
    for want in wanted:
        # "Remote" as a wanted location is settled by the remote branch above;
        # it must never match a posting by its text.
        if want != "remote" and _location_matches(want, have, have_words):
            return 20
    return 8 if not have else 0


def _location_matches(want: str, have: str, have_words: set[str]) -> bool:
    if _contains(have, want):
        return True
    words = set(_words(want))
    if words and words <= have_words:
        return True
    # "Austin, TX" should still match "Austin, Texas" and plain "Austin".
    if "," not in want:
        return False
    city_words = set(_words(want.split(",", 1)[0]))
    return bool(city_words) and city_words <= have_words


def _score_keywords(
    haystack: str, criteria: SearchCriteria, profile_tags: set[str]
) -> tuple[int, list[str]]:
    pool = set(criteria.normalised(criteria.keywords))
    pool.update(tag.strip().lower() for tag in profile_tags if tag and tag.strip())
    if not pool:
        return 12, []
    matched = sorted(term for term in pool if _contains(haystack, term))
    hits = min(len(matched), _KEYWORD_FULL_HITS)
    return round(hits / _KEYWORD_FULL_HITS * _MAX["keywords"]), matched[:_REASON_TERMS]


def _reason(breakdown: dict[str, int], matched: list[str]) -> str:
    parts = [f"{name} {points}/{_MAX[name]}" for name, points in breakdown.items()]
    if matched:
        parts[-1] += f" ({', '.join(matched)})"
    return " · ".join(parts)


# ----------------------------------------------------------------- helpers --


def _text(job: Mapping[str, Any], key: str) -> str:
    value = job.get(key)
    return "" if value is None else str(value)


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _flag(value: Any) -> bool:
    """True for True or 1: sqlite hands the column back as 0/1 when not converted."""
    return value is True or value == 1


def _clean(values: list[str]) -> list[str]:
    """Like `SearchCriteria.normalised` but keeps the case, for the reason text."""
    return [v.strip() for v in values if v and v.strip()]


def _words(text: str) -> list[str]:
    return _WORD.findall(text.lower())


def _contains(text: str, term: str) -> bool:
    return bool(term) and _pattern(term).search(text.lower()) is not None


@lru_cache(maxsize=4096)
def _pattern(term: str) -> re.Pattern[str]:
    """Whole-word for a single token, whitespace-tolerant substring for a phrase.

    `\\b` is wrong for terms that end in a symbol ("c++", "c#"): the boundary
    here is "not glued to a word character or another such symbol", so "go"
    does not hit "google" and "c" does not hit "c++", while "node.js" and
    "c#" match themselves.
    """
    parts = term.split()
    if len(parts) > 1:
        return re.compile(r"\s+".join(re.escape(part) for part in parts))
    return re.compile(rf"(?<![\w+#]){re.escape(term)}(?![\w+#])")
