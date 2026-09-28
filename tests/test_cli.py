from __future__ import annotations

import json
import time

import pytest

from jobagent import main
from jobagent.discovery.criteria import load_criteria
from jobagent.discovery.pipeline import RunReport


@pytest.fixture
def cli_settings(settings, monkeypatch):
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    return settings


def test_default_command_is_serve():
    args = main.build_parser().parse_args([])
    assert args.command is None
    args = main.build_parser().parse_args(["serve", "--port", "9000"])
    assert args.command == "serve" and args.port == 9000


def test_criteria_set_and_show(cli_settings, db, capsys):
    main.run(
        [
            "criteria",
            "--title",
            "Software Engineer",
            "--title",
            "Backend Engineer",
            "--board",
            "greenhouse:stripe",
            "--board",
            "lever:netflix:Netflix",
            "--site",
            "Indeed",
            "--salary-min",
            "150000",
            "--no-remote",
        ]
    )
    shown = json.loads(capsys.readouterr().out)
    assert shown["titles"] == ["Software Engineer", "Backend Engineer"]
    assert shown["boards"][1] == {"ats": "lever", "slug": "netflix", "company": "Netflix"}
    assert shown["jobspy_sites"] == ["indeed"]
    assert shown["salary_min"] == 150000 and shown["remote_ok"] is False

    saved = load_criteria(db.connection())
    assert saved.titles == ["Software Engineer", "Backend Engineer"]

    main.run(["criteria", "--min-score", "70"])
    shown = json.loads(capsys.readouterr().out)
    assert shown["min_score"] == 70 and shown["titles"] == ["Software Engineer", "Backend Engineer"]


def test_criteria_rejects_bad_values(cli_settings, capsys):
    with pytest.raises(SystemExit):
        main.run(["criteria", "--min-score", "500"])
    with pytest.raises(SystemExit):
        main.run(["criteria", "--board", "nonsense"])


def test_discover_prints_summary(cli_settings, monkeypatch, capsys):
    def fake_run(conn, settings, **kwargs):
        report = RunReport(run_id=7, found=4, new=4, scored=4, passed_rules=2, queued=1, skipped=3)
        report.notes.append("nothing to see")
        report.source_errors["lever"] = "HTTPError: 500"
        return report

    monkeypatch.setattr("jobagent.discovery.pipeline.run_discovery", fake_run)
    main.run(["discover", "--no-classify"])
    out = capsys.readouterr().out
    assert out.startswith("Run 7: found 4 (4 new, 0 seen before)")
    assert "lever failed: HTTPError: 500" in out and "note: nothing to see" in out

    main.run(["discover", "--json"])
    assert json.loads(capsys.readouterr().out)["queued"] == 1


def test_tailor_prints_one_line_per_job(cli_settings, db, monkeypatch, capsys):
    from jobagent.discovery import store as jobs
    from jobagent.discovery.models import RawJob
    from jobagent.tailor import store as variants
    from jobagent.tailor.models import Issue, TailoredResume, Variant

    conn = db.connection()
    ids = jobs.upsert_jobs(
        conn,
        [
            RawJob(url="https://a.example/1", title="A", company="A", source="indeed"),
            RawJob(url="https://b.example/2", title="B", company="B", source="indeed"),
            RawJob(url="https://c.example/3", title="C", company="C", source="indeed"),
        ],
    ).new_ids
    for jid in ids[:2]:
        jobs.set_status(conn, jid, "queued")
    variants.save_variant(conn, Variant(job_id=ids[0], base_id=None, content=TailoredResume()))

    def fake(conn, job_id, settings, **kwargs):
        if job_id == ids[1]:
            return Variant(
                job_id=job_id,
                base_id=None,
                content=TailoredResume(),
                keyword_coverage=0.5,
                base_coverage=0.25,
                pdf_path="/tmp/v.pdf",
            )
        return Variant(
            job_id=job_id,
            base_id=None,
            content=TailoredResume(),
            status="rejected",
            attempts=2,
            issues=[Issue(where="summary", message="bad", term="go")],
        )

    monkeypatch.setattr("jobagent.tailor.pipeline.tailor_job", fake)

    main.run(["tailor", "--queued"])  # only the queued job with no ready variant
    out = capsys.readouterr().out
    assert f"{ids[1]}: ready, keywords 50% (uploaded resume 25%)" in out
    assert ids[0] not in out and ids[2] not in out

    main.run(["tailor", ids[2]])  # a rejected variant is a result, not a failure
    out = capsys.readouterr().out
    assert f"{ids[2]}: rejected after 2 attempts" in out and "summary: bad [go]" in out

    with pytest.raises(SystemExit):
        main.run(["tailor"])


# ------------------------------------------------------------ submission --


def _applied_job(conn, url="https://jobs.lever.co/acme/1", title="Engineer", company="Acme"):
    from jobagent.discovery import store as jobs
    from jobagent.discovery.models import RawJob

    raw = RawJob(url=url, title=title, company=company, source="lever", ats_type="lever")
    jid = jobs.upsert_jobs(conn, [raw]).new_ids[0]
    jobs.set_status(conn, jid, "queued")
    return jid


def test_apply_needs_something_to_apply_to(cli_settings, capsys):
    with pytest.raises(SystemExit):
        main.run(["apply"])
    assert "pass job ids or use --queued" in capsys.readouterr().err


def test_apply_prints_a_line_per_job(cli_settings, db, monkeypatch, capsys):
    from jobagent.apply.pipeline import ApplyReport

    seen = {}

    def fake(conn, settings, **kwargs):
        seen.update(kwargs)
        report = ApplyReport(run_id=1, mode=kwargs["mode"] or "dry_run", considered=2, dry_run=1)
        report.needs_input = 1
        report.results = [
            {
                "job_id": "j1",
                "application_id": 7,
                "outcome": "dry_run",
                "handler": "lever",
                "filled": 12,
                "screenshot_path": "/tmp/j1-1.png",
                "needed": [],
            },
            {
                "job_id": "j2",
                "application_id": 8,
                "outcome": "needs_input",
                "handler": "greenhouse",
                "filled": 9,
                "needed": [
                    {
                        "label": "Are you authorized to work?",
                        "answer_key": "work_authorization",
                        "required": True,
                    },
                    {"label": "Anything else?", "answer_key": "q:anything", "required": False},
                ],
            },
        ]
        report.notes = ["no model for open questions: no key"]
        return report

    monkeypatch.setattr("jobagent.apply.pipeline.run_apply", fake)

    main.run(["apply", "--queued", "--limit", "5"])
    out = capsys.readouterr().out
    assert "mode dry_run, considered 2, dry run 1, needs input 1" in out
    assert "j1: dry_run via lever, 12 fields filled, application 7, /tmp/j1-1.png" in out
    assert "ask: Are you authorized to work? [work_authorization]" in out
    assert "Anything else?" not in out, "only what must be answered is printed"
    assert "note: no model for open questions: no key" in out
    assert seen["limit"] == 5 and seen["mode"] is None and seen["job_ids"] is None


def test_apply_submit_and_headed_and_json(cli_settings, db, monkeypatch, capsys):
    from jobagent.apply.pipeline import ApplyReport

    seen = {}

    def fake(conn, settings, **kwargs):
        seen.update(kwargs, headless=settings.headless)
        return ApplyReport(run_id=1, mode="auto", considered=1, submitted=1)

    monkeypatch.setattr("jobagent.apply.pipeline.run_apply", fake)

    main.run(["apply", "j1", "--submit", "--headed", "--no-generic", "--json"])
    out = capsys.readouterr().out
    assert json.loads(out)["submitted"] == 1
    assert seen["mode"] == "auto" and seen["job_ids"] == ["j1"]
    assert seen["allow_generic"] is False and seen["headless"] is False


def test_apply_exits_non_zero_when_a_job_failed(cli_settings, db, monkeypatch):
    from jobagent.apply.pipeline import ApplyReport

    report = ApplyReport(run_id=1, mode="dry_run", considered=1, failed=1)
    monkeypatch.setattr("jobagent.apply.pipeline.run_apply", lambda *a, **k: report)
    with pytest.raises(SystemExit):
        main.run(["apply", "j1"])


def test_applications_lists_what_each_one_waits_on(cli_settings, db, capsys):
    from jobagent.apply import store
    from jobagent.apply.models import HandlerResult, NeededInput
    from jobagent.db.database import utcnow

    conn = db.connection()
    job_id = _applied_job(conn)
    app_id = store.get_or_create_application(conn, job_id, mode="dry_run", ats="lever")
    store.record_attempt(
        conn,
        app_id,
        HandlerResult(
            outcome="needs_input",
            needed=[
                NeededInput(
                    key="q1",
                    label="Authorized to work?",
                    kind="select",
                    required=True,
                    answer_key="work_authorization",
                )
            ],
        ),
        mode="dry_run",
        handler="lever",
        started_at=utcnow(),
    )

    main.run(["applications"])
    out = capsys.readouterr().out
    assert f"{app_id}: needs_input  Engineer at Acme" in out
    assert "ask: Authorized to work? [work_authorization]" in out

    main.run(["applications", "--status", "applied"])
    assert "No applications yet." in capsys.readouterr().out

    main.run(["applications", "--json"])
    assert json.loads(capsys.readouterr().out)[0]["id"] == app_id


def test_answer_stores_answers_and_tries_again(cli_settings, db, monkeypatch, capsys):
    from jobagent.apply.models import NeededInput

    stored = {}

    def fake_answer(conn, application_id, answers):
        stored.update(answers)
        return [
            NeededInput(
                key="q2", label="Start date?", kind="text", required=True, answer_key="start_date"
            )
        ]

    monkeypatch.setattr("jobagent.apply.pipeline.answer_questions", fake_answer)
    monkeypatch.setattr(
        "jobagent.apply.pipeline.retry_application",
        lambda conn, application_id, settings, **kw: {
            "job_id": "j1",
            "application_id": application_id,
            "outcome": "dry_run",
            "handler": "lever",
            "filled": 11,
            "needed": [],
        },
    )

    main.run(["answer", "3", "work_authorization=Yes", "salary=$200k"])
    out = capsys.readouterr().out
    assert stored == {"work_authorization": "Yes", "salary": "$200k"}
    assert "still needed: Start date? [start_date]" in out
    assert "j1: dry_run via lever" in out

    main.run(["answer", "3", "work_authorization=Yes", "--no-retry"])
    assert "dry_run" not in capsys.readouterr().out

    with pytest.raises(SystemExit):
        main.run(["answer", "3", "not-a-pair"])
    assert "Expected KEY=VALUE" in capsys.readouterr().err


def test_answer_and_approve_report_what_went_wrong(cli_settings, db, monkeypatch, capsys):
    from jobagent.apply.pipeline import ApplyError

    def boom(*args, **kwargs):
        raise ApplyError("No application 3.")

    monkeypatch.setattr("jobagent.apply.pipeline.answer_questions", boom)
    monkeypatch.setattr("jobagent.apply.pipeline.approve_application", boom)

    with pytest.raises(SystemExit):
        main.run(["answer", "3", "a=b"])
    assert "No application 3." in capsys.readouterr().err

    with pytest.raises(SystemExit):
        main.run(["approve", "3"])
    assert "No application 3." in capsys.readouterr().err


def test_approve_prints_the_result(cli_settings, db, monkeypatch, capsys):
    monkeypatch.setattr(
        "jobagent.apply.pipeline.approve_application",
        lambda conn, application_id, settings, **kw: {
            "job_id": "j1",
            "application_id": application_id,
            "outcome": "submitted",
            "handler": "lever",
            "confirmation": "Application submitted",
            "needed": [],
        },
    )
    main.run(["approve", "3"])
    assert "j1: submitted via lever: Application submitted" in capsys.readouterr().out


# ------------------------------------------------------------------ inbox --


@pytest.fixture
def fake_mailbox(monkeypatch):
    """A mailbox holding the fixture mail, in place of a real IMAP server."""
    from pathlib import Path

    from jobagent.inbox import poller
    from jobagent.inbox.mailbox import parse_message

    emails = Path(__file__).parent / "fixtures" / "emails"

    class FakeMailbox:
        name = "fake"

        def __init__(self, *names):
            self.messages = [parse_message((emails / f"{n}.eml").read_bytes()) for n in names]

        def fetch(self, *, since=None, limit=200):
            return list(self.messages)

    box = FakeMailbox("rejection", "newsletter")
    monkeypatch.setattr(poller, "open_mailbox", lambda settings: box)
    return box


@pytest.fixture
def an_application(conn):
    from jobagent.apply import store as applications
    from jobagent.discovery.models import RawJob
    from jobagent.discovery.store import upsert_jobs

    raw = RawJob(
        url="https://boards.greenhouse.io/acme/jobs/1",
        title="Senior Backend Engineer",
        company="Acme Robotics",
        source="greenhouse",
    )
    job_id = upsert_jobs(conn, [raw]).new_ids[0]
    app_id = applications.get_or_create_application(conn, job_id, mode="auto")
    conn.execute(
        "UPDATE applications SET submitted_at = '2026-09-20T12:00:00+00:00' WHERE id = ?",
        (app_id,),
    )
    return app_id


def test_inbox_reads_the_mailbox_and_says_what_it_did(
    cli_settings, an_application, fake_mailbox, capsys
):
    main.run(["inbox"])
    out = capsys.readouterr().out
    assert "2 messages, 1 matched, 1 unmatched, 1 statuses moved." in out
    assert f"application {an_application}" in out
    assert "reads as rejected" in out and "moved to rejected" in out
    assert "unmatched, attach it with" in out


def test_inbox_dry_run_records_nothing(cli_settings, an_application, fake_mailbox, conn, capsys):
    from jobagent.inbox import store as inbox

    main.run(["inbox", "--dry-run"])
    assert "Nothing was recorded." in capsys.readouterr().out
    assert inbox.list_messages(conn) == []


def test_inbox_list_shows_the_log(cli_settings, an_application, fake_mailbox, capsys):
    main.run(["inbox"])
    capsys.readouterr()
    main.run(["inbox", "--list"])
    out = capsys.readouterr().out
    assert "rejected" in out and "digest@jobsweekly.example" in out

    main.run(["inbox", "--list", "--unmatched", "--json"])
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]["application_id"] is None


def test_inbox_attach_files_a_message_and_moves_the_status(
    cli_settings, an_application, fake_mailbox, conn, capsys
):
    from jobagent.inbox import store as inbox

    main.run(["inbox"])
    capsys.readouterr()
    row_id = inbox.list_messages(conn, matched=False)[0]["id"]

    main.run(["inbox", "--attach", f"{row_id}={an_application}", "--status", "screening"])
    out = capsys.readouterr().out
    assert f"filed against application {an_application}" in out


def test_inbox_attach_wants_two_numbers(cli_settings, an_application, fake_mailbox, capsys):
    with pytest.raises(SystemExit) as exc:
        main.run(["inbox", "--attach", "twelve"])
    assert exc.value.code == 2
    assert "MESSAGE=APPLICATION" in capsys.readouterr().err


def test_inbox_without_a_mailbox_says_what_to_set(cli_settings, capsys):
    with pytest.raises(SystemExit) as exc:
        main.run(["inbox"])
    assert exc.value.code == 2
    assert "JOBAGENT_IMAP_HOST" in capsys.readouterr().err


def test_inbox_json_prints_the_whole_report(cli_settings, an_application, fake_mailbox, capsys):
    main.run(["inbox", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["fetched"] == 2 and report["matched"] == 1
    assert report["results"][0]["reading"]["label"] == "rejected"


# ----------------------------------------------------------------- login --


def test_login_saves_the_session_from_a_visible_browser(
    cli_settings, settings, monkeypatch, capsys
):
    from jobagent.apply import sessions

    seen = {}

    def fake_login(settings_, url, is_done, *, timeout_s):
        seen.update(url=url, timeout=timeout_s)
        cookies = [{"name": "li_at", "value": "x", "domain": ".linkedin.com", "expires": -1}]
        assert not is_done([]) and is_done(cookies)
        return {"cookies": cookies, "origins": []}

    monkeypatch.setattr("jobagent.apply.browser.session.interactive_login", fake_login)
    main.run(["login", "linkedin", "--timeout", "60"])
    out = capsys.readouterr().out
    assert seen == {"url": "https://www.linkedin.com/login", "timeout": 60}
    assert "Signed in to LinkedIn" in out and "password goes to LinkedIn only" in out
    assert sessions.session_status(settings, "linkedin")["signed_in"]

    main.run(["login", "linkedin", "--status"])
    assert "LinkedIn: signed in" in capsys.readouterr().out
    main.run(["login", "linkedin", "--forget"])
    assert "LinkedIn sign-in deleted." in capsys.readouterr().out
    main.run(["login", "linkedin", "--status"])
    assert "not signed in. Run: jobagent login linkedin" in capsys.readouterr().out


def test_login_workday_waits_for_enter_and_saves_that_company(
    cli_settings, settings, monkeypatch, capsys
):
    import io

    from jobagent.apply import sessions

    posting = "https://acme.wd5.myworkdayjobs.com/careers/job/Remote/Engineer_R1"
    seen = {}

    def fake_login(settings_, url, is_done, *, timeout_s, confirmed):
        seen.update(url=url, timeout=timeout_s)
        cookies = [{"name": "PLAY_SESSION", "value": "x", "domain": "acme.wd5.myworkdayjobs.com"}]
        assert not is_done(cookies), "Workday has no known sign-in cookie"
        for _ in range(200):
            if confirmed():
                break
            time.sleep(0.01)
        assert confirmed(), "Enter in the terminal ends the wait"
        return {"cookies": cookies, "origins": []}

    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    monkeypatch.setattr("jobagent.apply.browser.session.interactive_login", fake_login)
    main.run(["login", "workday", posting, "--timeout", "90", "--no-check"])
    out = capsys.readouterr().out
    assert seen == {"url": posting, "timeout": 90}
    assert "press Enter" in out and "password goes to Workday only" in out
    assert sessions.workday_hosts(settings) == ["acme.wd5.myworkdayjobs.com"]

    main.run(["login", "workday", "--status", "--no-check"])
    out = capsys.readouterr().out
    assert "Workday acme.wd5.myworkdayjobs.com: saved" in out and "not checked since" in out
    main.run(["login", "workday", posting, "--forget"])
    assert "deleted" in capsys.readouterr().out
    assert sessions.workday_hosts(settings) == []


def test_login_workday_status_opens_the_site_to_see_if_the_sign_in_works(
    cli_settings, settings, monkeypatch, capsys
):
    """Cookies said "11 live" while CrowdStrike had ended the session: the
    status comes from opening the posting."""
    from contextlib import contextmanager

    from jobagent.apply import sessions
    from jobagent.apply.handlers.workday import WorkdayHandler

    host = "acme.wd5.myworkdayjobs.com"
    posting = f"https://{host}/careers/job/Remote/Engineer_R1"
    cookie = {"name": "PLAY_SESSION", "value": "x", "domain": host, "path": "/", "expires": -1}
    sessions.save_workday_session(settings, host, {"cookies": [cookie]}, url=posting)
    sessions.mark_workday_signed_out(settings, host, job_id="j1")
    sessions.save_workday_session(settings, host, {"cookies": [cookie]})

    class Browser:
        @contextmanager
        def new_page(self):
            yield object()

    @contextmanager
    def fake_open(settings_):
        yield Browser()

    checked = []
    monkeypatch.setattr("jobagent.apply.browser.session.open_browser", fake_open)
    monkeypatch.setattr(WorkdayHandler, "keep_alive", lambda self, page, url, **kw: "unknown")
    monkeypatch.setattr(
        WorkdayHandler,
        "check_session",
        lambda self, page, url, **kw: checked.append(url) or "signed_out",
    )
    main.run(["login", "workday", "--status"])
    out = capsys.readouterr().out
    assert checked == [posting], "the posting when Candidate Home cannot tell"
    assert f"Workday {host}: the sign-in has ended; sign in again: jobagent login workday" in out
    assert "1 application(s) waiting on it" in out


def test_login_workday_status_asks_candidate_home_before_the_posting(
    cli_settings, settings, monkeypatch, capsys
):
    from contextlib import contextmanager

    from jobagent.apply import sessions
    from jobagent.apply.handlers.workday import WorkdayHandler

    host = "acme.wd5.myworkdayjobs.com"
    posting = f"https://{host}/careers/job/Remote/Engineer_R1"  # applied to, or closed
    cookie = {"name": "PLAY_SESSION", "value": "x", "domain": host, "path": "/", "expires": -1}
    sessions.save_workday_session(settings, host, {"cookies": [cookie]}, url=posting)

    class Browser:
        @contextmanager
        def new_page(self):
            yield object()

    @contextmanager
    def fake_open(settings_):
        yield Browser()

    postings = []
    monkeypatch.setattr("jobagent.apply.browser.session.open_browser", fake_open)
    monkeypatch.setattr(WorkdayHandler, "keep_alive", lambda self, page, url, **kw: "signed_in")
    monkeypatch.setattr(
        WorkdayHandler, "check_session", lambda self, page, url, **kw: postings.append(url)
    )
    main.run(["login", "workday", "--status"])
    out = capsys.readouterr().out
    assert postings == [], "Candidate Home answered; the posting is not opened"
    assert "could not tell" not in out
    assert sessions.workday_status(settings, host)["state"] == "signed_in"


def test_login_workday_status_says_when_the_check_ran_out_of_time(
    cli_settings, settings, monkeypatch, capsys
):
    from contextlib import contextmanager

    from jobagent.apply import sessions
    from jobagent.apply.handlers.workday import WorkdayHandler

    host = "acme.wd5.myworkdayjobs.com"
    posting = f"https://{host}/careers/job/Remote/Engineer_R1"
    cookie = {"name": "PLAY_SESSION", "value": "x", "domain": host, "path": "/", "expires": -1}
    sessions.save_workday_session(settings, host, {"cookies": [cookie]}, url=posting)

    class Browser:
        @contextmanager
        def new_page(self):
            yield object()

    @contextmanager
    def fake_open(settings_):
        yield Browser()

    given = []

    def stuck(self, page, url, *, timeout_s=None):
        given.append(timeout_s)
        self.timings = [("open the posting", 3.0), ("press Apply and Apply Manually", 42.0)]
        return "timed_out"

    monkeypatch.setattr("jobagent.apply.browser.session.open_browser", fake_open)
    monkeypatch.setattr(WorkdayHandler, "keep_alive", stuck)
    monkeypatch.setattr(WorkdayHandler, "check_session", stuck)
    main.run(["login", "workday", posting, "--status", "--check-timeout", "45"])
    out = capsys.readouterr().out
    assert given == [45, 45]
    assert "within 45s (open the posting 3s, press Apply and Apply Manually 42s)" in out
    assert sessions.workday_status(settings, host)["state"] == "unchecked"


def test_login_workday_runs_the_applications_waiting_on_it(
    cli_settings, settings, monkeypatch, capsys
):
    import io

    from jobagent.apply import sessions
    from jobagent.apply.pipeline import ApplyReport

    host = "acme.wd5.myworkdayjobs.com"
    posting = f"https://{host}/careers/job/Remote/Engineer_R1"
    sessions.mark_workday_signed_out(settings, host, job_id="j1", url=posting)
    sessions.mark_workday_signed_out(settings, host, job_id="j2")

    def fake_login(settings_, url, is_done, *, timeout_s, confirmed):
        return {"cookies": [{"name": "PLAY_SESSION", "value": "x", "domain": host}]}

    ran = {}

    def fake_run_apply(conn, settings_, *, job_ids, limit):
        ran.update(job_ids=job_ids, limit=limit)
        return ApplyReport(run_id=1, mode="dry_run", considered=2)

    checks = []
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    monkeypatch.setattr("jobagent.apply.browser.session.interactive_login", fake_login)
    monkeypatch.setattr("jobagent.apply.pipeline.run_apply", fake_run_apply)
    monkeypatch.setattr(
        main, "_check_workday", lambda s, hosts, **kw: checks.append(hosts) or {host: "signed_in"}
    )
    main.run(["login", "workday", posting])
    out = capsys.readouterr().out
    assert checks == [[host]], "the new sign-in is tried before anything runs on it"
    assert f"Signed in to {host}: it works." in out
    assert ran == {"job_ids": ["j1", "j2"], "limit": 2}
    assert "Running the 2 application(s) that were waiting on it now." in out
    assert "Leave this window open" in out

    ran.clear()
    main.run(["login", "workday", posting, "--no-retry"])
    assert ran == {}
    assert "jobagent apply j1 j2" in capsys.readouterr().out


def test_a_workday_sign_in_that_did_not_finish_says_so_and_runs_nothing(
    cli_settings, settings, monkeypatch, capsys
):
    import io

    from jobagent.apply import sessions

    host = "acme.wd5.myworkdayjobs.com"
    posting = f"https://{host}/careers/job/Remote/Engineer_R1"
    sessions.mark_workday_signed_out(settings, host, job_id="j1", url=posting)

    def early_enter(settings_, url, is_done, *, timeout_s, confirmed):
        return {"cookies": [{"name": "PLAY_SESSION", "value": "x", "domain": host}]}

    ran = []
    monkeypatch.setattr("sys.stdin", io.StringIO("\n"))
    monkeypatch.setattr("jobagent.apply.browser.session.interactive_login", early_enter)
    monkeypatch.setattr("jobagent.apply.pipeline.run_apply", lambda *a, **k: ran.append(a))
    monkeypatch.setattr(main, "_check_workday", lambda s, hosts, **kw: {host: "signed_out"})
    with pytest.raises(SystemExit) as exc:
        main.run(["login", "workday", posting])
    assert exc.value.code == 2
    assert "NOT SIGNED IN" in capsys.readouterr().err
    assert ran == []
    assert sessions.workday_waiting(settings, host) == ["j1"]


def test_a_workday_sign_in_that_ran_out_of_time_says_nothing_was_saved(
    cli_settings, settings, monkeypatch, capsys
):
    import io

    from jobagent.apply import sessions

    host = "acme.wd5.myworkdayjobs.com"
    posting = f"https://{host}/careers/job/Remote/Engineer_R1"
    given = []

    def too_slow(settings_, url, is_done, *, timeout_s, confirmed):
        given.append(timeout_s)
        raise TimeoutError(f"not signed in after {timeout_s} seconds")

    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    monkeypatch.setattr("jobagent.apply.browser.session.interactive_login", too_slow)
    with pytest.raises(SystemExit) as exc:
        main.run(["login", "workday", posting])
    assert exc.value.code == 2
    assert given == [900], "a new Workday account's email check takes a while"
    err = capsys.readouterr().err
    assert f"NOT SAVED: nothing was kept for {host}" in err
    assert not sessions.workday_status(settings, host)["saved"]


def test_jobs_run_after_a_workday_sign_in_stop_waiting_unless_it_ended_again(
    settings, monkeypatch, capsys
):
    from jobagent.apply import sessions
    from jobagent.apply.pipeline import ApplyReport

    host = "acme.wd5.myworkdayjobs.com"
    for job in ("j1", "j2", "j3"):
        sessions.mark_workday_signed_out(settings, host, job_id=job)

    def fake_run_apply(conn, settings_, *, job_ids, limit):
        report = ApplyReport(run_id=1, mode="dry_run", considered=2)
        report.results = [
            {"job_id": "j1", "outcome": "dry_run", "application_id": 1, "filled": 28},
            {"job_id": "j2", "outcome": "skipped", "reason": "waiting", "sign_in": host},
        ]
        return report

    monkeypatch.setattr("jobagent.apply.pipeline.run_apply", fake_run_apply)
    main._run_waiting(settings, ["j1", "j2"], host=host)
    assert sessions.workday_waiting(settings, host) == ["j2", "j3"]


def test_login_workday_needs_a_workday_posting(cli_settings, capsys):
    with pytest.raises(SystemExit) as exc:
        main.run(["login", "workday"])
    assert exc.value.code == 2 and "posting's link" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        main.run(["login", "workday", "https://www.linkedin.com/jobs/view/1/"])
    assert exc.value.code == 2 and "not a Workday posting" in capsys.readouterr().err


def test_login_that_times_out_saves_nothing(cli_settings, settings, monkeypatch, capsys):
    from jobagent.apply import sessions

    def fake_login(*a, **k):
        raise TimeoutError("not signed in after 300 seconds")

    monkeypatch.setattr("jobagent.apply.browser.session.interactive_login", fake_login)
    with pytest.raises(SystemExit) as exit_:
        main.run(["login", "indeed"])
    assert exit_.value.code == 2
    assert "not signed in after 300 seconds" in capsys.readouterr().err
    assert not sessions.session_path(settings, "indeed").exists()


def test_keep_alive_visits_each_signed_in_company_and_keeps_what_it_left(
    settings, monkeypatch, capsys
):
    from contextlib import contextmanager

    from jobagent.apply import sessions
    from jobagent.apply.handlers.workday import WorkdayHandler

    def cookie(host, value):
        return {"name": "PLAY_SESSION", "value": value, "domain": host, "path": "/", "expires": -1}

    live, gone, lapsed = (f"{c}.wd5.myworkdayjobs.com" for c in ("live", "gone", "lapsed"))
    for host in (live, gone, lapsed):
        posting = f"https://{host}/careers/job/Remote/Engineer_R1"
        sessions.save_workday_session(
            settings, host, {"cookies": [cookie(host, "old")]}, url=posting
        )
    sessions.mark_workday_signed_out(settings, lapsed)

    class Context:
        def cookies(self):
            return [cookie(live, "fresh")]

    class Page:
        context = Context()

    class Browser:
        @contextmanager
        def new_page(self):
            yield Page()

    @contextmanager
    def fake_open(settings_):
        yield Browser()

    visited = []

    def fake_keep_alive(self, page, url, **kw):
        visited.append(url)
        return "signed_in" if live in url else "signed_out"

    monkeypatch.setattr("jobagent.apply.browser.session.open_browser", fake_open)
    monkeypatch.setattr(WorkdayHandler, "keep_alive", fake_keep_alive)
    assert main._keep_workday_alive(settings, every_min=30, once=True) == 0
    out = capsys.readouterr().out
    assert len(visited) == 2, "a company whose sign-in has ended is left alone"
    assert f"Workday {live}: still signed in; cookies refreshed (candidate home" in out
    assert f"Workday {gone}: the sign-in has ended" in out
    assert f"Workday {lapsed}: needs a new sign-in" in out
    assert sessions.load_workday_session(settings, live)[0]["value"] == "fresh"
    assert sessions.workday_status(settings, live)["state"] == "signed_in"
    assert sessions.workday_status(settings, gone)["state"] == "needs_sign_in"
    assert "keep-alive round 1: 3 companies with a saved sign-in" in out
    # Each visit is stamped, so --status shows the keep-alive is at work.
    assert sessions.workday_status(settings, live)["keep_alive"] == "signed_in"
    assert sessions.workday_status(settings, gone)["keep_alive"] == "signed_out"
    assert sessions.workday_status(settings, lapsed)["kept_alive_at"] is None


def test_keep_alive_checks_the_posting_when_candidate_home_says_nothing(
    settings, monkeypatch, capsys
):
    from contextlib import contextmanager

    from jobagent.apply import sessions
    from jobagent.apply.handlers.workday import WorkdayHandler

    def cookie(host, value):
        return {"name": "PLAY_SESSION", "value": value, "domain": host, "path": "/", "expires": -1}

    odd, broken, fine = (f"{c}.wd5.myworkdayjobs.com" for c in ("odd", "broken", "fine"))
    for host in (odd, broken, fine):
        sessions.save_workday_session(
            settings,
            host,
            {"cookies": [cookie(host, "old")]},
            url=f"https://{host}/careers/job/Remote/Engineer_R1",
        )

    class Page:
        url = ""

        class context:  # noqa: N801
            @staticmethod
            def cookies():
                return [cookie(odd, "fresh")]

        def content(self):
            return "<html><body>A page nobody expected</body></html>"

    class Browser:
        @contextmanager
        def new_page(self):
            yield Page()

    @contextmanager
    def fake_open(settings_):
        yield Browser()

    def fake_keep_alive(self, page, url, **kw):
        if broken in url:
            raise RuntimeError("page crashed")
        return "unknown" if odd in url else "signed_in"

    checked = []

    def fake_check(self, page, url, **kw):
        checked.append(url)
        return "signed_in"

    monkeypatch.setattr("jobagent.apply.browser.session.open_browser", fake_open)
    monkeypatch.setattr(WorkdayHandler, "keep_alive", fake_keep_alive)
    monkeypatch.setattr(WorkdayHandler, "check_session", fake_check)
    assert main._keep_workday_alive(settings, every_min=30, once=True) == 0
    out = capsys.readouterr().out
    assert checked == [f"https://{odd}/careers/job/Remote/Engineer_R1"]
    assert f"Workday {odd}: still signed in; cookies refreshed (candidate home, then" in out
    assert sessions.load_workday_session(settings, odd)[0]["value"] == "fresh"
    # A failure at one company is recorded, and the next company still gets its visit.
    assert f"Workday {broken}: the visit failed: RuntimeError: page crashed" in out
    assert sessions.workday_status(settings, broken)["keep_alive"] == "error"
    assert sessions.workday_status(settings, fine)["keep_alive"] == "signed_in"
    # Candidate Home is kept only when it did not settle the question.
    assert not list(sessions.workday_dir(settings).glob("*-home.html"))


def test_keep_alive_keeps_what_an_unreadable_candidate_home_showed(settings, monkeypatch, capsys):
    import stat
    from contextlib import contextmanager

    from jobagent.apply import sessions
    from jobagent.apply.handlers.workday import WorkdayHandler

    host = "odd.wd5.myworkdayjobs.com"
    sessions.save_workday_session(
        settings,
        host,
        {"cookies": [{"name": "s", "value": "v", "domain": host, "path": "/", "expires": -1}]},
        url=f"https://{host}/careers/job/Remote/Engineer_R1",
    )

    class Page:
        url = f"https://{host}/careers/userHome"

        def content(self):
            return "<html><body>A page nobody expected</body></html>"

    class Browser:
        @contextmanager
        def new_page(self):
            yield Page()

    @contextmanager
    def fake_open(settings_):
        yield Browser()

    monkeypatch.setattr("jobagent.apply.browser.session.open_browser", fake_open)
    monkeypatch.setattr(WorkdayHandler, "keep_alive", lambda self, page, url, **kw: "unknown")
    monkeypatch.setattr(WorkdayHandler, "check_session", lambda self, page, url, **kw: "unknown")
    assert main._keep_workday_alive(settings, every_min=30, once=True) == 0
    out = capsys.readouterr().out
    saved = sessions.workday_dir(settings) / f"{host}-home.html"
    assert "A page nobody expected" in saved.read_text()
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert "could not tell (unknown)" in out and str(saved) in out
    status = sessions.workday_status(settings, host)
    assert status["keep_alive"] == "unknown" and status["state"] == "unchecked"
    assert "keep-alive last visited" in main._workday_line(status)
