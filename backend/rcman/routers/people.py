"""Adding and removing people, and the limits on what they and their workspaces may use, for
administrators. rcman-admin (admin.py) does the same from a shell."""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request
from pydantic import AfterValidator, BaseModel, Field
from pydantic_core import PydanticCustomError

from helpers import rules
from rcman import db, limits, people
from rcman.audit import Record, Recorder
from rcman.identity import admin_user
from rcman.problems import RULE_REFUSED, Problem, ruled

router = APIRouter(prefix="/api/people", tags=["people"], dependencies=[Depends(admin_user)])
Email = ruled(rules.EMAIL)
PersonId = Annotated[int, Path(ge=1, description="The person's id.")]
WorkspaceId = Annotated[int, Path(ge=1, description="The workspace's id.")]


def _atleast_minimum(parse: Callable[[str], int]) -> AfterValidator:
    """A rule's shape is checked by the type; what a regular expression cannot hold, the
    minimum, is checked by limits.py, and refused in the rule's words as well."""

    def check(text: str) -> str:
        try:
            parse(text)
        except limits.Refused as refused:
            raise PydanticCustomError(
                RULE_REFUSED, "{sentence}", {"sentence": str(refused)}
            ) from None
        return text

    return AfterValidator(check)


Memory = Annotated[ruled(rules.MEMORY), _atleast_minimum(limits.parse_memory)]
Cpu = Annotated[ruled(rules.CPU), _atleast_minimum(limits.parse_cpu)]
Pids = Annotated[ruled(rules.PIDS), _atleast_minimum(limits.parse_pids)]


class NewPerson(BaseModel):
    email: Email = Field(description="Their Google address.")
    admin: bool = Field(False, description="Make them an administrator.")


class LimitsBody(BaseModel):
    """The whole set of limits: one left null is no limit, and is taken away if there was one."""

    memory: Memory | None = Field(
        description="Memory: a whole number and M for mebibytes or G for gibibytes. Null for no "
        "limit."
    )
    cpu: Cpu | None = Field(
        description="CPU: cores, with up to two decimals, of the machine's. Null for no limit."
    )
    pids: Pids | None = Field(
        description="Processes: a whole number; threads count as processes. Null for no limit."
    )

    def held(self) -> limits.Limits:
        return limits.parse(self.memory, self.cpu, self.pids)


def shown(person: people.Person, workspaces: int) -> dict:
    return {
        "id": person.id,
        "email": person.email,
        "admin": person.admin,
        "state": person.state,
        "workspaces": workspaces,
        "limits": person.limits.text(),
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


def found_person(request: Request, person: int) -> people.Person:
    con = db.connect(request.app.state.settings.database)
    try:
        found = people.by_id(con, person)
    finally:
        con.close()
    if found is None:
        raise Problem("person_not_found")
    return found


@router.get("/{person}/limits")
def limits_of(person: PersonId, request: Request) -> dict:
    """The person's limits and what their workspaces use together, and each workspace's own:
    ``person`` and ``workspaces``, each with ``limits`` (``memory``, ``cpu`` and ``pids`` as
    text, null for no limit) and ``usage`` (``memory``, ``memory_bytes`` and ``tasks``; null
    where it could not be read)."""
    return request.app.state.workspaces.limits_report(found_person(request, person))


@router.put("/{person}/limits")
def set_limits(
    person: PersonId,
    body: LimitsBody,
    request: Request,
    record: Record = Depends(Recorder("person.limits")),
) -> dict:
    """Set the limits on what the person's workspaces may use together. A memory or process
    limit below what is in use now is refused, so that no running work is ended."""
    found = found_person(request, person)
    held = body.held()
    record.target = f"{found.email} {held.describe()}"
    request.app.state.workspaces.set_limits(found, None, held)
    return {"limits": held.text()}


@router.put("/{person}/workspaces/{workspace}/limits")
def set_workspace_limits(
    person: PersonId,
    workspace: WorkspaceId,
    body: LimitsBody,
    request: Request,
    record: Record = Depends(Recorder("workspace.limits")),
) -> dict:
    """Set the limits on one of the person's workspaces, which the person's own limits hold it
    to besides. A workspace that is not ready can be given limits. A memory or process limit
    below what the workspace uses now is refused, so that no running work is ended."""
    found = found_person(request, person)
    owned = request.app.state.workspaces.owned(found.id, workspace)
    held = body.held()
    record.target = f"{found.email}/{owned.name} {held.describe()}"
    request.app.state.workspaces.set_limits(found, owned.id, held)
    return {"limits": held.text()}
