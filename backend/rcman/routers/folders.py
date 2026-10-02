"""Folders under the signed-in user's home, through rc-helper."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from helpers import rc_helper
from rcman.audit import Record, Recorder
from rcman.helpers_client import FOLDER_DELETE_TIMEOUT_SECONDS, call_helper
from rcman.identity import Identity, current_user
from rcman.problems import ruled

router = APIRouter(prefix="/api/folders", tags=["folders"])
FolderName = ruled(rc_helper.FOLDER_NAME, min_length=1, max_length=rc_helper.MAX_PATH_LENGTH)


class NewFolder(BaseModel):
    parent: str = Field(
        default="",
        max_length=rc_helper.MAX_PATH_LENGTH,
        description="The folder to make it in, relative to the home folder; empty for the home.",
    )
    name: FolderName = Field(description="The new folder's name.")


@router.get("")
def folders(
    request: Request,
    path: str = Query(
        "", max_length=rc_helper.MAX_PATH_LENGTH, description="Relative to the home folder."
    ),
    depth: int = Query(1, ge=1, le=rc_helper.MAX_TREE_DEPTH),
    all: bool = Query(False, description="Include dot-folders."),
    identity: Identity = Depends(current_user),
) -> dict:
    args = [
        "tree",
        f"--depth={depth}",
        *(["--all"] if all else []),
        "--",
        *([path] if path else []),
    ]
    return call_helper(request.app.state.helper, identity, args)


@router.post("")
def create(
    body: NewFolder, request: Request, record: Record = Depends(Recorder("folder.create"))
) -> dict:
    record.target = f"{body.parent}/{body.name}" if body.parent else body.name
    args = ["mkdir", f"--name={body.name}", "--", *([body.parent] if body.parent else [])]
    return call_helper(request.app.state.helper, record.identity, args)


@router.delete("")
def delete(
    request: Request,
    path: str = Query(
        min_length=1,
        max_length=rc_helper.MAX_PATH_LENGTH,
        description="Relative to the home folder.",
    ),
    record: Record = Depends(Recorder("folder.delete")),
) -> dict:
    """Stop and forget every server in the folder or below it, then delete it."""
    record.target = path
    return call_helper(
        request.app.state.helper,
        record.identity,
        ["delete", "--", path],
        timeout=FOLDER_DELETE_TIMEOUT_SECONDS,
    )
