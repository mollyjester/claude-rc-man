"""Adding and removing people, for administrators, through rc-admin."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Path, Query, Request
from pydantic import BaseModel, Field

from helpers import users_file as uf
from rcman.audit import Record, Recorder
from rcman.helpers_client import call_admin
from rcman.identity import Identity, admin_user
from rcman.problems import ruled

router = APIRouter(prefix="/api/users", tags=["users"], dependencies=[Depends(admin_user)])
LinuxName = ruled(uf.LINUX_NAME)
Email = ruled(uf.EMAIL)


class NewUser(BaseModel):
    linux: LinuxName = Field(description="Linux user name.")
    email: Email = Field(description="Google email address.")
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
    return call_admin(request.app.state.admin, ["add", *flags, "--", body.linux, body.email])


@router.delete("/{linux}")
def delete_user(
    request: Request,
    linux: Annotated[LinuxName, Path()],
    home: Literal["delete", "keep"] = Query(description="Delete or keep their home folder."),
    record: Record = Depends(Recorder("user.delete")),
) -> dict:
    record.target = linux
    decision = "--delete" if home == "delete" else "--keep-home"
    return call_admin(
        request.app.state.admin,
        ["del", decision, f"--requester={record.identity.email}", "--", linux],
    )
