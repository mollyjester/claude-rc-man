"""The people who may use rcman: one row each in ``people``, and the allowed list.

A person is an address, whether they are an administrator, and a slot: the uid
range their workspaces run in (runtime.RANGE ids from slot × RANGE, runtime.py), their own
and never anyone else's. oauth2-proxy lets in only the addresses in the
allowed list, which is written from the table, whole and by rename, on every
change and whenever rcman starts; rcman itself checks every request against
the table (identity.py).
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from helpers import rules
from rcman.limits import Limits
from rcman.problems import Problem

# How many people rcman has room for: the slots 1 to MAX_PEOPLE. The installer gives
# rcman enough subordinate ids for every slot (installer/install.py, SUBORDINATE_COUNT).
MAX_PEOPLE = 64
# The slot of the devcontainer builder's uid range, above every person's and never given to
# one: a build that broke out of its sandbox owns nothing of anyone's (runtime.py, builder.py).
# The installer gives rcman subordinate ids for it as well.
BUILDER_SLOT = MAX_PEOPLE + 1
ACTIVE, DELETING = "active", "deleting"
ALLOWED_MODE = 0o640  # oauth2-proxy reads it through rcman's group


@dataclass(frozen=True)
class Person:
    id: int
    email: str
    admin: bool
    slot: int
    state: str
    # What their workspaces together may use (limits.py); no limit where a value is None.
    limits: Limits = Limits()


def _person(row: sqlite3.Row | None) -> Person | None:
    if row is None:
        return None
    return Person(
        row["id"],
        row["email"],
        bool(row["admin"]),
        row["slot"],
        row["state"],
        Limits(row["memory_mib"], row["cpu_percent"], row["pids"]),
    )


def everyone(con: sqlite3.Connection) -> list[Person]:
    rows = con.execute("select * from people order by email").fetchall()
    return [_person(row) for row in rows]


def by_email(con: sqlite3.Connection, email: str) -> Person | None:
    return _person(con.execute("select * from people where email = ?", (email,)).fetchone())


def by_id(con: sqlite3.Connection, person_id: int) -> Person | None:
    return _person(con.execute("select * from people where id = ?", (person_id,)).fetchone())


def add(
    con: sqlite3.Connection, email: str, admin: bool, *, last_slot: int | None = None
) -> Person:
    """Add a person, in the lowest free slot from 1 to ``last_slot``, MAX_PEOPLE unless given:
    below the slot the service's builder has (Workspaces.builder_slot)."""
    if not rules.EMAIL.fullmatch(email):
        raise Problem("email_invalid")
    email = email.lower()
    if by_email(con, email) is not None:
        raise Problem("email_taken")
    taken = {row[0] for row in con.execute("select slot from people")}
    last = MAX_PEOPLE if last_slot is None else last_slot
    slot = next((s for s in range(1, last + 1) if s not in taken), None)
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


def set_limits(con: sqlite3.Connection, person_id: int, held: Limits) -> None:
    """Record the limits on the person's workspaces together; applying them is the runtime's."""
    con.execute(
        "update people set memory_mib = ?, cpu_percent = ?, pids = ? where id = ?",
        (held.memory_mib, held.cpu_percent, held.pids, person_id),
    )
    con.commit()


def mark_deleting(con: sqlite3.Connection, person: Person) -> None:
    """From now on the person is let in nowhere, while their workspaces go."""
    con.execute("update people set state = ? where id = ?", (DELETING, person.id))
    con.commit()


def remove(con: sqlite3.Connection, person: Person) -> None:
    """The person's row and settings; their workspaces must be gone first."""
    con.execute("delete from user_settings where email = ?", (person.email,))
    con.execute("delete from people where id = ?", (person.id,))
    con.commit()


def claude_account(con: sqlite3.Connection, person_id: int) -> tuple[dict | None, bool]:
    """The person's Claude account as their last sign-in named it, or None; and whether
    a sign-out of it waits for their running servers."""
    row = con.execute(
        "select claude_account, claude_signing_out from people where id = ?", (person_id,)
    ).fetchone()
    if row is None:
        return None, False
    found = json.loads(row[0]) if row[0] else None
    return found, bool(row[1])


def set_claude_account(
    con: sqlite3.Connection, person_id: int, account: dict | None, signing_out: bool = False
) -> None:
    con.execute(
        "update people set claude_account = ?, claude_signing_out = ? where id = ?",
        (None if account is None else json.dumps(account), int(signing_out), person_id),
    )
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
