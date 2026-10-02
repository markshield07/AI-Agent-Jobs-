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


def test_each_part_of_an_address_is_its_own_answer(packet):
    """Workday asks for street, city, state and ZIP in separate boxes, beside
    a phone number split into code, number and extension."""
    packet.contact["location"] = "Menifee, California, United States"
    packet.answers = {"address": "1 Main St", "postal_code": "92584"}
    fields = [
        F("street", "Address Line 1", required=True),
        F("street2", "Address Line 2"),
        F("city", "City", required=True),
        F("state", "State", kind="select", options=["Arizona", "California"], required=True),
        F("zip", "Postal Code", required=True),
        F("code", "Country Phone Code", required=True),
        F("phone", "Phone Number", required=True),
        F("ext", "Phone Extension"),
        F("preferred", "I have a preferred name", kind="checkbox"),
    ]
    plan = plan_fills(fields, packet)
    assert fills(plan) == {
        "street": "1 Main St",
        "city": "Menifee",
        "state": "California",
        "zip": "92584",
        "phone": "+1 555 0100",
    }
    assert [(n.key, n.answer_key) for n in plan.needed] == [("code", "phone_country")]
    packet.answers = {}
    plan = plan_fills(fields[:5], packet)
    assert {n.key: n.answer_key for n in plan.needed} == {
        "street": "address",
        "zip": "postal_code",
    }, "street and ZIP are asked for under keys of their own"


def test_a_work_history_entry_never_gets_contact_details(packet):
    """Location in a job held (Workday's My Experience) is where the job was."""
    fields = [
        F("loc", "Location", section="experience"),
        F("title", "Job Title", section="experience", required=True),
    ]
    plan = plan_fills(fields, packet)
    assert fills(plan) == {}
    assert [(n.key, n.answer_key) for n in plan.needed] == [("title", "q:job title")]


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


def test_the_us_answers_are_not_used_for_another_country(packet):
    packet.answers = {"work_authorization": "Yes", "visa_sponsorship": "No"}
    fields = [
        F("ca", "Are you authorized to work in Canada?", kind="radio", options=["Yes", "No"]),
        F("uk", "Will you require sponsorship in the UK?", kind="radio", options=["Yes", "No"]),
    ]
    plan = plan_fills(fields, packet)
    assert plan.fills == []
    assert {n.key for n in plan.needed} == {"ca", "uk"}


@pytest.mark.parametrize(
    "label, key",
    [
        ("Are you currently employed?", None),
        ("Are you currently employed by Acme?", "previously_employed"),
        ("Do you have at least 18 months of experience with Python?", None),
        ("Are you 18 years of age or older?", "over_18"),
        ("Are you at least 18?", "over_18"),
        ("What is your availability for interviews?", None),
        ("What is your availability?", "start_date"),
    ],
)
def test_look_alike_questions_get_their_own_key(label, key):
    assert answering._key_of_text(label) == key


def test_a_yes_or_no_about_the_salary_is_not_the_salary(packet):
    packet.answers = {"salary_expectation": "150000"}
    field = F(
        "range",
        "Is the salary range of $120,000 - $140,000 acceptable?",
        kind="radio",
        options=["Yes", "No"],
        required=True,
    )
    plan = plan_fills([field], packet)
    assert plan.fills == []
    assert plan.needed[0].reason == NEVER_GUESSED
    assert plan.needed[0].answer_key.startswith("q:"), "its Yes is kept apart from the amount"


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


def test_terms_are_agreed_to_only_once_the_person_has_said_so(packet):
    fields = [
        F("privacy", "I have read and agree to the privacy policy", kind="checkbox", required=True),
        F("news", "Keep me updated about future opportunities", kind="checkbox"),
        F("certify", "I certify that the information above is accurate", kind="checkbox"),
    ]
    plan = plan_fills(fields, packet)
    assert fills(plan) == {"news": False}, "no box agreed to on the person's behalf"
    assert [(n.key, n.answer_key, n.reason) for n in plan.needed] == [
        ("privacy", answering.CONSENT_KEY, answering.NEVER_GUESSED)
    ]
    packet.answers[answering.CONSENT_KEY] = "Yes"
    plan = plan_fills(fields, packet)
    assert fills(plan) == {"privacy": True, "news": False, "certify": True}
    assert sources(plan)["privacy"] == "answer_bank"
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


def test_a_choice_the_model_answers_while_asking_for_a_person_is_taken(packet):
    # Luxoft's, 2026-09-28: the model marked it for a person and answered anyway.
    field = F(
        "lead",
        "Do you have Team Lead or Technical Lead experience listed in your resume?",
        kind="radio",
        options=["Yes", "No"],
        required=True,
    )
    reason = "The answer is Yes: as Sr. Manager Field Engineering at GoTo he leads the team."
    completer = FakeCompleter(
        DraftAnswers(answers=[DraftAnswer(key="lead", needs_human=True, reason=reason)])
    )
    plan = plan_fills([field], packet, completer=completer)
    assert fills(plan) == {"lead": "Yes"} and plan.needed == []


def test_free_text_the_model_marks_for_a_person_stays_with_the_person(packet):
    field = F("why", "Why do you want this role?", kind="textarea", required=True)
    completer = FakeCompleter(
        DraftAnswers(
            answers=[DraftAnswer(key="why", answer="Because.", needs_human=True, reason="a plan")]
        )
    )
    plan = plan_fills([field], packet, completer=completer)
    assert plan.fills == [] and plan.needed[0].key == "why"


def test_a_question_about_the_resume_is_not_the_resume_upload():
    field = F("q", "Do you have CISCO & Load Balancing (F5) listed in your resume?", kind="radio")
    assert answering.canonical_key(field) is None
    assert answering.canonical_key(F("cv", "Resume", kind="file")) == "resume"


def test_the_commute_question_is_kept_per_place(packet):
    field = F(
        "c",
        "Are you comfortable commuting to this job's location?",
        kind="radio",
        options=["Yes", "No"],
        required=True,
    )
    assert answering.canonical_key(field) == "commute_ok"
    packet.job["location"] = "Long Beach, CA"
    assert answering.answer_key_for(field, packet.job) == "commute_ok:long_beach_ca"
    packet.answers["commute_ok"] = "Yes"  # not this place's answer
    plan = plan_fills([field], packet)
    assert plan.fills == [] and plan.needed[0].answer_key == "commute_ok:long_beach_ca"
    packet.answers["commute_ok:long_beach_ca"] = "No"
    assert fills(plan_fills([field], packet)) == {"c": "No"}


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


@pytest.mark.parametrize(
    ("label", "from_letter"),
    [
        ("Why are you interested in working for CrowdStrike?", True),
        ("Why do you want to join Beta?", True),
        ("Tell us why you are applying for this role", True),
        ("What interests you in joining our team?", False),
        ("Why are you leaving your current job?", False),
    ],
)
def test_why_this_company_is_answered_from_the_cover_letter(packet, label, from_letter):
    field = FormField(key="why", label=label, kind="textarea")
    plan = plan_fills([field], packet)
    if from_letter:
        assert fills(plan) == {"why": "I led the billing rewrite at Acme."}
        assert sources(plan) == {"why": "cover_letter"}
    else:
        assert "why" not in fills(plan)


@pytest.mark.parametrize(
    ("label", "key"),
    [
        (
            "Are you eligible to work in the country in which this position is located?",
            "work_authorization",
        ),
        (
            "Will you now or in the future require work authorization sponsorship?",
            "visa_sponsorship",
        ),
        (
            "Conflict of Interest If hired, do you expect that you will engage in any outside "
            "employment that could conflict with our business? You do not need to disclose an "
            "activity that will end before your start date.",
            None,
        ),
        ("Country", "country"),
        ("Address Line 1", "address"),
        (
            "Are you legally authorized to work in the United States without the need for "
            "sponsorship?",
            "work_authorization",
        ),
        ("Will you now or in the future require visa sponsorship?", "visa_sponsorship"),
    ],
)
def test_long_questions_are_read_by_their_first_sentence(label, key):
    assert canonical_key(F("k", label)) == key


@pytest.mark.parametrize(
    "label",
    [
        "Conflict of Interest If hired, do you expect that you will engage in any outside "
        "employment that could conflict with our business?",
        "Acknowledgment: CrowdStrike is an AI-native company. Do you agree to these terms?",
        "Are you bound by a non-compete agreement?",
    ],
)
def test_legal_attestations_are_never_drafted(packet, label):
    field = FormField(key="k", label=label, kind="select", required=True, options=["Yes", "No"])
    calls = []
    plan = answering.make_answerer(
        packet, completer=lambda *a, **k: calls.append(a), allow_model=True
    )([field])
    assert calls == [], "never sent to the model"
    assert [n.reason for n in plan.needed] == [answering.NEVER_GUESSED]
    assert plan.needed[0].answer_key.startswith("q:")


# Greenhouse's Anthropic form, word for word: neither label says "agree", but
# every choice does, and the model picked them for the person.
_ARBITRATION = [
    (
        "Please read the arbitration agreement below",
        ["I will read the arbitration agreement below."],
    ),
    (
        "Agreement to Arbitrate",
        ["I understand and agree to the terms of the Agreement to Arbitrate set forth above."],
    ),
    ("Do you accept these terms?", ["I accept", "I do not accept"]),
    ("Class Action Waiver", ["Yes", "No"]),
]


@pytest.mark.parametrize(("label", "options"), _ARBITRATION)
def test_arbitration_and_agreeing_choices_are_never_drafted(packet, label, options):
    field = FormField(key="k", label=label, kind="select", required=True, options=options)
    calls = []
    plan = answering.make_answerer(
        packet, completer=lambda *a, **k: calls.append(a), allow_model=True
    )([field])
    assert calls == [], "never sent to the model"
    assert [n.reason for n in plan.needed] == [answering.NEVER_GUESSED]


def test_an_arbitration_answer_on_file_is_used(packet):
    label, options = _ARBITRATION[1]
    packet.answers[answering.question_key(label)] = options[0]
    field = FormField(key="k", label=label, kind="select", required=True, options=options)
    assert fills(plan_fills([field], packet)) == {"k": options[0]}


def test_an_attestation_answered_once_is_kept(packet):
    label = "Conflict of Interest If hired, do you expect outside employment?"
    packet.answers[answering.question_key(label)] = "No"
    field = FormField(key="k", label=label, kind="select", required=True, options=["Yes", "No"])
    assert fills(plan_fills([field], packet)) == {"k": "No"}


def test_an_accommodation_request_is_not_the_disability_self_id(packet):
    from jobagent.apply.answering import answer_key_for

    label = (
        "Do you need a reasonable accommodation due to a disability or medical need for "
        "applying, interviewing, or otherwise participating in the application process?"
    )
    field = F("k", label, kind="select")
    field.options = ["Yes", "No"]
    assert answer_key_for(field).startswith("q:")
    assert answer_key_for(F("k", "Disability status", kind="select")) == "disability"
    packet.answers["disability"] = "No"
    plan = plan_fills([field], packet)
    assert fills(plan) == {}, "the self-ID answer is not an accommodation request"
    assert [n.reason for n in plan.needed] == [answering.NEVER_GUESSED]


@pytest.mark.parametrize(
    ("letter", "body"),
    [
        (
            "Dear CrowdStrike Hiring Team,\n\nI run networks.\n\nI led teams.\n\n"
            "Sincerely,\nMark Shield",
            "I run networks.\n\nI led teams.",
        ),
        ("I run networks.\n\nBest regards,\n\nMark Shield", "I run networks."),
        ("Dear Hiring Manager:\n\nI run networks.\n\nThank you,\nMark", "I run networks."),
        ("I run networks. Thank you for your time.", "I run networks. Thank you for your time."),
    ],
)
def test_a_form_answer_takes_the_letter_without_greeting_or_sign_off(letter, body):
    assert answering.letter_body(letter) == body


def test_auto_mode_agrees_to_required_terms_and_says_so(packet):
    fields = [
        F("privacy", "I have read and agree to the privacy policy", kind="checkbox", required=True),
        F("certify", "I certify that the information above is accurate", kind="checkbox"),
    ]
    plan = plan_fills(fields, packet, agree_to_terms=True)
    assert fills(plan) == {"privacy": True}, "an optional box stays unticked"
    assert sources(plan) == {"privacy": "auto_mode"}
    assert any("auto mode" in n for n in plan.notes)
    assert plan.needed == []

    packet.answers[answering.CONSENT_KEY] = "No"
    plan = plan_fills(fields, packet, agree_to_terms=True)
    assert fills(plan) == {"privacy": False, "certify": False}, "the person's No wins"


# ------------------------------------------------ what Serco's form showed --


@pytest.mark.parametrize(
    "label, key",
    [
        ("Town/City", "city"),
        ("City / Town", "city"),
        ("Home Phone", "other_phone"),
        ("Work Phone Number", "other_phone"),
        ("Mobile Phone", "phone"),
        ("Desired Salary Currency", "salary_currency"),
        ("Desired Salary Timeframe", "salary_period"),
        ("Pay frequency", "salary_period"),
        ("Desired Salary Amount", "salary_expectation"),
        ("Security Clearance Level", "security_clearance"),
        ("Security Clearance Status", "clearance_status"),
        ("Source", "heard_about_source"),
        ("Which job board?", "heard_about_source"),
        ("Best Time to Contact", "contact_time"),
        ("Preferred Contact Method", "contact_method"),
    ],
)
def test_serco_labels_have_keys_of_their_own(label, key):
    assert canonical_key(F("x", label)) == key


def test_city_country_and_a_second_phone_on_a_serco_form(packet):
    packet.contact["location"] = "Menifee, California, United States"
    fields = [
        F("city", "Town/City", required=True),
        F(
            "country",
            "Country of Residence",
            kind="select",
            required=True,
            options=["Canada", "United States", "Uruguay"],
        ),
        F("mobile", "Mobile Phone", required=True),
        F("home", "Home Phone"),
    ]
    plan = plan_fills(fields, packet)
    assert fills(plan) == {"city": "Menifee", "country": "United States", "mobile": "+1 555 0100"}
    assert plan.needed == []

    packet.answers["home_phone"] = "+1 555 0199"
    assert fills(plan_fills(fields, packet))["home"] == "+1 555 0199"
    del packet.answers["home_phone"]
    fields[3].required = True
    assert fills(plan_fills(fields, packet))["home"] == "+1 555 0100", "required: the main number"


def test_salary_currency_and_period_follow_the_salary_on_file(packet):
    fields = [
        F(
            "cur",
            "Desired Salary Currency",
            kind="select",
            required=True,
            options=["USD $", "CAN $"],
        ),
        F("amt", "Desired Salary Amount", kind="number", required=True),
        F("per", "Desired Salary Timeframe", kind="select", required=True, options=["Yr.", "Hr."]),
    ]
    plan = plan_fills(fields, packet)
    assert {n.key for n in plan.needed} == {"cur", "amt", "per"}, "no salary on file: asked"
    assert all(n.reason == NEVER_GUESSED for n in plan.needed)

    packet.answers["salary_expectation"] = "$150,000"
    plan = plan_fills(fields, packet)
    assert fills(plan) == {"cur": "USD $", "amt": "150000", "per": "Yr."}
    assert plan.needed == []

    packet.answers["salary_expectation"] = "65"
    assert fills(plan_fills(fields, packet))["per"] == "Hr."
    packet.answers["salary_period"] = "Yearly"
    fields[2].options = ["Hourly", "Yearly"]
    assert fills(plan_fills(fields, packet))["per"] == "Yearly", "the answer bank wins"


def test_a_required_privacy_notice_that_mentions_future_positions_is_terms(packet):
    label = (
        "* By continuing I understand that the information I disclose will be visible to and "
        "shared between HR and Hiring Managers. Such information disclosed by me will be used "
        "to support the recruitment selection processes for this or future positions."
    )
    field = F("privacy", label, kind="checkbox", required=True)
    assert answering.answer_key_for(field) == answering.CONSENT_KEY
    plan = plan_fills([field], packet, agree_to_terms=True)
    assert fills(plan) == {"privacy": True} and sources(plan) == {"privacy": "auto_mode"}
    optional = F("news", "Keep me informed about future positions and updates", kind="checkbox")
    assert fills(plan_fills([optional], packet, agree_to_terms=True)) == {"news": False}


def test_how_to_reach_the_person_defaults_to_any_time_by_email(packet):
    fields = [
        F(
            "time",
            "Best Time to Contact",
            kind="select",
            required=True,
            options=["Anytime", "Morning", "Afternoon", "Evening"],
        ),
        F(
            "how",
            "Preferred Contact Method",
            kind="select",
            required=True,
            options=["Home Phone", "Cell Phone", "Email"],
        ),
    ]
    plan = plan_fills(fields, packet)
    assert fills(plan) == {"time": "Anytime", "how": "Email"}
    packet.answers["contact_method"] = "Cell Phone"
    assert fills(plan_fills(fields, packet))["how"] == "Cell Phone"


def test_a_source_list_gets_the_board_the_job_was_found_on(packet):
    widget = "api_key: undefined extensions:AwliWidget@https://www.linkedin.com/apply-with-linkedin"
    packet.job["source"] = "indeed"
    field = F("src", "Source", kind="select", required=True, options=[widget, "Indeed", "Other"])
    assert fills(plan_fills([field], packet)) == {"src": "Indeed"}
    field.options = [widget, "Dice", "Other"]
    assert fills(plan_fills([field], packet)) == {"src": "Other"}, "never the widget's text"
    packet.job["source"] = "linkedin"
    field = F(
        "src", "Source", kind="select", required=True, options=["Indeed", "LinkedIn", "Other"]
    )
    assert fills(plan_fills([field], packet)) == {"src": "LinkedIn"}


def test_a_source_list_read_before_its_choices_came_gets_the_board_by_name(packet):
    packet.job["source"] = "indeed"
    field = F("src", "Source", kind="select", required=True, options=[])
    assert fills(plan_fills([field], packet)) == {"src": "Indeed"}
    packet.job["source"] = "company site"
    assert plan_fills([field], packet).needed[0].key == "src"


# ------------------------------------------ from the 2026-09-28 LinkedIn runs --


def test_where_are_you_currently_located_is_the_home_location(packet):
    field = F("loc", "Where are you currently located? (city, state)", required=True)
    assert answering.canonical_key(field) == "location"
    assert fills(plan_fills([field], packet)) == {"loc": packet.contact["location"]}


def test_a_us_person_question_has_one_key_and_takes_an_answer_given_before(packet):
    field = F(
        "usp",
        "Please indicate whether you are a 'U.S. person' (a U.S. citizen, permanent resident "
        "or protected individual)?",
        kind="radio",
        options=["Yes", "No"],
        required=True,
    )
    assert answering.canonical_key(field) == "us_person"
    plan = plan_fills([field], packet)
    assert plan.fills == [] and plan.needed[0].answer_key == "us_person"
    # Answered on another company's form, under that form's wording.
    packet.answers["q:are you a u s person as defined by export regulations"] = "Yes"
    assert fills(plan_fills([field], packet)) == {"usp": "Yes"}
    packet.answers["us_person"] = "No"
    assert fills(plan_fills([field], packet)) == {"usp": "No"}, "its own key first"


def test_a_required_box_to_process_personal_information_is_agreed_to_in_auto_mode(packet):
    field = F(
        "pi", "Allow us to process your personal information.", kind="checkbox", required=True
    )
    plan = answering.make_answerer(packet, completer=None, agree_to_terms=True)([field])
    assert fills(plan) == {"pi": True}


@pytest.mark.parametrize(
    "label",
    [
        "What is the address from which you plan on working?",
        "Working address",
        "Full address",
    ],
)
def test_a_box_for_the_whole_address_gets_street_city_state_and_zip(packet, label):
    packet.contact["location"] = "Menifee, CA, United States"
    packet.answers = {"address": "29605 Rigging Way", "postal_code": "92584"}
    plan = plan_fills([F("where", label, required=True)], packet)
    assert fills(plan) == {"where": "29605 Rigging Way, Menifee, CA 92584"}


def test_a_street_box_still_gets_the_street_alone(packet):
    packet.contact["location"] = "Menifee, CA, United States"
    packet.answers = {"address": "29605 Rigging Way", "postal_code": "92584"}
    plan = plan_fills([F("street", "Street address", required=True)], packet)
    assert fills(plan) == {"street": "29605 Rigging Way"}
