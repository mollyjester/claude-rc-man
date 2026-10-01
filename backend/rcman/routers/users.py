"""Adding and removing people, for administrators, through rc-admin."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, Field

from helpers import users_file as uf
from rcman.audit import Record, Recorder
from rcman.helpers_client import call_admin
from rcman.identity import Identity, admin_user

router = APIRouter(prefix="/api/users", tags=["users"], dependencies=[Depends(admin_user)])


class NewUser(BaseModel):
    linux: str = Field(pattern=f"^{uf.LINUX_NAME.pattern}$", description="Linux user name.")
    email: str = Field(pattern=f"^{uf.EMAIL.pattern}$", description="Google email address.")
    admin: bool = Field(False, description="Make them an administrator.")
    adopt: bool = Field(False, description="Take over an existing Linux account.")


@router.get("")
def list_users(request: Request, identity: Identity = Depends(admin_user)) -> dict:
    return call_admin(request.app.state.admin, ["list"])


@router.post("")
def add_user(
    body: NewUser, request: Request, record: Record = Depends(Recorder("user.add"))
) -> dict:
    record.target = body.linux
    flags = [*(["--admin"] if body.admin else []), *(["--adopt"] if body.adopt else [])]
    return call_admin(request.app.state.admin, ["add", body.linux, body.email, *flags])


@router.delete("/{linux}")
def delete_user(
    request: Request,
    linux: str = Path(pattern=f"^{uf.LINUX_NAME.pattern}$"),
    home: Literal["delete", "keep"] = Query(description="Delete or keep their home folder."),
    record: Record = Depends(Recorder("user.delete")),
) -> dict:
    record.target = linux
    decision = "--delete" if home == "delete" else "--keep-home"
    return call_admin(
        request.app.state.admin, ["del", linux, decision, "--requester", record.identity.email]
    )
