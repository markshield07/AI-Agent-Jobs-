"""One full turn of the agent, for a schedule: find, tailor, apply, read replies.

    discover -> tailor the queued jobs that have no resume yet
             -> apply to queued jobs with a resume (in the configured mode)
             -> read the mailbox, if one is set up

Each step runs whatever the one before it left, and a step that fails is
recorded and the next one still runs: a job board being down is no reason to
skip reading replies. Submission follows `JOBAGENT_APPLY_MODE` like every other
way in, so a scheduled run in the default mode fills forms and sends nothing.

`jobagent run` does one turn; `jobagent run --every 6` keeps going, with the
pause jittered so the agent never shows up on the hour.
"""

from __future__ import annotations

import logging
import random
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jobagent.config import Settings
from jobagent.db.database import utcnow

log = logging.getLogger(__name__)

STEPS: tuple[str, ...] = ("discover", "tailor", "apply", "inbox")


@dataclass
class StepResult:
    step: str
    ok: bool
    summary: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class CycleReport:
    started_at: str
    finished_at: str | None = None
    steps: list[StepResult] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(s.ok for s in self.steps)

    def as_dict(self) -> dict[str, Any]:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "ok": self.ok,
            "steps": [
                {"step": s.step, "ok": s.ok, "summary": s.summary, "detail": s.detail}
                for s in self.steps
            ],
        }


def _discover(conn: sqlite3.Connection, settings: Settings, limits: dict[str, int]) -> StepResult:
    from jobagent.discovery.pipeline import run_discovery

    report = run_discovery(conn, settings)
    return StepResult("discover", True, report.summary(), report.as_dict())


def _tailor(conn: sqlite3.Connection, settings: Settings, limits: dict[str, int]) -> StepResult:
    from jobagent.discovery import store as jobs
    from jobagent.llm.backend import LLMError
    from jobagent.tailor import store as variants
    from jobagent.tailor.pipeline import TailorError, tailor_job

    wanted = [
        job["id"]
        for job in jobs.list_jobs(conn, status="queued", limit=500)
        if variants.latest_ready_variant(conn, job["id"]) is None
    ][: limits["tailor"]]
    ready = rejected = 0
    errors: list[str] = []
    for jid in wanted:
        try:
            variant = tailor_job(conn, jid, settings)
        except (TailorError, LLMError) as exc:
            errors.append(f"{jid}: {exc}")
            continue
        if variant.status == "ready":
            ready += 1
        else:
            rejected += 1
    summary = f"{ready} ready, {rejected} rejected by the fact check, {len(errors)} failed"
    detail = {"ready": ready, "rejected": rejected, "errors": errors}
    # One failed job is that job's problem; every one failing is the step's.
    return StepResult("tailor", not (errors and ready + rejected == 0), summary, detail)


def _apply(conn: sqlite3.Connection, settings: Settings, limits: dict[str, int]) -> StepResult:
    from jobagent.apply.pipeline import run_apply

    report = run_apply(conn, settings, limit=limits["apply"])
    return StepResult("apply", True, report.summary(), report.as_dict())


def _inbox(conn: sqlite3.Connection, settings: Settings, limits: dict[str, int]) -> StepResult:
    from jobagent.inbox.poller import poll_inbox

    if not (settings.imap_host and settings.imap_user):
        return StepResult("inbox", True, "no mailbox set up; skipped")
    report = poll_inbox(conn, settings)
    if report.error:
        return StepResult("inbox", False, report.error, report.as_dict())
    return StepResult("inbox", True, report.summary(), report.as_dict())


_RUNNERS: dict[str, Callable[[sqlite3.Connection, Settings, dict[str, int]], StepResult]] = {
    "discover": _discover,
    "tailor": _tailor,
    "apply": _apply,
    "inbox": _inbox,
}


def run_cycle(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    skip: tuple[str, ...] = (),
    tailor_limit: int = 10,
    apply_limit: int = 10,
    runners: dict[str, Callable[..., StepResult]] | None = None,
) -> CycleReport:
    """Every step not in `skip`, in order. A step that raises is a failed step."""
    runners = {**_RUNNERS, **(runners or {})}
    limits = {"tailor": tailor_limit, "apply": apply_limit}
    report = CycleReport(started_at=utcnow())
    for step in STEPS:
        if step in skip:
            continue
        try:
            result = runners[step](conn, settings, limits)
        except Exception as exc:  # one step's failure never stops the rest
            log.exception("%s step failed", step)
            result = StepResult(step, False, f"{type(exc).__name__}: {exc}")
        log.info("%s: %s", step, result.summary)
        report.steps.append(result)
    report.finished_at = utcnow()
    return report


def run_forever(
    once: Callable[[], CycleReport],
    every_hours: float,
    *,
    sleep: Callable[[float], None] = time.sleep,
    cycles: int | None = None,
) -> int:
    """Run `once` every `every_hours`, jittered by a tenth either way. Returns
    how many cycles ran; `cycles` bounds it (for tests), None runs until stopped."""
    done = 0
    while cycles is None or done < cycles:
        once()
        done += 1
        if cycles is not None and done >= cycles:
            break
        sleep(every_hours * 3600 * random.uniform(0.9, 1.1))
    return done
