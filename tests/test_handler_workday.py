"""The Workday handler against a company's posting and its steps, in a real browser.

The fixture draws the posting and the application with the data-automation-id
values Workday uses, and with its own controls: dropdowns that are buttons, a
"How did you hear about us?" prompt whose categories open further lists, and
a date in three spin buttons. Like the live site it draws each step a while
after Save and Continue, and its first two steps copy a live posting's markup.
Its variants prove the stops: a sign-in page, a question nobody on file
answers, a step that will not save, a closed posting, a Submit with no
confirmation.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from jobagent.apply.answering import make_answerer, question_key
from jobagent.apply.handlers import default_handlers, handler_for
from jobagent.apply.handlers.workday import WorkdayHandler, parse_date
from jobagent.apply.models import Fill, FillPlan, FormField, NeededInput, Packet
from jobagent.resume.facts import Fact

FIXTURE = Path(__file__).parent / "fixtures" / "forms" / "workday.html"
POSTING = (
    "https://acme.wd5.myworkdayjobs.com/acmecareers/job/USA-Remote/"
    "Network-Engineering-Manager_R12345"
)


def fixture_url(variant: str = "") -> str:
    return FIXTURE.as_uri() + (f"?variant={variant}" if variant else "")


@pytest.fixture
def packet(tmp_path) -> Packet:
    resume = tmp_path / "mark-shield.pdf"
    resume.write_bytes(b"%PDF-1.4 resume")
    return Packet(
        job={
            "id": "wd1",
            "title": "Network Engineering Manager",
            "company": "Acme Robotics",
            "source": "linkedin",
            "url": fixture_url(),
            "apply_url": None,
        },
        contact={
            "full_name": "Mark Shield",
            "first_name": "Mark",
            "last_name": "Shield",
            "email": "mark@example.com",
            "phone": "+1 555 0100",
            "location": "Austin, Texas, United States",
        },
        resume_path=str(resume),
        cover_letter="I run networks.",
        answers={
            "work_authorization": "Yes",
            "visa_sponsorship": "No",
            "previously_employed": "No",
            "address": "1 Congress Ave",
            "postal_code": "78701",
        },
        facts=[
            Fact(
                id=1,
                kind="role",
                text="Ran the network operations center at Globex",
                detail={
                    "employer": "Globex Corporation",
                    "title": "Network Operations Manager",
                    "start": "April 2018",
                    "end": "Present",
                },
            ),
            Fact(
                id=2,
                kind="role",
                text="Built the campus network at Initech",
                detail={
                    "employer": "Initech",
                    "title": "Network Engineer",
                    "start": "2012-06",
                    "end": "March 2018",
                },
            ),
        ],
    )


def run(page, packet, *, submit=False, variant="", shot=None, settle_ms=500, draw_ms=None):
    packet.job["url"] = fixture_url(variant)
    if draw_ms:
        packet.job["url"] += ("&" if variant else "?") + f"draw_ms={draw_ms}"
    answerer = make_answerer(packet, completer=None, allow_model=False)
    handler = WorkdayHandler()
    handler.settle_ms = settle_ms
    handler.pause_ms = 100
    handler.step_timeout_ms = 5_000
    handler.poll_ms = 50
    return handler.apply(page, packet, answerer, submit=submit, screenshot_path=shot)


def js(page, name):
    return page.evaluate(f"() => window.{name} === undefined ? null : window.{name}")


# ------------------------------------------------------------------- url --


def test_the_handler_claims_workday_postings():
    handler = WorkdayHandler()
    assert handler.matches(POSTING)
    assert handler.matches("https://acme.wd1.myworkdaysite.com/recruiting/acme/careers/job/1")
    assert not handler.matches("https://www.linkedin.com/jobs/view/1/")
    picked = handler_for(POSTING, None, default_handlers())
    assert picked is not None and picked.ats == "workday"


def test_dates_are_read_in_the_shapes_answers_come_in():
    assert parse_date("2026-09-27") == (9, 27, 2026)
    assert parse_date("9/27/2026") == (9, 27, 2026)
    assert parse_date("September 27, 2026") == (9, 27, 2026)
    assert parse_date("09/2026") == (9, None, 2026)
    assert parse_date("2019") == (None, None, 2019)
    assert parse_date("two weeks after an offer") is None


# --------------------------------------------------------------- dry run --


@pytest.mark.usefixtures("page")
def test_a_dry_run_walks_every_step_and_stops_at_submit(page, packet, tmp_path):
    shot = tmp_path / "shots" / "workday.png"
    result = run(page, packet, shot=str(shot))

    assert result.outcome == "dry_run", result.error
    assert js(page, "__start") == "autofill", "Autofill with Resume is the start chosen"
    assert js(page, "__steps") == [
        "autofill",
        "info",
        "experience",
        "questions",
        "disclosures",
        "selfid",
        "review",
    ]
    assert js(page, "__submitted") is None, "a dry run never presses Submit"
    assert shot.is_file()


@pytest.mark.usefixtures("page")
def test_workdays_own_controls_are_filled_like_a_person_would(page, packet):
    result = run(page, packet)
    assert result.outcome == "dry_run", result.error
    answers = js(page, "__answers")
    assert answers["info"]["phoneType"] == "Mobile", "phone device type is housekeeping"
    assert answers["info"]["phone"] == "+1 555 0100"
    assert answers["info"]["previous"] == "false"
    assert answers["questions"] == {
        "auth": "Yes",
        "sponsor": "No",
        "teams": "",
        "why": "I run networks.",
    }
    assert answers["disclosures"]["gender"] == "I do not wish to self-identify"
    assert answers["disclosures"]["veteran"] == "I don't wish to answer"
    assert answers["disclosures"]["terms"] is True
    today = date.today()
    assert answers["selfid"] == {
        "name": "Mark Shield",
        "month": f"{today.month:02d}",
        "day": f"{today.day:02d}",
        "year": str(today.year),
        "disability": ["decline"],
    }


@pytest.mark.usefixtures("page")
def test_my_information_is_read_once_drawn_and_every_box_filled(page, packet):
    """The live run read My Information before Workday drew it and pressed
    Save and Continue on an empty form."""
    result = run(page, packet)
    assert result.outcome == "dry_run", result.error
    assert js(page, "__answers")["info"] == {
        "source": "LinkedIn",
        "previous": "false",
        "country": "United States of America",
        "first": "Mark",
        "last": "Shield",
        "preferred": False,
        "street": "1 Congress Ave",
        "city": "Austin",
        "state": "Texas",
        "zip": "78701",
        "phoneType": "Mobile",
        "code": "United States of America (+1)",
        "phone": "+1 555 0100",
        "extension": "",
    }


@pytest.mark.usefixtures("page")
def test_the_unlabelled_autofill_box_takes_the_resume(page, packet):
    result = run(page, packet)
    uploads = [f for f in result.filled if f.file_path]
    assert [f.label for f in uploads] == ["Resume", "Resume"], "Autofill, then My Experience"
    assert js(page, "__answers")["autofillResume"] == "mark-shield.pdf"


@pytest.mark.usefixtures("page")
def test_my_information_is_read_as_the_page_means_it(page, packet):
    result = run(page, packet)
    info = {f.key: f for f in result.fields if f.key != "input-0"}
    keys = [f.key for f in result.fields]
    assert keys.count("source--source") == 1
    assert keys.count("phoneNumber--countryPhoneCode") == 1
    assert info["candidateIsPreviousWorker"].required, "required on its group and in its legend"
    state = info["address--countryRegion"]
    assert state.options == ["Arizona", "California", "Texas"], "a picked pill is not an option"
    assert info["phoneNumber--phoneType"].options == ["Home", "Mobile", "Work"]
    code = {f.key: f for f in result.filled}["phoneNumber--countryPhoneCode"]
    assert code.source == "prefilled"


@pytest.mark.usefixtures("page")
def test_how_did_you_hear_follows_the_board_the_job_came_from(page, packet):
    result = run(page, packet)
    assert js(page, "__answers")["info"]["source"] == "LinkedIn"
    by_label = {f.label: f for f in result.filled}
    assert by_label["How Did You Hear About Us?"].value == "Job Board"


@pytest.mark.usefixtures("page")
def test_a_category_with_no_known_board_is_asked_not_guessed(page, packet):
    packet.job["source"] = "greenhouse"
    result = run(page, packet, submit=True)
    assert result.outcome == "needs_input"
    asked = [n for n in result.needed if n.required]
    assert [n.label for n in asked] == ["How Did You Hear About Us?"]
    assert "further list" in asked[0].reason
    assert js(page, "__steps") == ["autofill", "info"]


@pytest.mark.usefixtures("page")
def test_what_autofill_read_is_kept_and_my_experience_takes_the_resume_again(page, packet):
    packet.contact["first_name"] = "Marcus"
    result = run(page, packet)
    by_label = {f.label: f for f in result.filled}
    assert by_label["First Name"].source == "prefilled"
    assert js(page, "__answers")["info"]["first"] == "Mark"
    assert by_label["Country"].value == "United States of America"
    assert js(page, "__answers")["autofillResume"] == "mark-shield.pdf"
    # Live, the required Resume/CV box on My Experience is empty after autofill.
    assert js(page, "__answers")["experienceResume"] == "mark-shield.pdf"
    assert js(page, "__uploads") == 2


@pytest.mark.usefixtures("page")
def test_work_history_dates_come_from_the_role_each_entry_names(page, packet):
    result = run(page, packet)
    assert result.outcome == "dry_run", result.error
    assert js(page, "__answers")["jobs"] == [
        {
            "title": "Network Operations Manager",
            "company": "Globex",
            "location": "",
            "description": "Ran the network operations center.",
            "current": True,
            "from": "04/2018",
            "to": "/",
        },
        {
            "title": "Network Engineer",
            "company": "Initech",
            "location": "",
            "description": "Built the campus network.",
            "current": False,
            "from": "06/2012",
            "to": "03/2018",
        },
    ]
    assert not any(n.key.endswith("--location") for n in result.needed)


@pytest.mark.usefixtures("page")
def test_an_entry_no_role_on_file_names_is_asked_about(page, packet):
    packet.facts = packet.facts[:1]
    result = run(page, packet)
    assert result.outcome == "needs_input"
    asked = sorted(n.key for n in result.needed if n.required)
    assert asked == ["workExperience-7--endDate", "workExperience-7--startDate"]
    assert all(n.answer_key.startswith("q:") for n in result.needed), "never the contact keys"


@pytest.mark.usefixtures("page")
def test_an_empty_entry_takes_the_most_recent_role(page, packet):
    # Live, Apply Manually and a saved draft show one empty entry, workExperience-5.
    packet.facts.reverse()
    result = run(page, packet, variant="manual")
    assert result.outcome == "dry_run", result.error
    assert js(page, "__answers")["jobs"] == [
        {
            "title": "Network Operations Manager",
            "company": "Globex Corporation",
            "location": "",
            "description": "Ran the network operations center at Globex",
            "current": True,
            "from": "04/2018",
            "to": "/",
        }
    ]
    assert not [n for n in result.needed if n.required]


@pytest.mark.usefixtures("page")
def test_a_date_box_under_its_display_still_takes_the_date(page, packet):
    """Live, "MM" is drawn over the month box and a plain click on it times out."""
    result = run(page, packet, variant="manual")
    assert result.outcome == "dry_run", result.error
    assert js(page, "__answers")["jobs"][0]["from"] == "04/2018"


@pytest.mark.usefixtures("page")
def test_the_next_step_is_awaited_while_the_bar_has_moved_over_the_old_page(page, packet):
    """Live, the progress bar moves first, then the page goes blank, then loads,
    and Save and Continue's page settles long before that."""
    result = run(page, packet, settle_ms=50, draw_ms=2000)
    assert result.outcome == "dry_run", result.error
    assert js(page, "__early") is None, "Save and Continue pressed on a step still going"
    assert js(page, "__steps") == [
        "autofill",
        "info",
        "experience",
        "questions",
        "disclosures",
        "selfid",
        "review",
    ]


@pytest.mark.usefixtures("page")
def test_why_this_company_takes_the_cover_letter(page, packet):
    result = run(page, packet)
    assert result.outcome == "dry_run", result.error
    assert js(page, "__answers")["questions"]["why"] == "I run networks."


@pytest.mark.usefixtures("page")
def test_entry_fields_keep_their_entry_and_say_which_one_it_is(page, packet):
    result = run(page, packet, variant="manual")
    entry = {f.key: f.label for f in result.fields if f.key.startswith("workExperience-")}
    assert entry["workExperience-5--jobTitle"] == "Job Title (work experience 1)"
    assert entry["workExperience-5--companyName"] == "Company (work experience 1)"
    assert (
        entry["workExperience-5--currentlyWorkHere"] == "I currently work here (work experience 1)"
    )
    assert entry["workExperience-5--roleDescription"] == "Role Description (work experience 1)"
    assert not {"jobTitle", "companyName", "location", "currentlyWorkHere"} & {
        f.key for f in result.fields
    }


@pytest.mark.usefixtures("page")
def test_entries_are_counted_in_page_order(page, packet):
    result = run(page, packet)
    labels = {f.key: f.label for f in result.fields}
    assert labels["workExperience-6--jobTitle"] == "Job Title (work experience 1)"
    assert labels["workExperience-7--startDate"] == "From (work experience 2)"


def test_the_role_given_to_an_empty_entry_outranks_a_model_guess():
    handler = WorkdayHandler()
    fields = [
        FormField(key="workExperience-5--jobTitle", label="Job Title", kind="text", required=True),
        FormField(
            key="workExperience-5--roleDescription", label="Role Description", kind="textarea"
        ),
    ]
    handler._entries = {
        "workExperience-5--jobTitle": ("workExperience-5", "jobTitle"),
        "workExperience-5--roleDescription": ("workExperience-5", "roleDescription"),
    }
    handler._entry_order = ["workExperience-5"]
    handler._values = {}
    handler._roles = [
        Fact(
            kind="role",
            text="Help desk",
            detail={"title": "Technician", "start": "2004", "end": "2006"},
        ),
        Fact(
            kind="role",
            text="Field teams",
            detail={"title": "Sr. Manager", "start": "04/2018", "end": "current"},
        ),
    ]
    plan = FillPlan(
        fills=[Fill(key="workExperience-5--roleDescription", value="Help desk", source="model")],
        needed=[
            NeededInput(
                key="workExperience-5--jobTitle", label="Job Title", kind="text", required=True
            )
        ],
    )
    handler._add_history(fields, plan)
    assert {f.key: f.value for f in plan.fills} == {
        "workExperience-5--jobTitle": "Sr. Manager",
        "workExperience-5--roleDescription": "Field teams",
    }
    assert plan.needed == []


@pytest.mark.usefixtures("page")
def test_a_saved_draft_is_continued_not_started_again(page, packet):
    result = run(page, packet, variant="draft")
    assert result.outcome == "dry_run", result.error
    assert js(page, "__start") is None, "Continue Application has no start choice"
    assert js(page, "__steps")[0] == "info"


@pytest.mark.usefixtures("page")
def test_apply_manually_uploads_the_resume_under_my_experience(page, packet):
    result = run(page, packet, variant="manual")
    assert result.outcome == "dry_run", result.error
    assert js(page, "__start") == "manual"
    assert js(page, "__answers")["experienceResume"] == "mark-shield.pdf"
    assert js(page, "__uploads") == 1


@pytest.mark.usefixtures("page")
def test_the_job_search_box_in_the_header_is_never_touched(page, packet):
    result = run(page, packet)
    assert all("job-search" not in f.selector for f in result.fields)
    assert page.input_value("#job-search") == ""


# ---------------------------------------------------------------- submit --


@pytest.mark.usefixtures("page")
def test_auto_submits_and_reads_the_confirmation(page, packet, tmp_path):
    result = run(page, packet, submit=True, shot=str(tmp_path / "sent.png"))
    assert result.outcome == "submitted", result.error
    assert js(page, "__submitted") is True
    assert "Application Submitted" in result.confirmation


@pytest.mark.usefixtures("page")
def test_submit_with_no_confirmation_is_unconfirmed(page, packet):
    result = run(page, packet, submit=True, variant="unconfirmed")
    assert js(page, "__submitted") is True
    assert result.outcome == "unconfirmed"


# ----------------------------------------------------------------- stops --


@pytest.mark.usefixtures("page")
def test_a_sign_in_page_stops_and_names_the_login_command(page, packet):
    packet.job["apply_url"] = None
    result = run(page, packet, submit=True, variant="signed_out")
    assert result.outcome == "blocked"
    assert "jobagent login workday" in result.error
    assert "own account" in result.error
    assert page.input_value("#si-password") == "", "a password is never typed"
    assert js(page, "__submitted") is None


@pytest.mark.usefixtures("page")
def test_a_sign_in_page_names_the_company_to_sign_in_to_again(page, packet):
    result = run(page, packet, variant="signed_out")
    assert result.outcome == "blocked"
    assert result.sign_in == "workday", "the fixture is a file, with no host to name"
    handler = WorkdayHandler()
    handler._posting = POSTING
    assert handler.sign_in_target() == "acme.wd5.myworkdayjobs.com"


@pytest.mark.usefixtures("page")
@pytest.mark.parametrize(
    ("variant", "state"),
    [
        ("", "signed_in"),
        ("draft", "signed_in"),
        ("signed_out", "signed_out"),
        ("closed", "unknown"),
    ],
)
def test_the_sign_in_check_opens_the_posting_and_reads_what_comes_up(page, variant, state):
    handler = WorkdayHandler()
    handler.settle_ms = 500
    handler.pause_ms = 100
    handler.step_timeout_ms = 5_000
    handler.poll_ms = 50
    assert handler.check_session(page, fixture_url(variant)) == state
    if variant == "":
        assert js(page, "__start") == "manual", "the check uploads nothing"
    assert js(page, "__submitted") is None


@pytest.mark.usefixtures("page")
def test_the_sign_in_check_gives_up_on_time(page):
    """Live, a lapsed sign-in's check ran past five minutes."""
    import time

    handler = WorkdayHandler()
    handler.poll_ms = 50
    began = time.monotonic()
    assert handler.check_session(page, fixture_url("stuck"), timeout_s=4) == "timed_out"
    assert time.monotonic() - began < 12
    assert [stage for stage, _ in handler.timings] == [
        "open the posting",
        "press Apply and Apply Manually",
    ]


def test_the_login_hint_names_the_companys_site():
    handler = WorkdayHandler()
    handler._posting = POSTING
    hint = handler.login_hint()
    assert "acme.wd5.myworkdayjobs.com" in hint
    assert f"jobagent login workday {POSTING}" in hint


@pytest.mark.usefixtures("page")
def test_a_question_nobody_can_answer_stops_before_submit(page, packet):
    result = run(page, packet, submit=True, variant="needs_input")
    assert result.outcome == "needs_input"
    asked = [n for n in result.needed if n.required]
    assert len(asked) == 1 and "network engineering teams" in asked[0].label
    assert js(page, "__submitted") is None


@pytest.mark.usefixtures("page")
def test_the_same_question_answered_goes_through(page, packet):
    key = question_key("How many years have you managed network engineering teams?")
    packet.answers[key] = "6"
    result = run(page, packet, submit=True, variant="needs_input")
    assert result.outcome == "submitted", result.error
    assert js(page, "__answers")["questions"]["teams"] == "6"


@pytest.mark.usefixtures("page")
def test_a_step_that_will_not_save_stops_with_its_message(page, packet):
    result = run(page, packet, submit=True, variant="invalid")
    assert result.outcome == "blocked"
    assert "could not be saved" in result.error
    assert js(page, "__submitted") is None


@pytest.mark.usefixtures("page")
def test_a_closed_posting_says_so(page, packet):
    result = run(page, packet, submit=True, variant="closed")
    assert result.outcome == "blocked"
    assert result.error == "the Workday posting is closed"


def test_the_loading_page_between_steps_is_not_the_next_step():
    from jobagent.apply.handlers.workday import _arrived

    before = "applyFlowAutoFillPage|current step 1 of 7 Autofill with Resume"
    loading = "applyFlowLoadingPage|current step 2 of 7 My Information"
    assert not _arrived(loading, before)
    assert not _arrived("", before)
    assert not _arrived(before, before)
    assert _arrived("applyFlowMyInfoPage|current step 2 of 7 My Information", before)


def test_a_moved_progress_bar_over_the_old_page_is_not_the_next_step():
    from jobagent.apply.handlers.workday import _arrived

    before = "applyFlowMyExpPage|current step 2 of 6 My Experience"
    moved = "applyFlowMyExpPage|current step 3 of 6 Application Questions 1 of 2"
    assert not _arrived(moved, before)
    assert _arrived(moved, before, seen_loading=True)

    questions = "applyFlowPrimaryQuestionsPage|current step 3 of 6 Application Questions 1 of 2"
    second = "applyFlowPrimaryQuestionsPage|current step 4 of 6 Application Questions 2 of 2"
    assert not _arrived(second, questions)
    assert _arrived(second, questions, seen_loading=True)

    blank = "|current step 3 of 6 Application Questions 1 of 2"
    assert not _arrived(blank, before)
    assert not _arrived(blank, before, seen_loading=True)
