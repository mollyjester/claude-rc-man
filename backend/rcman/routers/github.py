"""The signed-in person's GitHub token, which git uses for GitHub, through rc-helper.

The token goes to rc-helper on stdin, never in its arguments, and is never
recorded or logged: neither audit row has a target.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from helpers import rc_helper
from rcman.audit import Record, Recorder
from rcman.helpers_client import call_helper
from rcman.identity import Identity, current_user
from rcman.problems import ruled

router = APIRouter(prefix="/api/github", tags=["github"])
Token = ruled(rc_helper.GITHUB_TOKEN)


class TokenBody(BaseModel):
    token: Token = Field(description="A GitHub personal access token.")


@router.get("")
def status(request: Request, identity: Identity = Depends(current_user)) -> dict:
    """Whether a GitHub token is stored for git; never the token itself."""
    return call_helper(request.app.state.helper, identity, ["github-status"])


@router.post("/token")
def save(
    body: TokenBody, request: Request, record: Record = Depends(Recorder("github.token"))
) -> dict:
    """Store the token as git's credential for GitHub, in place of any stored before."""
    return call_helper(
        request.app.state.helper, record.identity, ["github-token"], stdin=body.token + "\n"
    )


@router.delete("/token")
def forget(request: Request, record: Record = Depends(Recorder("github.forget"))) -> dict:
    return call_helper(request.app.state.helper, record.identity, ["github-forget"])
