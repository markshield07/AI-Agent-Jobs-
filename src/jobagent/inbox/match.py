"""Deciding which application a mail is about.

There is no thread id to follow. An applicant tracking system replies from its
own domain, a recruiter replies from the company's, and neither quotes anything
the agent sent, so the only honest way through is to weigh several weak signals
and refuse to guess when they do not add up: the sender's domain against the
company's, the company's name in the sender's display name or the subject, and
the job's title in either. A message that scores under `THRESHOLD` is kept as
unmatched for the user to attach by hand rather than filed against the nearest
application.

Every function here is pure. `candidates` is the one that reads the database,
and it reads only.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from .models import InboxMessage, Match

# Enough signal to file a message against an application. Two moderate signals
# (the company in the display name and the title in the subject) clear it; one
# alone does not.
THRESHOLD = 0.55

# Mail from these comes on behalf of a company, so the domain says nothing
# about which one. The company's name has to be in the message itself.
ATS_DOMAINS = frozenset(
    {
        "greenhouse.io",
        "greenhouse-mail.io",
        "lever.co",
        "hire.lever.co",
        "ashbyhq.com",
        "myworkday.com",
        "myworkdayjobs.com",
        "workday.com",
        "icims.com",
        "smartrecruiters.com",
        "jobvite.com",
        "bamboohr.com",
        "breezy.hr",
        "workable.com",
        "teamtailor-mail.com",
        "recruitee.com",
        "indeed.com",
        "indeedemail.com",
        "linkedin.com",
        "ziprecruiter.com",
        "glassdoor.com",
    }
)

_LEGAL_SUFFIXES = frozenset(
    {
        "inc",
        "inc.",
        "llc",
        "ltd",
        "limited",
        "corp",
        "corporation",
        "co",
        "gmbh",
        "plc",
        "ag",
        "sa",
        "bv",
        "nv",
        "pty",
        "srl",
        "oy",
        "ab",
        "as",
    }
)
_TITLE_STOPWORDS = frozenset(
    {
        "senior",
        "staff",
        "principal",
        "junior",
        "lead",
        "the",
        "and",
        "for",
        "with",
        "remote",
        "engineer",
        "developer",
        "manager",
        "specialist",
        "analyst",
        "iii",
        "ii",
        "iv",
    }
)
_TWO_LABEL_TLDS = frozenset({"co", "com", "org", "net", "gov", "edu", "ac"})

_WORD = re.compile(r"[a-z0-9]+")


def normalise(text: str) -> str:
    return " ".join(_WORD.findall((text or "").lower()))


def squash(text: str) -> str:
    return "".join(_WORD.findall((text or "").lower()))


def company_tokens(company: str) -> list[str]:
    """The company's name, lowercased, with the article and legal suffix dropped."""
    tokens = [t for t in normalise(company).split() if t not in _LEGAL_SUFFIXES]
    return tokens[1:] if len(tokens) > 1 and tokens[0] == "the" else tokens


def domain_root(domain: str) -> str:
    """`careers.mail.acme.co.uk` -> `acme.co.uk`; good enough to compare senders."""
    labels = [label for label in (domain or "").lower().split(".") if label]
    if len(labels) <= 2:
        return ".".join(labels)
    if labels[-2] in _TWO_LABEL_TLDS and len(labels[-1]) <= 3 and len(labels) >= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def is_ats_domain(domain: str) -> bool:
    root = domain_root(domain)
    return root in ATS_DOMAINS or domain.lower() in ATS_DOMAINS


def host_of(url: str | None) -> str:
    match = re.match(r"^[a-z]+://([^/?#]+)", (url or "").strip(), re.IGNORECASE)
    return match.group(1).lower().split(":")[0] if match else ""


def company_domains(job: dict[str, Any]) -> set[str]:
    """Domain roots that belong to the company rather than to an ATS."""
    found = set()
    for url in (job.get("url"), job.get("apply_url")):
        host = host_of(url)
        if host and not is_ats_domain(host):
            found.add(domain_root(host))
    return found


def company_matches_domain(company: str, domain: str) -> bool:
    """`Acme Robotics` against `acmerobotics.com`, or `acme-robotics.com`.

    Deliberately not a substring test. `Acme` against `acmesupplies.com` is a
    different company often enough that the cost of the false match outweighs
    what the loose rule would buy.
    """
    root = domain_root(domain)
    label = root.split(".")[0] if root else ""
    tokens = company_tokens(company)
    squashed = squash(" ".join(tokens))
    if not squashed or not label:
        return False
    if squashed == label:
        return True
    return len(tokens[0]) >= 4 and (tokens[0] == label or tokens[0] in label.split("-"))


def title_tokens(title: str) -> list[str]:
    return [t for t in normalise(title).split() if len(t) >= 4 and t not in _TITLE_STOPWORDS]


def _title_signal(title: str, subject: str, body: str) -> tuple[float, str]:
    wanted = normalise(title)
    if not wanted:
        return 0.0, ""
    if wanted and wanted in normalise(subject):
        return 0.35, "title in subject"
    tokens = title_tokens(title)
    if tokens:
        in_subject = [t for t in tokens if t in normalise(subject)]
        if len(in_subject) >= 2 or (len(tokens) == 1 and in_subject):
            return 0.25, "title words in subject"
        if wanted in normalise(body):
            return 0.2, "title in body"
        if len([t for t in tokens if t in normalise(body)]) >= 2:
            return 0.12, "title words in body"
    return 0.0, ""


def _company_signal(company: str, message: InboxMessage) -> tuple[float, str]:
    wanted = " ".join(company_tokens(company))
    if not wanted:
        return 0.0, ""
    if wanted in normalise(message.from_name):
        return 0.4, "company in sender name"
    if wanted in normalise(message.subject):
        return 0.3, "company in subject"
    if wanted in normalise(message.body):
        return 0.15, "company in body"
    return 0.0, ""


@dataclass(slots=True)
class Candidate:
    """One application a message could belong to."""

    application_id: int
    company: str
    title: str
    job: dict[str, Any]
    since: str  # the mail has to be newer than this to be a reply


def candidates(conn: sqlite3.Connection, *, since: str | None = None) -> list[Candidate]:
    """Applications a reply could be about, newest first. Reads only."""
    rows = conn.execute(
        """SELECT a.id, a.submitted_at, a.created_at,
                  j.company, j.title, j.url, j.apply_url
           FROM applications a JOIN jobs j ON j.id = a.job_id
           ORDER BY COALESCE(a.submitted_at, a.created_at) DESC"""
    ).fetchall()
    found = []
    for row in rows:
        start = row["submitted_at"] or row["created_at"] or ""
        if since and start and start < since:
            continue
        found.append(
            Candidate(
                application_id=int(row["id"]),
                company=row["company"] or "",
                title=row["title"] or "",
                job={"url": row["url"], "apply_url": row["apply_url"]},
                since=start,
            )
        )
    return found


def score(message: InboxMessage, candidate: Candidate) -> tuple[float, list[str]]:
    """How much this message looks like a reply to this application."""
    if candidate.since and message.received_at and message.received_at < candidate.since:
        return 0.0, []  # it arrived before we applied, so it is not the reply

    total, reasons = 0.0, []
    domain = message.sender_domain
    if domain and not is_ats_domain(domain):
        # A mail from the company's own domain is on its own enough to file:
        # a recruiter's reply is often two lines with neither the company's
        # name nor the job title in it.
        if any(domain_root(domain) == d for d in company_domains(candidate.job)):
            total += 0.6
            reasons.append("sender domain is the company's")
        elif company_matches_domain(candidate.company, domain):
            total += 0.55
            reasons.append("sender domain matches the company")
    elif domain:
        total += 0.05
        reasons.append(f"sent via {domain_root(domain)}")

    weight, why = _company_signal(candidate.company, message)
    if weight:
        total += weight
        reasons.append(why)

    weight, why = _title_signal(candidate.title, message.subject, message.body)
    if weight:
        total += weight
        reasons.append(why)

    return min(total, 1.0), reasons


def match_message(message: InboxMessage, pool: list[Candidate]) -> Match | None:
    """The application this message belongs to, or None when nothing is clear.

    A tie between two applications at the same company goes to the newer one,
    which is what `candidates` already orders by.
    """
    best: Match | None = None
    for candidate in pool:
        total, reasons = score(message, candidate)
        if total < THRESHOLD:
            continue
        if best is None or total > best.confidence:
            best = Match(application_id=candidate.application_id, confidence=total, reasons=reasons)
    return best
