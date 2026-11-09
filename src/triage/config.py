"""Configuration loaded from environment variables."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from triage.errors import ConfigError
from triage.paths import REPO_ROOT

# Budgets (docs/architecture.md sections 3 and 4).
MAX_MODEL_TURNS = 6
MAX_TOOL_CALLS = 7  # read-tool attempts; one further attempt is reserved for export
MAX_LOG_RESULTS = 50
MAX_MESSAGE_CHARS = 300

DEFAULT_ENV_FILE = REPO_ROOT / ".env"
LLM_VARS = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")


class Settings(BaseModel):
    """LLM settings. Values may be missing until require_llm_settings() is called."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    LLM_BASE_URL: str | None = None
    LLM_API_KEY: str | None = Field(default=None, repr=False)
    LLM_MODEL: str | None = None
    LLM_TIMEOUT_S: float = Field(default=60.0, gt=0)
    LLM_JSON_MODE: bool = False  # "1" sends response_format={"type": "json_object"}


OPTIONAL_VARS = ("LLM_TIMEOUT_S", "LLM_JSON_MODE")


def load_settings(env_file: Path | None = DEFAULT_ENV_FILE) -> Settings:
    """Read settings from the environment, loading `env_file` first if it exists.

    Existing environment variables always win over values in the file.
    """
    if env_file is not None and env_file.is_file():
        load_dotenv(env_file, override=False)
    values: dict[str, str] = {}
    for name in (*LLM_VARS, *OPTIONAL_VARS):
        value = os.environ.get(name, "").strip()
        if value:
            values[name] = value
    try:
        return Settings.model_validate(values)
    except ValidationError as exc:
        # Name the fields only; never echo values (one of them is the API key).
        bad = sorted({str(err["loc"][0]) for err in exc.errors() if err["loc"]})
        raise ConfigError("invalid LLM configuration for: " + ", ".join(bad)) from None


def require_llm_settings(env_file: Path | None = DEFAULT_ENV_FILE) -> Settings:
    """Return settings, raising ConfigError that names every missing LLM variable."""
    settings = load_settings(env_file)
    missing = [name for name in LLM_VARS if not getattr(settings, name)]
    if missing:
        hint = ("copy .env.example to .env and fill them in"
                if env_file is None or not env_file.is_file()
                else f"set them in {env_file.name} or the environment")
        raise ConfigError(f"missing required environment variables: {', '.join(missing)} ({hint})")
    return settings
