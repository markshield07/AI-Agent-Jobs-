"""What response tracking works with: a message pulled out of the mailbox, the
application it belongs to, what it says, and what one poll did.

The shapes here are deliberately inert. A message is parsed once, matched by a
pure function and read by another, and only the poller touches the database,
so both hard parts can be tested against fixtures with no mailbox in the room.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# What a reply can say. `acknowledgement` is the automated "we got it" that
# every applicant tracking system sends; it is worth recording and worth no
# status change, which is why it is a label and not a status.
Label = Literal[
    "acknowledgement",
    "screening",
    "interviewing",
    "offer",
    "rejected",
    "withdrawn",
    "unknown",
]

# The status each label moves an application to. A label absent from this map
# never moves one.
LABEL_STATUS: dict[str, str] = {
    "screening": "screening",
    "interviewing": "interviewing",
    "offer": "offer",
    "rejected": "rejected",
    "withdrawn": "withdrawn",
}

# How far along each status is. A reply may only move an application forward,
# with one exception: a rejection or a withdrawal ends it from anywhere, which
# is what the poller checks with `is_terminal`.
STATUS_RANK: dict[str, int] = {
    "applied": 0,
    "screening": 1,
    "interviewing": 2,
    "offer": 3,
    "rejected": 4,
    "withdrawn": 4,
}

TERMINAL_STATUSES = ("rejected", "withdrawn")


def is_terminal(status: str | None) -> bool:
    return status in TERMINAL_STATUSES


@dataclass(slots=True)
class InboxMessage:
    """One mail, flattened. `body` is plain text however it arrived."""

    message_id: str
    subject: str = ""
    from_addr: str = ""
    from_name: str = ""
    to_addr: str = ""
    received_at: str = ""  # ISO 8601, UTC
    body: str = ""
    folder: str = "INBOX"

    @property
    def sender_domain(self) -> str:
        _, _, domain = self.from_addr.partition("@")
        return domain.strip().lower()

    @property
    def text(self) -> str:
        """Subject and body together: what both matching and reading look at."""
        return f"{self.subject}\n{self.body}"

    def excerpt(self, limit: int = 400) -> str:
        flat = " ".join(self.body.split())
        return flat[:limit]

    def as_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "subject": self.subject,
            "from_addr": self.from_addr,
            "from_name": self.from_name,
            "received_at": self.received_at,
            "excerpt": self.excerpt(),
        }


@dataclass(slots=True)
class Match:
    """Which application a message belongs to, and why we think so."""

    application_id: int
    confidence: float
    reasons: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return ", ".join(self.reasons)

    def as_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "confidence": round(self.confidence, 2),
            "reason": self.reason,
        }


@dataclass(slots=True)
class Reading:
    """What a message says about the application, and where that came from."""

    label: Label
    confidence: float
    reason: str = ""
    source: Literal["rules", "model", "none", "manual"] = "rules"

    @property
    def status(self) -> str | None:
        return LABEL_STATUS.get(self.label)

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "status": self.status,
            "confidence": round(self.confidence, 2),
            "reason": self.reason,
            "source": self.source,
        }


UNREAD = Reading(label="unknown", confidence=0.0, reason="no rule matched", source="none")


@dataclass(slots=True)
class PollReport:
    """What one pass over the mailbox did."""

    fetched: int = 0
    skipped: int = 0  # already recorded on an earlier poll
    matched: int = 0
    unmatched: int = 0
    advanced: int = 0  # applications whose status moved
    results: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    def summary(self) -> str:
        parts = [
            f"{self.fetched} messages",
            f"{self.matched} matched",
            f"{self.unmatched} unmatched",
            f"{self.advanced} statuses moved",
        ]
        if self.skipped:
            parts.append(f"{self.skipped} already seen")
        return ", ".join(parts) + "."

    def as_dict(self) -> dict[str, Any]:
        return {
            "fetched": self.fetched,
            "skipped": self.skipped,
            "matched": self.matched,
            "unmatched": self.unmatched,
            "advanced": self.advanced,
            "results": self.results,
            "notes": self.notes,
            "error": self.error,
        }
