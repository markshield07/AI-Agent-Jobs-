"""A scheduled cycle runs every step in order, and one failing step never stops the rest."""

from __future__ import annotations

import pytest

from jobagent import main
from jobagent.cycle import STEPS, StepResult, run_cycle, run_forever


def _ok(name, calls):
    def run(conn, settings, limits):
        calls.append((name, dict(limits)))
        return StepResult(name, True, f"{name} done")

    return run


def test_every_step_runs_in_order_with_the_limits(conn, settings):
    calls: list = []
    report = run_cycle(
        conn,
        settings,
        tailor_limit=3,
        apply_limit=4,
        runners={s: _ok(s, calls) for s in STEPS},
    )
    assert [c[0] for c in calls] == ["discover", "tailor", "apply", "inbox"]
    assert calls[0][1] == {"tailor": 3, "apply": 4}
    assert report.ok and report.finished_at
    assert [s["summary"] for s in report.as_dict()["steps"]][0] == "discover done"


def test_a_failing_step_is_recorded_and_the_rest_still_run(conn, settings):
    calls: list = []

    def broken(conn, settings, limits):
        raise RuntimeError("board down")

    runners = {s: _ok(s, calls) for s in STEPS} | {"discover": broken}
    report = run_cycle(conn, settings, runners=runners)
    assert not report.ok
    assert report.steps[0].summary == "RuntimeError: board down"
    assert [c[0] for c in calls] == ["tailor", "apply", "inbox"]


def test_skipped_steps_do_not_run(conn, settings):
    calls: list = []
    run_cycle(conn, settings, skip=("apply", "inbox"), runners={s: _ok(s, calls) for s in STEPS})
    assert [c[0] for c in calls] == ["discover", "tailor"]


def test_the_real_steps_on_an_empty_database(conn, settings):
    # Nothing queued, no mailbox: tailor and apply have nothing to do, the
    # inbox is skipped, and none of it needs a network or a browser.
    report = run_cycle(conn, settings, skip=("discover",))
    by_step = {s.step: s for s in report.steps}
    assert by_step["tailor"].ok and by_step["tailor"].summary.startswith("0 ready")
    assert by_step["apply"].ok
    assert by_step["inbox"].summary == "no mailbox set up; skipped"


def test_run_forever_pauses_with_jitter_between_cycles():
    slept: list[float] = []
    ran = run_forever(lambda: None, 2, sleep=slept.append, cycles=3)
    assert ran == 3 and len(slept) == 2
    assert all(0.9 * 7200 <= s <= 1.1 * 7200 for s in slept)


def test_cli_run_prints_each_step(monkeypatch, settings, capsys):
    from jobagent.cycle import CycleReport

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    seen = {}

    def fake(conn, settings_, **kw):
        seen.update(kw)
        return CycleReport(
            started_at="now",
            finished_at="later",
            steps=[
                StepResult("discover", True, "3 new"),
                StepResult("inbox", False, "login failed"),
            ],
        )

    monkeypatch.setattr("jobagent.cycle.run_cycle", fake)
    with pytest.raises(SystemExit) as exit_:
        main.run(["run", "--skip", "apply", "--apply-limit", "2"])
    assert exit_.value.code == 1
    out = capsys.readouterr().out
    assert "discover: 3 new" in out and "inbox: FAILED login failed" in out
    assert "mode dry_run" in out
    assert seen == {"skip": ("apply",), "tailor_limit": 10, "apply_limit": 2}

    with pytest.raises(SystemExit):
        main.run(["run", "--every", "0.1"])
    assert "at least 0.25 hours" in capsys.readouterr().err
