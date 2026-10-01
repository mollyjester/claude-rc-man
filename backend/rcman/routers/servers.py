"""The signed-in user's Remote Control servers, through rc-helper."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from helpers import rc_helper
from rcman.audit import Record, Recorder
from rcman.helpers_client import call_helper
from rcman.identity import Identity, current_user
from rcman.routers.me import prefix_of

router = APIRouter(prefix="/api/servers", tags=["servers"])


class PathBody(BaseModel):
    path: str = Field(
        min_length=1,
        max_length=rc_helper.MAX_PATH_LENGTH,
        description="Relative to the home folder.",
    )


class ConfirmBody(PathBody):
    answer: Literal["y", "n"] = Field(description="Answer to the folder-trust question.")


def _helper(request: Request, record: Record, args: list[str]) -> dict:
    return call_helper(request.app.state.helper, record.identity, args)


@router.get("")
def list_servers(request: Request, identity: Identity = Depends(current_user)) -> dict:
    return call_helper(request.app.state.helper, identity, ["list"])


@router.get("/output")
def output(
    request: Request,
    path: str = Query(min_length=1, max_length=rc_helper.MAX_PATH_LENGTH),
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
