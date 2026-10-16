"""The signed-in person's Claude account, and Claude Code versions for administrators.

Signing in and out is accounts.py's; ``connection`` reads the account's state from
its status, against the ``login_warning_days`` setting. The versions routes are the
report of what is approved, installed and running, the rollout of a version, and the
release site's suggestion of the latest one; the versions themselves are
claude_versions.py's.
"""

from __future__ import annotations

import datetime as dt
import logging

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from helpers import claude_release, rules
from rcman import db, people
from rcman.audit import Record, Recorder
from rcman.identity import Identity, admin_user, current_user
from rcman.problems import Problem, ruled
from rcman.runtime import RuntimeFailed, RuntimeUnavailable

log = logging.getLogger("rcman.claude_versions")

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


class LogoutBody(BaseModel):
    keep_sessions: bool = Field(
        True,
        description="Leave running servers running; the sign-out waits until the last stops.",
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


def _person(request: Request, identity: Identity) -> people.Person:
    con = db.connect(request.app.state.settings.database)
    try:
        return people.by_id(con, identity.person)
    finally:
        con.close()


def _answer(request: Request, status: dict) -> dict:
    found = connection(status, request.app.state.settings.login_warning_days)
    return {**found, "signing_out": status.get("signing_out", False)}


@router.get("")
def get_connection(request: Request, identity: Identity = Depends(current_user)) -> dict:
    """The account's state, read from its credentials file; read when the Claude page
    opens, never polled."""
    accounts = request.app.state.accounts
    return _answer(request, accounts.status(_person(request, identity)))


@router.post("/login")
def login(
    body: LoginBody, request: Request, record: Record = Depends(Recorder("claude.login"))
) -> dict:
    email = body.email or record.identity.email
    record.target = email
    return request.app.state.accounts.begin(_person(request, record.identity), email)


@router.post("/code")
def code(
    body: CodeBody, request: Request, record: Record = Depends(Recorder("claude.connect"))
) -> dict:
    person = _person(request, record.identity)
    return _answer(request, request.app.state.accounts.finish(person, body.code.strip()))


@router.post("/logout")
def logout(
    body: LogoutBody, request: Request, record: Record = Depends(Recorder("claude.disconnect"))
) -> dict:
    person = _person(request, record.identity)
    return _answer(request, request.app.state.accounts.sign_out(person, body.keep_sessions))


@router.get("/versions", dependencies=[Depends(admin_user)])
def versions(request: Request) -> dict:
    """What is approved, what is installed and by how many servers used, which running servers
    run another version than the approved one, and whether a rollout runs."""
    return request.app.state.versions.report()


@router.post("/upgrade", dependencies=[Depends(admin_user)])
def upgrade(
    body: UpgradeBody, request: Request, record: Record = Depends(Recorder("claude.upgrade"))
) -> dict:
    """Approve a version: download it (or take it from the store), run the canary's checks on
    it, and only if it passes, install it, approve it and make it the one new servers start. The
    request lasts as long as that does."""
    record.target = body.version
    try:
        return request.app.state.versions.upgrade(body.version, record.identity.email)
    except RuntimeUnavailable:
        raise Problem("runtime_unavailable") from None
    except RuntimeFailed as failed:
        log.warning("the checks of Claude Code %s could not be run: %s", body.version, failed)
        raise Problem("claude_canary_failed") from None


@router.get("/latest", dependencies=[Depends(admin_user)])
def latest(request: Request) -> dict:
    """The version the release site names as the newest, as a suggestion only: nothing is
    installed or approved. None where the site does not say."""
    try:
        return {"latest": claude_release.latest(request.app.state.settings.claude_download)}
    except claude_release.ReleaseError as failed:
        log.info("the latest Claude Code version could not be read: %s", failed)
        return {"latest": None}
