"""The Terminal tab: a shell in one of the signed-in person's ready workspaces, over a
WebSocket (terminals.py runs the shells).

The upgrade is refused before it is accepted, over HTTP with a problem, for want of an
identity or the public origin (identity.py: an upgrade counts as a change) and for a query that
names no workspace. Once accepted, a workspace that is not the person's, or not ready, is a
``problem`` frame and a close of ``close_problem`` plus its status. The page then sends a
``resize`` first, and one on every fit; binary frames are what is typed and what the shell
prints, as they are. The terminal closes with ``exit`` when the shell ends, with ``closed`` and
its reason when something else ends it, and with a code alone for a frame it cannot take.
Everything ``PROTOCOL`` names, backend.md's table names too.

Besides its shell, a terminal ends when its person signs out of rcman (routers/me.py), its
workspace is deleted (``Workspaces.on_removing``), another terminal is opened on the workspace,
rcman stops, and when a look every REVOCATION_SECONDS finds the person no longer allowed in or
the workspace no longer theirs and ready, as a change rcman did not make itself (rcman-admin's
deletion, from its own process) leaves them. There is no time limit. A page that vanished is
ended by uvicorn's ping, which it no longer answers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Query, WebSocket
from starlette.websockets import WebSocketDisconnect, WebSocketState

from rcman import db, people, problems
from rcman.audit import (
    OK,
    SERVICE_CLOSING,
    SIGNED_OUT,
    TERMINAL_LOST,
    TERMINAL_REPLACED,
    WORKSPACE_GONE,
    WORKSPACE_REBUILDING,
    Opened,
    Session,
)
from rcman.problems import Problem
from rcman.terminals import Terminal
from rcman.workspaces import READY, REBUILD_SWITCHING

log = logging.getLogger("rcman.terminals")
router = APIRouter(tags=["terminal"])

# uvicorn's implementation of WebSockets that rcman runs with (its unit's ExecStart and the
# browser tests' backend): the one whose keepalive ping ends a terminal whose page vanished.
WEBSOCKETS = "websockets-sansio"
# How often an open terminal looks whether its person and its workspace are still as it opened.
REVOCATION_SECONDS = 5.0
# How long the page has, once the socket is accepted, to send the first resize; the page sends
# it as the socket opens.
FIRST_RESIZE_SECONDS = 15.0
NOT_ALLOWED = "not_allowed"

# The terminal's protocol: the route and its query, the frames, the sizes, the close codes and
# the reasons a ``closed`` frame gives. backend.md has the same table, which a test keeps equal.
PROTOCOL = {
    "path": "/api/terminal",
    "query": "workspace",
    "resize": "resize",
    "exit": "exit",
    "closed": "closed",
    "problem": "problem",
    "size_min": 1,
    "size_max": 65535,
    "close_normal": 1000,
    "close_unsupported": 1003,
    "close_policy": 1008,
    "close_closing": 1012,
    "close_problem": problems.CLOSE_PROBLEM,
    "reasons": (
        TERMINAL_REPLACED,
        WORKSPACE_GONE,
        WORKSPACE_REBUILDING,
        SIGNED_OUT,
        NOT_ALLOWED,
        SERVICE_CLOSING,
    ),
}


class Refused(Exception):
    """A frame the terminal cannot take: the code it closes with."""

    def __init__(self, code: int):
        super().__init__(code)
        self.code = code


def _size(value) -> int:
    if type(value) is not int or not PROTOCOL["size_min"] <= value <= PROTOCOL["size_max"]:
        raise Refused(PROTOCOL["close_policy"])
    return value


def size_of(frame: dict) -> tuple[int, int]:
    """The columns and rows of a ``resize``."""
    return _size(frame.get("cols")), _size(frame.get("rows"))


def _frame(message: dict) -> dict:
    """A text frame as the object it must be, with a type the terminal knows."""
    try:
        frame = json.loads(message["text"])
    except ValueError:
        raise Refused(PROTOCOL["close_unsupported"]) from None
    if not isinstance(frame, dict) or frame.get("type") != PROTOCOL["resize"]:
        raise Refused(PROTOCOL["close_unsupported"])
    return frame


def _resize_of(message: dict) -> tuple[int, int]:
    if message.get("text") is None:
        raise Refused(PROTOCOL["close_policy"])  # nothing is typed before the size is known
    return size_of(_frame(message))


def _revoked(websocket: WebSocket, record: Opened, workspace: int) -> str | None:
    """Why the terminal may no longer be open, if it may not: its person is no longer allowed
    in, its workspace is no longer theirs and ready, or a rebuild is replacing its container."""
    con = db.connect(websocket.app.state.settings.database)
    try:
        person = people.by_email(con, record.identity.email)
    finally:
        con.close()
    if person is None or person.state != people.ACTIVE or person.id != record.identity.person:
        return NOT_ALLOWED
    found = websocket.app.state.workspaces.get(workspace)
    if found is None or found.person != record.identity.person or found.state != READY:
        return WORKSPACE_GONE
    if found.rebuild_state == REBUILD_SWITCHING:
        return WORKSPACE_REBUILDING
    return None


async def _watch(websocket: WebSocket, record: Opened, workspace: int) -> str:
    """Look every REVOCATION_SECONDS; a look that fails, as on a database that is busy, is
    logged and the next one tried."""
    while True:
        await asyncio.sleep(REVOCATION_SECONDS)
        try:
            reason = await asyncio.to_thread(_revoked, websocket, record, workspace)
        except Exception:  # noqa: BLE001 - see above
            log.exception("a terminal of workspace %s could not look at its rows", workspace)
            continue
        if reason is not None:
            return reason


async def _typed(websocket: WebSocket, terminal: Terminal) -> str:
    """Pass on what the page sends until it goes: the result that is then."""
    while True:
        message = await websocket.receive()
        if message["type"] == "websocket.disconnect":
            if message.get("code") == PROTOCOL["close_closing"]:
                return SERVICE_CLOSING  # uvicorn closes every socket as it stops
            return TERMINAL_LOST
        if message.get("bytes") is not None:
            await terminal.write(message["bytes"])
        else:
            terminal.resize(*size_of(_frame(message)))


async def _send(websocket: WebSocket, frame: dict | None, code: int) -> None:
    """Send ``frame``, if any, and close with ``code``, unless the socket is closed already."""
    with contextlib.suppress(Exception):  # a page that is gone hears nothing
        if websocket.application_state == WebSocketState.CONNECTED:
            if frame is not None:
                await websocket.send_text(json.dumps(frame))
            await websocket.close(code)


async def _refuse(websocket: WebSocket, record: Opened, refused: Problem) -> None:
    await asyncio.to_thread(record.opened, refused.code)
    body = problems.body(refused.code, refused.status, refused.title, **refused.extra)
    await _send(
        websocket,
        {"type": PROTOCOL["problem"], "problem": body},
        PROTOCOL["close_problem"] + refused.status,
    )


@router.websocket(PROTOCOL["path"])
async def terminal(
    websocket: WebSocket,
    workspace: Annotated[int, Query(ge=1, description="The workspace's id.")],
    record: Opened = Depends(Session("terminal")),
) -> None:
    """A shell in the signed-in person's ready workspace."""
    service = websocket.app.state.workspaces
    terminals = websocket.app.state.terminals
    person = record.identity.person
    await websocket.accept()
    sign_outs = terminals.sign_outs_of(person)  # a sign-out from now on ends what opens
    try:
        found = await asyncio.to_thread(service.owned, person, workspace)
        record.target = found.name  # one that is the person's, ready or not
        await asyncio.to_thread(service.ready, person, workspace)
    except Problem as refused:
        await _refuse(websocket, record, refused)
        return
    try:
        try:
            message = await asyncio.wait_for(websocket.receive(), FIRST_RESIZE_SECONDS)
        except TimeoutError:
            raise Refused(PROTOCOL["close_policy"]) from None
        if message["type"] == "websocket.disconnect":
            await asyncio.to_thread(record.opened, TERMINAL_LOST)
            return
        cols, rows = _resize_of(message)
    except Refused as refused:
        await asyncio.to_thread(record.opened, "validation_failed")
        await _send(websocket, None, refused.code)
        return
    try:
        opened = await terminals.open(
            workspace, person, cols, rows, websocket.send_bytes, sign_outs=sign_outs
        )
    except Problem as refused:
        await _refuse(websocket, record, refused)
        return
    ended = (TERMINAL_LOST, None, PROTOCOL["close_normal"])  # if it is cancelled first
    try:
        await asyncio.to_thread(record.opened, OK)
        ended = await _carried(websocket, record, opened)
    finally:
        # ended, and its close recorded, even when the socket's task is cancelled meanwhile
        await asyncio.shield(asyncio.ensure_future(_finish(websocket, record, opened, *ended)))


async def _finish(websocket: WebSocket, record: Opened, opened: Terminal, result, frame, code):
    await websocket.app.state.terminals.end(opened, result)
    if result == OK:
        frame = {"type": PROTOCOL["exit"], "code": opened.exit_code}
    await _send(websocket, frame, code)
    await asyncio.to_thread(record.closed, result)


# Of what ended a terminal together, what says most comes first: its own end, a revocation, the
# page.
PRIORITY = ("ended", "watch", "typed")


def most_telling(finished: list[str]) -> str:
    return min(finished, key=PRIORITY.index)


async def _carried(websocket: WebSocket, record: Opened, opened: Terminal):
    """Carry the terminal until something ends it: what to record, the frame to send (but for
    a shell that ended by itself, whose frame is known once it is cleaned up) and the code to
    close with."""
    tasks = {
        asyncio.create_task(_typed(websocket, opened)): "typed",
        asyncio.create_task(opened.wait_ended()): "ended",
        asyncio.create_task(_watch(websocket, record, opened.workspace.id)): "watch",
    }
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, Refused):
                await task
    which = most_telling([tasks[t] for t in done])
    task = next(t for t in done if tasks[t] == which)
    try:
        reason = task.result()
    except Refused as refused:
        return "validation_failed", None, refused.code
    except WebSocketDisconnect:
        return TERMINAL_LOST, None, PROTOCOL["close_normal"]
    if which == "ended":
        reason = opened.reason
        if reason is None:  # its shell ended: perhaps because its workspace went
            try:
                reason = await asyncio.to_thread(_revoked, websocket, record, opened.workspace.id)
            except Exception:  # noqa: BLE001 - its shell ended, which is all that is known
                log.exception(
                    "a terminal of workspace %s could not look at its rows", opened.workspace.id
                )
        if reason is None:
            return OK, None, PROTOCOL["close_normal"]
    if reason in (TERMINAL_LOST, SERVICE_CLOSING) and which == "typed":
        return reason, None, PROTOCOL["close_normal"]
    code = PROTOCOL["close_closing"] if reason == SERVICE_CLOSING else PROTOCOL["close_normal"]
    return reason, {"type": PROTOCOL["closed"], "reason": reason}, code
