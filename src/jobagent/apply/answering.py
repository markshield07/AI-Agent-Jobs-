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
    # Before "phone": the other boxes a phone number comes with (Workday).
    ("phone_country", r"\bphone\s+code\b|\bcountry\s+(?:calling\s+|dialing\s+)?code\b"),
    ("phone_extension", r"\bextension\b|\bext\b"),
    # A second number ("Home Phone" beside "Mobile Phone") is not the mobile again.
    (
        "other_phone",
        r"\b(?:home|work|office|business|alternate|alternative|secondary|other|evening|day)"
        r"\s+(?:phone|telephone|tel|number)\b|\blandline\b",
    ),
    ("phone", r"\b(?:phone|mobile|telephone|cell)\b"),
    # The parts of a postal address each have a key of their own, so a form
    # that asks for street, city, state and ZIP separately (Workday) does not
    # get the same answer four times, and each answer is kept apart.
    ("postal_code", r"\bpostal|\bzip\b|\bpost\s*code\b"),
    ("address_line_2", r"\baddress\s+line\s*2\b|\bapartment\b|\bapt\b|\bsuite\b"),
    # Where a job was, not where the person lives (a work-history entry's
    # "Employer Location"): never the home address.
    ("employer_location", r"\b(?:employer|company|job|work|office)\s+(?:location|city|address)\b"),
    ("address", r"\b(?:street\s+)?address\b"),
    ("city", r"^\W*(?:city|town|city\s*/\s*town|town\s*/\s*city)\W*$"),
    ("state", r"^\W*(?:state|province|region|state\s*/\s*province)\W*$"),
    ("country", r"\bcountry\b"),
    # Whether the person can get to this job's place ("Are you comfortable
    # commuting to this job's location?"): a question about the job, not the
    # person's own location, and its answer differs from job to job.
    ("commute_ok", r"\bcommut"),
    (
        "location",
        r"\blocation\b|\bcity\b"
        r"|\bwhere (?:are|do) you (?:currently |now |presently )?(?:based|located|live|reside)\b",
    ),
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
    # The list a site shows once "How did you hear" is answered (Serco's
    # "Source" after Job Board): which one, a question of its own.
    (
        "heard_about_source",
        r"^\W*(?:job\s+|applicant\s+|candidate\s+|referral\s+)?source(?:\s+(?:name|detail))?\W*$"
        r"|\bwhich\s+(?:job\s+board|website|site)\b",
    ),
    # How a recruiter should reach the person: plain defaults, overridable.
    ("contact_time", r"\b(?:best|preferred)\s+time\s+to\s+(?:contact|call|reach)\b"),
    (
        "contact_method",
        r"\b(?:preferred|best)\s+(?:contact\s+method|method\s+of\s+contact|way\s+to\s+contact)"
        r"|\bcontact\s+preference\b",
    ),
    # Never guessed. These come from the answer bank or from the user.
    # Sponsorship before authorization: "require work authorization
    # sponsorship?" asks about sponsorship, and the two answers are opposite.
    # "Authorized to work ... without sponsorship?" is still about authorization.
    ("work_authorization", r"\bwithout\b[^?]{0,60}\bsponsor"),
    ("visa_sponsorship", r"\bsponsor"),
    (
        "work_authorization",
        r"\bauthori[sz]ed\s+to\s+work\b|\blegally\s+(?:able|eligible|authori[sz]ed|permitted)\b"
        r"|\bwork\s+authori[sz]ation\b|\bright\s+to\s+work\b|\beligible\s+to\s+work\b"
        r"|\bwork\s+permit\b",
    ),
    # Export-control rules' "U.S. person" (a citizen, a permanent resident or
    # a protected person): one answer for every company that asks.
    ("us_person", r"\bu\.?\s?s\.?\s+persons?\b|\bunited\s+states\s+persons?\b"),
    ("citizenship", r"\bcitizen"),
    # The parts of a salary a form asks for apart from the amount: they follow
    # from salary_expectation, so they are tried before it.
    ("salary_currency", r"\bcurrency\b"),
    (
        "salary_period",
        r"\b(?:salary|pay|compensation|rate)\b.{0,30}\b(?:time\s*frame|period|frequency|basis|unit)\b"
        r"|\bpay\s+(?:period|frequency)\b",
    ),
    (
        "salary_expectation",
        r"\bsalary\b|\bcompensation\b|\bpay\s+expectation|\bdesired\s+pay\b|\bhourly\s+rate\b"
        r"|\bexpected\s+(?:pay|rate)\b",
    ),
    (
        "start_date",
        r"\bstart\s+date\b|\bavailable\s+to\s+start\b|\bnotice\s+period\b"
        # "Availability for an interview" is not a start date.
        r"|\bavailability\b(?!\s+(?:for|to)\s+(?:an?\s+)?interview)"
        r"|\bwhen\s+(?:can|could)\s+you\s+start\b|\bearliest\s+start\b",
    ),
    ("relocation", r"\brelocat"),
    ("work_arrangement", r"\bremote\b|\bhybrid\b|\bon-?site\b|\bin[- ]office\b|\bin\s+person\b"),
    # "Clearance Status" beside "Clearance Level" (Serco) is a second answer.
    ("clearance_status", r"\bclearance\s+status\b"),
    ("security_clearance", r"\bclearance\b"),
    # Age only: "at least 18 months of experience" is a question about experience.
    (
        "over_18",
        r"\b(?:18|eighteen)\b(?!\s*(?:\+\s*)?(?:months?|years?\s+of\s+(?!age)|years?\s+experience))"
        r"|\blegal\s+age\b|\bage\s+of\s+majority\b",
    ),
    ("background_check", r"\bbackground\s+check\b|\bdrug\s+(?:test|screen)"),
    (
        "previously_employed",
        r"\b(?:previously|ever)\s+(?:worked|employed|been\s+employed|interviewed)\b"
        # "Are you currently employed?" asks whether the person has a job now.
        r"|\bcurrently\s+(?:work(?:ing)?|employed)\s+(?:by|with|at|for)\b"
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
        "us_person",
        "salary_expectation",
        "salary_currency",
        "salary_period",
        "start_date",
        "relocation",
        "commute_ok",
        "work_arrangement",
        "security_clearance",
        "clearance_status",
        "over_18",
        "background_check",
        "previously_employed",
        "non_compete",
        "referral",
    }
)
_QUESTION_ORDER = tuple(
    [(key, pattern) for key, pattern in _COMPILED if key in SENSITIVE]
    + [(key, pattern) for key, pattern in _COMPILED if key not in SENSITIVE]
)
NEVER_GUESSED = "never guessed: answer it once and it is kept"
# One answer for every form's terms or privacy-policy box: whether to agree
# to a company's application terms when its form asks. Never assumed.
CONSENT_KEY = "consent_terms"
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
# "Do you need an accommodation due to a disability?" asks for a request, not
# the voluntary disability self-identification, and the answers are not
# interchangeable: it is a question of its own, and never guessed.
_ACCOMMODATION = re.compile(r"\baccommodat", re.IGNORECASE)
_DECLINE = re.compile(
    r"decline|prefer not|don'?t wish|do not wish|rather not|not to (?:answer|say|disclose)"
    r"|choose not|i don'?t want|(?:do not|don'?t) want to (?:answer|disclose|say)",
    re.IGNORECASE,
)
_CONSENT = re.compile(
    r"\bprivacy\b|\bterms\b|\bconsent\b|\bagree\b|\backnowledge\b|\bcertify\b|\bauthori[sz]e\b"
    r"|\bconfirm\b|\baccept\b|\bpolicy\b|\bgdpr\b|\bdata\s+(?:processing|retention)\b"
    r"|\bi\s+understand\b|\bi\s+have\s+read\b"
    r"|\bprocess(?:ing)?\b[^.]{0,40}\bpersonal\s+(?:information|data)\b"
    r"|\bby\s+(?:continuing|submitting|clicking|checking|applying|proceeding)\b",
    re.IGNORECASE,
)
_MARKETING = re.compile(
    r"\bmarketing\b|\bnewsletter\b|\bupdates?\b|\bpromotion|\bcommunications?\b|\btalent\b"
    r"|\bfuture\s+(?:opportunit|roles|positions)|\bkeep\s+(?:me|my)\b|\bcontact\s+me\b",
    re.IGNORECASE,
)
# Legal attestations a form asks as a question: answered once by the person,
# never drafted, whatever the model would make of them.
_ATTESTATION = re.compile(
    r"\bconflicts?\s+of\s+interest\b|\boutside\s+(?:employment|business|activit)"
    r"|\bnon[-\s]?(?:compete|solicit)|\bconvicted\b|\bfelony\b|\bcriminal\b"
    r"|\backnowledg|\battest\b|\bcertify\b"
    # Signing away rights: an arbitration agreement, a class or jury waiver.
    r"|\barbitrat|\bclass[-\s]+action\b|\bjury\s+trial\b|\bwaive(?:r|s)?\b",
    re.IGNORECASE,
)
# A choice that agrees to something on the person's behalf ("I understand and
# agree to the terms of the Agreement to Arbitrate"): whatever the label says,
# the question is a legal one.
_AGREEING_OPTION = re.compile(
    r"\bi\s+(?:understand\s+and\s+)?(?:agree|accept|consent)\b|\bagree\s+to\s+the\b",
    re.IGNORECASE,
)


def _is_attestation(field: FormField) -> bool:
    """A legal question only the person answers: by its label, or by a choice
    that would agree to terms for them."""
    if _ATTESTATION.search(field.label or ""):
        return True
    options = field.options or ()
    return any(_ATTESTATION.search(o) or _AGREEING_OPTION.search(o) for o in options)


# "Why are you interested in working for us?": what the cover letter says,
# in words already checked against the facts.
_WHY_US = re.compile(
    r"\bwhy\b[^?]*\b(?:interested|want\s+to\s+(?:work|join)|join(?:ing)?|appl(?:y|ying))\b"
    r"|\binterest(?:ed)?\s+in\s+(?:working|joining)\b",
    re.IGNORECASE,
)
_YES = frozenset({"yes", "y", "true", "1", "i do", "i am", "i have"})
_NO = frozenset({"no", "n", "false", "0", "i do not", "i am not", "i have not"})
# Text a page leaves among a list's choices that is no choice at all: another
# site's widget (Apply with LinkedIn's hidden box) or a web address.
_NOT_AN_OPTION = re.compile(r"api_key|AwliWidget|apply-with-linkedin|://", re.IGNORECASE)
_BOARDS = frozenset({"linkedin", "indeed", "glassdoor", "ziprecruiter", "dice", "monster"})
_BOARD_NAMES = {"linkedin": "LinkedIn", "ziprecruiter": "ZipRecruiter"}
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
    """The canonical key `field` asks for, from its label first, then its name.

    A question (a label with a "?" or more than a few words) is read by its
    first sentence only, and the never-guessed keys are tried on it first:
    "Are you eligible to work in the country ...?" is about work
    authorization, not the country, and a conflict-of-interest question that
    mentions a start date in its fine print is not asking for one.
    """
    for text in (field.label, field.name or "", field.key):
        key = _key_of_text(text, upload=field.kind == "file")
        if key is not None:
            return key
    return None


# Keys whose answer is a value (an amount, a choice of arrangement), never a
# bare yes or no: "Is the salary range of $120k acceptable?" is a question of
# its own, still never guessed, and its "Yes" must not become the amount.
_VALUE_KEYS = frozenset({"salary_expectation", "work_arrangement"})


def _yes_or_no(field: FormField) -> bool:
    options = {_norm(o) for o in field.options or () if _norm(o)}
    return bool(options) and options <= (_YES | _NO)


def _key_of_text(text: str, *, upload: bool = False) -> str | None:
    if not text:
        return None
    haystack = _lead(text).replace("_", " ").replace("-", " ")
    question = _is_question(text)
    order = _QUESTION_ORDER if question else _COMPILED
    for key, pattern in order:
        # "Do you have Team Lead experience listed in your resume?" asks
        # about the resume; only an upload asks for it.
        if question and key in _UPLOADS and not upload:
            continue
        if pattern.search(haystack):
            return key
    return None


_UPLOADS = frozenset({"resume", "cover_letter"})
# Answers that hold for one country: "authorized to work in Canada?" is not
# answered with the answer about the United States.
_COUNTRY_BOUND = frozenset({"work_authorization", "visa_sponsorship", "citizenship"})
_FOREIGN = re.compile(
    r"\b(?:canada|canadian|mexic|united\s+kingdom|u\.?k\.?|britain|ireland|europe|e\.?u\.?"
    r"|germany|france|spain|netherlands|poland|india|philippines|australia|new\s+zealand"
    r"|singapore|japan|china|brazil|costa\s+rica|colombia|argentina|israel|uae|dubai)\b",
    re.IGNORECASE,
)
# Questions whose answer is the same whatever the form's wording, so an answer
# given to one company's wording before the key existed serves the next. Not
# work authorization or sponsorship: "authorized to work in Canada?" is
# another question.
_SAME_EVERYWHERE = frozenset({"us_person"})


def _is_question(text: str) -> bool:
    return "?" in text or len(text.split()) > 6


def _lead(text: str) -> str:
    """The first sentence of a label: up to its first "?", at most 200 characters."""
    end = text.find("?")
    return text[: end + 1 if end >= 0 else 200][:200]


_SALUTATION = re.compile(
    r"^\s*(?:(?:dear|hello|hi|greetings)\b[^,:\n]{0,80}|to\s+whom\s+it\s+may\s+concern)[,:]?\s*",
    re.IGNORECASE,
)
_CLOSING = re.compile(
    r"\n\s*(?:(?:yours\s+)?sincerely|(?:best|kind|warm)(?:est)?\s+regards|regards|best|"
    r"respectfully(?:\s+yours)?|yours\s+(?:truly|faithfully)|with\s+thanks|thank\s+you)"
    r"\s*,[^\n]*(?:\n[^\n]{0,60}){0,3}\s*$",
    re.IGNORECASE,
)


def letter_body(letter: str) -> str:
    """The cover letter as a form answer: no "Dear ... Team," and no sign-off."""
    text = _SALUTATION.sub("", letter.strip(), count=1) if _SALUTATION.match(letter) else letter
    return _CLOSING.sub("", "\n" + text.strip()).strip()


def _is_terms_box(field: FormField) -> bool:
    """A box agreeing to terms or a privacy policy, not one asking for updates.
    A required box is terms whatever else it mentions: Serco's privacy notice
    speaks of "future positions", and the form cannot be sent without it."""
    return (
        field.kind == "checkbox"
        and not field.options
        and field.section in ("consent", "other", "questions", "eeo")
        and bool(_CONSENT.search(field.label))
        and (field.required or not _MARKETING.search(field.label))
    )


def commute_key(job: Mapping[str, Any] | None) -> str:
    """The answer-bank key for whether the person can commute to `job`: one
    per place ("commute_ok:long_beach_ca"), else per company, since the
    answer for one job's place says nothing about another's."""
    job = job or {}
    where = _norm(job.get("location") or "") or _norm(job.get("company") or "")
    return "commute_ok:" + where.replace(" ", "_") if where else "commute_ok"


def answer_key_for(field: FormField, job: Mapping[str, Any] | None = None) -> str:
    """Where an answer to `field` lives, or would live, in the answer bank.
    `job` is the posting, for the questions whose answer depends on it."""
    if _is_terms_box(field):
        return CONSENT_KEY
    if field.section == "experience":
        # "Location" in a job held is not where the person lives.
        return question_key(field.label)
    if _EEO.search(field.label) and not _ACCOMMODATION.search(field.label):
        for key, pattern in _EEO_COMPILED:
            if pattern.search(field.label):
                return key
    key = canonical_key(field)
    if key == "commute_ok":
        return commute_key(job)
    if key in _VALUE_KEYS and _yes_or_no(field):
        return question_key(field.label)
    return key or question_key(field.label)


# A single box that wants the whole postal address, not its street line: a
# question about where one lives or will work, or "full/mailing/home address".
_WHOLE_ADDRESS = re.compile(
    r"\b(?:full|complete|mailing|home|residential|current|working)\s+address\b"
    r"|\baddress\s+(?:from\s+)?(?:which|where)\b",
    re.IGNORECASE,
)

_CONTACT_KEYS = frozenset(
    {
        "first_name",
        "last_name",
        "full_name",
        "preferred_name",
        "email",
        "phone",
        "phone_country",
        "other_phone",
        "location",
        "address",
        "address_line_2",
        "city",
        "state",
        "postal_code",
        "country",
    }
)


def field_section(field: FormField) -> str:
    """What part of an application a field is (contact, resume, links...),
    read from its label where the page did not say."""
    return _section(field)


def _section(field: FormField) -> str:
    if field.section not in ("other", "questions"):
        return field.section
    if field.kind == "checkbox" and not field.options:
        if _CONSENT.search(field.label) or _MARKETING.search(field.label):
            return "consent"
    if _EEO.search(field.label) and not _ACCOMMODATION.search(field.label):
        return "eeo"
    if field.kind == "checkbox" and not field.options:
        # A lone box ("I have a preferred name") holds no contact detail.
        return "questions"
    key = canonical_key(field)
    if key in _CONTACT_KEYS:
        return "contact"
    if key in ("linkedin", "github", "website"):
        return "links"
    if key == "resume":
        return "resume"
    if key == "cover_letter":
        return "cover_letter"
    return "questions"


def _in_a_us_state(location: str) -> bool:
    """ "Menifee, CA 92584" or "Austin, Texas": a place in one of the states."""
    from jobagent.discovery.sources.jobspy_source import _STATES

    for part in location.split(",")[1:]:
        words = re.sub(r"\d", " ", part).split()
        if not words:
            continue
        if (len(words) == 1 and words[0] in _STATES.values()) or " ".join(words).lower() in _STATES:
            return True
    return False


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
        self.agree_to_terms = False  # auto mode: a required terms box is agreed to

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
                answer_key=answer_key_for(field, self.packet.job),
            )
        )

    def _skip(self, field: FormField, note: str) -> None:
        self.plan.notes.append(f"skipped {field.label!r}: {note}")

    def _bank_by_meaning(self, key: str) -> str | None:
        """An answer kept under another form's wording of the same question
        ("q:please indicate whether you are a u s person ..." for us_person),
        from before the question had a key of its own. Only when every such
        answer agrees."""
        found = {
            str(value).strip()
            for name, value in self.packet.answers.items()
            if name.startswith("q:")
            and value is not None
            and str(value).strip()
            and _key_of_text(name[2:] + "?") == key
        }
        return found.pop() if len(found) == 1 else None

    def _place_part(self, key: str) -> str | None:
        """City or state: the answer bank's, else that part of a location on
        file that reads "City, State, ..."."""
        value = self._bank(key)
        if value is not None:
            return value
        parts = [p.strip() for p in (self.packet.contact.get("location") or "").split(",")]
        index = 0 if key == "city" else 1
        return parts[index] if len(parts) >= 2 and parts[index] else None

    def _bank(self, *keys: str) -> str | None:
        for key in keys:
            value = self.packet.answers.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return None

    def _apply_value(self, field: FormField, value: str, source: str) -> bool:
        """Put `value` on `field` in the shape the control takes. False if it does not fit."""
        if field.kind == "select" and not field.options and _section(field) == "contact":
            # A typeahead that lists places only once something is typed
            # (LinkedIn's "Location (city)"): the fill types it and takes the hit.
            self._fill(field, value, source)
            return True
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
        if field.kind == "number":
            # "$150,000" in the answer bank is 150000 in a number box.
            number = re.sub(r"[^\d.]", "", value.split("-")[0]).rstrip(".")
            if not number:
                return False
            value = number
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
        if section == "experience":
            banked = self._bank(question_key(field.label))
            if banked is not None:
                if not self._apply_value(field, banked, "answer_bank"):
                    self._need(field, NO_OPTION)
                return
            self._plan_open(field)
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
        # Questions. The answer bank first, by canonical key and by label;
        # a commute question only by this job's own key.
        keys = (key, question_key(field.label))
        if key == "commute_ok":
            keys = (commute_key(self.packet.job), None)
        elif key in _VALUE_KEYS and _yes_or_no(field):
            keys = (question_key(field.label), None)
        elif key in _COUNTRY_BOUND and _FOREIGN.search(field.label or ""):
            # The stored answers are about the United States.
            keys = (question_key(field.label), None)
        banked = self._bank(*(k for k in keys if k))
        if banked is None and key in _SAME_EVERYWHERE:
            banked = self._bank_by_meaning(key)
        if banked is not None:
            if not self._apply_value(field, banked, "answer_bank"):
                self._need(field, NO_OPTION)
            return
        if key in ("salary_currency", "salary_period"):
            self._plan_salary_part(field, key)
            return
        if key in ("contact_time", "contact_method"):
            self._plan_reach(field, key)
            return
        if key in SENSITIVE or _is_attestation(field) or _ACCOMMODATION.search(field.label):
            self._need(field, NEVER_GUESSED)
            return
        if key in ("heard_about", "heard_about_source"):
            self._plan_heard_about(field, which=key == "heard_about_source")
            return
        body = letter_body(self.packet.cover_letter or "")
        if field.kind == "textarea" and body and _WHY_US.search(field.label):
            self._fill(field, body, "cover_letter")
            return
        self._plan_open(field)

    def _plan_open(self, field: FormField) -> None:
        """For the model to answer from the facts, else asked (required) or skipped."""
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
        # Agreeing to terms is the person's to say, once for every form.
        blanket = self._bank(CONSENT_KEY)
        if blanket is not None and _as_bool(blanket) is not None:
            self._fill(field, _as_bool(blanket), "answer_bank")
        elif field.required and self.agree_to_terms:
            self._fill(field, True, "auto_mode")
            self.plan.notes.append(
                f"agreed to {field.label!r} because applications go all the way (auto mode); "
                f"answer {CONSENT_KEY} No to stop agreeing to terms"
            )
        elif field.required:
            self._need(field, NEVER_GUESSED)
        else:
            self._skip(field, "left unticked: agreeing to terms is yours to say once")

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
        elif key == "phone_country" or (key == "phone" and field.kind in ("select", "radio")):
            self._value_or_need(field, self._bank("phone_country"), "answer_bank", NOT_ON_FILE)
            return
        elif key in ("email", "phone", "location"):
            value = contact.get(key)
        elif key == "other_phone":
            # A second number is only ever the one on file for it; a required
            # one with none on file gets the main number.
            value = self._bank("other_phone", "home_phone")
            source = "answer_bank"
            if value is None and not field.required:
                self._skip(field, "no second phone number on file")
                return
            if value is None:
                value, source = contact.get("phone"), "contact"
        elif key == "address" and _WHOLE_ADDRESS.search(field.label or ""):
            # One box for the whole address ("the address from which you plan
            # on working"): the street alone would read as half an answer.
            street = self._bank("address")
            value = (
                ", ".join(
                    p
                    for p in (
                        street,
                        self._place_part("city"),
                        " ".join(
                            p for p in (self._place_part("state"), self._bank("postal_code")) if p
                        ),
                    )
                    if p
                )
                if street
                else None
            )
            source = "answer_bank"
        elif key in ("address", "address_line_2", "postal_code"):
            value = self._bank(key)
            source = "answer_bank"
        elif key == "country":
            # The answer bank, else the last part of "City, State, Country".
            value = self._bank(key)
            source = "answer_bank"
            location = contact.get("location") or ""
            parts = [p.strip() for p in location.split(",")]
            if value is None and len(parts) >= 3 and parts[-1]:
                value, source = parts[-1], "contact"
            elif value is None and _in_a_us_state(location):
                # "Menifee, CA 92584": a US state, so the country is not in doubt.
                value, source = "United States", "contact"
        elif key in ("city", "state"):
            # From the answer bank, else from the location on file when it
            # reads "City, State, ...".
            value = self._bank(key)
            source = "answer_bank"
            if value is None:
                value = self._place_part(key)
                source = "contact" if value else source
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

    def _plan_salary_part(self, field: FormField, key: str) -> None:
        """The currency or the period of the salary on file: USD, and yearly
        for an amount in the thousands, hourly below that, unless the answer
        bank says otherwise (salary_currency, salary_period)."""
        salary = self._bank("salary_expectation")
        if key == "salary_currency":
            wanted = [self._bank("salary_currency") or "USD", "US dollar", "$"]
        else:
            banked = self._bank("salary_period")
            if banked:
                wanted = [banked]
            elif salary is None:
                wanted = []
            else:
                amount = re.sub(r"[^\d.]", "", salary.split("-")[0]) or "0"
                yearly = float(amount.rstrip(".") or 0) >= 1000
                wanted = (
                    ["annually", "annual", "yearly", "year", "yr", "per year", "salary"]
                    if yearly
                    else ["hourly", "hour", "hr", "per hour"]
                )
        if salary is None and not self._bank(key):
            self._need(field, NEVER_GUESSED)
            return
        if not field.options:
            self._fill(field, wanted[0], "answer_bank")
            return
        for want in wanted:
            option = pick_option(want, field.options)
            if option:
                self._fill(field, option, "answer_bank")
                return
        self._need(field, NO_OPTION)

    def _plan_reach(self, field: FormField, key: str) -> None:
        """When and how a recruiter should get in touch: the answer bank, else
        any time, by email."""
        banked = self._bank(key)
        wanted = [banked] if banked else []
        wanted += ["anytime", "any time", "no preference"] if key == "contact_time" else ["email"]
        for want in wanted:
            if self._apply_value(field, want, "answer_bank" if want == banked else "default"):
                return
        if field.required:
            self._need(field, NO_OPTION if field.options else NOT_ON_FILE)
        else:
            self._skip(field, "no option fits")

    def _plan_heard_about(self, field: FormField, *, which: bool = False) -> None:
        """How the person heard of the job: the board it was found on first
        ("Indeed" in a Source list), then the kind of place that is. `which`
        is the list asking which board, where only the board or Other fits."""
        options = [o for o in field.options if not _NOT_AN_OPTION.search(o)]
        board = str(self.packet.job.get("source") or "").strip().lower()
        board = board if board in _BOARDS else ""
        if options:
            boards = [board] if board else []
            kinds = ["other"] if which else ["job board", "linkedin", "online", "internet", "other"]
            for wanted in (*boards, *kinds):
                option = pick_option(wanted, options)
                if option:
                    self._fill(field, option, "default")
                    return
        if field.kind in ("text", "textarea"):
            self._fill(field, board.title() if which and board else "Job board", "default")
            return
        if field.kind == "select" and which and board:
            # Its choices had not come when the form was read: the board by
            # name, which the page's own list is matched against when filled.
            self._fill(field, _BOARD_NAMES.get(board, board.title()), "default")
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
                _take_stated_answer(draft, field)
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


_STATED = re.compile(r"^\W*(?:the\s+)?answer\s*(?:is|would\s+be|:)\s*[\"'“]?([^\"'”:;,.(]+)", re.I)


def _take_stated_answer(draft: DraftAnswer, field: FormField) -> None:
    """A choice drafted for a person that answers anyway ("The answer is Yes:
    as Sr. Manager ... at GoTo ...") is taken as the option it names, and then
    checked like any other: an option not on the list goes back to the model.
    Free text marked for a person stays for a person."""
    if not draft.needs_human:
        return
    stated = _STATED.match(draft.reason or "")
    said = stated.group(1).strip() if stated else ""
    if field.kind in ("select", "radio"):
        if draft.option or said:
            draft.option = draft.option or said
            draft.needs_human = False
    elif field.kind == "multiselect":
        if draft.options or said:
            draft.options = list(draft.options) or [said]
            draft.needs_human = False


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
        "- A question whether the candidate has some experience or skill, or has it on the",
        "  resume, is settled by the facts when they show it: answer it (Yes) with",
        "  `needs_human` false. Never put an answer only in `reason`.",
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
    agree_to_terms: bool = False,
) -> FillPlan:
    """The plan for `fields`: what goes where, from which source, and what is missing.

    `agree_to_terms` (auto mode, which the person chose so applications go all
    the way) ticks a required terms box when `consent_terms` has no answer,
    as source auto_mode with a note, instead of stopping to ask."""
    planner = _Planner(packet, completer, allow_model)
    planner.agree_to_terms = agree_to_terms
    return planner.run(list(fields))


def make_answerer(
    packet: Packet,
    *,
    completer: Completer | None = None,
    allow_model: bool = True,
    agree_to_terms: bool = False,
) -> Answerer:
    """Bind the packet and the model, so a handler only ever passes the fields."""

    def answer(fields: list[FormField]) -> FillPlan:
        return plan_fills(
            fields,
            packet,
            completer=completer,
            allow_model=allow_model,
            agree_to_terms=agree_to_terms,
        )

    return answer
