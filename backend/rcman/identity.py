"""Who is asking.

oauth2-proxy, behind Caddy's forward_auth, signs people in with Google and
passes the address on as ``X-Forwarded-Email``. Installed, rcman listens only
on a unix socket only Caddy's group can reach (see installer/install.py), so
the header is trusted; the address must be an active person's in the people
table (people.py), read on every request. A request that changes something must
also carry an ``Origin`` equal to the configured public origin.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request

from rcman import db, people
from rcman.problems import Problem

EMAIL_HEADER = "x-forwarded-email"
CHANGING = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True)
class Identity:
    email: str
    admin: bool
    person: int
    slot: int


def current_user(request: Request) -> Identity:
    email = request.headers.get(EMAIL_HEADER)
    if not email:
        raise Problem("not_authenticated")
    con = db.connect(request.app.state.settings.database)
    try:
        person = people.by_email(con, email)
    finally:
        con.close()
    if person is None or person.state != people.ACTIVE:
        raise Problem("not_allowed")
    if (
        request.method in CHANGING
        and request.headers.get("origin") != request.app.state.settings.public_origin
    ):
        raise Problem("origin_refused")
    return Identity(email=person.email, admin=person.admin, person=person.id, slot=person.slot)


def admin_user(identity: Identity = Depends(current_user)) -> Identity:
    if not identity.admin:
        raise Problem("admin_only")
    return identity
