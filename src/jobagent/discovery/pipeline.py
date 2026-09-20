"""One discovery run, start to finish.

    sources -> store -> enrich -> rules -> classify -> queue

Each stage reads its input back from the jobs table instead of from the stage
before it, so a run that dies half way strands nothing: whatever is still
pending is picked up by the next run. The rules pass is free and judges every
new posting; the model only sees the survivors, in batches, against a profile
that is identical across batches and therefore cached.
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from jobagent.config import Settings
from jobagent.discovery import store
from jobagent.discovery.criteria import SearchCriteria, load_criteria
from jobagent.discovery.enrich import enrich_description
from jobagent.discovery.http import make_client
from jobagent.discovery.models import RawJob
from jobagent.discovery.scoring.classify import classify_jobs
from jobagent.discovery.scoring.rules import passes, score_rules
from jobagent.discovery.sources import all_sources
from jobagent.llm.backend import Completer, LLMUnavailable, resolve_backend
from jobagent.resume.profile import profile_tags, profile_text

if TYPE_CHECKING:
    import httpx

    from jobagent.discovery.sources.base import Source

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RunReport:
    """What one run did, for the CLI and the dashboard."""

    run_id: int
    found: int = 0  # postings the sources handed back
    new: int = 0  # of which never seen before
    duplicates: int = 0
    rejected: int = 0  # missing a url, title or company
    enriched: int = 0
    scored: int = 0  # postings the rules pass judged in this run
    passed_rules: int = 0
    classified: int = 0
    queued: int = 0
    skipped: int = 0
    unclassified: int = 0  # rule survivors the model did not tier; still pending
    input_tokens: int = 0
    output_tokens: int = 0
    per_source: dict[str, int] = field(default_factory=dict)
    source_errors: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        parts = [
            f"found {self.found} ({self.new} new, {self.duplicates} seen before)",
            f"enriched {self.enriched}",
            f"rules passed {self.passed_rules} of {self.scored}",
            f"queued {self.queued}",
            f"skipped {self.skipped}",
        ]
        if self.unclassified:
            parts.append(f"{self.unclassified} awaiting the model")
        line = f"Run {self.run_id}: " + ", ".join(parts) + "."
        if self.input_tokens or self.output_tokens:
            line += f" Tokens: {self.input_tokens:,} in / {self.output_tokens:,} out."
        return line


def run_discovery(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    criteria: SearchCriteria | None = None,
    sources: Sequence[Source] | None = None,
    completer: Completer | None = None,
    client: httpx.Client | None = None,
    run_id: int | None = None,
    enrich: bool = True,
    enrich_with_model: bool = False,
    max_enrich: int = 100,
    classify: bool = True,
) -> RunReport:
    """Search, store, enrich, score and tier. Returns what happened.

    `sources`, `completer` and `client` are injectable for tests; by default
    they come from the criteria, the settings and a fresh HTTP client. With no
    usable model backend the run still completes: rule survivors stay pending
    and the report says so.
    """
    criteria = criteria or load_criteria(conn)
    run_id = store.start_run(conn) if run_id is None else run_id
    report = RunReport(run_id=run_id)

    own_client = client is None
    client = client or make_client()
    try:
        if sources is None:
            sources = all_sources(criteria, client=client)
        _search(conn, criteria, sources, report, run_id)

        if classify or enrich_with_model:
            completer = _resolve_completer(settings, completer, report)
        if enrich:
            _enrich(conn, report, client=client, completer=completer if enrich_with_model else None)
        _score(conn, criteria, report)
        if classify:
            _classify(conn, criteria, report, completer)
    finally:
        if own_client:
            client.close()
        store.finish_run(
            conn,
            run_id,
            found=report.found,
            scored=report.scored,
            tokens_in=report.input_tokens,
            tokens_out=report.output_tokens,
        )
    return report


def _resolve_completer(
    settings: Settings, completer: Completer | None, report: RunReport
) -> Completer | None:
    if completer is not None:
        return completer
    try:
        return resolve_backend(settings)
    except LLMUnavailable as exc:
        log.warning("model stages skipped: %s", exc)
        report.notes.append(f"No model backend, so rule survivors were left pending: {exc}")
        return None


def _search(
    conn: sqlite3.Connection,
    criteria: SearchCriteria,
    sources: Sequence[Source],
    report: RunReport,
    run_id: int,
) -> None:
    for source in sources:
        batch: list[RawJob] = []
        try:
            for job in source.search(criteria):
                batch.append(job)
        except Exception as exc:  # a source that cannot reach its service; keep what it yielded
            log.warning("source %s failed: %s", source.name, exc)
            report.source_errors[source.name] = f"{type(exc).__name__}: {exc}"
        report.found += len(batch)
        report.per_source[source.name] = len(batch)
        result = store.upsert_jobs(conn, batch, run_id=run_id)
        report.new += len(result.new_ids)
        report.duplicates += result.duplicates
        report.rejected += result.rejected
        log.info(
            "%s: %d postings, %d new, %d seen before",
            source.name,
            len(batch),
            len(result.new_ids),
            result.duplicates,
        )


def _enrich(
    conn: sqlite3.Connection,
    report: RunReport,
    *,
    client: httpx.Client,
    completer: Completer | None,
    max_enrich: int = 100,
) -> None:
    todo = store.jobs_needing_enrichment(conn)
    if len(todo) > max_enrich:
        report.notes.append(
            f"Enrichment capped at {max_enrich} of {len(todo)} postings; "
            "the rest wait for the next run."
        )
        todo = todo[:max_enrich]
    for job in todo:
        text = enrich_description(job["url"], client=client, completer=completer)
        if text:
            store.set_description(conn, job["id"], text)
            report.enriched += 1


def _score(conn: sqlite3.Connection, criteria: SearchCriteria, report: RunReport) -> None:
    tags = profile_tags(conn)
    for job in store.jobs_pending_rules(conn):
        result = score_rules(job, criteria, tags)
        store.set_rule_score(conn, job["id"], result.score, result.reason)
        report.scored += 1
        if passes(result, criteria):
            report.passed_rules += 1
        else:
            store.set_status(conn, job["id"], "skipped")
            report.skipped += 1


def _classify(
    conn: sqlite3.Connection,
    criteria: SearchCriteria,
    report: RunReport,
    completer: Completer | None,
) -> None:
    candidates = store.jobs_pending_classification(conn, criteria.min_score)
    if not candidates:
        return
    if completer is None:
        report.unclassified += len(candidates)
        return

    result = classify_jobs(candidates, profile=profile_text(conn), completer=completer)
    report.input_tokens += result.input_tokens
    report.output_tokens += result.output_tokens
    for item in result.items:
        store.set_classification(
            conn,
            item.job_id,
            tier=item.tier,
            fit_score=item.fit_score,
            category=item.category,
            reason=item.reason,
        )
        report.classified += 1
        if item.tier <= criteria.max_tier:
            store.set_status(conn, item.job_id, "queued")
            report.queued += 1
        else:
            store.set_status(conn, item.job_id, "skipped")
            report.skipped += 1
    report.unclassified += len(result.unclassified)
