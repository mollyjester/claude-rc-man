"""Who is asking.

A person signs in with Google or, where an administrator turned it on, with Microsoft.

oauth2-proxy, behind Caddy's forward_auth, signs people in with Google and passes the address on
as ``X-Forwarded-Email``. Installed, rcman listens only on a unix socket only Caddy's group can
reach (see installer/install.py), and Caddy strips the header from every request and sets it
only from oauth2-proxy, so the header is trusted, and read first.

A Microsoft sign-in ends in rcman's own session cookie (microsoft.py), which Caddy passes to
rcman without asking oauth2-proxy. Without the header, the cookie is opened and its generation
checked against the settings' (microsoft_settings.py). Either way, the address must be an active
person's in the people table (people.py), read on every request. Where the address came from the
cookie, a refusal for a missing or unknown identity also deletes the cookie, so a stale
Microsoft session never keeps anyone from signing in with Google.

A request that changes something must also carry an ``Origin`` equal to the configured public
origin, and so must every upgrade to a WebSocket: a page of any site may open one, and the
browser sends rcman's cookies with it.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends
from starlette.requests import HTTPConnection

from rcman import db, microsoft, people
from rcman.problems import Problem

EMAIL_HEADER = "x-forwarded-email"
CHANGING = {"POST", "PUT", "PATCH", "DELETE"}
# The routes that change something although they are a GET, which carries no Origin, each with
# what stands for the Origin there; the reasons are in project-knowledge/backend.md.
NAVIGATIONS = {
    ("GET", "/api/forward/hand-off"): (
        "a navigation from a forwarded port's host, which sends no Origin: refused unless the "
        "browser says it loads a document, and it hands out a way in only to the person's own port"
    ),
}


@dataclass(frozen=True)
class Identity:
    email: str
    admin: bool
    person: int
    slot: int


def changing(connection: HTTPConnection) -> bool:
    """Whether the request changes something: one of CHANGING, or a WebSocket."""
    return connection.scope["type"] == "websocket" or connection.scope.get("method") in CHANGING


def _session_email(connection: HTTPConnection) -> str | None:
    """The address the Microsoft session cookie names, None where there is no such cookie;
    ``not_authenticated``, deleting it, where it is no valid session now."""
    value = connection.cookies.get(microsoft.SESSION_COOKIE)
    if not value:
        return None
    state = connection.app.state
    email = microsoft.session_email(state.sealer, state.microsoft_settings, value)
    if email is None:
        raise _refused_session("not_authenticated")
    return email


def _refused_session(code: str) -> Problem:
    return Problem(code, headers={"set-cookie": microsoft.deletion(microsoft.SESSION_COOKIE)})


def current_user(connection: HTTPConnection) -> Identity:
    """Who is asking, for a request or a WebSocket's upgrade alike."""
    email = connection.headers.get(EMAIL_HEADER)
    from_session = False
    if not email:
        email = _session_email(connection)
        from_session = email is not None
    if not email:
        raise Problem("not_authenticated")
    con = db.connect(connection.app.state.settings.database)
    try:
        person = people.by_email(con, email)
    finally:
        con.close()
    if person is None or person.state != people.ACTIVE:
        raise _refused_session("not_allowed") if from_session else Problem("not_allowed")
    if (
        changing(connection)
        and connection.headers.get("origin") != connection.app.state.settings.public_origin
    ):
        raise Problem("origin_refused")
    return Identity(email=person.email, admin=person.admin, person=person.id, slot=person.slot)


def admin_user(identity: Identity = Depends(current_user)) -> Identity:
    if not identity.admin:
        raise Problem("admin_only")
    return identity
