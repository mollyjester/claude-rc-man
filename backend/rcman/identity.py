"""Who is asking.

oauth2-proxy, behind Caddy's forward_auth, signs people in with Google and
passes the address on as ``X-Forwarded-Email``. Installed, rcman listens only
on a unix socket only Caddy's group can reach (see installer/install.py), so
the header is trusted; the address must be in the users file,
read on every request. A request that changes something must also carry an
``Origin`` equal to the configured public origin.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Depends, Request

from helpers import users_file as uf
from rcman.problems import Problem

EMAIL_HEADER = "x-forwarded-email"
CHANGING = {"POST", "PUT", "PATCH", "DELETE"}


@dataclass(frozen=True)
class Identity:
    email: str
    linux: str
    admin: bool


def current_user(request: Request) -> Identity:
    email = request.headers.get(EMAIL_HEADER)
    if not email:
        raise Problem("not_authenticated")
    try:
        users = uf.load(request.app.state.settings.users_file)
    except uf.UsersFileError:
        raise Problem("users_file_invalid") from None
    user = uf.by_email(users, email)
    if user is None:
        raise Problem("not_allowed")
    if (
        request.method in CHANGING
        and request.headers.get("origin") != request.app.state.settings.public_origin
    ):
        raise Problem("origin_refused")
    return Identity(email=user.email, linux=user.linux, admin=user.admin)


def admin_user(identity: Identity = Depends(current_user)) -> Identity:
    if not identity.admin:
        raise Problem("admin_only")
    return identity
