"""The hand-off to a forwarded port (forward.py), on rcman's own origin, through oauth2-proxy: a
navigation to a port's host with no session there is sent here, and the page sends its owner
back to the port with a way in, posted, never in an address."""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import HTMLResponse

from rcman import forward
from rcman.audit import Record, Recorder
from rcman.problems import HAND_OFF_PAGE, Problem

router = APIRouter(prefix="/api/forward", tags=["forward"])


@router.get("/hand-off", response_class=HTMLResponse)
def hand_off(
    request: Request,
    host: Annotated[
        str, Query(description="The port's host, such as 6080-12.ws.rcman.example.com.")
    ],
    record: Record = Depends(Recorder("forward.hand_off")),
) -> HTMLResponse:
    """A page that posts a way in to ``host``, a port of one of the person's own ready
    workspaces, to the port's host, and marks the host as wanted. Only a navigation that loads
    a document is answered (``Sec-Fetch-Dest: document``); the way in is spent once, soon, and
    only its hash is kept. It changes something, though its method is GET: the navigation from
    the port's host carries no Origin (identity.NAVIGATIONS)."""
    forwarding = request.app.state.forward
    target = forwarding.hosts.target(host)
    record.target = target.host if target is not None else None
    if request.headers.get("sec-fetch-dest") != "document":
        raise Problem("hand_off_refused")
    token = forwarding.hand_off(record.identity, host)
    workspace = request.app.state.workspaces.get(target.workspace)
    nonce = secrets.token_urlsafe(16)
    origin = forwarding.hosts.origin(target)
    words = {"port": target.port, "name": workspace.name if workspace else ""}
    page = forward.html_page(
        HAND_OFF_PAGE["title"].format(**words),
        HAND_OFF_PAGE["lead"].format(**words),
        HAND_OFF_PAGE["button"].format(**words),
        f"{origin}{forward.ENTER_PATH}",
        token,
        nonce,
    )
    policy = (
        f"default-src 'none'; script-src 'nonce-{nonce}'; form-action {origin}; "
        "base-uri 'none'; frame-ancestors 'none'"
    )
    return HTMLResponse(
        page,
        headers={
            "Content-Security-Policy": policy,
            "Referrer-Policy": "strict-origin",
            "Cache-Control": "no-store",
        },
    )
