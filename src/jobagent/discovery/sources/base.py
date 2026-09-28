"""The contract every job source implements."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol

from jobagent.discovery.criteria import SearchCriteria
from jobagent.discovery.models import RawJob


class Source(Protocol):
    """A place that lists jobs.

    `search` yields postings lazily and must never raise for an individual bad
    posting: log it, skip it, keep going. A source that cannot reach its
    service at all may raise; the run records the failure and continues with
    the other sources.
    """

    name: str

    def search(self, criteria: SearchCriteria) -> Iterator[RawJob]: ...
