"""Turn a form's fields into a plan, from the user's own data and nothing else.

Three sources, in order. The packet: contact details, the resume, the cover
letter, links. The answer bank: standing answers keyed by question, which the
user fills once. The model, which may draft an answer to an open question only
from the fact base, and whose draft is checked against it before it goes on a
form. Some questions are never guessed by anyone: work authorisation,
sponsorship, salary, start date and the like come from the answer bank or from
the user, because a wrong answer there is a wrong application. Whatever is
left unanswered and required stops the submission and is reported back as a
`NeededInput`; answered once, it goes into the answer bank and is never asked
again (jobpilot's human-in-the-loop, AutoApply's answer bank).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, Field

from jobagent.llm.backend import Completer, LLMError
from jobagent.resume.facts import Fact
from jobagent.tailor.generate import build_system_prompt
from jobagent.tailor.validate import validate_text

from .models import Answerer, Fill, FillPlan, FormField, NeededInput, Packet

log = logging.getLogger(__name__)

# Label patterns that name a canonical answer-bank key. Order matters: the
# first match wins, so "last name" is tried before "name".
_PATTERNS: tuple[tuple[str, str], ...] = (
    ("first_name", r"\b(?:first|given)\s+name\b"),
    ("last_name", r"\b(?:last|family|sur)\s*name\b"),
    ("preferred_name", r"\bpreferred\s+(?:first\s+)?name\b|\bnickname\b"),
    ("full_name", r"^\W*(?:full\s+|legal\s+|your\s+)?name\W*$|\bfull\s+name\b"),
    ("email", r"\be-?mail\b"),
    ("phone", r"\b(?:phone|mobile|telephone|cell)\b"),
    ("address", r"\b(?:street\s+)?address\b|\bpostal|\bzip\b|\bpost\s*code\b"),
    ("country", r"\bcountry\b"),
    ("location", r"\blocation\b|\bcity\b|\bwhere (?:are|do) you (?:based|located|live)\b"),
    ("linkedin", r"\blinkedin\b"),
    ("github", r"\bgithub\b"),
    ("website", r"\bwebsite\b|\bportfolio\b|\bpersonal\s+site\b|\bhomepage\b"),
    ("resume", r"\bresume\b|\br[ée]sum[ée]\b|\bcv\b|\bcurriculum\b"),
    ("cover_letter", r"\bcover\s*letter\b"),
    ("pronouns", r"\bpronouns?\b"),
    (
        "current_company",
        r"\bcurrent\s+(?:company|employer|organi[sz]ation)\b|\bcompany\s+you\s+work\b"
        r"|\bpresent\s+employer\b|^org$",
    ),
    ("current_title", r"\bcurrent\s+(?:title|role|position|job\s+title)\b"),
    ("referral", r"\breferred\s+by\b|\breferral\b|\bemployee\s+referr"),
    (
        "heard_about",
        r"\b(?:hear|heard|find\s+out|learn|found\s+out)\s+about\b|\bhow did you find\b",
    ),
    # Never guessed. These come from the answer bank or from the user.
    (
        "work_authorization",
        r"\bauthori[sz]ed\s+to\s+work\b|\blegally\s+(?:able|eligible|authori[sz]ed|permitted)\b"
        r"|\bwork\s+authori[sz]ation\b|\bright\s+to\s+work\b|\beligible\s+to\s+work\b"
        r"|\bwork\s+permit\b",
    ),
    ("visa_sponsorship", r"\bsponsor"),
    ("citizenship", r"\bcitizen"),
    (
        "salary_expectation",
        r"\bsalary\b|\bcompensation\b|\bpay\s+expectation|\bdesired\s+pay\b|\bhourly\s+rate\b"
        r"|\bexpected\s+(?:pay|rate)\b",
    ),
    (
        "start_date",
        r"\bstart\s+date\b|\bavailable\s+to\s+start\b|\bavailability\b|\bnotice\s+period\b"
        r"|\bwhen\s+(?:can|could)\s+you\s+start\b|\bearliest\s+start\b",
    ),
    ("relocation", r"\brelocat"),
    ("work_arrangement", r"\bremote\b|\bhybrid\b|\bon-?site\b|\bin[- ]office\b|\bin\s+person\b"),
    ("security_clearance", r"\bclearance\b"),
    ("over_18", r"\b18\b|\beighteen\b|\blegal\s+age\b|\bage\s+of\s+majority\b"),
    ("background_check", r"\bbackground\s+check\b|\bdrug\s+(?:test|screen)"),
    (
        "previously_employed",
        r"\b(?:previously|ever|currently)\s+(?:worked|employed|been\s+employed|interviewed)\b"
        r"|\bformer\s+employee\b|\bcurrent(?:ly)?\s+(?:an?\s+)?employee\b",
    ),
    ("non_compete", r"\bnon-?compete\b|\bnon-?solicit"),
)
_COMPILED = tuple((key, re.compile(pattern, re.IGNORECASE)) for key, pattern in _PATTERNS)

SENSITIVE: frozenset[str] = frozenset(
    {
        "work_authorization",
        "visa_sponsorship",
        "citizenship",
        "salary_expectation",
        "start_date",
        "relocation",
        "work_arrangement",
        "security_clearance",
        "over_18",
        "background_check",
        "previously_employed",
        "non_compete",
        "referral",
    }
)
NEVER_GUESSED = "never guessed: answer it once and it is kept"
NOT_ON_FILE = "no answer on file"
NOT_GROUNDED = "the model could not answer it from the facts on file"
NO_MODEL = "no model to draft an answer"
MODEL_FAILED = "the model call failed"
NO_OPTION = "no option matches the answer on file"

_EEO = re.compile(
    r"\bgender\b|\brace\b|\bethnicit|\bhispanic\b|\blatin[ox]\b|\bveteran\b|\bdisabilit"
    r"|\bsexual\s+orientation\b|\btransgender\b|\bself[- ]identif",
    re.IGNORECASE,
)
_EEO_KEYS: tuple[tuple[str, str], ...] = (
    ("gender", r"\bgender\b|\bsex\b|\btransgender\b|\bsexual\s+orientation\b"),
    ("hispanic", r"\bhispanic\b|\blatin[ox]\b"),
    ("race", r"\brace\b|\bethnicit"),
    ("veteran", r"\bveteran\b"),
    ("disability", r"\bdisabilit"),
)
_EEO_COMPILED = tuple((key, re.compile(p, re.IGNORECASE)) for key, p in _EEO_KEYS)
_DECLINE = re.compile(
    r"decline|prefer not|don'?t wish|do not wish|rather not|not to (?:answer|say|disclose)"
    r"|choose not|i don'?t want",
    re.IGNORECASE,
)
_CONSENT = re.compile(
    r"\bprivacy\b|\bterms\b|\bconsent\b|\bagree\b|\backnowledge\b|\bcertify\b|\bauthori[sz]e\b"
    r"|\bconfirm\b|\baccept\b|\bpolicy\b|\bgdpr\b|\bdata\s+(?:processing|retention)\b",
    re.IGNORECASE,
)
_MARKETING = re.compile(
    r"\bmarketing\b|\bnewsletter\b|\bupdates?\b|\bpromotion|\bcommunications?\b|\btalent\b"
    r"|\bfuture\s+(?:opportunit|roles|positions)|\bkeep\s+(?:me|my)\b|\bcontact\s+me\b",
    re.IGNORECASE,
)
_YES = frozenset({"yes", "y", "true", "1", "i do", "i am", "i have"})
_NO = frozenset({"no", "n", "false", "0", "i do not", "i am not", "i have not"})
_ASKABLE = frozenset({"text", "textarea", "select", "multiselect", "radio", "number", "unknown"})
_MAX_MODEL_QUESTIONS = 20
_DESCRIPTION_CHARS = 2500


# ------------------------------------------------------------------ keys --


def normalise_label(label: str) -> str:
    text = re.sub(r"[^\w\s]", " ", (label or "").lower())
    return re.sub(r"\s+", " ", text).strip()[:120]


def question_key(label: str) -> str:
    """The answer-bank key for a question no pattern recognises."""
    return "q:" + normalise_label(label)


def canonical_key(field: FormField) -> str | None:
    """The canonical key `field` asks for, from its label first, then its name."""
    for text in (field.label, field.name or "", field.key):
        if not text:
            continue
        haystack = text.replace("_", " ").replace("-", " ")
        for key, pattern in _COMPILED:
            if pattern.search(haystack):
                return key
    return None


def answer_key_for(field: FormField) -> str:
    """Where an answer to `field` lives, or would live, in the answer bank."""
    if _EEO.search(field.label):
        for key, pattern in _EEO_COMPILED:
            if pattern.search(field.label):
                return key
    return canonical_key(field) or question_key(field.label)


def _section(field: FormField) -> str:
    if field.section not in ("other", "questions"):
        return field.section
    if field.kind == "checkbox" and not field.options:
        if _CONSENT.search(field.label) or _MARKETING.search(field.label):
            return "consent"
    if _EEO.search(field.label):
        return "eeo"
    key = canonical_key(field)
    if key in ("first_name", "last_name", "full_name", "preferred_name", "email", "phone"):
        return "contact"
    if key in ("location", "address", "country"):
        return "contact"
    if key in ("linkedin", "github", "website"):
        return "links"
    if key == "resume":
        return "resume"
    if key == "cover_letter":
        return "cover_letter"
    return "questions"


# --------------------------------------------------------------- options --


def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s+]", " ", str(value or "").lower())).strip()


def pick_option(value: str, options: Sequence[str]) -> str | None:
    """The option `value` names, or None. Exact first, then yes/no, then prefix, then contains."""
    if not options:
        return None
    wanted = _norm(value)
    if not wanted:
        return None
    normed = [(option, _norm(option)) for option in options]
    for option, norm in normed:
        if norm == wanted:
            return option
    if wanted in _YES or wanted in _NO:
        target = "yes" if wanted in _YES else "no"
        for option, norm in normed:
            if norm == target or norm.startswith(target + " ") or norm.startswith(target + ","):
                return option
        return None
    for option, norm in normed:
        if norm.startswith(wanted) or wanted.startswith(norm):
            return option
    for option, norm in normed:
        if re.search(rf"\b{re.escape(wanted)}\b", norm):
            return option
    return None


def pick_options(value: str, options: Sequence[str]) -> list[str]:
    picked: list[str] = []
    for part in re.split(r"[,;|]", value):
        option = pick_option(part, options)
        if option and option not in picked:
            picked.append(option)
    return picked


def _decline_option(options: Sequence[str]) -> str | None:
    for option in options:
        if _DECLINE.search(option):
            return option
    return None


def _as_bool(value: str) -> bool | None:
    wanted = _norm(value)
    if wanted in _YES or wanted in ("checked", "on", "agree"):
        return True
    if wanted in _NO or wanted in ("unchecked", "off", "disagree"):
        return False
    return None


# ----------------------------------------------------------------- plan --


class _Planner:
    def __init__(self, packet: Packet, completer: Completer | None, allow_model: bool) -> None:
        self.packet = packet
        self.completer = completer
        self.allow_model = allow_model
        self.plan = FillPlan()
        self.open: list[FormField] = []  # for the model, once, at the end
        self.used_links: set[str] = set()  # a link goes on the form once

    # -- entry -----------------------------------------------------------

    def run(self, fields: Sequence[FormField]) -> FillPlan:
        for field in fields:
            try:
                self._plan_field(field)
            except Exception as exc:  # a planning bug must not sink the run
                log.exception("planning %r", field.label)
                self._need(field, f"planning failed: {exc}")
        if self.open:
            self._ask_model(self.open)
        return self.plan

    # -- helpers ---------------------------------------------------------

    def _fill(
        self, field: FormField, value: Any, source: str, file_path: str | None = None
    ) -> None:
        self.plan.fills.append(
            Fill(key=field.key, value=value, file_path=file_path, source=source, label=field.label)
        )

    def _need(self, field: FormField, reason: str) -> None:
        self.plan.needed.append(
            NeededInput(
                key=field.key,
                label=field.label,
                kind=field.kind,
                required=field.required,
                options=list(field.options),
                reason=reason,
                answer_key=answer_key_for(field),
            )
        )

    def _skip(self, field: FormField, note: str) -> None:
        self.plan.notes.append(f"skipped {field.label!r}: {note}")

    def _bank(self, *keys: str) -> str | None:
        for key in keys:
            value = self.packet.answers.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    def _apply_value(self, field: FormField, value: str, source: str) -> bool:
        """Put `value` on `field` in the shape the control takes. False if it does not fit."""
        if field.kind in ("select", "radio"):
            option = pick_option(value, field.options)
            if option is None:
                return False
            self._fill(field, option, source)
            return True
        if field.kind == "multiselect" or (field.kind == "checkbox" and field.options):
            picked = pick_options(value, field.options)
            if not picked:
                return False
            self._fill(field, picked, source)
            return True
        if field.kind == "checkbox":
            flag = _as_bool(value)
            if flag is None:
                return False
            self._fill(field, flag, source)
            return True
        self._fill(field, value, source)
        return True

    def _value_or_need(self, field: FormField, value: str | None, source: str, reason: str) -> None:
        if value is None:
            if field.required:
                self._need(field, reason)
            else:
                self._skip(field, reason)
            return
        if not self._apply_value(field, value, source):
            self._need(field, NO_OPTION)

    # -- per field -------------------------------------------------------

    def _plan_field(self, field: FormField) -> None:
        section = _section(field)
        if field.kind == "file":
            self._plan_file(field, section)
            return
        if section == "consent":
            self._plan_consent(field)
            return
        if section == "eeo":
            self._plan_eeo(field)
            return
        key = canonical_key(field)
        if section == "contact":
            self._plan_contact(field, key)
            return
        if section == "links":
            self._plan_link(field, key)
            return
        if section == "cover_letter":
            self._value_or_need(field, self.packet.cover_letter, "cover_letter", NOT_ON_FILE)
            return
        # Questions. The answer bank first, by canonical key and by label.
        banked = self._bank(*(k for k in (key, question_key(field.label)) if k))
        if banked is not None:
            if not self._apply_value(field, banked, "answer_bank"):
                self._need(field, NO_OPTION)
            return
        if key in SENSITIVE:
            self._need(field, NEVER_GUESSED)
            return
        if key == "heard_about":
            self._plan_heard_about(field)
            return
        if self.allow_model and self.completer is not None and field.kind in _ASKABLE:
            self.open.append(field)
            return
        reason = NO_MODEL if field.kind in _ASKABLE else NOT_ON_FILE
        if field.required:
            self._need(field, reason)
        else:
            self._skip(field, reason)

    def _plan_file(self, field: FormField, section: str) -> None:
        key = canonical_key(field)
        if key == "resume" or section == "resume":
            self._fill(field, None, "resume", file_path=self.packet.resume_path)
        elif key == "cover_letter" or section == "cover_letter":
            if self.packet.cover_letter_path:
                self._fill(field, None, "cover_letter", file_path=self.packet.cover_letter_path)
            elif field.required:
                self._need(field, NOT_ON_FILE)
            else:
                self._skip(field, "no cover letter file")
        elif field.required:
            self._need(field, NOT_ON_FILE)
        else:
            self._skip(field, "an upload nothing on file matches")

    def _plan_consent(self, field: FormField) -> None:
        banked = self._bank(question_key(field.label))
        if banked is not None and _as_bool(banked) is not None:
            self._fill(field, _as_bool(banked), "answer_bank")
            return
        if _MARKETING.search(field.label) and not field.required:
            self._fill(field, False, "default")
            return
        self._fill(field, True, "default")

    def _plan_eeo(self, field: FormField) -> None:
        banked = self._bank(answer_key_for(field), question_key(field.label))
        if banked is not None and self._apply_value(field, banked, "answer_bank"):
            return
        decline = _decline_option(field.options)
        if decline is not None:
            self._fill(field, decline, "default")
            return
        if field.kind in ("text", "textarea"):
            self._value_or_need(field, None, "default", NOT_ON_FILE)
            return
        if field.required:
            self._need(field, NOT_ON_FILE)
        else:
            self._skip(field, "no decline option; left blank")

    def _plan_contact(self, field: FormField, key: str | None) -> None:
        contact = self.packet.contact
        value: str | None = None
        source = "contact"
        if key == "first_name":
            value = contact.get("first_name") or _split_name(contact.get("full_name"))[0]
        elif key == "last_name":
            value = contact.get("last_name") or _split_name(contact.get("full_name"))[1]
        elif key == "full_name":
            value = contact.get("full_name") or " ".join(
                p for p in (contact.get("first_name"), contact.get("last_name")) if p
            )
        elif key == "preferred_name":
            value = self._bank("preferred_name") or contact.get("first_name")
            value = value or _split_name(contact.get("full_name"))[0]
            source = "default"
        elif key == "phone" and field.kind in ("select", "radio"):
            self._value_or_need(field, self._bank("phone_country"), "answer_bank", NOT_ON_FILE)
            return
        elif key in ("email", "phone", "location"):
            value = contact.get(key)
        elif key in ("address", "country"):
            value = self._bank(key)
            source = "answer_bank"
        if value is None and key:
            value = self._bank(key)
            source = "answer_bank"
        self._value_or_need(field, value or None, source, NOT_ON_FILE)

    def _plan_link(self, field: FormField, key: str | None) -> None:
        """A named link from the packet. A form that offers several boxes for
        the same kind of link ("Portfolio", "Other website") gets the value
        once: repeating it says nothing and reads like a filled-in blank."""
        links = self.packet.links
        value = None
        if key == "linkedin":
            value = links.get("linkedin")
        elif key == "github":
            value = links.get("github")
        elif key == "website":
            value = next(
                (
                    url
                    for url in (links.get("website"), links.get("portfolio"))
                    if url and url not in self.used_links
                ),
                None,
            )
        value = value or (self._bank(key) if key else None)
        if value is None and not field.required:
            self._skip(field, "no such link on file")
            return
        if value is not None:
            self.used_links.add(value)
        self._value_or_need(field, value, "links", NOT_ON_FILE)

    def _plan_heard_about(self, field: FormField) -> None:
        if field.options:
            for wanted in ("job board", "linkedin", "online", "internet", "other"):
                option = pick_option(wanted, field.options)
                if option:
                    self._fill(field, option, "default")
                    return
        if field.kind in ("text", "textarea"):
            self._fill(field, "Job board", "default")
            return
        if field.required:
            self._need(field, NOT_ON_FILE)
        else:
            self._skip(field, "no option fits")

    # -- the model -------------------------------------------------------

    def _ask_model(self, fields: list[FormField]) -> None:
        assert self.completer is not None
        if len(fields) > _MAX_MODEL_QUESTIONS:
            self.plan.notes.append(
                f"{len(fields) - _MAX_MODEL_QUESTIONS} open questions beyond the model limit"
            )
            for field in fields[_MAX_MODEL_QUESTIONS:]:
                self._need(field, NOT_ON_FILE)
            fields = fields[:_MAX_MODEL_QUESTIONS]
        by_key = {f.key: f for f in fields}
        facts = {f.id: f for f in self.packet.facts if f.id is not None}
        allow = [
            self.packet.job.get("company") or "",
            self.packet.job.get("title") or "",
            self.packet.contact.get("full_name") or "",
        ]
        objections: dict[str, list[str]] = {}
        settled: dict[str, DraftAnswer] = {}
        for attempt in range(2):
            pending = [f for f in fields if f.key not in settled]
            if not pending:
                break
            try:
                drafts = self._draft(pending, objections)
            except LLMError as exc:
                log.warning("answer drafting failed: %s", exc)
                for field in pending:
                    self._need(field, f"{MODEL_FAILED}: {exc}")
                return
            objections = {}
            for draft in drafts:
                field = by_key.get(draft.key)
                if field is None or field.key in settled:
                    continue
                problems = _check_draft(draft, field, facts, self.packet.never_claim, allow)
                if problems:
                    objections[field.key] = problems
                    if attempt == 1:
                        settled[field.key] = DraftAnswer(
                            key=field.key, needs_human=True, reason="; ".join(problems)
                        )
                else:
                    settled[field.key] = draft
            for field in pending:
                if field.key not in settled and field.key not in objections:
                    settled[field.key] = DraftAnswer(
                        key=field.key, needs_human=True, reason="no draft returned"
                    )
        for field in fields:
            draft = settled.get(field.key)
            if draft is None or draft.needs_human:
                reason = NOT_GROUNDED
                if draft is not None and draft.reason:
                    reason = f"{NOT_GROUNDED}: {draft.reason}"
                if field.required:
                    self._need(field, reason)
                else:
                    self._skip(field, reason)
                continue
            if field.kind in ("select", "radio"):
                self._fill(field, draft.option, "model")
            elif field.kind == "multiselect":
                self._fill(field, list(draft.options), "model")
            else:
                self._fill(field, draft.answer.strip(), "model")

    def _draft(
        self, fields: Sequence[FormField], objections: Mapping[str, list[str]]
    ) -> list[DraftAnswer]:
        assert self.completer is not None
        system = build_system_prompt(list(self.packet.facts), list(self.packet.never_claim))
        prompt = build_answers_prompt(self.packet, fields, objections)
        completion = self.completer.complete(
            system=system,
            prompt=prompt,
            output=DraftAnswers,
            max_tokens=4000,
            cache_system=True,
        )
        self.plan.input_tokens += completion.input_tokens
        self.plan.output_tokens += completion.output_tokens
        result = completion.result
        if not isinstance(result, DraftAnswers):
            raise LLMError(f"expected DraftAnswers, got {type(result).__name__}")
        return list(result.answers)


class DraftAnswer(BaseModel):
    key: str
    answer: str = ""
    option: str | None = None
    options: list[str] = Field(default_factory=list)
    needs_human: bool = False
    reason: str = ""


class DraftAnswers(BaseModel):
    answers: list[DraftAnswer] = Field(default_factory=list)


def _check_draft(
    draft: DraftAnswer,
    field: FormField,
    facts: Mapping[int, Fact],
    never_claim: Sequence[str],
    allow: Sequence[str],
) -> list[str]:
    """Why `draft` may not go on the form; [] when it may."""
    if draft.needs_human:
        return []
    if field.kind in ("select", "radio"):
        if not draft.option or draft.option not in field.options:
            picked = pick_option(draft.option or draft.answer, field.options)
            if picked is None:
                return ["option is not one of the choices"]
            draft.option = picked
        return []
    if field.kind == "multiselect":
        picked = [o for o in draft.options if o in field.options]
        if not picked:
            picked = pick_options(", ".join(draft.options) or draft.answer, field.options)
        if not picked:
            return ["no option among the choices"]
        draft.options = picked
        return []
    text = draft.answer.strip()
    if not text:
        return ["empty answer"]
    if field.kind == "number":
        return [] if re.fullmatch(r"[\d.,]+", text) else ["not a number"]
    issues = validate_text(text, facts, never_claim, allow=allow, where=field.key)
    return [f"{i.message}" + (f" [{i.term}]" if i.term else "") for i in issues]


_ANSWER_RULES = "\n".join(
    [
        "You are filling in a job application form for the candidate described by the fact",
        "base above. Answer each question below from the fact base and nothing else. Rules:",
        "- Free-text answers: first person, two to four sentences at most, and every",
        "  technology, employer, title and number in them must appear in the fact base. Do",
        "  not add anything the facts do not state; do not flatter the company with claims",
        "  about it.",
        "- Choice questions: pick exactly one of the listed options, verbatim, in `option`",
        "  (or several in `options` when the question allows more than one).",
        "- Numbers: answer with a number only, derived from the dates and facts on file.",
        "- If the facts do not settle a question (anything about preferences, plans,",
        "  availability, eligibility, or anything you would have to invent), set",
        "  `needs_human` true and say why in `reason`. A blank is better than a guess.",
        "Return one entry per question key, in the same order.",
    ]
)


def build_answers_prompt(
    packet: Packet, fields: Sequence[FormField], objections: Mapping[str, list[str]]
) -> str:
    job = packet.job
    lines = [
        _ANSWER_RULES,
        "",
        f"Role: {job.get('title') or ''} at {job.get('company') or ''}",
    ]
    description = (job.get("description") or "").strip()
    if description:
        lines.append("Posting (for context only; it is not a source of facts about the candidate):")
        lines.append(description[:_DESCRIPTION_CHARS])
    lines.append("")
    lines.append("Questions:")
    for field in fields:
        line = f"- key={field.key!r} kind={field.kind} required={field.required}: {field.label}"
        if field.help_text:
            line += f" ({field.help_text})"
        if field.options:
            line += "\n  options: " + " | ".join(field.options)
        if field.key in objections:
            line += "\n  your previous answer was refused: " + "; ".join(objections[field.key])
        lines.append(line)
    return "\n".join(lines)


def _split_name(full: str | None) -> tuple[str | None, str | None]:
    parts = (full or "").split()
    if not parts:
        return None, None
    if len(parts) == 1:
        return parts[0], None
    return parts[0], " ".join(parts[1:])


def plan_fills(
    fields: Iterable[FormField],
    packet: Packet,
    *,
    completer: Completer | None = None,
    allow_model: bool = True,
) -> FillPlan:
    """The plan for `fields`: what goes where, from which source, and what is missing."""
    return _Planner(packet, completer, allow_model).run(list(fields))


def make_answerer(
    packet: Packet, *, completer: Completer | None = None, allow_model: bool = True
) -> Answerer:
    """Bind the packet and the model, so a handler only ever passes the fields."""

    def answer(fields: list[FormField]) -> FillPlan:
        return plan_fills(fields, packet, completer=completer, allow_model=allow_model)

    return answer
