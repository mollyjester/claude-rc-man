"""The git repositories of the signed-in person's workspaces: the one at the root of each.

Every route names a workspace by its id and runs git in it, through its channel
(workspaces.py: ``repository``), as the workspace's person; nothing is started for it,
and no server or workspace lock is needed. What a person writes for git — a commit's
message, the files to stage — reaches git on stdin, and is never recorded or logged: an
audit row names the workspace and nothing else. A commit is made as the author in the
global git configuration rcman writes in the person's volume (gitaccount.py), and is
refused until a name is set. A clone's address is checked by a rule that takes no user
or password in it, so it is recorded as it is given; the clone makes a workspace (workspaces.py),
and is the one route here that names none.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from helpers import rules
from rcman.audit import COMMITTED_NOT_PUSHED, PULLED_NOT_PUSHED, Record, Recorder
from rcman.identity import Identity, current_user
from rcman.problems import Problem, ruled
from rcman.repository import MAX_FILES_PER_CALL, MAX_PATH_LENGTH, Repository
from rcman.routers.me import author_of
from rcman.routers.workspaces import WorkspaceName, person_of
from rcman.workspaces import answered

router = APIRouter(prefix="/api/git", tags=["git"])

CommitMessage = ruled(rules.COMMIT_MESSAGE, max_length=rules.MAX_COMMIT_MESSAGE_LENGTH)
BranchName = ruled(rules.BRANCH_NAME)
GitHubUrl = ruled(rules.GITHUB_URL)
FileName = Annotated[str, Field(max_length=MAX_PATH_LENGTH)]


class RepositoryBody(BaseModel):
    workspace: int = Field(ge=1, description="The workspace's id.")


class FilesBody(RepositoryBody):
    files: list[FileName] = Field(
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
    name: WorkspaceName | None = Field(
        default=None,
        description="The new workspace's name; the repository's name when it is left out.",
    )


WorkspaceQuery = Annotated[int, Query(ge=1, description="The workspace's id.")]


def _run(
    request: Request,
    identity: Identity,
    workspace: int,
    verb: Callable[[Repository], dict],
    record: Record | None = None,
    author: bool = False,
) -> dict:
    """``verb`` run on the repository of the person's ready workspace, which a change
    names in its audit ``record``; with ``author``, only once a commit author is set."""
    service = request.app.state.workspaces
    found = service.ready(identity.person, workspace)
    if record is not None:
        record.target = found.name
    if author and author_of(request, identity) is None:
        raise Problem("author_not_set")
    with answered():
        return verb(service.repository(found))


def _act(
    request: Request,
    record: Record,
    workspace: int,
    verb: Callable[[Repository], dict],
    author: bool = False,
) -> dict:
    return _run(request, record.identity, workspace, verb, record, author)


@router.get("/status")
def status(
    request: Request, workspace: WorkspaceQuery, identity: Identity = Depends(current_user)
) -> dict:
    """The branch, the staged and not staged files, and who commits are recorded as."""
    out = _run(request, identity, workspace, lambda git: git.status())
    author = author_of(request, identity)
    out["author"] = author and {"name": author.name, "email": author.email}
    return out


@router.get("/branches")
def branches(
    request: Request, workspace: WorkspaceQuery, identity: Identity = Depends(current_user)
) -> dict:
    return _run(request, identity, workspace, lambda git: git.branches())


@router.post("/stage")
def stage(
    body: FilesBody, request: Request, record: Record = Depends(Recorder("git.stage"))
) -> dict:
    return _act(request, record, body.workspace, lambda git: git.stage(body.files))


@router.post("/unstage")
def unstage(
    body: FilesBody, request: Request, record: Record = Depends(Recorder("git.unstage"))
) -> dict:
    return _act(request, record, body.workspace, lambda git: git.unstage(body.files))


@router.post("/commit")
def commit(
    body: CommitBody, request: Request, record: Record = Depends(Recorder("git.commit"))
) -> dict:
    """Commit what is staged as the person's commit author, refused until a name is set."""
    return _act(request, record, body.workspace, lambda git: git.commit(body.message), author=True)


@router.post("/switch")
def switch(
    body: SwitchBody, request: Request, record: Record = Depends(Recorder("git.switch"))
) -> dict:
    return _act(
        request, record, body.workspace, lambda git: git.switch(body.branch, remote=body.remote)
    )


@router.post("/branch")
def new_branch(
    body: NewBranchBody, request: Request, record: Record = Depends(Recorder("git.branch"))
) -> dict:
    return _act(request, record, body.workspace, lambda git: git.new_branch(body.name))


@router.post("/fetch")
def fetch(
    body: RepositoryBody, request: Request, record: Record = Depends(Recorder("git.fetch"))
) -> dict:
    """git fetch --prune: refreshes the remote-tracking branches, and nothing else."""
    return _act(request, record, body.workspace, lambda git: git.fetch())


@router.post("/push")
def push(
    body: RepositoryBody, request: Request, record: Record = Depends(Recorder("git.push"))
) -> dict:
    """Push the current branch; one with no upstream is pushed to origin and set to track it.

    Refused while no branch is checked out, for a branch with no upstream in a repository
    with no origin, for a push to GitHub until a token is stored, and before the branch's
    first commit.
    """
    return _act(request, record, body.workspace, lambda git: git.push())


@router.post("/sync")
def sync(
    body: RepositoryBody, request: Request, record: Record = Depends(Recorder("git.sync"))
) -> dict:
    """Bring the current branch up to date with its upstream, then push it, as Sync does
    elsewhere: git fetch --prune, a fast-forward where the branch is behind, and a push
    where its push destination lacks a commit of it; a branch with no upstream is published.

    Refused as a push is, before anything is fetched; where the upstream is gone; and where
    the branch and its upstream have diverged, which is left to a merge or a rebase. When
    something was pulled and the push fails, the answer says so with the push's problem,
    and the audit row says pulled_not_pushed.
    """
    answer = _act(request, record, body.workspace, lambda git: git.sync())
    if answer["push_problem"] is not None:
        record.result = PULLED_NOT_PUSHED
    return answer


@router.post("/commit-push")
def commit_push(
    body: CommitBody, request: Request, record: Record = Depends(Recorder("git.commit_push"))
) -> dict:
    """Commit what is staged, then push it.

    Nothing is committed when the push would be refused before it was tried: no branch
    checked out, nowhere to push, or a push to GitHub with no token. A branch with no
    commit yet is committed to and pushed. When the commit is made and the push fails,
    the answer is the commit and the push's problem, and the audit row says
    committed_not_pushed.
    """
    answer = _act(
        request, record, body.workspace, lambda git: git.commit_push(body.message), author=True
    )
    if answer["push_problem"] is not None:
        record.result = COMMITTED_NOT_PUSHED
    return answer


@router.post("/clone", status_code=202)
def clone(
    body: CloneBody, request: Request, record: Record = Depends(Recorder("git.clone"))
) -> dict:
    """Make a workspace by cloning a repository from GitHub into it: all its branches, the
    default one checked out. The workspace is made in the background and listed as
    creating until it is cloned; one that fails says why, and is deleted like any other,
    which also stops a clone that is running. The address is recorded as it is, which
    GITHUB_URL makes safe: it takes none with a user or a password in it."""
    record.target = body.url
    service = request.app.state.workspaces
    made = service.create(person_of(request, record.identity), body.name, body.url)
    return {"workspace": service.listing([made])[0]}
