"""What submission works with: the fields on a form, the packet that fills
them, the plan, and what a handler reports back.

Two things shape this module. The answering logic is pure: it turns a list of
`FormField` into a `FillPlan` with no browser in the room, so it can be tested
exhaustively, and a handler only executes the plan against the page. And a
form can ask something nobody on file can answer; the right result then is not
a guess but a `NeededInput`, which the user answers once, into the answer
bank, and never again.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

FieldKind = Literal[
    "text",
    "textarea",
    "email",
    "tel",
    "url",
    "number",
    "date",
    "select",
    "multiselect",
    "radio",
    "checkbox",
    "file",
    "unknown",
]
Section = Literal[
    "contact", "resume", "cover_letter", "links", "questions", "eeo", "consent", "other"
]
Mode = Literal["dry_run", "review", "auto"]
Outcome = Literal[
    "submitted", "dry_run", "review", "needs_input", "blocked", "unconfirmed", "failed"
]
FillSource = Literal[
    "contact",
    "resume",
    "cover_letter",
    "links",
    "answer_bank",
    "model",
    "default",
    "skip",
    "prefilled",  # the site filled it from the signed-in account; left as it was
]

MODES: tuple[str, ...] = ("dry_run", "review", "auto")
OUTCOMES: tuple[str, ...] = (
    "submitted",
    "dry_run",
    "review",
    "needs_input",
    "blocked",
    "unconfirmed",
    "failed",
)


@dataclass(slots=True)
class FormField:
    """One control on an application form, as the handler found it."""

    key: str  # stable within one form: the input's name or id, else handler-made
    label: str
    kind: FieldKind
    required: bool = False
    options: list[str] = field(default_factory=list)  # selects, radios, checkbox groups
    section: Section = "other"
    selector: str = ""  # how the handler locates the control; opaque to the answerer
    name: str | None = None
    accept: str | None = None  # file inputs: the accept attribute
    multiple: bool = False
    help_text: str | None = None


@dataclass(slots=True)
class Fill:
    """One value the plan puts on the form. `file_path` for uploads, else `value`."""

    key: str
    value: str | list[str] | bool | None = None
    file_path: str | None = None
    source: FillSource = "answer_bank"
    label: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "value": self.value,
            "file_path": self.file_path,
            "source": self.source,
        }


@dataclass(slots=True)
class NeededInput:
    """A question the form asks that nothing on file answers."""

    key: str
    label: str
    kind: FieldKind
    required: bool
    options: list[str] = field(default_factory=list)
    reason: str = ""
    answer_key: str = ""  # the answer-bank key an answer is stored under

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "required": self.required,
            "options": self.options,
            "reason": self.reason,
            "answer_key": self.answer_key,
        }


@dataclass(slots=True)
class FillPlan:
    fills: list[Fill] = field(default_factory=list)
    needed: list[NeededInput] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def complete(self) -> bool:
        """True when every required field has a fill; optional gaps do not block."""
        return not any(n.required for n in self.needed)


@dataclass(slots=True)
class Packet:
    """Everything a handler may put on a form: the user's own data, nothing else."""

    job: dict[str, Any]
    contact: dict[str, str]  # full_name, first_name, last_name, email, phone, location
    resume_path: str
    cover_letter: str | None = None
    cover_letter_path: str | None = None
    links: dict[str, str] = field(default_factory=dict)  # linkedin, github, website, ...
    answers: dict[str, str] = field(default_factory=dict)  # the answer bank, key -> value
    facts: list[Any] = field(default_factory=list)  # active Facts: all a model answer may use
    never_claim: list[str] = field(default_factory=list)
    variant_id: int | None = None


@dataclass(slots=True)
class HandlerResult:
    """What happened on the page. `submitted` only when the confirmation was seen;
    `unconfirmed` when the button was pressed and neither a confirmation nor an
    error followed, which a person must check before the job is tried again."""

    outcome: Outcome
    filled: list[Fill] = field(default_factory=list)
    needed: list[NeededInput] = field(default_factory=list)
    fields: list[FormField] = field(default_factory=list)
    confirmation: str | None = None
    error: str | None = None
    screenshot_path: str | None = None
    final_url: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


# Turns the fields a handler found into a plan. The pipeline builds one per job
# with the packet, the answer bank and (optionally) the model bound in, so a
# handler never talks to the model itself.
Answerer = Callable[[list[FormField]], FillPlan]


class Handler(Protocol):
    """Fills one applicant tracking system's form.

    `apply` opens the job's application page on `page`, discovers the form,
    asks `answerer` for a plan, fills what the plan has, and either stops
    before the submit button (`submit=False`) or clicks it and waits for the
    confirmation. It must never raise for an ordinary failure: a missing
    control, a captcha, a changed layout all come back as a `HandlerResult`
    with the outcome that fits (`failed`, `blocked`) and an `error`. A plan
    with a required gap comes back as `needs_input` with the gap listed, and
    the form is left unsubmitted.
    """

    ats: str

    def matches(self, url: str) -> bool: ...

    def apply(
        self,
        page: Any,
        packet: Packet,
        answerer: Answerer,
        *,
        submit: bool,
        screenshot_path: str | None = None,
    ) -> HandlerResult: ...
