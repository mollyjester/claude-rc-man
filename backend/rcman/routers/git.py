"""The git repositories in the signed-in person's home, through rc-helper's git verbs.

What git is given that a person wrote — the files to stage or unstage and a
commit's message — reaches rc-helper on stdin, never in its arguments; a
commit's message is never recorded or logged. A clone's address is an argument:
the rule it is checked by takes no user or password in it. Pushing to GitHub needs
the person's GitHub token (routers/github.py), which rc-helper checks for.
"""

from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from helpers import rc_helper
from rcman import problems
from rcman.audit import COMMITTED_NOT_PUSHED, Record, Recorder
from rcman.helpers_client import GIT_ANSWER_MARGIN_SECONDS, call_helper
from rcman.identity import Identity, current_user
from rcman.problems import Problem, ruled
from rcman.routers.folders import FolderPath, PathOrHome
from rcman.routers.me import author_of

router = APIRouter(prefix="/api/git", tags=["git"])
# How long rcman waits for each kind of git verb: the helper's own deadline, and
# time to start and to answer.
LOCAL_TIMEOUT = rc_helper.GIT_TIMEOUT + GIT_ANSWER_MARGIN_SECONDS
NETWORK_TIMEOUT = rc_helper.GIT_NETWORK_TIMEOUT + GIT_ANSWER_MARGIN_SECONDS
CLONE_TIMEOUT = rc_helper.CLONE_TIMEOUT + GIT_ANSWER_MARGIN_SECONDS

CommitMessage = ruled(rc_helper.COMMIT_MESSAGE, max_length=rc_helper.MAX_COMMIT_MESSAGE_LENGTH)
BranchName = ruled(rc_helper.BRANCH_NAME)
GitHubUrl = ruled(rc_helper.GITHUB_URL)


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


class CloneBody(BaseModel):
    parent: PathOrHome = Field(
        default="",
        description="The folder to clone into, relative to the home folder; empty for the home.",
    )
    url: GitHubUrl = Field(description="The repository's address on GitHub.")


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


@router.post("/push")
def push(
    body: RepositoryBody, request: Request, record: Record = Depends(Recorder("git.push"))
) -> dict:
    """Push the current branch; one with no upstream is pushed to origin and set to track it.

    Refused while no branch is checked out, and for a push to GitHub until a token is stored.
    """
    record.target = body.path
    return _git(request, record.identity, "git-push", body.path, timeout=NETWORK_TIMEOUT)


@router.post("/commit-push")
def commit_push(
    body: CommitBody, request: Request, record: Record = Depends(Recorder("git.commit_push"))
) -> dict:
    """Commit what is staged, then push it.

    Nothing is committed when the push would be refused before it was tried: no
    branch checked out, or a push to GitHub with no token. When the commit is made
    and the push fails, the answer is the commit and the push's problem, and the
    audit row says committed_not_pushed.
    """
    record.target = body.path
    author = author_of(request, record.identity)
    if author is None:
        raise Problem("author_not_set")
    _git(request, record.identity, "git-push", body.path, "--check", timeout=NETWORK_TIMEOUT)
    options = (f"--author-name={author.name}", f"--author-email={author.email}")
    made = _git(request, record.identity, "git-commit", body.path, *options, stdin=body.message)
    answer = {"path": made["path"], "commit": made["commit"], "push": None, "push_problem": None}
    try:
        answer["push"] = _git(
            request, record.identity, "git-push", body.path, timeout=NETWORK_TIMEOUT
        )
    except Problem as failed:
        answer["push_problem"] = problems.body(
            failed.code, failed.status, failed.title, **failed.extra
        )
        record.result = COMMITTED_NOT_PUSHED
    return answer


@router.post("/clone")
def clone(
    body: CloneBody, request: Request, record: Record = Depends(Recorder("git.clone"))
) -> dict:
    """Clone a repository from GitHub into a folder, or into the home folder, in the
    folder git names; a private one with the person's GitHub token.

    The address is recorded as it is: GITHUB_URL takes none with a user or a password
    in it.
    """
    record.target = body.url
    args = ["git-clone", f"--url={body.url}", "--", *([body.parent] if body.parent else [])]
    return call_helper(request.app.state.helper, record.identity, args, timeout=CLONE_TIMEOUT)
