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
