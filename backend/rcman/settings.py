"""Settings, from ``RCMAN_*`` environment variables (see docs/configuration.md)."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

APP_ROOT = Path(__file__).resolve().parent.parent.parent
ORIGIN = re.compile(r"https?://[A-Za-z0-9.-]+(:\d+)?")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="RCMAN_")

    public_origin: str = Field(
        description="The scheme, host and port browsers use to reach rcman, such as "
        "https://rcman.example.com. Requests that change something must come from it."
    )
    users_file: Path = Field(
        Path("/etc/rcman/users.toml"), description="The list of people who may use rcman."
    )
    database: Path = Field(Path("/var/lib/rcman/rcman.db"), description="The SQLite database file.")
    helpers_dir: Path = Field(
        APP_ROOT / "helpers", description="The directory holding rc-helper and rc-admin."
    )
    docs_dir: Path = Field(
        APP_ROOT / "docs", description="The directory of documentation pages served to users."
    )
    audit_retention_days: int = Field(
        90, ge=1, description="Audit events older than this many days are deleted."
    )
    audit_max_rows: int = Field(
        100_000, ge=1, description="At most this many audit events are kept; the oldest go first."
    )

    login_warning_days: int = Field(
        3, ge=1, description="Warn this many days before someone's Claude sign-in expires."
    )

    @field_validator("public_origin")
    @classmethod
    def _bare_origin(cls, value: str) -> str:
        if not ORIGIN.fullmatch(value):
            raise ValueError("must be scheme://host[:port], with no path or trailing slash")
        return value
