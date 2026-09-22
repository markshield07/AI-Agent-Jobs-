"""One handler per applicant tracking system, plus the generic fallback.

`handler_for` picks by the application URL first and the job's recorded ATS
second; the generic filler takes anything left when it is allowed. Handlers
are imported lazily so a missing optional module never breaks the package.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Sequence

from jobagent.apply.models import Handler
from jobagent.discovery.ats import detect_ats

log = logging.getLogger(__name__)

# Module and class per ATS, in the order they are tried.
_KNOWN: tuple[tuple[str, str, str], ...] = (
    ("greenhouse", "jobagent.apply.handlers.greenhouse", "GreenhouseHandler"),
    ("lever", "jobagent.apply.handlers.lever", "LeverHandler"),
    ("ashby", "jobagent.apply.handlers.ashby", "AshbyHandler"),
)
_GENERIC = ("generic", "jobagent.apply.handlers.generic", "GenericHandler")


def _load(spec: tuple[str, str, str]) -> Handler | None:
    ats, module_name, class_name = spec
    try:
        module = importlib.import_module(module_name)
        return getattr(module, class_name)()
    except (ImportError, AttributeError) as exc:
        log.warning("no %s handler available: %s", ats, exc)
        return None


def default_handlers(*, generic: bool = True) -> list[Handler]:
    specs = [*_KNOWN, _GENERIC] if generic else [*_KNOWN]
    return [h for h in (_load(spec) for spec in specs) if h is not None]


def handler_for(
    url: str | None, ats_type: str | None, handlers: Sequence[Handler]
) -> Handler | None:
    """The first handler that claims `url`, else the one named by `ats_type`,
    else the generic one if present."""
    for handler in handlers:
        if url and handler.ats != "generic" and handler.matches(url):
            return handler
    ats = ats_type or detect_ats(url)
    for handler in handlers:
        if handler.ats == ats:
            return handler
    for handler in handlers:
        if handler.ats == "generic":
            return handler
    return None
