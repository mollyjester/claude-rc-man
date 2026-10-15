"""The signed-in person's GitHub token, which git uses for GitHub in their workspaces.

The token is stored by gitaccount.py: in the person's volume, where git in every
workspace of theirs reads it, and in SQLite. It is never answered, recorded or logged,
so neither audit row has a target.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field

from helpers import rules
from rcman.audit import Record, Recorder
from rcman.identity import Identity, current_user
from rcman.problems import ruled

router = APIRouter(prefix="/api/github", tags=["github"])
Token = ruled(rules.GITHUB_TOKEN)


class TokenBody(BaseModel):
    token: Token = Field(description="A GitHub personal access token.")


@router.get("")
def status(request: Request, identity: Identity = Depends(current_user)) -> dict:
    """Whether a GitHub token is stored for git; never the token itself."""
    return {"token": request.app.state.git_account.token_stored(identity.person)}


@router.post("/token")
def save(
    body: TokenBody, request: Request, record: Record = Depends(Recorder("github.token"))
) -> dict:
    """Store the token as git's credential for GitHub in every workspace, in place of any
    stored before."""
    request.app.state.git_account.save_token(record.identity, body.token)
    return {"token": True}


@router.delete("/token")
def forget(request: Request, record: Record = Depends(Recorder("github.forget"))) -> dict:
    """Forget the token: git in every workspace has none from its next call on."""
    request.app.state.git_account.forget_token(record.identity)
    return {"token": False}
