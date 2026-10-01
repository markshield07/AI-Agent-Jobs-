"""Tailor the resume to one job: plan, check, gate, render, keep.

    facts + posting -> model plan -> validator -> coverage gate -> PDF

The model proposes; the validator disposes. A plan that claims anything the
fact base does not state goes back to the model with the exact objections,
up to twice, and a third failure is kept as a rejected variant rather than
shipped. The coverage gate applies the same discipline to usefulness: a
tailored resume that mentions clearly fewer of the posting's keywords than the
uploaded one did is not an improvement, so it is sent back too. "Clearly" is
`COVERAGE_TOLERANCE`: a one-page selection leaves some facts out on purpose,
and a point or two of coverage is not worth a rejected resume.

A rejected job is not tailored again on a schedule until something it was
tailored from changes: the posting, the facts, the never-claim list, the
uploaded resume, the search keywords, or these rules (`TAILOR_RULES`).
`inputs_fingerprint` is that list, hashed, and `jobs_to_tailor` compares it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections.abc import Sequence
from pathlib import Path

from jobagent.answers import list_answers
from jobagent.config import Settings
from jobagent.discovery import store as jobs
from jobagent.discovery.criteria import SearchCriteria, load_criteria
from jobagent.llm.backend import Completer, resolve_backend
from jobagent.resume.facts import Fact, list_facts, list_never_claim
from jobagent.tailor import store
from jobagent.tailor.generate import generate_cover_letter, generate_resume
from jobagent.tailor.keywords import coverage, posting_keywords, split_by_support
from jobagent.tailor.models import Issue, TailoredResume, Variant
from jobagent.tailor.render import resume_html, resume_text, write_pdf
from jobagent.tailor.terms import contains_term
from jobagent.tailor.validate import fact_pool_text, validate_cover_letter, validate_resume

log = logging.getLogger(__name__)

# How far under the uploaded resume's keyword coverage a tailored one may fall.
COVERAGE_TOLERANCE = 0.05

# Bump when keyword extraction, the prompt or the gates change, so jobs rejected
# under the old rules get one more try under the new ones.
TAILOR_RULES = 2

CONTACT_KEYS = ("full_name", "email", "phone", "location")


class TailorError(RuntimeError):
    """The job cannot be tailored for as things stand: no such job, or no facts."""


def _renderable_text(facts: Sequence[Fact]) -> str:
    """What the facts can put on a page: their text, titles and employers. Tags never show."""
    lines: list[str] = []
    for fact in facts:
        detail = fact.detail if isinstance(fact.detail, dict) else {}
        lines += [fact.text, str(detail.get("title") or ""), str(detail.get("employer") or "")]
    return "\n".join(line for line in lines if line.strip())


def _uploaded_resume(conn: sqlite3.Connection) -> dict | None:
    """The newest uploaded resume's id and text, the baseline the coverage gate uses."""
    row = conn.execute(
        "SELECT id, parsed_text FROM resume_base ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {"id": row["id"], "parsed_text": row["parsed_text"]} if row else None


def inputs_fingerprint(
    job: dict,
    facts: Sequence[Fact],
    never: Sequence[str],
    base_id: int | None,
    criteria_keywords: Sequence[str],
) -> str:
    """A hash of everything a tailoring of `job` depends on, `TAILOR_RULES` included."""
    payload = {
        "rules": TAILOR_RULES,
        "job": [job.get(k) for k in ("title", "company", "location", "description")],
        "facts": [
            [f.id, f.kind, f.text, sorted(f.tags or []), f.detail] for f in facts if f.active
        ],
        "never": sorted(t.lower() for t in never),
        "base": base_id,
        "keywords": sorted(k.lower() for k in criteria_keywords),
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


def jobs_to_tailor(
    conn: sqlite3.Connection, settings: Settings, limit: int
) -> tuple[list[str], int]:
    """(job ids to tailor, how many were passed over), in apply order.

    A queued job is wanted when it has no ready resume, unless its newest
    attempt was rejected and nothing it was tailored from has changed since:
    the same inputs would be rejected again, and every hour spent on them is
    an hour the jobs behind them wait.
    """
    from jobagent.apply.pipeline import in_apply_order

    facts = [f for f in list_facts(conn) if f.active]
    never = [row["term"] for row in list_never_claim(conn)]
    base = _uploaded_resume(conn)
    criteria = load_criteria(conn)
    wanted: list[str] = []
    passed_over = 0
    queued = jobs.list_jobs(conn, status="queued", limit=500)
    for job in in_apply_order(queued, settings.apply_site_list):
        if len(wanted) >= limit:
            break
        if store.latest_ready_variant(conn, job["id"]) is not None:
            continue
        latest = store.latest_variant(conn, job["id"])
        if latest is not None and latest.status == "rejected" and latest.inputs_sha:
            now = inputs_fingerprint(
                job, facts, never, base["id"] if base else None, criteria.keywords
            )
            if latest.inputs_sha == now:
                passed_over += 1
                continue
        wanted.append(job["id"])
    return wanted, passed_over


def tailor_job(
    conn: sqlite3.Connection,
    job_id: str,
    settings: Settings,
    *,
    completer: Completer | None = None,
    with_cover_letter: bool = True,
    max_attempts: int = 3,
    criteria: SearchCriteria | None = None,
) -> Variant:
    """Produce a variant for `job_id` and store it. Returns it ready or rejected."""
    job = jobs.get_job(conn, job_id)
    if job is None:
        raise TailorError(f"No job {job_id}.")

    all_facts = list_facts(conn, include_inactive=True)
    active = [f for f in all_facts if f.active]
    if not active:
        raise TailorError("No facts on file. Upload a resume first.")
    by_id: dict[int, Fact] = {f.id: f for f in all_facts if f.id is not None}
    never = [row["term"] for row in list_never_claim(conn)]
    contact = {row["key"]: row["value"] for row in list_answers(conn) if row["key"] in CONTACT_KEYS}

    criteria = criteria or load_criteria(conn)
    company = job.get("company") or ""
    keywords = posting_keywords(
        job.get("description"),
        job.get("title"),
        extra=criteria.keywords,
        exclude=[company, job.get("location") or ""],
    )
    supported, missing = split_by_support(keywords, fact_pool_text(active))

    base = _uploaded_resume(conn)
    base_text = base["parsed_text"] if base else ""
    # The baseline counts only keywords a fact puts on the page. One the
    # uploaded resume has only in its address or headings ("CA", "United
    # States") is one no tailored resume can bring back, so holding a plan to
    # it rejects good plans for words that were never skills.
    reachable = _renderable_text(active)
    base_cov = (
        coverage(base_text, [k for k in keywords if contains_term(reachable, k)], of=keywords)
        if base
        else None
    )

    completer = completer or resolve_backend(settings)
    variant = Variant(
        job_id=job_id,
        base_id=base["id"] if base else None,
        content=TailoredResume(),
        keywords=keywords,
        keywords_missing=missing,
        base_coverage=base_cov,
        inputs_sha=inputs_fingerprint(
            job, active, never, base["id"] if base else None, criteria.keywords
        ),
    )

    issues: list[Issue] = []
    for attempt in range(1, max_attempts + 1):
        plan, completion = generate_resume(
            job,
            active,
            never,
            completer=completer,
            keywords_supported=supported,
            keywords_missing=missing,
            issues=issues,
        )
        variant.attempts = attempt
        variant.tokens_in += completion.input_tokens
        variant.tokens_out += completion.output_tokens
        variant.content = plan

        issues = validate_resume(plan, by_id, never, posting_terms=keywords)
        if not issues:
            text = resume_text(plan, by_id, contact)
            variant.keyword_coverage = coverage(text, keywords)
            if base_cov is None or variant.keyword_coverage >= base_cov - COVERAGE_TOLERANCE:
                break
            dropped = [
                k
                for k in keywords
                if contains_term(reachable, k)
                and contains_term(base_text, k)
                and not contains_term(text, k)
            ]
            issues = [
                Issue(
                    where="coverage",
                    message=(
                        f"covers {variant.keyword_coverage:.0%} of the posting's keywords; "
                        f"the uploaded resume covers {base_cov:.0%}. Bring back the facts "
                        "that state the dropped keywords."
                    ),
                    term=", ".join(dropped) or None,
                )
            ]
        log.warning(
            "tailoring %s: attempt %d rejected (%s)",
            job_id,
            attempt,
            "; ".join(f"{i.where}: {i.message}" for i in issues[:5]),
        )

    if issues:
        variant.status = "rejected"
        variant.issues = issues
        store.save_variant(conn, variant)
        return variant

    if with_cover_letter:
        _add_cover_letter(variant, job, active, by_id, never, keywords, contact, completer)

    store.save_variant(conn, variant)
    assert variant.id is not None
    pdf_path = settings.variants_dir / f"{job_id}-{variant.id}.pdf"
    html = resume_html(variant.content, by_id, contact, target_title=job.get("title"))
    write_pdf(html, Path(pdf_path))
    store.set_pdf_path(conn, variant.id, str(pdf_path))
    variant.pdf_path = str(pdf_path)
    return variant


def _add_cover_letter(
    variant: Variant,
    job: dict,
    active: list[Fact],
    by_id: dict[int, Fact],
    never: list[str],
    keywords: list[str],
    contact: dict[str, str],
    completer: Completer,
    max_attempts: int = 2,
) -> None:
    """Attach a validated letter, or none: a resume without a letter still ships."""
    allow = [v for v in (job.get("company"), job.get("title"), contact.get("full_name")) if v]
    issues: list[Issue] = []
    for _ in range(max_attempts):
        letter, completion = generate_cover_letter(
            job,
            active,
            never,
            variant.content,
            completer=completer,
            candidate_name=contact.get("full_name"),
            issues=issues,
        )
        variant.tokens_in += completion.input_tokens
        variant.tokens_out += completion.output_tokens
        issues = validate_cover_letter(letter, by_id, never, posting_terms=keywords, allow=allow)
        if not issues:
            variant.cover_letter = letter
            return
    log.warning("cover letter for %s dropped after validation failures", variant.job_id)
    variant.issues.extend(
        Issue(
            where=i.where,
            message=f"cover letter dropped: {i.message}",
            fact_id=i.fact_id,
            term=i.term,
        )
        for i in issues
    )
