"""Finding terms in text, the same way everywhere in tailoring.

A term matches as a whole word, but "c++", "c#", ".net" and "node.js" carry
symbols that `\\b` mishandles, so the boundary is any character that is not a
word character, "+" or "#". Multi-word terms match with any whitespace
between the words. Everything is case-insensitive.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

# Words that look like names or acronyms in a sentence but claim nothing.
STOPWORDS: frozenset[str] = frozenset(
    """
    a an and the of to in for on at by with from as is are was were be been being or
    nor not no yes this that these those it its into onto over under than then there
    their they them we our us you your i me my he she his her him who whom which what
    when where why how all any each every some such own same so too very can will
    would should could may might must shall do does did done have has had having
    make made making using used use via per about across after before during while
    within without up down out off again further once here also both between through
    led lead built build building drove drive owned own delivered deliver improved
    improve reduced reduce increased increase designed design developed develop
    created create managed manage shipped ship migrated migrate wrote write ran run
    team teams member members role roles project projects work worked working
    experience years year month months day days new senior junior lead staff principal
    engineer engineers engineering software developer development company companies
    customer customers customers' product products service services system systems
    platform platforms application applications data business technical technology
    technologies tool tools solution solutions process processes result results
    responsible responsibilities requirements ability strong excellent good great
    """.split()
)

# The trailing guard refuses a letter or a further decimal, not a full stop: a
# number that ends a sentence ("cut costs 40%.") must keep its suffix.
# A full stop after one of these does not end a sentence.
_ABBREVIATIONS: frozenset[str] = frozenset(
    "e.g i.e etc vs mr mrs ms dr jr sr inc ltd co st approx dept".split()
)

_NUMBER = re.compile(r"(?<![\w.])[$€£]?\d[\d,]*(?:\.\d+)?(?:%|[kKmMbB]\b|\+)?(?!\w|\.\d)")


@lru_cache(maxsize=4096)
def _pattern(term: str) -> re.Pattern[str]:
    words = [re.escape(w) for w in term.strip().lower().split()]
    body = r"\s+".join(words)
    return re.compile(rf"(?<![\w+#]){body}(?![\w+#])", re.IGNORECASE)


def contains_term(text: str, term: str) -> bool:
    """True when `term` appears in `text` as a whole word or phrase."""
    if not term or not term.strip() or not text:
        return False
    return _pattern(term).search(text) is not None


def find_terms(text: str, terms: Iterable[str]) -> list[str]:
    """The distinct terms present in `text`, lowercased, in the order given."""
    seen: dict[str, None] = {}
    for term in terms:
        key = term.strip().lower()
        if key and key not in seen and contains_term(text, key):
            seen[key] = None
    return list(seen)


def numbers_in(text: str) -> set[str]:
    """Every number-like token: 40%, $1.2M, 3,000, 10k, 5+. Normalised for comparison."""
    found: set[str] = set()
    for match in _NUMBER.finditer(text or ""):
        token = match.group(0).lower().replace(",", "")
        found.add(token)
    return found


def tokens(text: str) -> list[str]:
    """Word-ish tokens keeping the symbols that matter in technology names."""
    return re.findall(r"[A-Za-z][\w+#.]*[\w+#]|[A-Za-z]|\d[\w.]*", text or "")


def keyword_like(text: str) -> list[str]:
    """Tokens that read as a name, an acronym or a technology, not ordinary prose.

    A token qualifies when it carries a digit or symbol (c++, k8s, node.js), is an
    acronym (AWS, CI/CD's parts), or is capitalised somewhere other than the start
    of a sentence. Stopwords never qualify. Returned lowercased, distinct, in order.
    """
    seen: dict[str, None] = {}
    sentence_start = True
    previous = ""
    # A token never swallows the full stop that ends its sentence, or the next
    # sentence's first word would look capitalised mid-sentence.
    for raw in re.findall(r"[A-Za-z][\w+#./-]*[\w+#]|[A-Za-z]|\d[\w.]*[\w]|\d|[.!?]", text or ""):
        if raw in ".!?":
            if raw != "." or previous not in _ABBREVIATIONS:
                sentence_start = True
            previous = raw
            continue
        previous = raw.lower()
        token = raw.strip("./-")
        if not token:
            continue
        lowered = token.lower()
        starts_sentence = sentence_start
        sentence_start = False
        if lowered in STOPWORDS or lowered in _ABBREVIATIONS or len(lowered) < 2:
            continue
        has_symbol = any(ch.isdigit() or ch in "+#./-" for ch in token)
        acronym = token.isupper() and len(token) >= 2
        capitalised = token[0].isupper() and not starts_sentence
        if has_symbol or acronym or capitalised:
            seen.setdefault(lowered, None)
    return list(seen)
