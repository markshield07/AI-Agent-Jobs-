"""What someone scanning a list needs from a posting: a few lines on the role,
the main duties and asks, and the pay the posting states.

Everything here is taken from the posting as published. The summary is its own
sentences, cut short; pay is the source's salary fields or, failing those, a
yearly range written out in the description. A posting that states no pay has
none here, and nothing is estimated.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

# Descriptions from LinkedIn and Indeed arrive as markdown; the company boards
# arrive as plain text with one line per paragraph or list item.
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!&|>~])")
_BULLET = re.compile(r"^\s*(?:[*\-•·◦▪‣]|\d{1,2}[.)])\s+")
_EMPHASIS = re.compile(r"(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1")
_MD_HEADING = re.compile(r"^\s*#{1,6}\s*")

_ROLE_HEAD = re.compile(
    r"^(?:about (?:the|this) (?:role|position|job|opportunity|team)|the role|your role"
    r"|the opportunity|role (?:overview|summary|description)|position (?:overview|summary)"
    r"|job (?:overview|summary|description)|who we.re looking for)\b",
    re.I,
)
# A heading that may cover the company as easily as the role: used only when no
# heading names the role.
_GENERIC_HEAD = re.compile(r"^(?:overview|summary|description|about the job)\b", re.I)
_DUTIES_HEAD = re.compile(
    r"^(?:(?:key |primary |core |essential )?(?:responsibilities|duties)"
    r"|what you.ll (?:do|be doing|work on)|what you will (?:do|be doing)|in this role"
    r"|your (?:responsibilities|impact|day)|the work|day.to.day|how you.ll)",
    re.I,
)
_NEEDS_HEAD = re.compile(
    r"^(?:(?:minimum |basic |required |preferred |key )?(?:qualifications|requirements|skills)"
    r"|what you.ll (?:bring|need)|what you (?:bring|have|need)|what we.re looking for"
    r"|you (?:have|bring|are|might be)|who you are|about you|must.haves?|experience)",
    re.I,
)
_ANY_HEAD = re.compile(
    rf"{_ROLE_HEAD.pattern}|{_GENERIC_HEAD.pattern}|{_DUTIES_HEAD.pattern}|{_NEEDS_HEAD.pattern}"
    r"|^(?:about (?:us|the company|[A-Z]\w+)|benefits|perks|compensation|salary|pay"
    r"|why (?:join|work)|our (?:benefits|culture|mission|values)|equal (?:opportunity|employment)"
    r"|eeo|location|nice to have|bonus points)",
    re.I,
)
# Paragraphs that say nothing about the job itself.
_BOILERPLATE = re.compile(
    r"equal (?:opportunity|employment)|without regard to|reasonable accommodation"
    r"|e-verify|background check|privacy (?:notice|policy)|applicants? (?:with|who)"
    r"|we are an? (?:equal|eeo)"
    # Pay has its own line on a card.
    r"|\$\s?\d|salary|compensation|pay range",
    re.I,
)

_MONEY = r"\$\s?(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+(?:\.\d+)?\s?[kK]\b|\d{4,7}(?:\.\d{2})?)"
_RANGE = re.compile(
    _MONEY + r"\s*(?:USD|usd|US)?\s*(?:-|–|—|to|and)\s*" + _MONEY + r"(?=(?P<after>[^\n]{0,24}))"
)
# Money that is not a salary: a bonus, equity, a stipend, a company's revenue.
# Read from the same sentence before the range and the words right after it.
_NOT_PAY_BEFORE = re.compile(
    r"bonus|equity|stock|rsu|stipend|relocation|revenue|funding|raised|budget|sales|quota",
    re.I,
)
_NOT_PAY_AFTER = re.compile(
    r"^\s*(?:USD\s*)?(?:(?:signing|sign-on|annual|target|performance|in)\s+)?"
    r"(?:bonus|equity|stock|rsus?|stipend|relocation|revenue|funding)",
    re.I,
)
_HOURLY = re.compile(r"^\s*(?:USD\s*)?(?:/\s*h(?:ou)?r|per hour|an hour|hourly|/\s*hour)", re.I)
# A yearly figure is at least this; anything smaller is hourly, weekly or not pay.
_YEARLY_FLOOR = 20_000
_YEARLY_CEILING = 2_000_000


def plain_lines(description: str | None) -> list[tuple[str, bool, bool]]:
    """(text, was_heading, was_bullet) for each non-empty line, markup removed."""
    out: list[tuple[str, bool, bool]] = []
    for raw in (description or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        heading = bool(_MD_HEADING.match(line))
        line = _MD_HEADING.sub("", line)
        bullet = bool(_BULLET.match(line))
        line = _BULLET.sub("", line)
        line = _LINK.sub(r"\1", line)
        line = _ESCAPE.sub(r"\1", line)
        bold_only = bool(re.fullmatch(r"(\*\*|__)(.+)\1:?", line))
        line = _EMPHASIS.sub(r"\2", line).strip()
        line = re.sub(r"\s+", " ", line)
        if not line:
            continue
        heading = (
            heading or (bold_only and len(line) <= 80) or (not bullet and _looks_like_heading(line))
        )
        out.append((line, heading, bullet and not heading))
    return out


def _looks_like_heading(line: str) -> bool:
    if len(line) > 70 or line.endswith((".", "!", "?", ";", ",")):
        return False
    if line.endswith(":"):
        return True
    # Without a colon, only a short phrase: "Experience with UPS, generators and
    # CRAC systems" is a list item that happens to start like a heading.
    return "," not in line and len(line.split()) <= 5 and bool(_ANY_HEAD.match(line))


def _sections(lines: list[tuple[str, bool, bool]]) -> list[tuple[str, list[tuple[str, bool]]]]:
    """The description split at its headings: (heading, [(line, was_bullet)])."""
    sections: list[tuple[str, list[tuple[str, bool]]]] = [("", [])]
    for text, heading, bullet in lines:
        if heading:
            sections.append((text.rstrip(":").strip(), []))
        else:
            sections[-1][1].append((text, bullet))
    return [s for s in sections if s[1]]


def summary(description: str | None, limit: int = 320) -> str:
    """A few sentences on the role, in the posting's own words, cut to about `limit`."""
    sections = _sections(plain_lines(description))
    prose = [
        (
            head,
            [t for t, bullet in body if not bullet and len(t) >= 40 and not _BOILERPLATE.search(t)],
        )
        for head, body in sections
    ]
    chosen = next((body for head, body in prose if body and _ROLE_HEAD.match(head)), None)
    if chosen is None:
        chosen = next((body for head, body in prose if body and _GENERIC_HEAD.match(head)), None)
    if chosen is None:
        # The first real paragraph, passing over a line or two about the company
        # when a paragraph about the role follows.
        paragraphs = [t for _, body in prose for t in body]
        about_role = [
            t
            for t in paragraphs[:4]
            if re.search(r"\b(?:you|this role|we.re (?:looking|seeking|hiring))\b", t, re.I)
        ]
        chosen = about_role[:1] + paragraphs if about_role else paragraphs
    if not chosen:
        bullets = [t for _, body in sections for t, bullet in body if bullet]
        chosen = ["; ".join(bullets[:3])] if bullets else []
    return _clip(" ".join(dict.fromkeys(chosen)), limit)


def highlights(description: str | None, per_list: int = 4) -> dict[str, list[str]]:
    """The first few duties and asks, from sections headed as such."""
    found: dict[str, list[str]] = {"duties": [], "needs": []}
    for head, body in _sections(plain_lines(description)):
        key = "duties" if _DUTIES_HEAD.match(head) else "needs" if _NEEDS_HEAD.match(head) else None
        if key is None or found[key]:
            continue
        items = [t for t, bullet in body if bullet] or [t for t, _ in body if len(t) <= 220]
        found[key] = [_clip(t, 160) for t in items[:per_list]]
    return found


def pay(job: Mapping[str, Any]) -> dict[str, Any] | None:
    """The yearly pay range the posting gives, and where it was read from."""
    low, high = _int(job.get("salary_min")), _int(job.get("salary_max"))
    if low or high:
        return {"min": low, "max": high, "period": "year", "from": "listing"}
    stated = pay_in_text(job.get("description"))
    if stated:
        return {"min": stated[0], "max": stated[1], "period": "year", "from": "description"}
    return None


def pay_in_text(description: str | None) -> tuple[int, int] | None:
    """A yearly range written in the posting, e.g. "$120,000 - $150,000".

    Several ranges (pay zones by city, say) give the lowest and highest of them.
    Hourly rates and single figures are left out rather than converted.
    """
    text = _ESCAPE.sub(r"\1", description or "")
    ranges: list[tuple[int, int]] = []
    for match in _RANGE.finditer(text):
        after = match.group("after")
        before = re.split(r"[.!?\n]", text[max(0, match.start() - 60) : match.start()])[-1]
        if _HOURLY.match(after) or _NOT_PAY_AFTER.match(after) or _NOT_PAY_BEFORE.search(before):
            continue
        low, high = _amount(match.group(1)), _amount(match.group(2))
        if low is None or high is None or low > high:
            continue
        if _YEARLY_FLOOR <= low and high <= _YEARLY_CEILING:
            ranges.append((low, high))
    if not ranges:
        return None
    return min(r[0] for r in ranges), max(r[1] for r in ranges)


def _amount(text: str) -> int | None:
    cleaned = text.replace(",", "").replace(" ", "")
    scale = 1
    if cleaned[-1:] in "kK":
        cleaned, scale = cleaned[:-1], 1000
    try:
        return int(float(cleaned) * scale)
    except ValueError:
        return None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(float(str(value).replace(",", "")))
    except ValueError:
        return None
    return number if number > 0 else None


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    # End on a sentence when one ends in the last third, else on a word.
    stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if stop >= limit * 2 // 3:
        return cut[: stop + 1]
    return cut[: cut.rfind(" ")].rstrip(" ,;:-–—") + "…" if " " in cut else cut + "…"


def card(job: Mapping[str, Any]) -> dict[str, Any]:
    """The fields a list shows for one job; the full description stays out."""
    out = {k: v for k, v in job.items() if k != "description"}
    out["summary"] = summary(job.get("description"))
    out["pay"] = pay(job)
    return out
