"""The free pass: a deterministic 0-100 score for every discovered posting.

Adapted from AutoApply's filter module. Four hard disqualifiers (an excluded
keyword, a blacklisted company, a stated salary under the floor, an on-site or
hybrid posting, by its flag, title, location or own description, outside the
places wanted) zero a posting
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
# A posting that names one of these in its title or location wants people in
# an office, whatever the board's remote flag says.
_IN_OFFICE = ("hybrid", "on-site", "onsite", "in-office", "in office")

# The same, said in a posting's description. A board's remote flag can be
# wrong (Indeed's "Remote" on a job "onsite at Naval Station Norfolk"), so a
# plain statement that the job itself is on-site or hybrid outweighs it.
# Occasional visits, travel and "not onsite" do not count.
_ONSITE = r"(?:on-?site|on\s+site|in-office|in\s+office|in-person|in\s+person)"
_OFFICE_SAYS = [
    re.compile(p, re.I)
    for p in (
        rf"\b(?:this|the)\s+(?:position|role|job|opportunity)\s+(?:is|will\s+be)\s+"
        rf"(?:an?\s+)?(?:100%\s+|fully\s+|full[- ]time\s+)?(?:{_ONSITE}|hybrid)\b",
        rf"\b(?:100%|fully|entirely|full[- ]time)\s+{_ONSITE}\b",
        rf"\b(?:must|required\s+to|expected\s+to|will\s+need\s+to)\s+(?:work|be|report)\s+"
        rf"(?:{_ONSITE}|in\s+(?:the|our)\s+office|to\s+(?:the|our)\s+office)\b",
        rf"\b(?:work(?:place)?[- ]?(?:type|arrangement|model|setting|location)|location\s+type"
        rf"|work\s+mode)\s*:\s*(?:{_ONSITE}|hybrid)\b",
        r"\bhybrid\s+(?:role|position|schedule|work(?:ing)?|model|arrangement|opportunity)\b",
        rf"\b\d\s+days?\s+(?:a|per|each)\s+week\s+(?:{_ONSITE}|in\s+(?:the|our)\s+office)\b",
        rf"\b\d\s+days?\s+(?:{_ONSITE}|in\s+(?:the|our)\s+office)\b",
    )
]
# "onsite at Naval Station Norfolk": on-site at a named place (capitalised).
_ONSITE_AT = re.compile(
    rf"(?i:\b{_ONSITE}\s+(?:at|in))\s+(?:(?:the|our|its)\s+)?[A-Z][\w.'&-]*"
    r"(?:\s+[A-Z][\w.'&-]*)*"
)
# Words in the same sentence that make on-site an occasional thing.
_NOW_AND_THEN = re.compile(
    r"\b(?:occasional(?:ly)?|as\s+needed|when\s+(?:needed|required)|if\s+needed|periodic(?:ally)?"
    r"|travel|visits?|up\s+to\s+\d+\s*%|quarterly|monthly|annual(?:ly)?|some)\b",
    re.I,
)
_NEGATED = re.compile(r"\b(?:not|no|non|never)\b[\s-]*(?:an?\s+|be\s+|required\s+)?$", re.I)
# The description's own plain word that the job is remote.
SAYS_REMOTE = re.compile(
    r"\b(?:fully|100%|completely|entirely) remote\b"
    r"|\bremote[- ](?:position|role|opportunity|job|first)\b"
    r"|\b(?:work|working) (?:from home|remotely)\b"
    r"|\bthis (?:is a|position is|role is) remote\b",
    re.I,
)
NOT_REMOTE = re.compile(r"\bnot (?:a )?remote\b|\bno remote\b|\bnon-remote\b", re.I)


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

    rejected = _disqualify(job, criteria, haystack) or _not_remote(job, title, criteria)
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


def _not_remote(job: Mapping[str, Any], title: str, criteria: SearchCriteria) -> str | None:
    """A reason to drop a posting that plainly wants someone in an office the
    person did not ask for: the board marks it on-site, or its title or
    location says hybrid or on-site, and its location is none of the places
    wanted. Remote postings, postings in a wanted place, and postings that
    leave the question open stay in. With no locations set, nothing is
    dropped here."""
    wanted = criteria.normalised(criteria.locations)
    if not wanted:
        return None
    if _is_remote(job, title):
        return None
    flag = job.get("remote")
    have = _text(job, "location").strip()
    said = office_in_description(_text(job, "description"))
    if not (flag is False or flag == 0 or _in_office(title) or _in_office(have) or said):
        return None
    lower = have.lower()
    have_words = set(_words(lower))
    places = [want for want in wanted if want != "remote"]
    if any(_location_matches(want, lower, have_words) for want in places):
        return None
    if _found_near_wanted(job, places):
        return None
    if said and any(_location_matches(want, said.lower(), set(_words(said))) for want in places):
        return None
    where = f'the description says "{said}"' if said and not _in_office(have) else have
    if not places:
        return f"not remote ({where or 'on-site'}); only Remote is wanted"
    return f"not remote ({where or 'on-site'}) and not in {', '.join(places)}"


def not_remote(job: Mapping[str, Any], criteria: SearchCriteria) -> str | None:
    """Why this posting wants someone in an office the person did not ask
    for, or None; the same test scoring applies, for a job queued before."""
    return _not_remote(job, _text(job, "title"), criteria)


def office_in_description(text: str) -> str | None:
    """The words in a description saying the job itself is on-site or hybrid,
    or None. A description that also says plainly it is remote says nothing
    definite, and occasional visits or travel are not an office job."""
    if not text or (SAYS_REMOTE.search(text) and not NOT_REMOTE.search(text)):
        return None
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", text):
        for pattern in (*_OFFICE_SAYS, _ONSITE_AT):
            for match in pattern.finditer(sentence):
                if _NEGATED.search(sentence[: match.start()]):
                    continue
                if _NOW_AND_THEN.search(sentence):
                    continue
                return re.sub(r"\s+", " ", match.group(0)).strip().rstrip(".,;:")
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
    if _is_remote(job, title) and criteria.remote_ok:
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
    if _found_near_wanted(job, [w for w in wanted if w != "remote"]):
        return 20
    return 8 if not have else 0


def _found_near_wanted(job: Mapping[str, Any], places: list[str]) -> bool:
    """A board's search around a wanted place found it: "Irvine, CA" from a
    search for Orange County is near enough, as the board's radius says."""
    near = _text(job, "found_near").strip().lower()
    if not near:
        return False
    near_words = set(_words(near))
    return any(_location_matches(want, near, near_words) for want in places)


def _is_remote(job: Mapping[str, Any], title: str) -> bool:
    have = _text(job, "location").lower()
    if _in_office(title) or _in_office(have):
        return False
    if office_in_description(_text(job, "description")):
        return False
    return _flag(job.get("remote")) or _contains(have, "remote") or _contains(title, "remote")


def _in_office(text: str) -> bool:
    lower = text.lower()
    return any(term in lower for term in _IN_OFFICE)


def place_matches(want: str, have: str) -> bool:
    """Whether the wanted place (lower case) names the location `have`."""
    lower = have.lower()
    return _location_matches(want, lower, set(_words(lower)))


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
