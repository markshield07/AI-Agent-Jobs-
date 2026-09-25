"""The apply run: job + ready resume -> packet -> handler -> attempt on record.

No browser and no model here. A fake handler stands in for the form and a
fake browser hands out token pages, so these tests cover the orchestration:
modes, the daily cap, the pause between submissions, skips, follow-ups.
"""

from __future__ import annotations

import dataclasses
from contextlib import contextmanager
from pathlib import Path

import pytest

from jobagent.answers import list_answers, set_answers
from jobagent.apply import store
from jobagent.apply.browser.session import BrowserUnavailable
from jobagent.apply.models import Fill, HandlerResult, NeededInput
from jobagent.apply.pipeline import (
    ApplyError,
    ApplyReport,
    answer_questions,
    apply_to_job,
    approve_application,
    build_packet,
    retry_application,
    run_apply,
)
from jobagent.discovery import store as jobs
from jobagent.discovery.models import RawJob
from jobagent.resume.facts import Fact, add_fact, add_never_claim
from jobagent.tailor import store as variants
from jobagent.tailor.models import CoverLetter, TailoredResume, Variant

FAKE_HOST = "jobs.fake.example"


class FakeBrowser:
    def __init__(self) -> None:
        self.pages = 0

    @contextmanager
    def new_page(self):
        self.pages += 1
        yield {"page": self.pages}


class NeverCalled:
    """A completer that must not be reached: the fake handler never asks."""

    def complete(self, **kwargs):  # pragma: no cover - reaching it is the failure
        raise AssertionError("the model was called")


def _result(outcome: str, **kw) -> HandlerResult:
    return HandlerResult(outcome=outcome, **kw)


def _fills(n: int) -> list[Fill]:
    return [Fill(key=f"f{i}", value=f"v{i}", source="contact", label=f"F{i}") for i in range(n)]


class FakeHandler:
    """Returns canned results in order, the last one repeating; records each call."""

    def __init__(self, *results: HandlerResult, ats="fake", hosts=(FAKE_HOST,), error=None):
        self.ats = ats
        self.hosts = hosts
        self.results = list(results) or [_result("dry_run", filled=_fills(3))]
        self.error = error
        self.calls: list[dict] = []

    def matches(self, url: str) -> bool:
        return any(host in url for host in self.hosts)

    def apply(self, page, packet, answerer, *, submit, screenshot_path=None):
        self.calls.append(
            {"page": page, "packet": packet, "submit": submit, "screenshot": screenshot_path}
        )
        if self.error is not None:
            raise self.error
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        return dataclasses.replace(result, screenshot_path=screenshot_path)


@pytest.fixture
def ready_job(conn, tmp_path):
    """A queued job whose tailored resume PDF exists on disk."""

    def make(
        url: str = f"https://{FAKE_HOST}/acme/1",
        *,
        title="Engineer",
        company="Acme",
        status="queued",
        letter=True,
        pdf=True,
        pdf_exists=True,
    ) -> str:
        raw = RawJob(url=url, title=title, company=company, source="greenhouse")
        jid = jobs.upsert_jobs(conn, [raw]).new_ids[0]
        jobs.set_status(conn, jid, status)
        pdf_path = tmp_path / f"{jid}.pdf"
        if pdf_exists:
            pdf_path.write_bytes(b"%PDF-1.4 fake")
        variants.save_variant(
            conn,
            Variant(
                job_id=jid,
                base_id=None,
                content=TailoredResume(),
                cover_letter=CoverLetter(paragraphs=["Hello", "Bye"]) if letter else None,
                pdf_path=str(pdf_path) if pdf else None,
            ),
        )
        return jid

    return make


@pytest.fixture
def contact(conn):
    set_answers(
        conn,
        {
            "full_name": "Mark Shield",
            "email": "mark@example.com",
            "phone": "+1 555 0100",
            "linkedin": "https://www.linkedin.com/in/markshield",
        },
    )


def _apply(conn, jid, settings, handler, **kw):
    kw.setdefault("completer", NeverCalled())
    kw.setdefault("browser", FakeBrowser())
    return apply_to_job(conn, jid, settings, handlers=[handler], **kw)


# --------------------------------------------------------------- one job --


def test_dry_run_fills_the_form_and_stops_at_the_button(conn, settings, ready_job, contact):
    jid = ready_job()
    handler = FakeHandler()
    browser = FakeBrowser()

    record = _apply(conn, jid, settings, handler, browser=browser)

    assert record["outcome"] == "dry_run" and record["handler"] == "fake"
    assert record["filled"] == 3 and record["error"] is None and record["needed"] == []
    assert browser.pages == 1
    call = handler.calls[0]
    assert call["submit"] is False and call["page"] == {"page": 1}
    assert call["screenshot"] == str(settings.screenshots_dir / f"{jid}-1.png")
    packet = call["packet"]
    assert packet.contact["full_name"] == "Mark Shield" and packet.job["id"] == jid
    assert Path(packet.resume_path).is_file()
    assert packet.cover_letter == "Hello\n\nBye" and Path(packet.cover_letter_path).is_file()

    app = store.get_application(conn, record["application_id"])
    assert app["status"] == "dry_run" and app["submitted_at"] is None
    assert app["mode"] == "dry_run" and app["ats"] == "fake"
    assert app["screenshot_path"] == call["screenshot"]
    assert app["cover_letter_path"] == packet.cover_letter_path
    assert jobs.get_job(conn, jid)["status"] == "queued", "a dry run leaves the job queued"
    attempts = store.list_attempts(conn, record["application_id"])
    assert len(attempts) == 1 and attempts[0]["id"] == record["attempt_id"]
    assert attempts[0]["outcome"] == "dry_run" and attempts[0]["handler"] == "fake"


def test_review_mode_parks_the_filled_form_for_approval(conn, settings, ready_job):
    jid = ready_job()
    record = _apply(conn, jid, settings, FakeHandler(), mode="review")
    assert record["outcome"] == "review"
    assert store.get_application(conn, record["application_id"])["status"] == "review"
    assert jobs.get_job(conn, jid)["status"] == "queued"


def test_auto_mode_submits_once_and_marks_the_job_applied(conn, settings, ready_job):
    jid = ready_job()
    handler = FakeHandler(
        _result("submitted", filled=_fills(4), confirmation="Thanks for applying!")
    )

    record = _apply(conn, jid, settings, handler, mode="auto")

    assert record["outcome"] == "submitted" and record["confirmation"] == "Thanks for applying!"
    assert handler.calls[0]["submit"] is True
    app = store.get_application(conn, record["application_id"])
    assert app["status"] == "applied" and app["submitted_at"]
    assert jobs.get_job(conn, jid)["status"] == "applied"
    assert [e["to_status"] for e in store.list_events(conn, app["id"])] == ["applied"]

    again = _apply(conn, jid, settings, handler, mode="auto")
    assert again["outcome"] == "skipped" and again["reason"] == "already applied"
    assert again["application_id"] == app["id"] and len(handler.calls) == 1


def test_mode_comes_from_settings_unless_given(conn, settings, ready_job):
    jid = ready_job()
    handler = FakeHandler(_result("submitted"))
    auto = settings.model_copy(update={"apply_mode": "auto"})
    assert _apply(conn, jid, auto, handler)["outcome"] == "submitted"
    assert handler.calls[0]["submit"] is True
    with pytest.raises(ApplyError, match="unknown mode"):
        _apply(conn, jid, settings, handler, mode="yolo")


def test_a_submission_in_a_mode_that_does_not_submit_is_noted(conn, settings, ready_job):
    jid = ready_job()
    record = _apply(conn, jid, settings, FakeHandler(_result("submitted")), mode="dry_run")
    assert record["outcome"] == "submitted"
    assert any("does not submit" in note for note in record["notes"])
    assert jobs.get_job(conn, jid)["status"] == "applied", "what happened is what is recorded"


def test_a_question_nobody_can_answer_parks_the_job(conn, settings, ready_job):
    jid = ready_job()
    asked = NeededInput(
        key="q_auth",
        label="Are you legally authorized to work in the US?",
        kind="select",
        required=True,
        options=["Yes", "No"],
        reason="never guessed",
        answer_key="work_authorization",
    )
    handler = FakeHandler(_result("needs_input", filled=_fills(2), needed=[asked]))

    record = _apply(conn, jid, settings, handler)

    assert record["outcome"] == "needs_input" and record["needed"] == [asked.as_dict()]
    app_id = record["application_id"]
    assert store.get_application(conn, app_id)["status"] == "needs_input"
    assert jobs.get_job(conn, jid)["status"] == "needs_review"
    assert store.needed_for(conn, app_id) == [asked]


def test_blocked_unconfirmed_and_failed_outcomes_route_the_job(conn, settings, ready_job):
    expected = {"blocked": "needs_review", "unconfirmed": "needs_review", "failed": "failed"}
    for outcome, job_status in expected.items():
        jid = ready_job(f"https://{FAKE_HOST}/acme/{outcome}")
        record = _apply(conn, jid, settings, FakeHandler(_result(outcome, error="x")))
        assert record["outcome"] == outcome and record["error"] == "x"
        assert jobs.get_job(conn, jid)["status"] == job_status
        assert store.get_application(conn, record["application_id"])["status"] == outcome


def test_a_handler_crash_is_a_failed_attempt_not_a_dead_run(conn, settings, ready_job):
    jid = ready_job()
    record = _apply(conn, jid, settings, FakeHandler(error=RuntimeError("boom")))
    assert record["outcome"] == "failed" and record["error"] == "RuntimeError: boom"
    assert jobs.get_job(conn, jid)["status"] == "failed"
    attempt = store.get_attempt(conn, record["attempt_id"])
    assert attempt["outcome"] == "failed" and attempt["error"] == "RuntimeError: boom"


def test_a_missing_browser_is_an_error_not_a_failed_job(conn, settings, ready_job, monkeypatch):
    jid = ready_job()

    @contextmanager
    def no_browser(_settings):
        raise BrowserUnavailable("no chromium here")
        yield  # pragma: no cover

    monkeypatch.setattr("jobagent.apply.pipeline.open_browser", no_browser)
    with pytest.raises(ApplyError, match="no chromium here"):
        apply_to_job(conn, jid, settings, handlers=[FakeHandler()], completer=NeverCalled())
    assert jobs.get_job(conn, jid)["status"] == "queued"
    assert store.list_attempts(conn, store.application_for_job(conn, jid)["id"]) == []


def test_skips_and_refusals(conn, settings, ready_job):
    handler = FakeHandler()
    with pytest.raises(ApplyError, match="No job nope"):
        _apply(conn, "nope", settings, handler)

    raw = RawJob(url=f"https://{FAKE_HOST}/acme/bare", title="T", company="C", source="lever")
    bare = jobs.upsert_jobs(conn, [raw]).new_ids[0]
    assert "no ready resume" in _apply(conn, bare, settings, handler)["reason"]

    no_pdf = ready_job(f"https://{FAKE_HOST}/acme/nopdf", pdf=False)
    assert "no ready resume" in _apply(conn, no_pdf, settings, handler)["reason"]

    gone = ready_job(f"https://{FAKE_HOST}/acme/gone", pdf_exists=False)
    assert "no ready resume" in _apply(conn, gone, settings, handler)["reason"]

    elsewhere = ready_job("https://jobs.lever.co/acme/1")
    record = _apply(conn, elsewhere, settings, FakeHandler(hosts=("nowhere",)))
    assert record["outcome"] == "skipped" and record["reason"] == "no handler for lever"
    assert store.application_for_job(conn, elsewhere) is None, "a skip leaves no application"
    assert handler.calls == []


def test_the_url_picks_the_handler_and_the_generic_one_keeps_the_jobs_ats(
    conn, settings, ready_job
):
    jid = ready_job("https://jobs.lever.co/acme/1")
    generic = FakeHandler(ats="generic", hosts=())
    lever = FakeHandler(ats="lever", hosts=("lever.co",))

    record = _apply(conn, jid, settings, generic)
    assert record["handler"] == "generic"
    assert store.get_application(conn, record["application_id"])["ats"] == "lever"

    record = apply_to_job(
        conn,
        jid,
        settings,
        handlers=[generic, lever],
        completer=NeverCalled(),
        browser=FakeBrowser(),
    )
    assert record["handler"] == "lever" and len(lever.calls) == 1


def test_attempts_are_numbered_in_the_screenshot_name(conn, settings, ready_job):
    jid = ready_job()
    handler = FakeHandler()
    _apply(conn, jid, settings, handler)
    _apply(conn, jid, settings, handler)
    assert [c["screenshot"] for c in handler.calls] == [
        str(settings.screenshots_dir / f"{jid}-1.png"),
        str(settings.screenshots_dir / f"{jid}-2.png"),
    ]


# ---------------------------------------------------------------- packet --


def test_build_packet_gathers_contact_links_facts_and_the_letter(
    conn, settings, ready_job, contact
):
    jid = ready_job()
    set_answers(
        conn,
        {
            "links": "https://github.com/markshield, https://markshield.dev , ",
            "work_authorization": "Yes",
        },
    )
    add_fact(conn, Fact(kind="skill", text="Python"))
    add_never_claim(conn, "kubernetes")
    job = jobs.get_job(conn, jid)
    variant = variants.latest_ready_variant(conn, jid)

    packet = build_packet(conn, job, variant, settings)

    assert packet.contact == {
        "full_name": "Mark Shield",
        "email": "mark@example.com",
        "phone": "+1 555 0100",
    }
    assert packet.links == {
        "linkedin": "https://www.linkedin.com/in/markshield",
        "github": "https://github.com/markshield",
        "website": "https://markshield.dev",
    }
    assert packet.answers["work_authorization"] == "Yes"
    assert [f.text for f in packet.facts] == ["Python"]
    assert packet.never_claim == ["kubernetes"]
    assert packet.variant_id == variant.id and packet.resume_path == variant.pdf_path
    assert packet.cover_letter == "Hello\n\nBye"
    letter = Path(packet.cover_letter_path)
    assert letter == settings.variants_dir / f"{jid}-{variant.id}-letter.pdf"
    assert letter.read_bytes().startswith(b"%PDF")

    stamp = letter.stat().st_mtime_ns
    again = build_packet(conn, job, variant, settings)
    assert again.cover_letter_path == str(letter) and letter.stat().st_mtime_ns == stamp


def test_build_packet_without_a_letter(conn, settings, ready_job):
    jid = ready_job(letter=False)
    packet = build_packet(
        conn, jobs.get_job(conn, jid), variants.latest_ready_variant(conn, jid), settings
    )
    assert packet.cover_letter is None and packet.cover_letter_path is None
    assert packet.contact == {} and packet.links == {}


# ------------------------------------------------------------------- run --


def test_run_apply_takes_queued_jobs_with_a_ready_resume(conn, settings, ready_job):
    ready = [ready_job(f"https://{FAKE_HOST}/acme/{i}") for i in range(3)]
    ready_job(f"https://{FAKE_HOST}/acme/new", status="pending")  # not queued
    raw = RawJob(url=f"https://{FAKE_HOST}/acme/bare", title="T", company="C", source="lever")
    bare = jobs.upsert_jobs(conn, [raw]).new_ids[0]
    jobs.set_status(conn, bare, "queued")  # queued, but no resume yet
    handler = FakeHandler()
    browser = FakeBrowser()

    report = run_apply(conn, settings, handlers=[handler], completer=NeverCalled(), browser=browser)

    assert report.mode == "dry_run" and report.considered == 3
    assert report.attempted == 3 and report.dry_run == 3 and report.submitted == 0
    assert {r["job_id"] for r in report.results} == set(ready)
    assert browser.pages == 3 and all(not c["submit"] for c in handler.calls)
    assert report.summary() == "mode dry_run, considered 3, dry run 3"
    run = jobs.list_runs(conn)[0]
    assert run["id"] == report.run_id and run["ended_at"] and run["applied"] == 0
    assert report.as_dict()["results"] == report.results


def test_run_apply_with_ids_dedupes_and_tallies_skips(conn, settings, ready_job):
    a = ready_job(f"https://{FAKE_HOST}/acme/a")
    b = ready_job("https://jobs.lever.co/acme/b")  # no handler for it
    handler = FakeHandler(_result("failed", error="x"))

    report = run_apply(
        conn,
        settings,
        job_ids=[a, a, b],
        handlers=[handler],
        completer=NeverCalled(),
        browser=FakeBrowser(),
    )

    assert report.considered == 2 and report.attempted == 1
    assert report.failed == 1 and report.skipped == 1
    assert report.summary() == "mode dry_run, considered 2, failed 1, skipped 1"
    assert jobs.list_runs(conn)[0]["failed"] == 1


def test_run_apply_with_nothing_to_do(conn, settings):
    report = run_apply(conn, settings, handlers=[FakeHandler()], completer=NeverCalled())
    assert report.considered == 0 and report.run_id is None
    assert report.notes == ["nothing to apply to: no queued job has a ready resume"]


def test_run_apply_honours_the_limit(conn, settings, ready_job):
    for i in range(4):
        ready_job(f"https://{FAKE_HOST}/acme/{i}")
    report = run_apply(
        conn,
        settings,
        limit=2,
        handlers=[FakeHandler()],
        completer=NeverCalled(),
        browser=FakeBrowser(),
    )
    assert report.considered == 2 and report.attempted == 2


def test_auto_run_pauses_between_jobs_and_stops_at_the_daily_cap(conn, settings, ready_job):
    for i in range(3):
        ready_job(f"https://{FAKE_HOST}/acme/{i}")
    capped = settings.model_copy(update={"daily_apply_cap": 2, "apply_delay_seconds": 10.0})
    handler = FakeHandler(_result("submitted", confirmation="ok"))
    slept: list[float] = []

    report = run_apply(
        conn,
        capped,
        mode="auto",
        handlers=[handler],
        completer=NeverCalled(),
        browser=FakeBrowser(),
        sleep=slept.append,
    )

    assert report.submitted == 2 and report.attempted == 2 and report.cap_hit is True
    assert report.notes == ["daily cap of 2 reached; 1 left for tomorrow"]
    assert "daily cap reached" in report.summary()
    assert len(slept) == 2 and all(7.0 <= s <= 13.0 for s in slept), "jittered around 10s"
    assert store.submitted_last_day(conn) == 2
    assert jobs.list_runs(conn)[0]["applied"] == 2


def test_earlier_submissions_today_count_toward_the_cap(conn, settings, ready_job):
    first = ready_job(f"https://{FAKE_HOST}/acme/first")
    handler = FakeHandler(_result("submitted"))
    _apply(conn, first, settings, handler, mode="auto")
    ready_job(f"https://{FAKE_HOST}/acme/second")
    capped = settings.model_copy(update={"daily_apply_cap": 1})

    report = run_apply(
        conn,
        capped,
        mode="auto",
        handlers=[handler],
        completer=NeverCalled(),
        browser=FakeBrowser(),
    )

    assert report.considered == 1 and report.attempted == 0 and report.cap_hit
    assert len(handler.calls) == 1


def test_dry_runs_never_pause_and_ignore_the_cap(conn, settings, ready_job):
    for i in range(3):
        ready_job(f"https://{FAKE_HOST}/acme/{i}")
    capped = settings.model_copy(update={"daily_apply_cap": 1, "apply_delay_seconds": 10.0})
    slept: list[float] = []
    report = run_apply(
        conn,
        capped,
        handlers=[FakeHandler()],
        completer=NeverCalled(),
        browser=FakeBrowser(),
        sleep=slept.append,
    )
    assert report.dry_run == 3 and not report.cap_hit and slept == []


def test_no_delay_means_no_sleep(conn, settings, ready_job):
    for i in range(2):
        ready_job(f"https://{FAKE_HOST}/acme/{i}")
    slept: list[float] = []
    run_apply(
        conn,
        settings,
        mode="auto",
        delay=0,
        handlers=[FakeHandler(_result("submitted"))],
        completer=NeverCalled(),
        browser=FakeBrowser(),
        sleep=slept.append,
    )
    assert slept == []


def test_run_without_a_browser_is_a_note(conn, settings, ready_job, monkeypatch):
    jid = ready_job()

    @contextmanager
    def no_browser(_settings):
        raise BrowserUnavailable("Chromium could not start.")
        yield  # pragma: no cover

    monkeypatch.setattr("jobagent.apply.pipeline.open_browser", no_browser)
    report = run_apply(conn, settings, handlers=[FakeHandler()], completer=NeverCalled())
    assert report.considered == 1 and report.attempted == 0
    assert report.notes == ["Chromium could not start."]
    assert jobs.list_runs(conn)[0]["ended_at"], "the run row is closed either way"
    assert jobs.get_job(conn, jid)["status"] == "queued"


def test_no_model_is_a_note_not_a_stop(conn, settings, ready_job, monkeypatch):
    from jobagent.llm.backend import LLMUnavailable

    ready_job()

    def unavailable(_settings):
        raise LLMUnavailable("no key, no cli")

    monkeypatch.setattr("jobagent.apply.pipeline.resolve_backend", unavailable)
    report = run_apply(conn, settings, handlers=[FakeHandler()], browser=FakeBrowser())
    assert report.dry_run == 1
    assert report.notes == ["no model for open questions: no key, no cli"]

    quiet = settings.model_copy(update={"apply_model_answers": False})
    report = run_apply(conn, quiet, handlers=[FakeHandler()], browser=FakeBrowser())
    assert report.notes == [], "with model answers off, no model is looked for"


def test_report_tokens_are_summed(conn, settings, ready_job):
    for i in range(2):
        ready_job(f"https://{FAKE_HOST}/acme/{i}")
    handler = FakeHandler(_result("dry_run", input_tokens=100, output_tokens=7))
    report = run_apply(
        conn, settings, handlers=[handler], completer=NeverCalled(), browser=FakeBrowser()
    )
    assert (report.input_tokens, report.output_tokens) == (200, 14)
    assert report.summary().endswith("tokens 200 in / 14 out")
    run = jobs.list_runs(conn)[0]
    assert (run["tokens_in"], run["tokens_out"]) == (200, 14)


def test_report_shape():
    report = ApplyReport(run_id=None, mode="auto")
    assert report.as_dict()["results"] == [] and report.summary() == "mode auto, considered 0"


# ------------------------------------------------------------ follow-ups --


def _park(conn, settings, ready_job, *questions: NeededInput) -> tuple[str, int, FakeHandler]:
    jid = ready_job()
    handler = FakeHandler(
        _result("needs_input", needed=list(questions)), _result("dry_run", filled=_fills(5))
    )
    record = _apply(conn, jid, settings, handler)
    return jid, record["application_id"], handler


AUTH = NeededInput(
    key="q_auth",
    label="Authorized to work?",
    kind="select",
    required=True,
    options=["Yes", "No"],
    answer_key="work_authorization",
)
SALARY = NeededInput(
    key="q_salary", label="Desired salary", kind="text", required=False, answer_key="salary"
)


def test_answers_go_to_the_bank_under_the_answer_key(conn, settings, ready_job):
    _, app_id, _ = _park(conn, settings, ready_job, AUTH, SALARY)

    remaining = answer_questions(conn, app_id, {"work_authorization": "Yes", "q_salary": "  "})

    assert remaining == [SALARY], "an empty value answers nothing"
    bank = {row["key"]: row for row in list_answers(conn)}
    assert bank["work_authorization"]["value"] == "Yes"
    pattern = conn.execute(
        "SELECT question_pattern FROM answers WHERE key = 'work_authorization'"
    ).fetchone()["question_pattern"]
    assert pattern == "Authorized to work?", "the label is kept, so the next form matches it"
    assert "salary" not in bank and "q_salary" not in bank

    assert answer_questions(conn, app_id, {"q_salary": "$150k", "extra": "kept"}) == []
    bank = {row["key"]: row["value"] for row in list_answers(conn)}
    assert bank["salary"] == "$150k" and bank["extra"] == "kept", "form keys map; unknown keys stay"


def test_answer_questions_needs_an_application(conn):
    with pytest.raises(ApplyError, match="No application 99"):
        answer_questions(conn, 99, {"a": "b"})


def test_retry_fills_the_form_again_in_the_applications_own_mode(conn, settings, ready_job):
    jid, app_id, handler = _park(conn, settings, ready_job, AUTH)
    answer_questions(conn, app_id, {"work_authorization": "Yes"})

    record = retry_application(
        conn, app_id, settings, handlers=[handler], completer=NeverCalled(), browser=FakeBrowser()
    )

    assert record["outcome"] == "dry_run" and record["application_id"] == app_id
    assert record["attempt_id"] != store.list_attempts(conn, app_id)[0]["id"]
    assert handler.calls[1]["submit"] is False
    assert handler.calls[1]["packet"].answers["work_authorization"] == "Yes"
    assert jobs.get_job(conn, jid)["status"] == "queued"
    assert store.get_application(conn, app_id)["status"] == "dry_run"
    assert store.needed_for(conn, app_id) == []

    with pytest.raises(ApplyError, match="No application 99"):
        retry_application(conn, 99, settings)


def test_approve_submits_what_a_dry_run_left_at_the_button(conn, settings, ready_job):
    jid = ready_job()
    handler = FakeHandler(_result("dry_run", filled=_fills(2)), _result("submitted"))
    app_id = _apply(conn, jid, settings, handler)["application_id"]

    record = approve_application(
        conn, app_id, settings, handlers=[handler], completer=NeverCalled(), browser=FakeBrowser()
    )

    assert record["outcome"] == "submitted" and handler.calls[1]["submit"] is True
    app = store.get_application(conn, app_id)
    assert app["status"] == "applied" and app["mode"] == "auto"
    assert jobs.get_job(conn, jid)["status"] == "applied"

    with pytest.raises(ApplyError, match="already submitted"):
        approve_application(conn, app_id, settings, handlers=[handler])
    with pytest.raises(ApplyError, match="No application 99"):
        approve_application(conn, 99, settings)
    assert len(handler.calls) == 2


def test_linkedin_and_indeed_each_have_their_own_lower_cap(conn, settings, ready_job):
    capped = settings.model_copy(update={"easy_apply_daily_cap": 1})
    linkedin = FakeHandler(_result("submitted", confirmation="sent"), ats="linkedin")
    first = ready_job(f"https://{FAKE_HOST}/acme/first")
    second = ready_job(f"https://{FAKE_HOST}/acme/second")
    third = ready_job(f"https://{FAKE_HOST}/acme/third")

    assert _apply(conn, first, capped, linkedin, mode="auto")["outcome"] == "submitted"
    record = _apply(conn, second, capped, linkedin, mode="auto")
    assert record["outcome"] == "skipped"
    assert record["reason"] == "LinkedIn cap of 1 applications a day reached; it goes out tomorrow"
    assert len(linkedin.calls) == 1

    # A dry run is not a submission, and another site's count is its own.
    filling = FakeHandler(ats="linkedin")
    assert _apply(conn, second, capped, filling, mode="dry_run")["outcome"] == "dry_run"
    indeed = FakeHandler(_result("submitted", confirmation="sent"), ats="indeed")
    assert _apply(conn, third, capped, indeed, mode="auto")["outcome"] == "submitted"
    assert store.submitted_last_day(conn, ats="linkedin") == 1
    assert store.submitted_last_day(conn, ats="indeed") == 1
    assert store.submitted_last_day(conn) == 2
