"""The git repositories in the signed-in person's home, through rc-helper's git verbs.

What git is given that a person wrote — the files to stage or unstage and a
commit's message — reaches rc-helper on stdin, never in its arguments; a
commit's message is never recorded or logged.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from helpers import rc_helper
from rcman.audit import Record, Recorder
from rcman.helpers_client import GIT_ANSWER_MARGIN_SECONDS, call_helper
from rcman.identity import Identity, current_user
from rcman.problems import Problem, ruled
from rcman.routers.folders import FolderPath
from rcman.routers.me import author_of

router = APIRouter(prefix="/api/git", tags=["git"])
# How long rcman waits for each kind of git verb: the helper's own deadline, and
# time to start and to answer.
LOCAL_TIMEOUT = rc_helper.GIT_TIMEOUT + GIT_ANSWER_MARGIN_SECONDS
NETWORK_TIMEOUT = rc_helper.GIT_NETWORK_TIMEOUT + GIT_ANSWER_MARGIN_SECONDS

CommitMessage = ruled(rc_helper.COMMIT_MESSAGE, max_length=rc_helper.MAX_COMMIT_MESSAGE_LENGTH)
BranchName = ruled(rc_helper.BRANCH_NAME)


class RepositoryBody(BaseModel):
    path: FolderPath = Field(description="The repository's folder, relative to the home folder.")


class FilesBody(RepositoryBody):
    files: list[str] = Field(
        min_length=1,
        max_length=rc_helper.MAX_FILES_PER_CALL,
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


def _git(request: Request, identity: Identity, verb: str, path: str, *options: str, **kw) -> dict:
    kw.setdefault("timeout", LOCAL_TIMEOUT)
    return call_helper(request.app.state.helper, identity, [verb, *options, "--", path], **kw)


RepositoryQuery = Annotated[
    FolderPath, Query(description="The repository's folder, relative to the home folder.")
]


@router.get("/status")
def status(
    request: Request,
    path: RepositoryQuery,
    identity: Identity = Depends(current_user),
) -> dict:
    """The branch, the staged and not staged files, and who commits are recorded as."""
    out = _git(request, identity, "git-status", path)
    author = author_of(request, identity)
    out["author"] = author and {"name": author.name, "email": author.email}
    return out


@router.get("/branches")
def branches(
    request: Request,
    path: RepositoryQuery,
    identity: Identity = Depends(current_user),
) -> dict:
    return _git(request, identity, "git-branches", path)


@router.post("/stage")
def stage(
    body: FilesBody, request: Request, record: Record = Depends(Recorder("git.stage"))
) -> dict:
    record.target = body.path
    return _git(request, record.identity, "git-stage", body.path, stdin=json.dumps(body.files))


@router.post("/unstage")
def unstage(
    body: FilesBody, request: Request, record: Record = Depends(Recorder("git.unstage"))
) -> dict:
    record.target = body.path
    return _git(request, record.identity, "git-unstage", body.path, stdin=json.dumps(body.files))


@router.post("/commit")
def commit(
    body: CommitBody, request: Request, record: Record = Depends(Recorder("git.commit"))
) -> dict:
    """Commit what is staged as the person's commit author, refused until a name is set."""
    record.target = body.path
    author = author_of(request, record.identity)
    if author is None:
        raise Problem("author_not_set")
    options = (f"--author-name={author.name}", f"--author-email={author.email}")
    return _git(request, record.identity, "git-commit", body.path, *options, stdin=body.message)


@router.post("/switch")
def switch(
    body: SwitchBody, request: Request, record: Record = Depends(Recorder("git.switch"))
) -> dict:
    record.target = body.path
    options = (f"--branch={body.branch}", *(["--remote"] if body.remote else []))
    return _git(request, record.identity, "git-switch", body.path, *options)


@router.post("/branch")
def new_branch(
    body: NewBranchBody, request: Request, record: Record = Depends(Recorder("git.branch"))
) -> dict:
    record.target = body.path
    return _git(request, record.identity, "git-branch-new", body.path, f"--name={body.name}")


@router.post("/fetch")
def fetch(
    body: RepositoryBody, request: Request, record: Record = Depends(Recorder("git.fetch"))
) -> dict:
    """git fetch --prune: refreshes the remote-tracking branches, and nothing else."""
    record.target = body.path
    return _git(request, record.identity, "git-fetch", body.path, timeout=NETWORK_TIMEOUT)
