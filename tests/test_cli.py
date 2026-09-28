from __future__ import annotations

import json

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
