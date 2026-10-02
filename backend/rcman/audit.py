"""The audit trail: one row per request that changes something.

A route is audited by depending on :class:`Recorder`, which writes the row
when the request finishes — ``ok`` or the error code it failed with — and logs
the same event as a JSON line on standard error. ``tests/test_audit.py`` holds every changing
route to it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import sys
from dataclasses import dataclass, field

from fastapi import Depends, Request
from fastapi.routing import APIRoute

from rcman import db
from rcman.identity import Identity, current_user
from rcman.problems import Problem

log = logging.getLogger("rcman.audit")
log.setLevel(logging.INFO)


def install_logging() -> None:
    """Send audit events to standard error, one JSON line each, which systemd sends to the journal.

    uvicorn configures only its own loggers, so without this the events go nowhere.
    """
    if not log.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        log.addHandler(handler)


# (path, method): reason. A changing route listed here records nothing; its
# reason also appears in docs/audit.md.
EXEMPT: dict[tuple[str, ...], str] = {}


@dataclass
class Record:
    action: str
    identity: Identity
    target: str | None = None
    extra: dict = field(default_factory=dict)


def write(con: sqlite3.Connection, record: Record, result: str) -> None:
    event = {
        "at": dt.datetime.now(dt.UTC).isoformat(),
        "email": record.identity.email,
        "linux": record.identity.linux,
        "action": record.action,
        "target": record.target,
        "result": result,
    }
    con.execute(
        "insert into audit (at, email, linux, action, target, result) values (?, ?, ?, ?, ?, ?)",
        tuple(event.values()),
    )
    con.commit()
    log.info(json.dumps(event))


class Recorder:
    """A dependency that audits the request it is part of as ``action``."""

    def __init__(self, action: str):
        self.action = action

    def __call__(self, request: Request, identity: Identity = Depends(current_user)):
        record = Record(self.action, identity)
        con = db.connect(request.app.state.settings.database)
        try:
            try:
                yield record
            except Problem as error:
                write(con, record, error.code)
                raise
            except Exception:
                write(con, record, "internal_error")
                raise
            else:
                write(con, record, "ok")
        finally:
            con.close()


def action_of(route: APIRoute) -> str | None:
    """The audit action a route records, found in its dependency tree."""
    pending = list(route.dependant.dependencies)
    while pending:
        dependant = pending.pop()
        if isinstance(dependant.call, Recorder):
            return dependant.call.action
        pending.extend(dependant.dependencies)
    return None


def apply_retention(con: sqlite3.Connection, now: dt.datetime, days: int, max_rows: int) -> int:
    """Delete events older than ``days``, then all but the newest ``max_rows``."""
    cutoff = (now - dt.timedelta(days=days)).isoformat()
    deleted = con.execute("delete from audit where at < ?", (cutoff,)).rowcount
    deleted += con.execute(
        "delete from audit where id not in "
        "(select id from audit order by at desc, id desc limit ?)",
        (max_rows,),
    ).rowcount
    con.commit()
    return deleted
