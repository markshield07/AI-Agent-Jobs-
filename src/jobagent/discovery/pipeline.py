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
import random
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from jobagent.config import Settings
from jobagent.discovery import store
from jobagent.discovery.criteria import SearchCriteria, load_criteria
from jobagent.discovery.enrich import RateLimited, enrich_description
from jobagent.discovery.http import make_client
from jobagent.discovery.models import RawJob
from jobagent.discovery.scoring.classify import classify_jobs
from jobagent.discovery.scoring.rules import RuleScore, passes, score_rules
from jobagent.discovery.sources import all_sources
from jobagent.discovery.sources.filters import title_matches
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
    # Why postings failed the rules pass this run, by kind of reason.
    rule_misses: dict[str, int] = field(default_factory=dict)
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
        if self.rule_misses:
            top = sorted(self.rule_misses.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
            line += " Turned away by the rules: " + ", ".join(f"{k} {n}" for k, n in top) + "."
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
            sources = all_sources(criteria, client=client, apply_sites=settings.apply_site_list)
        _search(conn, criteria, sources, report, run_id)

        if classify or enrich_with_model:
            completer = _resolve_completer(settings, completer, report)
        if enrich:
            _enrich(
                conn,
                report,
                client=client,
                completer=completer if enrich_with_model else None,
                criteria=criteria,
            )
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
    criteria: SearchCriteria | None = None,
    max_enrich: int = 100,
    sleep: Any = None,
) -> None:
    """Fetch the full postings, those whose title matches first. LinkedIn
    answers 429 Too Many Requests to a quick run of page loads, so its pages
    are spaced out and a run reads only so many, and a 429 from any site ends
    that site's fetches for the run: the rest wait for the next one."""
    sleep = sleep or time.sleep
    todo = store.jobs_needing_enrichment(conn)
    if criteria is not None:
        todo.sort(key=lambda job: not title_matches(job.get("title") or "", criteria))
    if len(todo) > max_enrich:
        report.notes.append(
            f"Enrichment capped at {max_enrich} of {len(todo)} postings; "
            "the rest wait for the next run."
        )
        todo = todo[:max_enrich]
    last: dict[str, float] = {}
    stopped: set[str] = set()
    counted: dict[str, int] = {}
    for job in todo:
        host = _host(job["url"])
        if host in stopped:
            continue
        cap = next((c for h, c in ENRICH_CAPS.items() if host.endswith(h)), None)
        if cap is not None and counted.get(host, 0) >= cap:
            continue
        counted[host] = counted.get(host, 0) + 1
        gap = next((g for h, g in ENRICH_GAPS.items() if host.endswith(h)), 0.0)
        if gap and host in last:
            wait = last[host] + gap * random.uniform(0.8, 1.4) - time.monotonic()
            if wait > 0:
                sleep(wait)
        last[host] = time.monotonic()
        try:
            text = enrich_description(job["url"], client=client, completer=completer)
        except RateLimited:
            stopped.add(host)
            report.notes.append(
                f"{host} said too many requests; its other postings are read next run."
            )
            continue
        except Exception as exc:  # one unfetchable page must not end the run
            log.warning("enrichment of %s failed: %s", job["url"], exc)
            continue
        if text:
            store.set_description(conn, job["id"], text)
            report.enriched += 1


# Seconds between page loads on a site that limits them, before jitter, and
# how many of its pages one run reads at most (LinkedIn answered 429 at 4s).
ENRICH_GAPS = {"linkedin.com": 8.0}
ENRICH_CAPS = {"linkedin.com": 25}


def _host(url: str) -> str:
    from urllib.parse import urlparse

    return (urlparse(url or "").netloc or "").lower()


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
            kind = miss_kind(result, criteria)
            report.rule_misses[kind] = report.rule_misses.get(kind, 0) + 1


def miss_kind(result: RuleScore, criteria: SearchCriteria) -> str:
    """A short name for why a posting failed the rules, to count them by."""
    if result.disqualified:
        reason = result.reason
        if reason.startswith("not remote"):
            return "not remote"
        if reason.startswith("salary"):
            return "salary under the minimum"
        return reason.split(":", 1)[0]
    weak = [name for name, pts in result.breakdown.items() if pts == 0]
    if "title" in weak:
        return "title not a match"
    if "location" in weak:
        return "location not a match"
    return f"score under {criteria.min_score}"


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
