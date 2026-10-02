"""The signed-in user's Claude account, and Claude Code versions for administrators."""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from helpers import rc_admin
from helpers import users_file as uf
from rcman.audit import Record, Recorder
from rcman.helpers_client import ADMIN_TIMEOUT, call_admin, call_helper
from rcman.identity import Identity, admin_user, current_user
from rcman.problems import ruled

router = APIRouter(prefix="/api/claude", tags=["claude"])
MAX_CODE_LENGTH = 512
Email = ruled(uf.EMAIL)
Version = ruled(rc_admin.VERSION)


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


def _status(request: Request, identity: Identity) -> dict:
    status = call_helper(request.app.state.helper, identity, ["auth-status"])
    return connection(status, request.app.state.settings.login_warning_days)


@router.get("")
def get_connection(request: Request, identity: Identity = Depends(current_user)) -> dict:
    return _status(request, identity)


@router.post("/login")
def login(
    body: LoginBody, request: Request, record: Record = Depends(Recorder("claude.login"))
) -> dict:
    email = body.email or record.identity.email
    record.target = email
    return call_helper(
        request.app.state.helper, record.identity, ["auth-login", f"--email={email}"]
    )


@router.post("/code")
def code(
    body: CodeBody, request: Request, record: Record = Depends(Recorder("claude.connect"))
) -> dict:
    call_helper(request.app.state.helper, record.identity, ["auth-code"], stdin=body.code + "\n")
    return _status(request, record.identity)


def version_report(request: Request, timeout: float = ADMIN_TIMEOUT) -> dict:
    """rc-admin's claude-status, with who differs from the approved version and whether
    the approved version is the tested one."""
    status = call_admin(request.app.state.admin, ["claude-status"], timeout=timeout)
    differs = [u["linux"] for u in status["users"] if u["installed"] != status["approved"]]
    return {**status, "differs": differs, "untested": status["approved"] != status["tested"]}


@router.get("/versions", dependencies=[Depends(admin_user)])
def versions(request: Request) -> dict:
    return version_report(request)


@router.post("/upgrade", dependencies=[Depends(admin_user)])
def upgrade(
    body: UpgradeBody, request: Request, record: Record = Depends(Recorder("claude.upgrade"))
) -> dict:
    record.target = body.version
    return call_admin(request.app.state.admin, ["claude-upgrade", body.version])
