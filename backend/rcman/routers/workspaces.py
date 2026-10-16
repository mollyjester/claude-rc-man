"""The signed-in person's workspaces: listing, making and deleting them."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field

from helpers import rules
from rcman import db, people
from rcman.audit import Record, Recorder
from rcman.identity import Identity, current_user
from rcman.problems import ruled

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"])
WorkspaceName = ruled(rules.WORKSPACE_NAME)
WorkspaceId = Annotated[int, Path(ge=1, description="The workspace's id.")]


class NewWorkspace(BaseModel):
    name: WorkspaceName = Field(description="The workspace's name, which is yours alone.")


def listing(request: Request, identity: Identity) -> list[dict]:
    """The person's workspaces as the API shows them, each shown to the watch."""
    service = request.app.state.workspaces
    found = service.listing(service.of_person(identity.person))
    request.app.state.watch.listed(identity.email, {w["name"]: w["server"] for w in found})
    return found


@router.get("")
def workspaces(request: Request, identity: Identity = Depends(current_user)) -> dict:
    """The person's workspaces, and the Claude Code version a server started now would run
    (``claude_approved``, None where none is installed), to set beside the version each
    server runs."""
    return {
        "workspaces": listing(request, identity),
        "claude_approved": request.app.state.versions.approved(),
    }


def person_of(request: Request, identity: Identity) -> people.Person:
    """The signed-in person as the people table has them, which a workspace is made for."""
    con = db.connect(request.app.state.settings.database)
    try:
        return people.by_id(con, identity.person)
    finally:
        con.close()


@router.post("", status_code=202)
def create(
    body: NewWorkspace, request: Request, record: Record = Depends(Recorder("workspace.create"))
) -> dict:
    """Make a workspace; it is made in the background, and listed as creating until then."""
    record.target = body.name
    service = request.app.state.workspaces
    made = service.create(person_of(request, record.identity), body.name)
    return {"workspace": service.listing([made])[0]}


@router.delete("/{workspace}")
def delete(
    workspace: WorkspaceId,
    request: Request,
    record: Record = Depends(Recorder("workspace.delete")),
) -> dict:
    """Delete a workspace and everything in it, its server too."""
    service = request.app.state.workspaces
    found = service.owned(record.identity.person, workspace)
    record.target = found.name
    service.delete(found)
    return {"deleted": found.id}
