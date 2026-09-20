"""The job sources a run searches, chosen from the criteria.

Company ATS boards are public JSON with full descriptions and no login, so
they come first (job-agent, AIApplyJobs). JobSpy covers the aggregators
(Indeed, LinkedIn and the rest) by scraping, which is slower and rate-limited,
so it only runs when the criteria name a site and a title to search for.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from jobagent.discovery.criteria import SearchCriteria
from jobagent.discovery.sources.ashby import AshbySource
from jobagent.discovery.sources.greenhouse import GreenhouseSource
from jobagent.discovery.sources.jobspy_source import JobSpySource
from jobagent.discovery.sources.lever import LeverSource

if TYPE_CHECKING:
    import httpx

    from jobagent.discovery.sources.base import Source

__all__ = ["all_sources"]


def all_sources(criteria: SearchCriteria, client: httpx.Client | None = None) -> list[Source]:
    """Every source that has something to search, given these criteria."""
    wanted = {board.ats for board in criteria.boards}
    sources: list[Source] = []
    if "greenhouse" in wanted:
        sources.append(GreenhouseSource(client))
    if "lever" in wanted:
        sources.append(LeverSource(client))
    if "ashby" in wanted:
        sources.append(AshbySource(client))
    if criteria.jobspy_sites and criteria.titles:
        sources.append(JobSpySource())
    return sources
