"""Settings base shared by the agents services: env prefix per service, one error for all."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CsvList = Annotated[list[str], NoDecode]
"""A list read from a comma-separated env value; blanks are dropped."""


class SettingsError(ValueError):
    """One error naming every bad environment variable."""

    def __init__(self, problems: dict[str, str]):
        self.problems = problems
        super().__init__(
            "invalid configuration: " + "; ".join(f"{k}: {v}" for k, v in problems.items())
        )


class BaseServiceSettings(BaseSettings):
    """Common fields; a subclass sets its own `env_prefix` through `model_config`."""

    model_config = SettingsConfigDict(extra="ignore")

    database_url: SecretStr = Field(repr=False)
    http_host: str = "0.0.0.0"
    http_port: int = 8000
    log_level: str = "INFO"
    orchestrator_url: str
    orchestrator_token: SecretStr = SecretStr("")
    orchestrator_token_file: str = ""
    shutdown_grace_seconds: float = 20

    @field_validator("*", mode="before")
    @classmethod
    def _split_csv(cls, value: object, info) -> object:
        field = cls.model_fields.get(info.field_name or "")
        if field is not None and NoDecode in field.metadata and isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        return value

    @model_validator(mode="after")
    def _token_from_file(self):
        prefix = str(self.model_config.get("env_prefix") or "").upper()
        if self.orchestrator_token_file:
            try:
                text = Path(self.orchestrator_token_file).read_text().strip()
            except OSError as exc:
                raise ValueError(
                    f"{prefix}ORCHESTRATOR_TOKEN_FILE::cannot read: {exc.strerror}"
                ) from None
            self.orchestrator_token = SecretStr(text)
        if not self.orchestrator_token.get_secret_value():
            raise ValueError(f"{prefix}ORCHESTRATOR_TOKEN::required")
        return self


def load_settings[S: BaseServiceSettings](cls: type[S]) -> S:
    """Instantiate `cls` from the environment or raise SettingsError naming every bad variable."""
    prefix = str(cls.model_config.get("env_prefix") or "").upper()
    try:
        return cls()
    except ValidationError as exc:
        problems: dict[str, str] = {}
        for err in exc.errors():
            msg = err["msg"].removeprefix("Value error, ")
            loc = err["loc"]
            var, sep, rest = msg.partition("::")
            if sep and var.startswith(prefix):
                problems.setdefault(var, rest)
                continue
            msg = "required" if err["type"] == "missing" else msg
            name = f"{prefix}{str(loc[0]).upper()}" if loc else f"{prefix}*"
            problems.setdefault(name, msg)
        raise SettingsError(problems) from None
