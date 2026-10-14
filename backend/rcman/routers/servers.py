"""The server of each of the signed-in person's workspaces, and everyone's, for
administrators, to look at only."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from rcman import everyone as everyone_
from rcman.audit import UNRECOGNISED_SCREEN, Record, Recorder
from rcman.identity import Identity, admin_user, current_user
from rcman.problems import Problem
from rcman.routers.me import prefix_of
from rcman.workspaces import Workspace

router = APIRouter(prefix="/api/servers", tags=["servers"])


class WorkspaceBody(BaseModel):
    workspace: int = Field(ge=1, description="The workspace's id.")


class ConfirmBody(WorkspaceBody):
    answer: Literal["y", "n"] = Field(description="Answer to the folder-trust question.")


def _ready(request: Request, identity: Identity, workspace: int) -> Workspace:
    return request.app.state.workspaces.ready(identity.person, workspace)


def _watched(request: Request, identity: Identity, verb: str, workspace: Workspace, call: Callable):
    """``call``'s answer, shown to the watch (unrecognised.py) on its way out."""
    watch = request.app.state.watch
    try:
        out = call()
    except Problem as problem:
        watch.failed(identity.email, verb, workspace.name, problem.code, problem.extra)
        raise
    watch.answered(identity.email, verb, workspace.name)
    if isinstance(out.get("server"), dict):
        watch.seen(identity.email, verb, workspace.name, out["server"])
    return out


def _act(request: Request, record: Record, verb: str, workspace: int, call) -> dict:
    found = _ready(request, record.identity, workspace)
    record.target = found.name
    out = _watched(request, record.identity, verb, found, lambda: call(found))
    if (out.get("server") or {}).get("state") == "unrecognised":
        record.result = UNRECOGNISED_SCREEN  # administrators are warned; see routers/attention.py
    return out


@router.get("/everyone", dependencies=[Depends(admin_user)])
def everyone(request: Request) -> dict:
    """Every person's workspaces and servers, to look at only: administrators cannot act
    on them."""
    return {"people": everyone_.everyone(request)}


@router.get("/output")
def output(
    request: Request,
    workspace: Annotated[int, Query(ge=1, description="The workspace's id.")],
    identity: Identity = Depends(current_user),
) -> dict:
    found = _ready(request, identity, workspace)
    service = request.app.state.workspaces
    return _watched(request, identity, "output", found, lambda: service.output(found))


@router.post("")
def start(
    body: WorkspaceBody, request: Request, record: Record = Depends(Recorder("server.start"))
) -> dict:
    service = request.app.state.workspaces

    def start_it(found: Workspace) -> dict:
        return service.start_server(found, prefix_of(request, record.identity, found))

    return _act(request, record, "start", body.workspace, start_it)


@router.post("/resume")
def resume(
    body: WorkspaceBody, request: Request, record: Record = Depends(Recorder("server.resume"))
) -> dict:
    service = request.app.state.workspaces

    def resume_it(found: Workspace) -> dict:
        return service.start_server(found, prefix_of(request, record.identity, found), resume=True)

    return _act(request, record, "resume", body.workspace, resume_it)


@router.post("/stop")
def stop(
    body: WorkspaceBody, request: Request, record: Record = Depends(Recorder("server.stop"))
) -> dict:
    return _act(request, record, "stop", body.workspace, request.app.state.workspaces.stop_server)


@router.post("/forget")
def forget(
    body: WorkspaceBody, request: Request, record: Record = Depends(Recorder("server.forget"))
) -> dict:
    service = request.app.state.workspaces
    return _act(request, record, "forget", body.workspace, service.forget_server)


@router.post("/confirm")
def confirm(
    body: ConfirmBody, request: Request, record: Record = Depends(Recorder("server.confirm"))
) -> dict:
    service = request.app.state.workspaces
    return _act(
        request,
        record,
        "confirm",
        body.workspace,
        lambda found: service.confirm(found, body.answer),
    )
