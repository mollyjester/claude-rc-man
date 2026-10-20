"""The audit trail: one row per request that changes something, and two per WebSocket.

A route is audited by depending on :class:`Recorder`, which writes the row
when the request finishes — ``ok`` or the error code it failed with — and logs
the same event as a JSON line on standard error (``journal.install``).
A WebSocket route depends on :class:`Session` instead, which records when the
socket opens and when it closes, as ``<action>.open`` and ``<action>.close``.
``tests/test_audit.py`` holds every changing route and every WebSocket route to them.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
from dataclasses import dataclass, field

from fastapi import Depends, Request
from starlette.requests import HTTPConnection

from rcman import db
from rcman.identity import Identity, current_user
from rcman.problems import code_of

log = logging.getLogger("rcman.audit")
log.setLevel(logging.INFO)


# (path, method): reason. A changing route listed here records nothing; its
# reason also appears in docs/audit.md.
EXEMPT: dict[tuple[str, ...], str] = {
    ("/api/client-errors", "POST"): (
        "changes nothing: a page's report of a failure rcman could not see, "
        "written to the journal only"
    ),
}

# The results an event can have besides the code of the error the request failed
# with; docs/audit.md lists each.
OK = "ok"
UNRECOGNISED_SCREEN = "unrecognised_screen"  # it worked, but ended on a screen rcman does not know
COMMITTED_NOT_PUSHED = "committed_not_pushed"  # Commit & Push: the commit was made, the push failed
PULLED_NOT_PUSHED = "pulled_not_pushed"  # Sync: what was pulled is in, the push failed
# How a terminal (routers/terminal.py) ended, besides ``ok``, its shell ending by itself:
TERMINAL_LOST = "terminal_lost"  # its page closed the socket, or vanished
TERMINAL_REPLACED = "terminal_replaced"  # a terminal opened on the same workspace replaced it
WORKSPACE_GONE = "workspace_gone"  # the workspace was deleted, or is no longer ready
WORKSPACE_REBUILDING = "workspace_rebuilding"  # its container is being made again by a rebuild
SIGNED_OUT = "signed_out"  # its person signed out of rcman
SERVICE_CLOSING = "service_closing"  # rcman stopped
RESULTS = (
    OK,
    UNRECOGNISED_SCREEN,
    COMMITTED_NOT_PUSHED,
    PULLED_NOT_PUSHED,
    TERMINAL_LOST,
    TERMINAL_REPLACED,
    WORKSPACE_GONE,
    WORKSPACE_REBUILDING,
    SIGNED_OUT,
    SERVICE_CLOSING,
)


@dataclass
class Record:
    action: str
    identity: Identity
    target: str | None = None
    extra: dict = field(default_factory=dict)
    result: str | None = None  # what to record when the request succeeds; ``ok`` when unset


def write(con: sqlite3.Connection, record: Record, result: str) -> None:
    event = {
        "at": dt.datetime.now(dt.UTC).isoformat(),
        "email": record.identity.email,
        "action": record.action,
        "target": record.target,
        "result": result,
    }
    con.execute(
        "insert into audit (at, email, action, target, result) values (?, ?, ?, ?, ?)",
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
            except Exception as error:
                write(con, record, code_of(error))
                raise
            else:
                write(con, record, record.result or OK)
        finally:
            con.close()


class Opened:
    """What a WebSocket's :class:`Session` records: its open and its close, each once."""

    def __init__(self, action: str, identity: Identity, database):
        self.action = action
        self.identity = identity
        self.database = database
        self.target: str | None = None
        self.open: str | None = None  # the result recorded for the open, once it is
        self.close: str | None = None  # and for the close

    def _write(self, verb: str, result: str) -> None:
        con = db.connect(self.database)
        try:
            write(con, Record(f"{self.action}.{verb}", self.identity, self.target), result)
        finally:
            con.close()

    def opened(self, result: str = OK) -> None:
        """Record the open: ``ok``, or the code it was refused with, which ends the session."""
        if self.open is None:
            self.open = result
            self._write("open", result)

    def closed(self, result: str) -> None:
        """Record the close of a session whose open was ``ok``."""
        if self.open == OK and self.close is None:
            self.close = result
            self._write("close", result)


class Session:
    """A dependency that audits the WebSocket it is part of as ``<action>.open`` when it
    opens, or is refused once accepted, and ``<action>.close`` when it closes. Whatever the
    route does not record by the time it ends is recorded then: an open never recorded as the
    code of what ended it, and a close as that code too, or ``ok``."""

    def __init__(self, action: str):
        self.action = action

    @property
    def actions(self) -> tuple[str, str]:
        return (f"{self.action}.open", f"{self.action}.close")

    def __call__(self, connection: HTTPConnection, identity: Identity = Depends(current_user)):
        record = Opened(self.action, identity, connection.app.state.settings.database)
        try:
            yield record
        except Exception as error:
            record.opened(code_of(error))
            record.closed(code_of(error))
            raise
        record.opened("internal_error")  # a route that ends without saying so is at fault
        record.closed(OK)


def _dependency(route, kinds: tuple[type, ...]):
    """The first dependency of ``kinds`` in a route's dependency tree."""
    pending = list(route.dependant.dependencies)
    while pending:
        dependant = pending.pop()
        if isinstance(dependant.call, kinds):
            return dependant.call
        pending.extend(dependant.dependencies)
    return None


def action_of(route) -> str | None:
    """The audit action a route records, found in its dependency tree: a request's
    Recorder's, or a WebSocket's Session's."""
    found = _dependency(route, (Recorder, Session))
    return None if found is None else found.action


def actions_of(route) -> tuple[str, ...]:
    """The actions a route's rows carry: a Recorder's one, a Session's open and close."""
    found = _dependency(route, (Recorder, Session))
    if found is None:
        return ()
    return found.actions if isinstance(found, Session) else (found.action,)


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
