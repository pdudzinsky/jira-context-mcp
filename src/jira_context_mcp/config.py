"""Runtime configuration loaded from environment variables and optional .env file."""

from functools import lru_cache

from pydantic import HttpUrl, SecretStr, TypeAdapter, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_HTTP_URL_ADAPTER: TypeAdapter[HttpUrl] = TypeAdapter(HttpUrl)


class Settings(BaseSettings):
    """Configuration for the Jira Cloud connection and HTTP client behavior.

    Values are sourced from environment variables. A local ``.env`` file is
    loaded automatically when present; it is silently skipped otherwise.
    Variable lookup is case-insensitive and unknown keys are ignored so the
    server boots cleanly in environments that set unrelated variables.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    jira_base_url: str
    jira_email: str
    jira_api_token: SecretStr

    request_timeout: float = 30.0
    max_retries: int = 3

    @field_validator("jira_base_url", mode="after")
    @classmethod
    def _normalize_base_url(cls, value: str) -> str:
        # Validate URL shape via pydantic, but keep the value as a plain string
        # so callers can safely concatenate paths without a double-slash or the
        # trailing slash that HttpUrl.__str__ would otherwise append.
        _HTTP_URL_ADAPTER.validate_python(value)
        return value.rstrip("/")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a process-wide cached :class:`Settings` instance.

    Tests that mutate environment variables should call
    ``get_settings.cache_clear()`` to force a reload.
    """
    return Settings()  # type: ignore[call-arg]
