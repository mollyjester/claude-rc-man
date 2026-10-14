"""The signed-in user, and their own settings.

Each person's settings are one row of ``user_settings``; a setting left unset
is null there and means its default. :func:`prefix_of` and :func:`author_of`
are what the rest of rcman reads them through.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_core import PydanticCustomError

from helpers import rules
from rcman import db
from rcman.audit import Record, Recorder
from rcman.identity import Identity, current_user
from rcman.problems import SETTINGS_NONE_SENT, ruled

router = APIRouter(prefix="/api/me", tags=["me"])
Prefix = ruled(rules.PREFIX.or_empty())
AuthorName = ruled(rules.AUTHOR_NAME)
AuthorEmail = ruled(rules.EMAIL.or_empty())


def _published(schema: dict) -> None:
    """Describe the body as it is validated: at least one setting is sent."""
    schema["minProperties"] = 1


class SettingsBody(BaseModel):
    """A change to some of the signed-in person's settings; those left out are unchanged."""

    model_config = ConfigDict(json_schema_extra=_published)

    prefix: Prefix = Field(
        default=None,
        description="Prefix of the names of new Remote Control sessions; empty for each "
        "workspace's own name.",
    )
    author_name: AuthorName = Field(
        default=None, description="Name commits made from rcman are recorded under."
    )
    author_email: AuthorEmail = Field(
        default=None,
        description="Email address commits are recorded under; empty for your own.",
    )

    @model_validator(mode="after")
    def _some_setting(self) -> SettingsBody:
        if not self.model_fields_set:
            raise PydanticCustomError(SETTINGS_NONE_SENT, "")
        return self


@dataclass(frozen=True)
class Author:
    name: str
    email: str


def _row(request: Request, identity: Identity) -> sqlite3.Row | None:
    con = db.connect(request.app.state.settings.database)
    try:
        return con.execute(
            "select * from user_settings where email = ?", (identity.email,)
        ).fetchone()
    finally:
        con.close()


def _prefix(row: sqlite3.Row | None) -> str | None:
    return row["prefix"] if row else None


def _author(row: sqlite3.Row | None, identity: Identity) -> Author | None:
    if row is None or row["author_name"] is None:
        return None
    return Author(name=row["author_name"], email=row["author_email"] or identity.email)


def prefix_of(request: Request, identity: Identity, workspace) -> str:
    """The prefix of the names of the sessions of a server in ``workspace``: the person's,
    or the workspace's name while they have set none."""
    return _prefix(_row(request, identity)) or workspace.name


def author_of(request: Request, identity: Identity) -> Author | None:
    """Who the person's commits are recorded as: None until a name is set."""
    return _author(_row(request, identity), identity)


def _settings(request: Request, identity: Identity) -> dict:
    row = _row(request, identity)
    author = _author(row, identity)
    return {
        "prefix": _prefix(row) or "",
        "author_name": (row and row["author_name"]) or "",
        "author_email": (row and row["author_email"]) or "",
        "effective_author": author and {"name": author.name, "email": author.email},
    }


@router.get("")
def me(request: Request, identity: Identity = Depends(current_user)) -> dict:
    return {
        "email": identity.email,
        "admin": identity.admin,
        "prefix": _prefix(_row(request, identity)),
    }


@router.get("/settings")
def get_settings(request: Request, identity: Identity = Depends(current_user)) -> dict:
    return _settings(request, identity)


@router.patch("/settings")
def patch_settings(
    body: SettingsBody, request: Request, record: Record = Depends(Recorder("settings.update"))
) -> dict:
    sent = body.model_dump(exclude_unset=True)
    record.target = json.dumps(sent, ensure_ascii=False)
    # Column names are the model's own fields, never anything from the request.
    columns = [name for name in SettingsBody.model_fields if name in sent]
    values = [sent[name] or None for name in columns]  # empty is the default
    con = db.connect(request.app.state.settings.database)
    try:
        con.execute(
            f"insert into user_settings (email, {', '.join(columns)}) "
            f"values (?{', ?' * len(columns)}) on conflict (email) do update set "
            + ", ".join(f"{name} = excluded.{name}" for name in columns),
            (record.identity.email, *values),
        )
        con.commit()
    finally:
        con.close()
    return _settings(request, record.identity)


# Where oauth2-proxy ends the web session (installer/templates/Caddyfile sends /oauth2/*
# to it).
SIGN_OUT_URL = "/oauth2/sign_out"


class SignOutBody(BaseModel):
    keep_sessions: bool = Field(
        True, description="Leave running servers running; otherwise they are stopped first."
    )


@router.post("/sign-out")
def sign_out(
    body: SignOutBody, request: Request, record: Record = Depends(Recorder("me.sign_out"))
) -> dict:
    """Signing out of rcman: running servers never depend on the web session, so they
    keep running unless asked otherwise. The page then goes to ``sign_out_url``."""
    if not body.keep_sessions:
        request.app.state.workspaces.stop_running(record.identity.person)
    return {"sign_out_url": SIGN_OUT_URL}
