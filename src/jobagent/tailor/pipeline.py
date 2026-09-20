"""Tailor the resume to one job: plan, check, gate, render, keep.

    facts + posting -> model plan -> validator -> coverage gate -> PDF

The model proposes; the validator disposes. A plan that claims anything the
fact base does not state goes back to the model with the exact objections,
once, and a second failure is kept as a rejected variant rather than shipped.
The coverage gate applies the same discipline to usefulness: a tailored resume
that mentions fewer of the posting's keywords than the uploaded one did is not
an improvement, so it is sent back too.
"""

from __future__ import annotations

import logging
import sqlite3
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

CONTACT_KEYS = ("full_name", "email", "phone", "location")


class TailorError(RuntimeError):
    """The job cannot be tailored for as things stand: no such job, or no facts."""


def _uploaded_resume(conn: sqlite3.Connection) -> dict | None:
    """The newest uploaded resume's id and text, the baseline the coverage gate uses."""
    row = conn.execute(
        "SELECT id, parsed_text FROM resume_base ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {"id": row["id"], "parsed_text": row["parsed_text"]} if row else None


def tailor_job(
    conn: sqlite3.Connection,
    job_id: str,
    settings: Settings,
    *,
    completer: Completer | None = None,
    with_cover_letter: bool = True,
    max_attempts: int = 2,
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
        exclude=[company, *company.split()],
    )
    supported, missing = split_by_support(keywords, fact_pool_text(active))

    base = _uploaded_resume(conn)
    base_text = base["parsed_text"] if base else ""
    base_cov = coverage(base_text, keywords) if base else None

    completer = completer or resolve_backend(settings)
    variant = Variant(
        job_id=job_id,
        base_id=base["id"] if base else None,
        content=TailoredResume(),
        keywords=keywords,
        keywords_missing=missing,
        base_coverage=base_cov,
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
            if base_cov is None or variant.keyword_coverage >= base_cov:
                break
            dropped = [
                k for k in supported if contains_term(base_text, k) and not contains_term(text, k)
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
