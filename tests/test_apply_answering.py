"""The answering logic: fields in, a plan out, nothing invented."""

from __future__ import annotations

import pytest

from jobagent.apply import answering
from jobagent.apply.answering import (
    NEVER_GUESSED,
    NO_OPTION,
    NOT_GROUNDED,
    NOT_ON_FILE,
    DraftAnswer,
    DraftAnswers,
    canonical_key,
    make_answerer,
    pick_option,
    pick_options,
    plan_fills,
    question_key,
)
from jobagent.apply.models import FormField, Packet
from jobagent.llm.backend import Completion, LLMError
from jobagent.resume.facts import Fact


def F(key, label, kind="text", **kw) -> FormField:  # noqa: N802
    return FormField(key=key, label=label, kind=kind, **kw)


@pytest.fixture
def packet() -> Packet:
    facts = [
        Fact(
            id=1,
            kind="role",
            text="Led the billing rewrite at Acme and cut costs 40%",
            detail={"employer": "Acme", "title": "Senior Engineer", "start": "2021", "end": "2024"},
            tags=["python", "aws", "billing"],
        ),
        Fact(id=2, kind="skill", text="Python", tags=["python"]),
        Fact(id=3, kind="skill", text="AWS", tags=["aws"]),
    ]
    return Packet(
        job={"title": "Platform Engineer", "company": "Beta", "description": "Python and AWS."},
        contact={
            "full_name": "Mark Shield",
            "email": "mark@example.com",
            "phone": "+1 555 0100",
            "location": "Austin, TX",
        },
        resume_path="/tmp/resume.pdf",
        cover_letter="Dear Beta team, I led the billing rewrite at Acme.",
        cover_letter_path="/tmp/letter.pdf",
        links={"linkedin": "https://linkedin.com/in/mark", "github": "https://github.com/mark"},
        answers={},
        facts=facts,
        never_claim=["kubernetes"],
    )


def fills(plan) -> dict[str, object]:
    return {f.key: (f.file_path if f.file_path else f.value) for f in plan.fills}


def sources(plan) -> dict[str, str]:
    return {f.key: f.source for f in plan.fills}


# ------------------------------------------------------------- recognition --


@pytest.mark.parametrize(
    "label, key",
    [
        ("First Name", "first_name"),
        ("Last Name *", "last_name"),
        ("Full name", "full_name"),
        ("Name", "full_name"),
        ("Email", "email"),
        ("Phone", "phone"),
        ("LinkedIn Profile", "linkedin"),
        ("GitHub URL", "github"),
        ("Website / Portfolio", "website"),
        ("Resume/CV", "resume"),
        ("Cover Letter", "cover_letter"),
        ("Current location (city)", "location"),
        ("Are you legally authorized to work in the United States?", "work_authorization"),
        ("Will you now or in the future require sponsorship?", "visa_sponsorship"),
        ("What are your salary expectations?", "salary_expectation"),
        ("Earliest start date", "start_date"),
        ("Are you willing to relocate?", "relocation"),
        ("Have you previously worked at Beta?", "previously_employed"),
        ("How did you hear about this job?", "heard_about"),
        ("Were you referred by an employee?", "referral"),
        ("Tell us about a project you are proud of", None),
    ],
)
def test_labels_map_to_canonical_keys(label, key):
    assert canonical_key(F("k", label)) == key


def test_input_name_is_used_when_the_label_says_nothing():
    assert canonical_key(F("job_application[first_name]", "", name="job_application[first_name]"))
    assert canonical_key(F("urls[LinkedIn]", "")) == "linkedin"


def test_question_key_is_stable_and_bounded():
    assert question_key("Why do you want to work at Acme?") == "q:why do you want to work at acme"
    assert question_key("  Why   do you...  ") == "q:why do you"
    assert len(question_key("x" * 500)) <= 123


@pytest.mark.parametrize(
    "value, options, expected",
    [
        ("Yes", ["--", "Yes", "No"], "Yes"),
        ("yes", ["No", "Yes, I am authorized"], "Yes, I am authorized"),
        ("true", ["Yes", "No"], "Yes"),
        ("no", ["Yes", "No, I am not"], "No, I am not"),
        ("United States", ["Canada", "United States of America"], "United States of America"),
        ("Remote", ["Hybrid (2 days)", "Fully remote", "On-site"], "Fully remote"),
        ("Purple", ["Red", "Blue"], None),
        ("", ["Red"], None),
    ],
)
def test_pick_option(value, options, expected):
    assert pick_option(value, options) == expected


def test_pick_options_splits_lists():
    assert pick_options("Python, AWS; Go", ["Python", "Java", "AWS"]) == ["Python", "AWS"]


# ------------------------------------------------------------------ packet --


def test_contact_resume_links_and_letter_come_from_the_packet(packet):
    fields = [
        F("first", "First Name", required=True),
        F("last", "Last Name", required=True),
        F("email", "Email", kind="email", required=True),
        F("phone", "Phone", kind="tel"),
        F("city", "Location (City)"),
        F("resume", "Resume/CV", kind="file", required=True),
        F("cl_file", "Cover Letter", kind="file"),
        F("cl_text", "Cover letter", kind="textarea"),
        F("li", "LinkedIn Profile", kind="url"),
        F("gh", "GitHub Profile", kind="url"),
        F("site", "Website", kind="url"),
    ]
    plan = plan_fills(fields, packet)
    assert fills(plan) == {
        "first": "Mark",
        "last": "Shield",
        "email": "mark@example.com",
        "phone": "+1 555 0100",
        "city": "Austin, TX",
        "resume": "/tmp/resume.pdf",
        "cl_file": "/tmp/letter.pdf",
        "cl_text": "Dear Beta team, I led the billing rewrite at Acme.",
        "li": "https://linkedin.com/in/mark",
        "gh": "https://github.com/mark",
    }
    assert sources(plan)["first"] == "contact" and sources(plan)["resume"] == "resume"
    assert plan.needed == [] and plan.complete
    assert any("Website" in n for n in plan.notes), "an optional link not on file is skipped"


def test_full_name_field_and_single_word_names(packet):
    plan = plan_fills([F("name", "Name", required=True)], packet)
    assert fills(plan) == {"name": "Mark Shield"}
    packet.contact = {"full_name": "Cher"}
    plan = plan_fills([F("first", "First Name"), F("last", "Last Name", required=True)], packet)
    assert fills(plan) == {"first": "Cher"}
    assert [n.answer_key for n in plan.needed] == ["last_name"]


def test_missing_required_contact_detail_is_asked_for(packet):
    packet.contact.pop("phone")
    plan = plan_fills([F("phone", "Phone Number", kind="tel", required=True)], packet)
    assert plan.fills == []
    assert plan.needed[0].answer_key == "phone" and plan.needed[0].reason == NOT_ON_FILE
    assert not plan.complete


def test_missing_optional_upload_is_skipped_not_asked(packet):
    packet.cover_letter_path = None
    plan = plan_fills([F("cl", "Cover Letter", kind="file")], packet)
    assert plan.fills == [] and plan.needed == []
    plan = plan_fills([F("cl", "Cover Letter", kind="file", required=True)], packet)
    assert plan.needed[0].key == "cl"


# ------------------------------------------------------------ answer bank --


def test_answer_bank_answers_by_canonical_key_and_by_label(packet):
    packet.answers = {
        "work_authorization": "Yes",
        "visa_sponsorship": "No",
        "q:why do you want to work here": "Because of the billing work.",
    }
    fields = [
        F("auth", "Are you authorized to work in the US?", kind="select", options=["Yes", "No"]),
        F("visa", "Do you require visa sponsorship?", kind="radio", options=["Yes", "No"]),
        F("why", "Why do you want to work here?", kind="textarea", required=True),
    ]
    plan = plan_fills(fields, packet)
    assert fills(plan) == {"auth": "Yes", "visa": "No", "why": "Because of the billing work."}
    assert set(sources(plan).values()) == {"answer_bank"}


def test_answer_that_fits_no_option_is_asked_again(packet):
    packet.answers = {"work_authorization": "Maybe"}
    field = F("auth", "Work authorization", kind="select", options=["Yes", "No"], required=True)
    plan = plan_fills([field], packet)
    assert plan.fills == []
    assert plan.needed[0].reason == NO_OPTION and plan.needed[0].options == ["Yes", "No"]


def test_sensitive_questions_are_never_guessed_even_with_a_model(packet):
    class NeverCalled:
        name = "never"

        def complete(self, **kw):
            raise AssertionError("the model must not see a sensitive question")

    fields = [
        F(
            "auth",
            "Are you legally authorized to work in the US?",
            kind="select",
            required=True,
            options=["Yes", "No"],
        ),
        F("salary", "Desired salary", required=True),
        F("start", "When can you start?", kind="date", required=False),
        F("reloc", "Willing to relocate?", kind="radio", options=["Yes", "No"], required=True),
    ]
    plan = plan_fills(fields, packet, completer=NeverCalled())
    assert plan.fills == []
    assert [n.answer_key for n in plan.needed] == [
        "work_authorization",
        "salary_expectation",
        "start_date",
        "relocation",
    ]
    assert all(n.reason == NEVER_GUESSED for n in plan.needed)
    assert [n.required for n in plan.needed] == [True, True, False, True]


# ------------------------------------------------------- eeo and consent --


def test_eeo_declines_unless_the_answer_bank_says_otherwise(packet):
    fields = [
        F(
            "gender",
            "Gender",
            kind="select",
            options=["Male", "Female", "Decline To Self Identify"],
        ),
        F(
            "vet",
            "Veteran Status",
            kind="select",
            options=["I am a veteran", "I am not a veteran", "I don't wish to answer"],
        ),
        F("race", "Race", kind="select", options=["Asian", "White"], required=True),
    ]
    plan = plan_fills(fields, packet)
    assert fills(plan) == {"gender": "Decline To Self Identify", "vet": "I don't wish to answer"}
    assert plan.needed[0].key == "race" and plan.needed[0].answer_key == "race"

    packet.answers = {"veteran": "I am not a veteran"}
    plan = plan_fills(fields[1:2], packet)
    assert fills(plan) == {"vet": "I am not a veteran"} and sources(plan)["vet"] == "answer_bank"


def test_consent_boxes_are_ticked_and_marketing_boxes_are_not(packet):
    fields = [
        F("privacy", "I have read and agree to the privacy policy", kind="checkbox", required=True),
        F("news", "Keep me updated about future opportunities", kind="checkbox"),
        F("certify", "I certify that the information above is accurate", kind="checkbox"),
    ]
    plan = plan_fills(fields, packet)
    assert fills(plan) == {"privacy": True, "news": False, "certify": True}
    packet.answers = {question_key("Keep me updated about future opportunities"): "yes"}
    assert fills(plan_fills(fields[1:2], packet)) == {"news": True}


def test_heard_about_picks_a_harmless_option(packet):
    field = F(
        "src",
        "How did you hear about us?",
        kind="select",
        options=["Employee referral", "LinkedIn", "Job board", "Other"],
    )
    assert fills(plan_fills([field], packet)) == {"src": "Job board"}
    field.options = ["Referral", "Conference", "Other"]
    assert fills(plan_fills([field], packet)) == {"src": "Other"}
    text = F("src", "How did you hear about this role?", required=True)
    assert fills(plan_fills([text], packet)) == {"src": "Job board"}


# ------------------------------------------------------------------ model --


class FakeCompleter:
    name = "fake"

    def __init__(self, *batches):
        self.batches = list(batches)
        self.calls = []

    def complete(
        self, *, system, prompt, output, max_tokens=16000, effort=None, cache_system=False
    ):
        self.calls.append(prompt)
        if isinstance(self.batches[0], Exception):
            raise self.batches.pop(0)
        return Completion(result=self.batches.pop(0), input_tokens=50, output_tokens=5)


def test_open_questions_go_to_the_model_once_and_grounded_answers_are_used(packet):
    fields = [
        F("why", "Why do you want this role?", kind="textarea", required=True),
        F("years", "Years of Python experience", kind="number", required=True),
        F(
            "level",
            "Seniority",
            kind="select",
            options=["Junior", "Senior", "Staff"],
            required=True,
        ),
    ]
    completer = FakeCompleter(
        DraftAnswers(
            answers=[
                DraftAnswer(key="why", answer="I led the billing rewrite at Acme, using Python."),
                DraftAnswer(key="years", answer="3"),
                DraftAnswer(key="level", option="Senior"),
            ]
        )
    )
    plan = plan_fills(fields, packet, completer=completer)
    assert len(completer.calls) == 1
    assert (
        "key='why'" in completer.calls[0]
        and "options: Junior | Senior | Staff" in completer.calls[0]
    )
    assert fills(plan) == {
        "why": "I led the billing rewrite at Acme, using Python.",
        "years": "3",
        "level": "Senior",
    }
    assert set(sources(plan).values()) == {"model"}
    assert plan.input_tokens == 50 and plan.output_tokens == 5


def test_ungrounded_answer_is_sent_back_once_then_asked_of_the_user(packet):
    field = F("why", "Why do you want this role?", kind="textarea", required=True)
    completer = FakeCompleter(
        DraftAnswers(answers=[DraftAnswer(key="why", answer="I ran Kubernetes at Acme.")]),
        DraftAnswers(answers=[DraftAnswer(key="why", answer="I scaled Kafka to 9 clusters.")]),
    )
    plan = plan_fills([field], packet, completer=completer)
    assert len(completer.calls) == 2
    assert "refused" in completer.calls[1] and "kubernetes" in completer.calls[1].lower()
    assert plan.fills == []
    assert plan.needed[0].key == "why" and plan.needed[0].reason.startswith(NOT_GROUNDED)


def test_model_may_decline_and_optional_questions_are_then_skipped(packet):
    fields = [
        F("hobby", "What do you do for fun?", required=False),
        F("plans", "Where do you see yourself in five years?", required=True),
    ]
    completer = FakeCompleter(
        DraftAnswers(
            answers=[
                DraftAnswer(key="hobby", needs_human=True, reason="not on file"),
                DraftAnswer(key="plans", needs_human=True, reason="a plan, not a fact"),
            ]
        )
    )
    plan = plan_fills(fields, packet, completer=completer)
    assert plan.fills == []
    assert [n.key for n in plan.needed] == ["plans"]
    assert "a plan, not a fact" in plan.needed[0].reason
    assert any("hobby" in note.lower() or "fun" in note.lower() for note in plan.notes)


def test_model_option_outside_the_choices_is_refused(packet):
    field = F("level", "Seniority", kind="select", options=["Junior", "Senior"], required=True)
    completer = FakeCompleter(
        DraftAnswers(answers=[DraftAnswer(key="level", option="Principal")]),
        DraftAnswers(answers=[DraftAnswer(key="level", option="Principal")]),
    )
    plan = plan_fills([field], packet, completer=completer)
    assert plan.fills == [] and plan.needed[0].key == "level"


def test_model_failure_parks_the_open_questions(packet):
    field = F("why", "Why us?", required=True)
    plan = plan_fills([field], packet, completer=FakeCompleter(LLMError("down")))
    assert plan.needed[0].key == "why" and "down" in plan.needed[0].reason


def test_no_model_means_open_questions_are_asked_of_the_user(packet):
    fields = [F("why", "Why us?", required=True), F("fun", "Fun fact", required=False)]
    plan = plan_fills(fields, packet, completer=None)
    assert [n.key for n in plan.needed] == ["why"]
    plan = plan_fills(fields, packet, completer=FakeCompleter(), allow_model=False)
    assert [n.key for n in plan.needed] == ["why"]


def test_answerer_binds_the_packet(packet):
    answer = make_answerer(packet)
    plan = answer([F("email", "Email", kind="email")])
    assert fills(plan) == {"email": "mark@example.com"}


def test_a_planning_bug_becomes_a_needed_input_not_a_crash(packet, monkeypatch):
    monkeypatch.setattr(answering, "_section", lambda field: 1 / 0)
    plan = plan_fills([F("x", "Anything", required=True)], packet)
    assert plan.needed[0].key == "x" and "planning failed" in plan.needed[0].reason
