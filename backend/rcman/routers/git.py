"""The git repositories of the signed-in person's workspaces.

git in workspaces is not there yet: every route keeps the shape it will have and
answers ``not_available_yet``. What a person writes for git — a commit's message,
the files to stage — would never be recorded or logged; a clone's address is
checked by a rule that takes no user or password in it.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from helpers import rules
from rcman.audit import Record, Recorder
from rcman.identity import Identity, current_user
from rcman.problems import Problem, ruled

router = APIRouter(prefix="/api/git", tags=["git"])
# The longest repository path taken, and the most files one stage or unstage takes.
MAX_PATH_LENGTH = 1024
MAX_FILES_PER_CALL = 500

RepositoryPath = Annotated[str, Field(min_length=1, max_length=MAX_PATH_LENGTH)]
CommitMessage = ruled(rules.COMMIT_MESSAGE, max_length=rules.MAX_COMMIT_MESSAGE_LENGTH)
BranchName = ruled(rules.BRANCH_NAME)
GitHubUrl = ruled(rules.GITHUB_URL)


class RepositoryBody(BaseModel):
    path: RepositoryPath = Field(description="The repository's folder.")


class FilesBody(RepositoryBody):
    files: list[str] = Field(
        min_length=1,
        max_length=MAX_FILES_PER_CALL,
        description="Files, relative to the repository, as git status lists them.",
    )


class CommitBody(RepositoryBody):
    message: CommitMessage = Field(description="The commit message.")


class SwitchBody(RepositoryBody):
    branch: BranchName = Field(description="A local branch, or a remote-tracking one.")
    remote: bool = Field(
        default=False,
        description="The branch is remote-tracking: a local branch tracking it is made.",
    )


class NewBranchBody(RepositoryBody):
    name: BranchName = Field(description="The new branch's name.")


class CloneBody(BaseModel):
    url: GitHubUrl = Field(description="The repository's address on GitHub.")


RepositoryQuery = Annotated[RepositoryPath, Query(description="The repository's folder.")]


def _not_yet():
    raise Problem("not_available_yet")


@router.get("/status")
def status(path: RepositoryQuery, identity: Identity = Depends(current_user)) -> dict:
    """The branch, the staged and not staged files, and who commits are recorded as."""
    _not_yet()


@router.get("/branches")
def branches(path: RepositoryQuery, identity: Identity = Depends(current_user)) -> dict:
    _not_yet()


@router.post("/stage")
def stage(body: FilesBody, record: Record = Depends(Recorder("git.stage"))) -> dict:
    record.target = body.path
    _not_yet()


@router.post("/unstage")
def unstage(body: FilesBody, record: Record = Depends(Recorder("git.unstage"))) -> dict:
    record.target = body.path
    _not_yet()


@router.post("/commit")
def commit(body: CommitBody, record: Record = Depends(Recorder("git.commit"))) -> dict:
    record.target = body.path
    _not_yet()


@router.post("/switch")
def switch(body: SwitchBody, record: Record = Depends(Recorder("git.switch"))) -> dict:
    record.target = body.path
    _not_yet()


@router.post("/branch")
def new_branch(body: NewBranchBody, record: Record = Depends(Recorder("git.branch"))) -> dict:
    record.target = body.path
    _not_yet()


@router.post("/fetch")
def fetch(body: RepositoryBody, record: Record = Depends(Recorder("git.fetch"))) -> dict:
    record.target = body.path
    _not_yet()


@router.post("/push")
def push(body: RepositoryBody, record: Record = Depends(Recorder("git.push"))) -> dict:
    record.target = body.path
    _not_yet()


@router.post("/commit-push")
def commit_push(body: CommitBody, record: Record = Depends(Recorder("git.commit_push"))) -> dict:
    record.target = body.path
    _not_yet()


@router.post("/clone")
def clone(body: CloneBody, record: Record = Depends(Recorder("git.clone"))) -> dict:
    record.target = body.url
    _not_yet()
