from pathlib import Path
from typing import Literal

from pydantic import Field, HttpUrl, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=None, extra="ignore", hide_input_in_errors=True
    )
    app_env: Literal["development", "production"] = "development"
    quote_url: HttpUrl = HttpUrl("http://127.0.0.1:8000")
    sqlite_path: Path = Path("data/agent.sqlite3")
    language_provider: Literal["fake", "gemini"] = "fake"
    language_model: str = Field(
        default="gemini-3.8-flash", pattern=r"^gemini-[a-z0-9.-]+$"
    )
    provider_secret: SecretStr = SecretStr("")
    hmac_secret: SecretStr = Field(min_length=32)
    max_attempts: int = Field(default=3, ge=1, le=3)
    attempt_timeout: float = Field(default=2, gt=0, le=2)
    connect_timeout: float = Field(default=0.5, gt=0, le=1)
    quote_deadline: float = Field(default=39, gt=0, le=39)
    llm_deadline: float = Field(default=30, gt=0, le=30)
    turn_deadline: float = Field(default=40, ge=3, le=40)
    finalization_reserve: float = Field(default=1, ge=0.5, le=2)
    quote_ttl: int = Field(default=300, ge=1, le=300)
    retention_days: int = Field(default=30, ge=1)
    app_version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    git_sha: str = Field(default="unknown", pattern=r"^(unknown|[a-f0-9]{7,40})$")
    prompt_revision: Literal["extract-v1"] = "extract-v1"

    @model_validator(mode="after")
    def deployment(self):
        if (
            self.language_provider == "gemini"
            and not self.provider_secret.get_secret_value()
        ):
            raise ValueError("provider secret required")
        if self.app_env == "production":
            if self.language_provider == "fake":
                raise ValueError("fake adapter forbidden in production")
            secrets = (self.hmac_secret, self.provider_secret)
            if any(
                any(
                    x in s.get_secret_value().lower()
                    for x in ("placeholder", "development", "change-me", "test")
                )
                or len(set(s.get_secret_value())) < 10
                for s in secrets
            ):
                raise ValueError("placeholder secrets forbidden in production")
        return self
