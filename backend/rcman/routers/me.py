"""The signed-in user, and their own settings."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from helpers import rc_helper
from rcman import db
from rcman.audit import Record, Recorder
from rcman.identity import Identity, current_user

router = APIRouter(prefix="/api/me", tags=["me"])


class SettingsBody(BaseModel):
    prefix: str = Field(
        pattern=f"^{rc_helper.PREFIX.pattern}$",
        description="Prefix of the names of new Remote Control sessions.",
    )


def prefix_of(request: Request, identity: Identity) -> str:
    con = db.connect(request.app.state.settings.database)
    try:
        row = con.execute(
            "select prefix from user_settings where email = ?", (identity.email,)
        ).fetchone()
    finally:
        con.close()
    return row["prefix"] if row else identity.linux


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
    return {"prefix": prefix_of(request, identity)}


@router.put("/settings")
def put_settings(
    body: SettingsBody, request: Request, record: Record = Depends(Recorder("settings.update"))
) -> dict:
    record.target = body.prefix
    con = db.connect(request.app.state.settings.database)
    try:
        con.execute(
            "insert into user_settings (email, prefix) values (?, ?) "
            "on conflict (email) do update set prefix = excluded.prefix",
            (record.identity.email, body.prefix),
        )
        con.commit()
    finally:
        con.close()
    return {"prefix": body.prefix}
