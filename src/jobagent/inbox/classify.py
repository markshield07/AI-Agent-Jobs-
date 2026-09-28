"""Reading what a reply says.

Rules first, the model second, and neither is trusted far. The phrases below
are the ones an applicant tracking system actually sends, and they are specific
enough that a hit is close to decisive; anything they do not cover goes to the
model, and a reading nobody is confident about stays `unknown`, which records
the mail and moves no status. That asymmetry is the point: a missed interview
invitation costs the user a look in their own inbox, a wrong rejection costs
them the interview.

The model never sees more than the subject, the sender and the first few
thousand characters of the body, and it decides one label and nothing else.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from jobagent.llm.backend import Completer, LLMError

from .models import UNREAD, InboxMessage, Reading

log = logging.getLogger(__name__)

STRONG, NORMAL, WEAK = 0.6, 0.35, 0.2
MAX_PROMPT_CHARS = 4_000

# Phrase, weight. Matched against the subject and body together, lowercased and
# with runs of whitespace collapsed, so a phrase spanning a line break still hits.
_RULES: dict[str, list[tuple[str, float]]] = {
    "rejected": [
        (r"decided (?:to|not to) (?:move|go|proceed|progress) (?:forward|ahead)", STRONG),
        (r"(?:will|won't|will not|shall not) be (?:moving|progressing|proceeding) forward", STRONG),
        (r"move forward with other (?:candidates|applicants)", STRONG),
        (r"pursue other (?:candidates|applicants)", STRONG),
        (r"not (?:be )?(?:selected|shortlisted|progressing) ", STRONG),
        (r"we regret to inform", STRONG),
        (r"no longer under consideration", STRONG),
        (r"(?:position|role|req(?:uisition)?) (?:has been|was) (?:filled|closed)", NORMAL),
        (r"not a (?:fit|match) (?:for|at) this time", NORMAL),
        (r"keep your (?:details|r[eé]sum[eé]|cv) on file", WEAK),
        (r"wish you (?:the best|well) in your (?:job )?search", WEAK),
    ],
    "offer": [
        (r"(?:pleased|excited|delighted|happy) to (?:extend|offer|make you)", STRONG),
        (r"offer of employment", STRONG),
        (r"offer letter", STRONG),
        (r"\bextend(?:ing)? (?:you )?an offer\b", STRONG),
        (r"\bformal offer\b", NORMAL),
        (r"\bcompensation package\b", WEAK),
    ],
    "interviewing": [
        (r"(?:invite|inviting) you to (?:an? )?(?:interview|next round|onsite)", STRONG),
        (r"invitation to interview", STRONG),
        (r"(?:schedule|set up|book) (?:an?|your|the) (?:interview|onsite|next round)", STRONG),
        (r"(?:technical|onsite|on-site|panel|final)(?: round)? interview", STRONG),
        (r"move (?:you )?(?:forward|ahead) to the (?:next|interview)", STRONG),
        (r"\bnext round\b", NORMAL),
        (r"(?:calendly|savvycal|greenhouse\.io/scheduling|book(?:ing)? a time)", NORMAL),
        (r"(?:find|pick|choose) a time that works", NORMAL),
        (r"\binterview (?:loop|panel|day)\b", NORMAL),
    ],
    "screening": [
        (r"(?:phone|initial|intro(?:ductory)?|recruiter) (?:screen|call|chat)", STRONG),
        (r"(?:take[- ]home|coding|online|skills) (?:assessment|challenge|exercise|test)", STRONG),
        (r"(?:would|we'?d) (?:love|like) to (?:connect|chat|speak|learn more)", NORMAL),
        (r"available for a (?:quick |short |brief )?(?:call|chat)", NORMAL),
        (r"(?:hear|learn) more about your (?:background|experience)", NORMAL),
        (r"\byour (?:application|profile) (?:stood out|caught)", NORMAL),
    ],
    "withdrawn": [
        (r"(?:you|your application) (?:have |has )?(?:been )?withdrawn", STRONG),
        (r"withdrawn your (?:application|candidacy)", STRONG),
    ],
    "acknowledgement": [
        (r"thank(?:s| you) for (?:applying|your application|your interest)", NORMAL),
        (r"(?:we|i) (?:have |'ve )?received your application", NORMAL),
        (r"your application (?:has been|was) received", NORMAL),
        (r"application (?:confirmation|received)", NORMAL),
        (r"(?:currently |now )?reviewing your application", WEAK),
        (r"\bdo not reply to this (?:e-?mail|message)\b", WEAK),
    ],
}

_COMPILED = {
    label: [(re.compile(pattern), weight) for pattern, weight in rules]
    for label, rules in _RULES.items()
}

# Labels that cannot both be true of one message. When the two best readings
# are one of these pairs and neither is clearly ahead, the rules abstain.
_CONTRADICTORY = (
    frozenset({"rejected", "interviewing"}),
    frozenset({"rejected", "offer"}),
    frozenset({"rejected", "screening"}),
)
_CLEARLY_AHEAD = 0.2


def _flatten(text: str) -> str:
    return " ".join(text.lower().split())


def score_labels(message: InboxMessage) -> dict[str, tuple[float, list[str]]]:
    """Per label the rules know about: how well it fits, and the words that said so."""
    haystack = _flatten(message.text)
    scores: dict[str, tuple[float, list[str]]] = {}
    for label, rules in _COMPILED.items():
        total, hits = 0.0, []
        for pattern, weight in rules:
            found = pattern.search(haystack)
            if found:
                total += weight
                hits.append(found.group(0))
        if hits:
            scores[label] = (min(total, 0.95), hits)
    return scores


def read_by_rules(message: InboxMessage) -> Reading:
    """What the phrase list makes of this message; `unknown` when it abstains."""
    scores = score_labels(message)
    if not scores:
        return UNREAD
    ranked = sorted(scores.items(), key=lambda kv: kv[1][0], reverse=True)
    label, (best, hits) = ranked[0]
    if len(ranked) > 1:
        runner, (second, _) = ranked[1]
        if frozenset({label, runner}) in _CONTRADICTORY and best - second < _CLEARLY_AHEAD:
            return Reading(
                label="unknown",
                confidence=0.0,
                reason=f"says both {label} and {runner}",
                source="rules",
            )
    return Reading(
        label=label,  # type: ignore[arg-type]
        confidence=best,
        reason=f"said {hits[0]!r}",
        source="rules",
    )


# ------------------------------------------------------------------ model --


class ReplyReading(BaseModel):
    label: Literal[
        "acknowledgement", "screening", "interviewing", "offer", "rejected", "withdrawn", "unknown"
    ] = Field(description="What this message says about the application.")
    confidence: float = Field(ge=0.0, le=1.0, description="0-1, how sure you are of the label.")
    reason: str = Field(description="One short sentence quoting the words that decided it.")


_SYSTEM = """You read replies to job applications and label each one. You are given one \
email. Answer with one label, how sure you are, and the words that decided it.

Labels:
acknowledgement = an automated confirmation that the application arrived. Nothing has \
happened yet.
screening = a recruiter wants a first call, or sends a take-home or an online assessment.
interviewing = an interview is being scheduled or has been offered, at any round.
offer = an offer of employment is being made.
rejected = the company has decided not to proceed.
withdrawn = the application was withdrawn, by either side, without a decision.
unknown = anything else, including newsletters, job alerts, and mail that is not about \
this application.

Rules:
- Label only what the message says. Never infer a decision from tone.
- A message that thanks the candidate and says nothing else is an acknowledgement, not a \
rejection.
- "unfortunately" on its own is not a rejection; a statement that they are not moving \
forward is.
- If the message could reasonably be two of these, answer unknown with a low confidence. \
Being unsure is a useful answer here; guessing is not.
- confidence is 0 to 1. Use above 0.8 only when the message states it outright."""


def read_by_model(message: InboxMessage, completer: Completer) -> Reading:
    """Ask the model. Any failure comes back as `unknown`, never as an exception."""
    prompt = (
        f"From: {message.from_name} <{message.from_addr}>\n"
        f"Subject: {message.subject}\n\n"
        f"{message.body[:MAX_PROMPT_CHARS]}"
    )
    try:
        completion = completer.complete(
            system=_SYSTEM, prompt=prompt, output=ReplyReading, max_tokens=1000, cache_system=True
        )
    except (LLMError, ValidationError) as exc:
        log.warning("could not read %s with the model: %s", message.message_id, exc)
        return Reading(label="unknown", confidence=0.0, reason=str(exc), source="none")
    read = completion.result
    assert isinstance(read, ReplyReading)
    return Reading(label=read.label, confidence=read.confidence, reason=read.reason, source="model")


def read_message(
    message: InboxMessage,
    *,
    completer: Completer | None = None,
    min_confidence: float = 0.6,
) -> Reading:
    """The rules, then the model on what they could not settle."""
    reading = read_by_rules(message)
    if reading.label != "unknown" and reading.confidence >= min_confidence:
        return reading
    if completer is None:
        return reading

    from_model = read_by_model(message, completer)
    if from_model.label != "unknown" and from_model.confidence >= min_confidence:
        return from_model
    if reading.label != "unknown":
        return reading  # the rules said something, just not confidently enough
    # Neither was sure. A label nobody stands behind would read on the
    # dashboard as a decision, so the answer is `unknown` and what the model
    # leaned towards goes in the reason.
    leaning = (
        f"unsure, closest is {from_model.label}: {from_model.reason}"
        if from_model.label != "unknown"
        else from_model.reason
    )
    return Reading(
        label="unknown", confidence=from_model.confidence, reason=leaning, source=from_model.source
    )
