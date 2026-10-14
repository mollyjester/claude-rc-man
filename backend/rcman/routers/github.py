"""The signed-in person's GitHub token, which git will use for GitHub.

Not there yet in workspaces: every route keeps the shape it will have and
answers ``not_available_yet``. The token would never be recorded or logged, so
neither audit row has a target.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from helpers import rules
from rcman.audit import Record, Recorder
from rcman.identity import Identity, current_user
from rcman.problems import Problem, ruled

router = APIRouter(prefix="/api/github", tags=["github"])
Token = ruled(rules.GITHUB_TOKEN)


class TokenBody(BaseModel):
    token: Token = Field(description="A GitHub personal access token.")


@router.get("")
def status(identity: Identity = Depends(current_user)) -> dict:
    """Whether a GitHub token is stored for git; never the token itself."""
    raise Problem("not_available_yet")


@router.post("/token")
def save(body: TokenBody, record: Record = Depends(Recorder("github.token"))) -> dict:
    raise Problem("not_available_yet")


@router.delete("/token")
def forget(record: Record = Depends(Recorder("github.forget"))) -> dict:
    raise Problem("not_available_yet")
