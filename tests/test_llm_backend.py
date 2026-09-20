"""The model seam: CLI envelope parsing, the fake `claude` binary, the API fake, selection."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from pydantic import BaseModel, ValidationError

from jobagent.config import Settings
from jobagent.llm.backend import (
    AnthropicBackend,
    ClaudeCodeBackend,
    Completion,
    LLMError,
    LLMUnavailable,
    _parse_cli_output,
    resolve_backend,
)


class Verdict(BaseModel):
    tier: int
    reason: str


GOOD_ENVELOPE = {
    "type": "result",
    "is_error": False,
    "result": "Tier 2.",
    "structured_output": {"tier": 2, "reason": "solid match"},
    "usage": {"input_tokens": 120, "output_tokens": 15},
    "total_cost_usd": 0.0042,
}


def _proc(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=["claude"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ------------------------------------------------------- _parse_cli_output --


def test_parse_cli_output_reads_structured_output_usage_and_cost():
    completion = _parse_cli_output(_proc(json.dumps(GOOD_ENVELOPE)), Verdict, "claude-code")

    assert isinstance(completion, Completion)
    assert completion.result == Verdict(tier=2, reason="solid match")
    assert completion.input_tokens == 120
    assert completion.output_tokens == 15
    assert completion.cost_usd == pytest.approx(0.0042)
    assert completion.backend == "claude-code"


def test_parse_cli_output_falls_back_to_json_in_result():
    envelope = {"type": "result", "result": json.dumps({"tier": 3, "reason": "meh"})}
    completion = _parse_cli_output(_proc(json.dumps(envelope)), Verdict, "x")
    assert completion.result == Verdict(tier=3, reason="meh")
    assert (completion.input_tokens, completion.output_tokens) == (0, 0)
    assert completion.cost_usd is None


def test_parse_cli_output_prefers_structured_output_over_result_text():
    envelope = {**GOOD_ENVELOPE, "result": json.dumps({"tier": 9, "reason": "stale"})}
    assert _parse_cli_output(_proc(json.dumps(envelope)), Verdict, "x").result.tier == 2


def test_parse_cli_output_tolerates_padding_and_a_nonzero_exit_with_output():
    stdout = "\n" + json.dumps(GOOD_ENVELOPE) + "\n\n"
    assert _parse_cli_output(_proc(stdout, returncode=1), Verdict, "x").result.tier == 2


def test_parse_cli_output_reports_is_error():
    envelope = {"type": "result", "is_error": True, "result": "Not logged in"}
    with pytest.raises(LLMError, match="reported an error: Not logged in"):
        _parse_cli_output(_proc(json.dumps(envelope)), Verdict, "x")


def test_parse_cli_output_rejects_output_that_does_not_match_the_schema():
    envelope = {**GOOD_ENVELOPE, "structured_output": {"tier": "two"}}
    with pytest.raises(LLMError, match="did not match Verdict"):
        _parse_cli_output(_proc(json.dumps(envelope)), Verdict, "x")


def test_parse_cli_output_rejects_result_text_that_is_not_json():
    envelope = {"type": "result", "result": "I think it is tier 2."}
    with pytest.raises(LLMError, match="no structured output"):
        _parse_cli_output(_proc(json.dumps(envelope)), Verdict, "x")


def test_parse_cli_output_rejects_an_empty_result():
    with pytest.raises(LLMError, match="no structured output"):
        _parse_cli_output(_proc(json.dumps({"type": "result"})), Verdict, "x")


@pytest.mark.parametrize("stdout", ["not json at all", "{'single': 'quotes'}", "[1, 2]", "null"])
def test_parse_cli_output_rejects_non_envelope_stdout(stdout):
    with pytest.raises(LLMError):
        _parse_cli_output(_proc(stdout), Verdict, "x")


def test_parse_cli_output_reports_a_failed_exit_with_stderr():
    with pytest.raises(LLMError, match=r"exited 2: boom"):
        _parse_cli_output(_proc("", returncode=2, stderr="boom\n"), Verdict, "x")


# --------------------------------------------------------- ClaudeCodeBackend --


@pytest.fixture
def fake_claude(tmp_path: Path) -> tuple[Path, Path]:
    """A stand-in `claude` that records how it was called and prints a canned envelope."""
    record = tmp_path / "record"
    record.mkdir()
    (record / "envelope.json").write_text(json.dumps(GOOD_ENVELOPE))
    script = tmp_path / "claude"
    script.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\0" "$0" "$@" > "{record}/argv"\n'
        f'env > "{record}/env"\n'
        f'pwd > "{record}/cwd"\n'
        f'cat > "{record}/stdin"\n'
        f'cat "{record}/envelope.json"\n'
    )
    script.chmod(0o755)
    return script, record


def _argv(record: Path) -> list[str]:
    return (record / "argv").read_bytes().decode().split("\0")[:-1]


def _option(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1]


def test_claude_code_backend_runs_the_cli_in_print_mode(fake_claude, tmp_path, monkeypatch):
    script, record = fake_claude
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("JOBAGENT_MARKER", "present")
    workdir = tmp_path / "wd" / "claude-code"

    backend = ClaudeCodeBackend(executable=str(script), model="claude-opus-5", workdir=workdir)
    completion = backend.complete(
        system="Judge the posting.", prompt="Posting body", output=Verdict, effort="low"
    )

    assert completion.result == Verdict(tier=2, reason="solid match")
    assert (completion.input_tokens, completion.output_tokens) == (120, 15)
    assert completion.cost_usd == pytest.approx(0.0042)
    assert completion.backend == "claude-code"

    argv = _argv(record)
    assert "-p" in argv
    assert _option(argv, "--output-format") == "json"
    assert json.loads(_option(argv, "--json-schema")) == Verdict.model_json_schema()
    assert _option(argv, "--system-prompt") == "Judge the posting."
    assert _option(argv, "--permission-mode") == "dontAsk"
    assert _option(argv, "--model") == "claude-opus-5"
    assert _option(argv, "--effort") == "low"
    assert (record / "stdin").read_text() == "Posting body"

    env_lines = (record / "env").read_text().splitlines()
    assert not any(line.startswith("ANTHROPIC_API_KEY=") for line in env_lines)
    assert "JOBAGENT_MARKER=present" in env_lines, "the rest of the environment is passed on"

    assert workdir.is_dir()
    assert Path((record / "cwd").read_text().strip()).resolve() == workdir.resolve()


def test_claude_code_backend_omits_model_and_effort_when_unset(fake_claude):
    script, record = fake_claude
    ClaudeCodeBackend(executable=str(script)).complete(system="s", prompt="p", output=Verdict)

    argv = _argv(record)
    assert "--model" not in argv
    assert "--effort" not in argv
    assert argv[0] == str(script)


def test_claude_code_backend_surfaces_cli_errors(fake_claude):
    script, record = fake_claude
    (record / "envelope.json").write_text(
        json.dumps({"type": "result", "is_error": True, "result": "usage limit reached"})
    )
    with pytest.raises(LLMError, match="usage limit reached"):
        ClaudeCodeBackend(executable=str(script)).complete(system="s", prompt="p", output=Verdict)


def test_claude_code_backend_reports_a_missing_executable(tmp_path):
    backend = ClaudeCodeBackend(executable=str(tmp_path / "no-such-claude"))
    with pytest.raises(LLMUnavailable, match="was not found"):
        backend.complete(system="s", prompt="p", output=Verdict)


def test_claude_code_backend_times_out(tmp_path):
    script = tmp_path / "slow"
    script.write_text("#!/bin/sh\nsleep 5\n")
    script.chmod(0o755)
    backend = ClaudeCodeBackend(executable=str(script), timeout_seconds=0.2)
    with pytest.raises(LLMError, match="did not answer within"):
        backend.complete(system="s", prompt="p", output=Verdict)


# ---------------------------------------------------------- resolve_backend --


@pytest.mark.parametrize(
    "mode, key, cli, expected",
    [
        ("auto", "sk-1", True, AnthropicBackend),
        ("auto", "sk-1", False, AnthropicBackend),
        ("auto", None, True, ClaudeCodeBackend),
        ("auto", None, False, LLMUnavailable),
        ("api", "sk-1", True, AnthropicBackend),
        ("api", "sk-1", False, AnthropicBackend),
        ("api", None, True, LLMUnavailable),
        ("api", None, False, LLMUnavailable),
        ("claude-code", "sk-1", True, ClaudeCodeBackend),
        ("claude-code", None, True, ClaudeCodeBackend),
        ("claude-code", "sk-1", False, LLMUnavailable),
        ("claude-code", None, False, LLMUnavailable),
    ],
)
def test_resolve_backend_selection_matrix(tmp_path, monkeypatch, mode, key, cli, expected):
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/claude" if cli else None)
    settings = Settings(data_dir=tmp_path, llm_backend=mode, anthropic_api_key=key)

    if expected is LLMUnavailable:
        with pytest.raises(LLMUnavailable):
            resolve_backend(settings)
        return

    backend = resolve_backend(settings)
    assert type(backend) is expected


def test_resolve_backend_configures_the_api_backend_from_settings(monkeypatch, tmp_path):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    settings = Settings(
        data_dir=tmp_path, llm_backend="api", anthropic_api_key="sk-2", model="claude-x"
    )
    backend = resolve_backend(settings)
    assert isinstance(backend, AnthropicBackend)
    assert backend.model == "claude-x"
    assert backend.api_key == "sk-2"
    assert backend.name == "api"


def test_resolve_backend_configures_claude_code_from_settings(monkeypatch, tmp_path):
    looked_up = []
    monkeypatch.setattr(shutil, "which", lambda name: looked_up.append(name) or "/opt/bin/cc")
    settings = Settings(
        data_dir=tmp_path,
        llm_backend="claude-code",
        anthropic_api_key=None,
        claude_code_executable="my-claude",
        claude_code_model="claude-y",
    )
    backend = resolve_backend(settings)
    assert isinstance(backend, ClaudeCodeBackend)
    assert looked_up == ["my-claude"]
    assert backend.executable == "my-claude"
    assert backend.model == "claude-y"
    assert backend.workdir == tmp_path / "claude-code"
    assert backend.name == "claude-code"


# --------------------------------------------------------- AnthropicBackend --


class _Recorder:
    def __init__(self) -> None:
        self.init_kwargs: list[dict] = []
        self.calls: list[dict] = []
        self.response = SimpleNamespace(
            parsed_output=Verdict(tier=1, reason="great"),
            usage=SimpleNamespace(input_tokens=200, output_tokens=40),
            stop_reason="end_turn",
        )
        self.raise_exc: Exception | None = None


@pytest.fixture
def fake_anthropic(monkeypatch) -> _Recorder:
    recorder = _Recorder()

    class Messages:
        def parse(self, **kwargs):
            recorder.calls.append(kwargs)
            if recorder.raise_exc is not None:
                raise recorder.raise_exc
            return recorder.response

    class FakeAnthropic:
        def __init__(self, **kwargs):
            recorder.init_kwargs.append(kwargs)
            self.messages = Messages()

    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    return recorder


def test_anthropic_backend_calls_parse_with_the_output_type(fake_anthropic):
    backend = AnthropicBackend(model="claude-x", api_key="sk-3")
    completion = backend.complete(system="Be brief.", prompt="Posting", output=Verdict)

    assert completion.result == Verdict(tier=1, reason="great")
    assert (completion.input_tokens, completion.output_tokens) == (200, 40)
    assert completion.cost_usd is None
    assert completion.backend == "api"

    assert fake_anthropic.init_kwargs == [{"api_key": "sk-3"}]
    (call,) = fake_anthropic.calls
    assert call["model"] == "claude-x"
    assert call["max_tokens"] == 16000
    assert call["output_format"] is Verdict
    assert call["messages"] == [{"role": "user", "content": "Posting"}]
    assert call["system"] == [{"type": "text", "text": "Be brief."}]
    assert call["thinking"] == {"type": "adaptive"}
    assert "output_config" not in call


def test_anthropic_backend_without_a_key_lets_the_sdk_find_one(fake_anthropic):
    AnthropicBackend(model="m").complete(system="s", prompt="p", output=Verdict)
    assert fake_anthropic.init_kwargs == [{}]


def test_anthropic_backend_passes_effort_and_max_tokens_through(fake_anthropic):
    AnthropicBackend(model="m").complete(
        system="s", prompt="p", output=Verdict, max_tokens=512, effort="high"
    )
    (call,) = fake_anthropic.calls
    assert call["output_config"] == {"effort": "high"}
    assert call["max_tokens"] == 512


def test_anthropic_backend_marks_the_system_block_cacheable_only_on_request(fake_anthropic):
    backend = AnthropicBackend(model="m")
    backend.complete(system="profile", prompt="p", output=Verdict, cache_system=True)
    backend.complete(system="profile", prompt="p", output=Verdict, cache_system=False)

    cached, plain = fake_anthropic.calls
    assert cached["system"] == [
        {"type": "text", "text": "profile", "cache_control": {"type": "ephemeral"}}
    ]
    assert plain["system"] == [{"type": "text", "text": "profile"}]


def test_anthropic_backend_treats_a_refusal_as_an_error(fake_anthropic):
    fake_anthropic.response.stop_reason = "refusal"
    with pytest.raises(LLMError, match="declined"):
        AnthropicBackend(model="m").complete(system="s", prompt="p", output=Verdict)


def test_anthropic_backend_treats_missing_parsed_output_as_an_error(fake_anthropic):
    fake_anthropic.response.parsed_output = None
    with pytest.raises(LLMError, match="no structured output"):
        AnthropicBackend(model="m").complete(system="s", prompt="p", output=Verdict)


def test_anthropic_backend_wraps_the_sdks_schema_validation_failure(fake_anthropic):
    # messages.parse validates the text block itself, so truncated or off-schema
    # JSON arrives as a pydantic error rather than an APIError.
    try:
        Verdict.model_validate({"tier": "two"})
    except ValidationError as exc:
        fake_anthropic.raise_exc = exc
    with pytest.raises(LLMError, match="did not match Verdict") as info:
        AnthropicBackend(model="m").complete(system="s", prompt="p", output=Verdict)
    assert isinstance(info.value.__cause__, ValidationError)


@pytest.mark.parametrize("parsed", [None, Verdict(tier=1, reason="cut o")])
def test_anthropic_backend_treats_running_out_of_tokens_as_an_error(fake_anthropic, parsed):
    fake_anthropic.response.stop_reason = "max_tokens"
    fake_anthropic.response.parsed_output = parsed
    with pytest.raises(LLMError, match=r"ran out of output tokens \(max_tokens=300\)"):
        AnthropicBackend(model="m").complete(system="s", prompt="p", output=Verdict, max_tokens=300)


def test_anthropic_backend_wraps_api_errors(fake_anthropic):
    fake_anthropic.raise_exc = anthropic.APIConnectionError(
        request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    )
    with pytest.raises(LLMError, match="Anthropic API call failed"):
        AnthropicBackend(model="m").complete(system="s", prompt="p", output=Verdict)


def test_anthropic_backend_tolerates_missing_usage(fake_anthropic):
    fake_anthropic.response.usage = None
    completion = AnthropicBackend(model="m").complete(system="s", prompt="p", output=Verdict)
    assert (completion.input_tokens, completion.output_tokens) == (0, 0)
