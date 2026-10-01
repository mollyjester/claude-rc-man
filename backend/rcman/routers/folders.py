"""Folders under the signed-in user's home, through rc-helper."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from helpers import rc_helper
from rcman.helpers_client import call_helper
from rcman.identity import Identity, current_user

router = APIRouter(prefix="/api/folders", tags=["folders"])


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
