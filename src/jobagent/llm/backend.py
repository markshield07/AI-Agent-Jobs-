"""Model access behind one seam.

Two backends return the same thing, a validated Pydantic object plus token
usage, from a system prompt, a user prompt and an output type:

- `AnthropicBackend`: the API. Metered per token, needs ANTHROPIC_API_KEY,
  runs anywhere with no session.
- `ClaudeCodeBackend`: the `claude` command line in print mode, which runs on
  the user's Claude subscription login. No key and no per-token bill, subject
  to the subscription's usage limits, needs `claude` installed and logged in
  on the machine that runs the agent.

`resolve_backend` picks one from settings. Everything that calls a model goes
through `Completer.complete`; nothing else imports `anthropic` or shells out.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

if TYPE_CHECKING:
    from jobagent.config import Settings

T = TypeVar("T", bound=BaseModel)


class LLMError(RuntimeError):
    """The model could not be called, or did not return what was asked for."""


class LLMUnavailable(LLMError):
    """No backend is configured or reachable."""


@dataclass(slots=True)
class Completion:
    result: BaseModel
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    backend: str = ""


class Completer(Protocol):
    name: str

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        output: type[T],
        max_tokens: int = 16000,
        effort: str | None = None,
        cache_system: bool = False,
    ) -> Completion: ...


# --------------------------------------------------------------------- API --


@dataclass(slots=True)
class AnthropicBackend:
    model: str
    api_key: str | None = None
    name: str = "api"

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        output: type[T],
        max_tokens: int = 16000,
        effort: str | None = None,
        cache_system: bool = False,
    ) -> Completion:
        import anthropic

        client = (
            anthropic.Anthropic(api_key=self.api_key) if self.api_key else anthropic.Anthropic()
        )

        # A cached system block only pays off when the same prefix is reused across
        # many calls (the profile in scoring, the fact base in tailoring). Callers
        # opt in; a one-off prompt below the minimum cacheable size just would not
        # cache anyway.
        system_block: dict = {"type": "text", "text": system}
        if cache_system:
            system_block["cache_control"] = {"type": "ephemeral"}

        kwargs: dict = {}
        if effort:
            kwargs["output_config"] = {"effort": effort}

        try:
            response = client.messages.parse(
                model=self.model,
                max_tokens=max_tokens,
                thinking={"type": "adaptive"},
                system=[system_block],
                messages=[{"role": "user", "content": prompt}],
                output_format=output,
                **kwargs,
            )
        except anthropic.APIError as exc:
            raise LLMError(f"Anthropic API call failed: {exc}") from exc
        except ValidationError as exc:
            # The SDK validates the text block against `output` while building the
            # response, so a truncated or off-schema answer surfaces here, not as
            # an APIError. Same failure as the CLI backend's, same error type.
            raise LLMError(f"The model's output did not match {output.__name__}: {exc}") from exc

        if response.stop_reason == "refusal":
            raise LLMError("The model declined this request.")
        if response.stop_reason == "max_tokens":
            raise LLMError(f"The model ran out of output tokens (max_tokens={max_tokens}).")
        if response.parsed_output is None:
            raise LLMError("The model returned no structured output.")

        usage = response.usage
        return Completion(
            result=response.parsed_output,
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            backend=self.name,
        )


# ------------------------------------------------------------- Claude Code --


@dataclass(slots=True)
class ClaudeCodeBackend:
    executable: str = "claude"
    model: str | None = None
    workdir: Path | None = None
    timeout_seconds: float = 900.0
    name: str = "claude-code"

    def complete(
        self,
        *,
        system: str,
        prompt: str,
        output: type[T],
        max_tokens: int = 16000,
        effort: str | None = None,
        cache_system: bool = False,
    ) -> Completion:
        del max_tokens, cache_system  # the CLI manages both itself
        schema = output.model_json_schema()
        cmd = [
            self.executable,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
            "--system-prompt",
            system,
            "--permission-mode",
            "dontAsk",
        ]
        if self.model:
            cmd += ["--model", self.model]
        if effort:
            cmd += ["--effort", effort]

        # Strip the API key so a subscription-mode run cannot silently bill the API,
        # and run from an empty directory so no CLAUDE.md or hooks load.
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        workdir = self.workdir
        if workdir is not None:
            workdir.mkdir(parents=True, exist_ok=True)

        try:
            proc = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                cwd=workdir,
                env=env,
                check=False,
            )
        except FileNotFoundError as exc:
            raise LLMUnavailable(
                f"`{self.executable}` was not found. Install Claude Code and run "
                "`claude auth login`, or set ANTHROPIC_API_KEY to use the API instead."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise LLMError(
                f"`{self.executable}` did not answer within {self.timeout_seconds}s"
            ) from exc

        return _parse_cli_output(proc, output, self.name)


def _parse_cli_output(proc: subprocess.CompletedProcess, output: type[T], name: str) -> Completion:
    stdout = (proc.stdout or "").strip()
    if proc.returncode != 0 and not stdout:
        raise LLMError(f"`claude` exited {proc.returncode}: {(proc.stderr or '').strip()[:500]}")

    try:
        envelope = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise LLMError(f"`claude` returned something other than JSON: {stdout[:300]}") from exc
    if not isinstance(envelope, dict):
        raise LLMError(f"`claude` returned JSON that is not a result object: {stdout[:300]}")

    if envelope.get("is_error"):
        raise LLMError(f"`claude` reported an error: {str(envelope.get('result', ''))[:500]}")

    payload = envelope.get("structured_output")
    if payload is None:
        # Older versions put the JSON in `result` as text.
        try:
            payload = json.loads(envelope.get("result") or "")
        except (json.JSONDecodeError, TypeError) as exc:
            raise LLMError("`claude` returned no structured output") from exc

    try:
        result = output.model_validate(payload)
    except ValidationError as exc:
        raise LLMError(f"`claude` output did not match {output.__name__}: {exc}") from exc

    usage = envelope.get("usage") or {}
    return Completion(
        result=result,
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cost_usd=envelope.get("total_cost_usd"),
        backend=name,
    )


# ----------------------------------------------------------------- choose --


def resolve_backend(settings: Settings) -> Completer:
    """Pick a backend from settings.

    `auto` prefers the API when a key is present, then Claude Code when the
    command is installed. `api` and `claude-code` force one and fail loudly if
    it is not usable.
    """
    mode = settings.llm_backend
    has_key = bool(settings.anthropic_api_key)
    has_cli = shutil.which(settings.claude_code_executable) is not None

    if mode == "api" or (mode == "auto" and has_key):
        if not has_key:
            raise LLMUnavailable("JOBAGENT_LLM_BACKEND=api but ANTHROPIC_API_KEY is not set.")
        return AnthropicBackend(model=settings.model, api_key=settings.anthropic_api_key)

    if mode == "claude-code" or (mode == "auto" and has_cli):
        if not has_cli:
            raise LLMUnavailable(
                f"JOBAGENT_LLM_BACKEND=claude-code but `{settings.claude_code_executable}` "
                "is not on PATH. Install Claude Code and run `claude auth login`."
            )
        return ClaudeCodeBackend(
            executable=settings.claude_code_executable,
            model=settings.claude_code_model,
            workdir=settings.data_dir / "claude-code",
        )

    raise LLMUnavailable(
        "No model backend available. Either install Claude Code and run "
        "`claude auth login` (uses your subscription), or set ANTHROPIC_API_KEY."
    )
