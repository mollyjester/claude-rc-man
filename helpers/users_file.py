"""users.toml: the one list of people who may use rcman.

Each entry maps a Google email to a Linux user and says whether that person is
an administrator. The oauth2-proxy email allowlist is generated from it and
nothing else. Python standard library only.
"""

from __future__ import annotations

import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

LINUX_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
FIELDS = {"email", "linux", "admin"}
HEADER = "# The people who may use rcman. Managed by rcman-user and the admin page.\n"


class UsersFileError(ValueError):
    pass


@dataclass(frozen=True)
class User:
    email: str
    linux: str
    admin: bool


def valid_linux(name: str) -> bool:
    return bool(LINUX_NAME.fullmatch(name))


def valid_email(email: str) -> bool:
    return bool(EMAIL.fullmatch(email))


def _validated(users: list[User]) -> list[User]:
    seen_email, seen_linux = set(), set()
    out = []
    for user in users:
        if not isinstance(user.admin, bool):
            raise UsersFileError(f"{user.linux}: admin must be true or false")
        if not isinstance(user.email, str) or not valid_email(user.email):
            raise UsersFileError(f"invalid email: {user.email!r}")
        if not isinstance(user.linux, str) or not valid_linux(user.linux):
            raise UsersFileError(f"invalid linux user name: {user.linux!r}")
        email = user.email.lower()
        if email in seen_email:
            raise UsersFileError(f"duplicate email: {email}")
        if user.linux in seen_linux:
            raise UsersFileError(f"duplicate linux user: {user.linux}")
        seen_email.add(email)
        seen_linux.add(user.linux)
        out.append(User(email=email, linux=user.linux, admin=user.admin))
    return sorted(out, key=lambda u: u.linux)


def load(path: Path) -> list[User]:
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as error:
        raise UsersFileError(f"cannot parse {path}: {error}") from None
    entries = data.get("user", [])
    if set(data) - {"user"} or not isinstance(entries, list):
        raise UsersFileError(f"{path}: only [[user]] entries are allowed")
    users = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise UsersFileError(f"{path}: every [[user]] entry is a table")
        extra, missing = set(entry) - FIELDS, FIELDS - set(entry)
        if extra:
            raise UsersFileError(f"unknown field(s): extra {', '.join(sorted(extra))}")
        if missing:
            raise UsersFileError(f"missing field(s): {', '.join(sorted(missing))}")
        users.append(User(email=entry["email"], linux=entry["linux"], admin=entry["admin"]))
    return _validated(users)


def render(users: list[User]) -> str:
    blocks = [
        f'[[user]]\nemail = "{u.email}"\nlinux = "{u.linux}"\n'
        f"admin = {'true' if u.admin else 'false'}\n"
        for u in _validated(users)
    ]
    return HEADER + "".join("\n" + block for block in blocks)


def save(path: Path, users: list[User], mode: int = 0o640, gid: int | None = None) -> None:
    write_atomic(path, render(users), mode=mode, gid=gid)


def allowlist(users: list[User]) -> str:
    return "".join(f"{u.email}\n" for u in _validated(users))


def write_atomic(path: Path, text: str, mode: int = 0o640, gid: int | None = None) -> None:
    """Replace ``path`` with ``text`` in one step, with the given mode and group."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        if gid is not None:
            os.chown(tmp, -1, gid)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def by_email(users: list[User], email: str) -> User | None:
    return next((u for u in users if u.email == email.lower()), None)


def by_linux(users: list[User], linux: str) -> User | None:
    return next((u for u in users if u.linux == linux), None)
