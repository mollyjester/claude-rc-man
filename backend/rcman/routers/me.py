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

from helpers import rc_helper
from rcman import db
from rcman.audit import Record, Recorder
from rcman.identity import Identity, current_user

router = APIRouter(prefix="/api/me", tags=["me"])


def _published(schema: dict) -> None:
    """Describe the body as it is validated: at least one setting is sent."""
    schema["minProperties"] = 1


class SettingsBody(BaseModel):
    """A change to some of the signed-in person's settings; those left out are unchanged."""

    model_config = ConfigDict(json_schema_extra=_published)

    prefix: str = Field(
        default=None,
        pattern=f"^{rc_helper.PREFIX.pattern}$",
        description="Prefix of the names of new Remote Control sessions.",
    )
    author_name: str = Field(
        default=None,
        pattern=f"^{rc_helper.AUTHOR_NAME.pattern}$",
        description="Name commits made from rcman are recorded under.",
    )
    author_email: str = Field(
        default=None,
        pattern=f"^(?:{rc_helper.EMAIL.pattern})?$",
        description="Email address commits are recorded under; empty for the users file's.",
    )

    @model_validator(mode="after")
    def _some_setting(self) -> SettingsBody:
        if not self.model_fields_set:
            raise ValueError("Send at least one setting.")
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


def _prefix(row: sqlite3.Row | None, identity: Identity) -> str:
    return row["prefix"] if row and row["prefix"] is not None else identity.linux


def _author(row: sqlite3.Row | None, identity: Identity) -> Author | None:
    if row is None or row["author_name"] is None:
        return None
    return Author(name=row["author_name"], email=row["author_email"] or identity.email)


def prefix_of(request: Request, identity: Identity) -> str:
    """The prefix of the person's new session names: the Linux user until one is set."""
    return _prefix(_row(request, identity), identity)


def author_of(request: Request, identity: Identity) -> Author | None:
    """Who the person's commits are recorded as: None until a name is set."""
    return _author(_row(request, identity), identity)


def _settings(request: Request, identity: Identity) -> dict:
    row = _row(request, identity)
    author = _author(row, identity)
    return {
        "prefix": _prefix(row, identity),
        "author_name": (row and row["author_name"]) or "",
        "author_email": (row and row["author_email"]) or "",
        "effective_author": author and {"name": author.name, "email": author.email},
    }


@router.get("")
def me(request: Request, identity: Identity = Depends(current_user)) -> dict:
    return {
        "email": identity.email,
        "linux": identity.linux,
        "admin": identity.admin,
        "prefix": prefix_of(request, identity),
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
    values = [sent[name] or None for name in columns]  # an empty email is the default
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
