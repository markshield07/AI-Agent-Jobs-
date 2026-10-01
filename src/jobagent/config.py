"""Runtime settings, read from the environment or a .env file."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

# The job boards whose own application forms the agent fills.
BOARD_SITES = frozenset({"linkedin", "indeed", "dice"})


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="JOBAGENT_", extra="ignore")

    data_dir: Path = Path("./data")

    # Which way model calls go. `auto` uses the API if ANTHROPIC_API_KEY is set,
    # otherwise the `claude` command line on your subscription login.
    llm_backend: Literal["auto", "api", "claude-code"] = "auto"
    model: str = "claude-opus-5"
    claude_code_executable: str = "claude"
    claude_code_model: str | None = None

    # Read without the JOBAGENT_ prefix so it matches what the Anthropic SDK expects.
    anthropic_api_key: str | None = None

    # Submission. `dry_run` fills every form and stops at the submit button;
    # `review` does the same and parks the application for your approval;
    # `auto` submits. Nothing is sent unless you change this.
    apply_mode: Literal["dry_run", "review", "auto"] = "dry_run"
    daily_apply_cap: int = 20
    # LinkedIn and Indeed each get their own, lower cap on top of the daily
    # one: both watch for accounts that apply faster than a person would.
    easy_apply_daily_cap: int = 10
    # Dice starts lower still, to be raised once its applications go through.
    dice_daily_cap: int = 3
    apply_delay_seconds: float = 45.0  # between submissions, with jitter
    # Where applications go, e.g. "linkedin,dice": only jobs that apply on
    # those sites' own forms. A job that sends the applicant to a company's
    # site is set aside (skipped, with the reason), not tried. Empty: every site.
    apply_sites: str = ""
    apply_model_answers: bool = True  # let the model draft answers from the fact base
    headless: bool = True
    browser_executable: str | None = None  # a Chromium binary, when Playwright's is absent

    # Response tracking. The mailbox is opened read-only and nothing is ever
    # sent, moved or deleted. The password is read from the environment or
    # .env at startup and never written to the database.
    imap_host: str | None = None
    imap_port: int = 993
    imap_user: str | None = None
    imap_password: str | None = None
    imap_folder: str = "INBOX"
    imap_ssl: bool = True
    inbox_lookback_days: int = 30  # how far back a first poll reads
    inbox_poll_limit: int = 200  # messages per poll
    inbox_model_triage: bool = True  # ask the model about replies the rules cannot read
    inbox_min_confidence: float = 0.6  # below this a reply moves no status

    @property
    def apply_site_list(self) -> tuple[str, ...]:
        """`apply_sites` as names, lower case; empty when every site is allowed."""
        names = (part.strip().lower() for part in self.apply_sites.split(","))
        return tuple(dict.fromkeys(name for name in names if name))

    @property
    def board_apply_only(self) -> bool:
        """Only the boards' own forms (LinkedIn, Indeed, Dice): discovery then
        asks those boards for jobs that apply there (Easy Apply, Indeed Apply)."""
        sites = self.apply_site_list
        return bool(sites) and set(sites) <= BOARD_SITES

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobagent.db"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "resumes"

    @property
    def variants_dir(self) -> Path:
        return self.data_dir / "variants"

    @property
    def screenshots_dir(self) -> Path:
        return self.data_dir / "screenshots"

    def ensure_dirs(self) -> None:
        self.upload_dir.mkdir(parents=True, exist_ok=True)
        self.variants_dir.mkdir(parents=True, exist_ok=True)
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    if settings.anthropic_api_key is None:
        settings.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    settings.ensure_dirs()
    return settings
