"""Settings, from ``RCMAN_*`` environment variables (see docs/configuration.md)."""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from helpers import claude_release, rules

APP_ROOT = Path(__file__).resolve().parent.parent.parent
ORIGIN = re.compile(r"https?://[A-Za-z0-9.-]+(:\d+)?")


class Settings(BaseSettings):
    # A refused value is never repeated in the error, which reaches the journal: the secret key's
    # among them.
    model_config = SettingsConfigDict(env_prefix="RCMAN_", hide_input_in_errors=True)

    public_origin: str = Field(
        description="The scheme, host and port browsers use to reach rcman, such as "
        "https://rcman.example.com. Requests that change something must come from it."
    )
    database: Path = Field(Path("/var/lib/rcman/rcman.db"), description="The SQLite database file.")
    allowed_emails: Path = Field(
        Path("/var/lib/rcman/allowed-emails"),
        description="The list of addresses oauth2-proxy lets in, which rcman writes from its "
        "people.",
    )
    podman_socket: Path | None = Field(
        None,
        description="The socket of the Podman service rcman runs its workspaces with: "
        "rcman's own user manager's, under XDG_RUNTIME_DIR, when unset.",
    )
    workspace_image: str = Field(
        "localhost/rcman-workspace",
        description="The image new workspaces are made from, unless a repository's "
        "devcontainer.json names or builds another; the installer names its default image here.",
    )
    builder_image: str = Field(
        "localhost/rcman-builder",
        description="The image devcontainer builds run in: the devcontainer CLI and a Podman of "
        "its own, started for one build and removed after it.",
    )
    tools_dir: Path = Field(
        Path("/opt/rcman/tools"),
        description="The static tools mounted read-only into every workspace: tmux, bsdtar, "
        "busybox, terminfo, the podman and docker wrappers with their configuration, the "
        "workspaces' seccomp profile, and the instructions for Claude bound at Claude Code's "
        "managed CLAUDE.md.",
    )
    claude_store: Path = Field(
        Path("/var/lib/rcman/claude"),
        description="The Claude Code versions store mounted read-only into every workspace.",
    )
    claude_download: str = Field(
        claude_release.DOWNLOAD,
        description="The address Claude Code's releases are downloaded from, as the installer "
        "downloads them and as rcman does when an administrator rolls out another version.",
    )
    docs_dir: Path = Field(
        APP_ROOT / "docs", description="The directory of documentation pages served to users."
    )
    frontend_dir: Path = Field(
        APP_ROOT / "frontend" / "dist", description="The built web interface rcman serves."
    )
    audit_retention_days: int = Field(
        90, ge=1, description="Audit events older than this many days are deleted."
    )
    login_warning_days: int = Field(
        3, ge=1, description="Warn this many days before someone's Claude sign-in expires."
    )
    web_upgrade: bool = Field(
        False,
        description="Whether administrators may upgrade rcman from its web interface: the "
        "installer sets it, with the units that carry an upgrade out, when told to.",
    )
    forward_socket: Path | None = Field(
        None,
        description="The unix socket rcman's forwarding server listens on, which Caddy passes "
        "the requests for workspaces' forwarded ports to, and its questions whether it may get "
        "a certificate for one; the installer sets it. Unset, no port is forwarded.",
    )
    secret_key: SecretStr | None = Field(
        None,
        description="The key rcman seals the cookies of Microsoft sign-in and its stored client "
        "secret with (sealing.py); the installer generates it and writes it in secret.env. "
        "In the form helpers/rules.py's SECRET_KEY holds it to. Unset or empty, Microsoft "
        "sign-in is not available.",
    )
    audit_max_rows: int = Field(
        100_000, ge=1, description="At most this many audit events are kept; the oldest go first."
    )

    @field_validator("claude_download")
    @classmethod
    def _release_address(cls, value: str) -> str:
        if not claude_release.DOWNLOAD_ADDRESS.fullmatch(value):
            raise ValueError("must be an http or https address with no slash at its end")
        return value

    @field_validator("secret_key", mode="before")
    @classmethod
    def _secret_key(cls, value):
        text = value.get_secret_value() if isinstance(value, SecretStr) else value
        if text is None or text == "":
            return None
        if not isinstance(text, str) or not rules.SECRET_KEY.fullmatch(text):
            raise ValueError(rules.SECRET_KEY.sentence)
        return text

    @field_validator("public_origin")
    @classmethod
    def _bare_origin(cls, value: str) -> str:
        if not ORIGIN.fullmatch(value):
            raise ValueError("must be scheme://host[:port], with no path or trailing slash")
        return value
