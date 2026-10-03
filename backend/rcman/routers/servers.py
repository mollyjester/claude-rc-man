"""The signed-in user's Remote Control servers, through rc-helper."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from rcman import everyone as everyone_
from rcman.audit import UNRECOGNISED_SCREEN, Record, Recorder
from rcman.helpers_client import call_helper
from rcman.identity import Identity, admin_user, current_user
from rcman.routers.folders import FolderPath
from rcman.routers.me import prefix_of

router = APIRouter(prefix="/api/servers", tags=["servers"])


class PathBody(BaseModel):
    path: FolderPath = Field(description="Relative to the home folder.")


class ConfirmBody(PathBody):
    answer: Literal["y", "n"] = Field(description="Answer to the folder-trust question.")


def _helper(request: Request, record: Record, args: list[str]) -> dict:
    out = call_helper(request.app.state.helper, record.identity, args)
    if (out.get("server") or {}).get("state") == "unrecognised":
        record.result = UNRECOGNISED_SCREEN  # administrators are warned; see routers/attention.py
    return out


@router.get("")
def list_servers(request: Request, identity: Identity = Depends(current_user)) -> dict:
    return call_helper(request.app.state.helper, identity, ["list"])


@router.get("/everyone", dependencies=[Depends(admin_user)])
def everyone(request: Request) -> dict:
    """Every person's servers, to look at only: administrators cannot act on them."""
    return {"people": everyone_.everyone(request)}


@router.get("/output")
def output(
    request: Request,
    path: Annotated[FolderPath, Query()],
    identity: Identity = Depends(current_user),
) -> dict:
    return call_helper(request.app.state.helper, identity, ["output", "--", path])


@router.post("")
def start(
    body: PathBody, request: Request, record: Record = Depends(Recorder("server.start"))
) -> dict:
    record.target = body.path
    prefix = prefix_of(request, record.identity)
    return _helper(request, record, ["start", f"--prefix={prefix}", "--", body.path])


@router.post("/resume")
def resume(
    body: PathBody, request: Request, record: Record = Depends(Recorder("server.resume"))
) -> dict:
    record.target = body.path
    prefix = prefix_of(request, record.identity)
    return _helper(request, record, ["resume", f"--prefix={prefix}", "--", body.path])


@router.post("/stop")
def stop(
    body: PathBody, request: Request, record: Record = Depends(Recorder("server.stop"))
) -> dict:
    record.target = body.path
    return _helper(request, record, ["stop", "--", body.path])


@router.post("/forget")
def forget(
    body: PathBody, request: Request, record: Record = Depends(Recorder("server.forget"))
) -> dict:
    record.target = body.path
    return _helper(request, record, ["forget", "--", body.path])


@router.post("/confirm")
def confirm(
    body: ConfirmBody, request: Request, record: Record = Depends(Recorder("server.confirm"))
) -> dict:
    record.target = body.path
    return _helper(request, record, ["confirm", f"--answer={body.answer}", "--", body.path])
