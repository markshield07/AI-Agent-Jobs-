"""The model pass: tier rule-passing jobs against the candidate's profile.

Adapted from job-agent's batched classifier. The rules pass is cheap and
keyword-shaped, so it lets through anything that mentions the right words;
this pass reads each posting against the profile and says how well it fits.
Jobs go to the model in batches under one system prompt that carries the
profile, so the profile is the cacheable prefix and each batch pays only for
its own postings. A batch the model fails is isolated: its ids come back as
unclassified and the other batches still run, since a job left untiered stays
pending and is picked up next run.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from jobagent.llm.backend import Completer, LLMError

log = logging.getLogger(__name__)


class ClassifiedJob(BaseModel):
    job_id: str = Field(description="The job_id exactly as given in the posting block.")
    tier: int = Field(
        ge=1,
        le=4,
        description="1 = strong match, 2 = good match, 3 = weak or a stretch, 4 = not a fit.",
    )
    fit_score: int = Field(
        ge=0, le=100, description="Overall confidence 0-100 that the candidate fits this role."
    )
    category: str = Field(
        description='A short label for the kind of role, e.g. "Backend Engineering".'
    )
    reason: str = Field(
        description="One sentence citing something from the posting and something from the profile."
    )


class ClassificationBatch(BaseModel):
    items: list[ClassifiedJob] = Field(description="One item per job_id in the request.")


@dataclass(slots=True)
class ClassifyResult:
    items: list[ClassifiedJob] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    unclassified: list[str] = field(default_factory=list)  # job ids the model did not return


_SYSTEM_HEAD = """You rank job postings for one specific candidate. For each posting you are \
given, judge how well it fits this candidate and assign a tier.

Tiers:
1 = strong match: the candidate meets the core requirements and the role sits squarely in \
their discipline; apply first.
2 = good match: the candidate meets most requirements and could credibly be shortlisted.
3 = weak match or a stretch: real gaps in seniority, stack, domain or a stated requirement.
4 = not a fit: a different discipline, a hard requirement the profile does not meet, or a \
role the candidate could not credibly apply for.

Category: a short label for the kind of role, such as "Backend Engineering", \
"Frontend Engineering", "Full-Stack Engineering", "Mobile Engineering", "Data Engineering", \
"Data Science", "Machine Learning", "DevOps / SRE", "Security", "QA / Test", \
"Engineering Management", "Product Management", "Design", "Sales" or "Other". Use the same \
label for the same kind of role across postings so they group cleanly.

Rules:
- Judge only from the candidate profile below and the posting text. Never assume skills, \
years of experience, credentials or eligibility the profile does not state.
- A title that is a different discipline from the candidate's is tier 4 regardless of how \
many keywords the posting shares with the profile.
- fit_score is your overall confidence, 0-100, that this candidate would be a competitive \
applicant; keep it consistent with the tier.
- reason is one sentence that cites something specific from the posting and something \
specific from the profile.
- Return exactly one item per job_id in the request, using each id exactly as written.

Candidate profile:
"""


def _text(value: Any) -> str:
    """A prompt-safe string: None becomes empty, anything else is str()-ed and stripped."""
    return "" if value is None else str(value).strip()


def _job_id(job: Mapping[str, Any]) -> str:
    return _text(job.get("id"))


def build_system_prompt(profile: str) -> str:
    """The role, tiers, rules and profile. Identical for every batch of a run."""
    return _SYSTEM_HEAD + (_text(profile) or "(no resume on file)")


def _money(value: Any) -> str | None:
    """A salary bound for the prompt, None when absent.

    Sources store ints, but the module accepts any mapping and a string amount
    must not abort the whole classification pass over a thousands separator.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f"{value:,.0f}"
    return _text(value) or None


def _salary_line(job: Mapping[str, Any]) -> str | None:
    low, high = _money(job.get("salary_min")), _money(job.get("salary_max"))
    if low and high:
        return f"Salary: {low} - {high}"
    if low:
        return f"Salary: from {low}"
    if high:
        return f"Salary: up to {high}"
    return None


def _location_line(job: Mapping[str, Any]) -> str:
    location = _text(job.get("location"))
    remote = job.get("remote")
    if location and remote:
        return f"Location: {location} (remote)"
    if location:
        return f"Location: {location}"
    return "Location: remote" if remote else "Location: not stated"


def _job_block(job: Mapping[str, Any], max_description_chars: int) -> str:
    description = _text(job.get("description"))
    if not description:
        description = "(no description available)"
    elif len(description) > max_description_chars:
        description = description[:max_description_chars].rstrip() + "\n[truncated]"

    lines = [
        f"### job_id: {_job_id(job)}",
        f"Title: {_text(job.get('title')) or '(untitled)'}",
        f"Company: {_text(job.get('company')) or '(unknown)'}",
        _location_line(job),
    ]
    salary = _salary_line(job)
    if salary:
        lines.append(salary)
    lines.append("Description:")
    lines.append(description)
    return "\n".join(lines)


def build_batch_prompt(
    batch: Sequence[Mapping[str, Any]], max_description_chars: int = 2500
) -> str:
    """One block per posting, framed by the list of ids the answer must cover."""
    if max_description_chars < 1:
        raise ValueError("max_description_chars must be at least 1")
    ids = ", ".join(_job_id(job) for job in batch)
    head = (
        f"Classify these {len(batch)} postings for the candidate. Return exactly one item "
        f"per job_id, for exactly these ids and no others: {ids}"
    )
    blocks = [_job_block(job, max_description_chars) for job in batch]
    return head + "\n\n" + "\n\n".join(blocks)


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


def _reconcile(
    batch_ids: list[str], returned: Sequence[ClassifiedJob]
) -> tuple[list[ClassifiedJob], list[str]]:
    """First item per id wins; ids not in the batch are dropped; the rest are missing."""
    wanted = set(batch_ids)
    kept: dict[str, ClassifiedJob] = {}
    for item in returned:
        jid = item.job_id.strip()
        if jid not in wanted:
            log.warning("classifier returned an id that was not asked for: %r", jid)
            continue
        if jid in kept:
            continue
        # The schema bounds these already; the clamp covers a backend that does
        # not validate, so a stray 0 or 5 cannot reach the database.
        kept[jid] = ClassifiedJob(
            job_id=jid,
            tier=_clamp(item.tier, 1, 4),
            fit_score=_clamp(item.fit_score, 0, 100),
            category=item.category.strip(),
            reason=item.reason.strip(),
        )
    items = [kept[jid] for jid in batch_ids if jid in kept]
    missing = [jid for jid in batch_ids if jid not in kept]
    return items, missing


def _distinct_rows(jobs: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Rows with a usable id, first occurrence of each; the rest are logged and dropped."""
    rows: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for job in jobs:
        jid = _job_id(job)
        if not jid:
            log.warning("skipping a job with no id: %r", _text(job.get("title")))
        elif jid in seen:
            log.warning("skipping a repeated job id: %s", jid)
        else:
            seen.add(jid)
            rows.append(job)
    return rows


def classify_jobs(
    jobs: Sequence[Mapping[str, Any]],
    *,
    profile: str,
    completer: Completer,
    batch_size: int = 8,
    effort: str = "low",
    max_description_chars: int = 2500,
) -> ClassifyResult:
    """Tier every job in `jobs` (jobs-table rows as dicts) against `profile`.

    Makes one model call per `batch_size` jobs and none for an empty list.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    if max_description_chars < 1:
        raise ValueError("max_description_chars must be at least 1")

    result = ClassifyResult()
    rows = _distinct_rows(jobs)
    if not rows:
        return result

    system = build_system_prompt(profile)
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        batch_ids = [_job_id(job) for job in batch]
        prompt = build_batch_prompt(batch, max_description_chars)
        try:
            completion = completer.complete(
                system=system,
                prompt=prompt,
                output=ClassificationBatch,
                effort=effort,
                cache_system=True,
            )
        except (LLMError, ValidationError) as exc:
            # The API backend validates the model's JSON after the call, and the
            # SDK drops numeric bounds from the schema it sends, so a tier of 5
            # arrives as a pydantic ValidationError rather than an LLMError.
            log.error("classifier batch of %d failed: %s", len(batch), exc)
            result.unclassified.extend(batch_ids)
            continue

        result.input_tokens += completion.input_tokens
        result.output_tokens += completion.output_tokens
        returned = completion.result
        if not isinstance(returned, ClassificationBatch):
            log.error("classifier returned %s, not a ClassificationBatch", type(returned).__name__)
            result.unclassified.extend(batch_ids)
            continue

        items, missing = _reconcile(batch_ids, returned.items)
        if missing:
            log.warning("classifier left %d of %d job(s) untiered", len(missing), len(batch))
        result.items.extend(items)
        result.unclassified.extend(missing)

    return result
