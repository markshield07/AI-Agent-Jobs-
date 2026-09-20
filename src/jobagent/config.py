"""Runtime settings, read from the environment or a .env file."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="JOBAGENT_", extra="ignore")

    data_dir: Path = Path("./data")
    model: str = "claude-opus-5"

    # Read without the JOBAGENT_ prefix so it matches what the Anthropic SDK expects.
    anthropic_api_key: str | None = None

    @property
    def db_path(self) -> Path:
        return self.data_dir / "jobagent.db"

    @property
    def upload_dir(self) -> Path:
        return self.data_dir / "resumes"

    def ensure_dirs(self) -> None:
        self.upload_dir.mkdir(parents=True, exist_ok=True)


@lru_cache
def get_settings() -> Settings:
    import os

    settings = Settings()
    if settings.anthropic_api_key is None:
        settings.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
    settings.ensure_dirs()
    return settings
