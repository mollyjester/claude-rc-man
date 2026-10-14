"""Adding and removing people, for administrators. rcman-admin (admin.py) does the same
from a shell."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field

from helpers import rules
from rcman import db, people
from rcman.audit import Record, Recorder
from rcman.identity import admin_user
from rcman.problems import Problem, ruled

router = APIRouter(prefix="/api/people", tags=["people"], dependencies=[Depends(admin_user)])
Email = ruled(rules.EMAIL)


class NewPerson(BaseModel):
    email: Email = Field(description="Their Google address.")
    admin: bool = Field(False, description="Make them an administrator.")


def shown(person: people.Person, workspaces: int) -> dict:
    return {
        "id": person.id,
        "email": person.email,
        "admin": person.admin,
        "state": person.state,
        "workspaces": workspaces,
    }


def add(settings, email: str, admin: bool) -> people.Person:
    """Add a person, and let them in at once."""
    con = db.connect(settings.database)
    try:
        person = people.add(con, email, admin)
        people.write_allowed(con, settings.allowed_emails)
    finally:
        con.close()
    return person


@router.get("")
def everyone(request: Request) -> dict:
    con = db.connect(request.app.state.settings.database)
    try:
        found = people.everyone(con)
    finally:
        con.close()
    counts: dict[int, int] = {}
    for workspace in request.app.state.workspaces.everything():
        counts[workspace.person] = counts.get(workspace.person, 0) + 1
    return {"people": [shown(p, counts.get(p.id, 0)) for p in found]}


@router.post("")
def create(
    body: NewPerson, request: Request, record: Record = Depends(Recorder("person.add"))
) -> dict:
    record.target = body.email.lower()
    return {"person": shown(add(request.app.state.settings, body.email, body.admin), 0)}


@router.delete("/{person}")
def delete(
    person: Annotated[int, Path(ge=1, description="The person's id.")],
    request: Request,
    record: Record = Depends(Recorder("person.delete")),
) -> dict:
    """Delete a person and every workspace of theirs. Anyone may be deleted, oneself and
    the last administrator too."""
    con = db.connect(request.app.state.settings.database)
    try:
        found = people.by_id(con, person)
    finally:
        con.close()
    if found is None:
        raise Problem("person_not_found")
    record.target = found.email
    request.app.state.workspaces.delete_person(found)
    return {"deleted": found.email}
