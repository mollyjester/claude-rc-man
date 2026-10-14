"""The people who may use rcman: one row each in ``people``, and the allowed list.

A person is an address, whether they are an administrator, and a slot: the uid
range their workspaces run in (runtime.RANGE ids from slot × RANGE, runtime.py), their own
and never anyone else's. oauth2-proxy lets in only the addresses in the
allowed list, which is written from the table, whole and by rename, on every
change and whenever rcman starts; rcman itself checks every request against
the table (identity.py).
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from helpers import rules
from rcman.problems import Problem

# How many people rcman has room for: the slots 1 to MAX_PEOPLE. The installer gives
# rcman enough subordinate ids for every slot (installer/install.py, SUBORDINATE_COUNT).
MAX_PEOPLE = 64
ACTIVE, DELETING = "active", "deleting"
ALLOWED_MODE = 0o640  # oauth2-proxy reads it through rcman's group


@dataclass(frozen=True)
class Person:
    id: int
    email: str
    admin: bool
    slot: int
    state: str


def _person(row: sqlite3.Row | None) -> Person | None:
    if row is None:
        return None
    return Person(row["id"], row["email"], bool(row["admin"]), row["slot"], row["state"])


def everyone(con: sqlite3.Connection) -> list[Person]:
    rows = con.execute("select * from people order by email").fetchall()
    return [_person(row) for row in rows]


def by_email(con: sqlite3.Connection, email: str) -> Person | None:
    return _person(con.execute("select * from people where email = ?", (email,)).fetchone())


def by_id(con: sqlite3.Connection, person_id: int) -> Person | None:
    return _person(con.execute("select * from people where id = ?", (person_id,)).fetchone())


def add(con: sqlite3.Connection, email: str, admin: bool) -> Person:
    """Add a person, in the lowest free slot."""
    if not rules.EMAIL.fullmatch(email):
        raise Problem("email_invalid")
    email = email.lower()
    if by_email(con, email) is not None:
        raise Problem("email_taken")
    taken = {row[0] for row in con.execute("select slot from people")}
    slot = next((s for s in range(1, MAX_PEOPLE + 1) if s not in taken), None)
    if slot is None:
        raise Problem("people_full")
    try:
        con.execute(
            "insert into people (email, admin, slot, state) values (?, ?, ?, ?)",
            (email, int(admin), slot, ACTIVE),
        )
        con.commit()
    except sqlite3.IntegrityError:  # someone else took the address or the slot meanwhile
        con.rollback()
        raise Problem("email_taken") from None
    return by_email(con, email)


def mark_deleting(con: sqlite3.Connection, person: Person) -> None:
    """From now on the person is let in nowhere, while their workspaces go."""
    con.execute("update people set state = ? where id = ?", (DELETING, person.id))
    con.commit()


def remove(con: sqlite3.Connection, person: Person) -> None:
    """The person's row and settings; their workspaces must be gone first."""
    con.execute("delete from user_settings where email = ?", (person.email,))
    con.execute("delete from people where id = ?", (person.id,))
    con.commit()


def write_allowed(con: sqlite3.Connection, path: Path) -> None:
    """The allowed list: every active person's address, one a line, replaced whole."""
    emails = [p.email for p in everyone(con) if p.state == ACTIVE]
    text = "".join(f"{email}\n" for email in sorted(emails))
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, ALLOWED_MODE)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
