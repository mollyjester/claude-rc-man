"""The signed-in person's Claude account, and Claude Code versions for administrators.

Signing in to Claude in workspaces, and rolling out Claude Code versions, are not
there yet: every route keeps the shape it will have and answers
``not_available_yet``. ``connection`` is how the account's state will be read from
its status, against the ``login_warning_days`` setting.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from helpers import rules
from rcman.audit import Record, Recorder
from rcman.identity import Identity, admin_user, current_user
from rcman.problems import Problem, ruled

router = APIRouter(prefix="/api/claude", tags=["claude"])
MAX_CODE_LENGTH = 512
Email = ruled(rules.EMAIL)
Version = ruled(rules.VERSION)


class LoginBody(BaseModel):
    email: Email | None = Field(
        None, description="The Claude account's email address; the Google address when omitted."
    )


class CodeBody(BaseModel):
    code: str = Field(
        min_length=1, max_length=MAX_CODE_LENGTH, description="The code Claude showed."
    )


class UpgradeBody(BaseModel):
    version: Version = Field(description="Such as 1.2.3.")


def connection(status: dict, warning_days: int, now: dt.datetime | None = None) -> dict:
    """The account's state: not_connected, connected, expiring or expired."""
    if not status.get("logged_in"):
        return {"connection": "not_connected", "account": None, "login_expires_at": None}
    expires = status.get("login_expires_at")
    state = "connected"
    if expires:
        left = dt.datetime.fromisoformat(expires) - (now or dt.datetime.now(dt.UTC))
        if left <= dt.timedelta(0):
            state = "expired"
        elif left <= dt.timedelta(days=warning_days):
            state = "expiring"
    account = {k: status.get(k) for k in ("email", "org_name", "subscription_type")}
    return {"connection": state, "account": account, "login_expires_at": expires}


@router.get("")
def get_connection(identity: Identity = Depends(current_user)) -> dict:
    raise Problem("not_available_yet")


@router.post("/login")
def login(body: LoginBody, record: Record = Depends(Recorder("claude.login"))) -> dict:
    record.target = body.email or record.identity.email
    raise Problem("not_available_yet")


@router.post("/code")
def code(body: CodeBody, record: Record = Depends(Recorder("claude.connect"))) -> dict:
    raise Problem("not_available_yet")


@router.get("/versions", dependencies=[Depends(admin_user)])
def versions() -> dict:
    raise Problem("not_available_yet")


@router.post("/upgrade", dependencies=[Depends(admin_user)])
def upgrade(body: UpgradeBody, record: Record = Depends(Recorder("claude.upgrade"))) -> dict:
    record.target = body.version
    raise Problem("not_available_yet")
