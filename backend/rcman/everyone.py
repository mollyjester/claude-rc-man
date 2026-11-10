"""Everyone's workspaces and servers, for administrators, to look at and, the one thing this page
lets them do to another person's, to stop (routers/workspaces.py).

Each person in the people table with each of their workspaces, its server read
side by side with the others' (Workspaces.listing). A workspace whose server
cannot be read is listed with why, so one never hides or stalls the others.
"""

from __future__ import annotations

from fastapi import Request

from rcman import db, people

# What is passed on about each server: never its screen, which may show anything
# the person's Claude showed, nor its claude.ai address, which is theirs alone. Beside the
# server, its workspace's ``claude_version`` is the version of Claude Code it started with.
SERVER_FIELDS = ("state", "session_name", "exit_code")


def shown(entry: dict) -> dict:
    """A workspace as an administrator is shown it, from its entry of ``Workspaces.listing``:
    its id, name, device, state, server (only SERVER_FIELDS), why its server could not be read,
    the version it runs, and whether it may be stopped now and is being stopped. Nothing else
    the listing gives - its error, its address, its steps, its rebuild's - reaches them."""
    server = entry["server"]
    return {
        "id": entry["id"],
        "name": entry["name"],
        "device": entry["device"],
        "state": entry["state"],
        "server": server and {key: server.get(key) for key in SERVER_FIELDS},
        "server_error": entry["server_error"],
        "claude_version": entry["claude_version"],
        "stoppable": entry["stoppable"],
        "stopping": entry["stopping"],
    }


def everyone(request: Request) -> list[dict]:
    """Each person, with their workspaces and what is known of each one's server."""
    con = db.connect(request.app.state.settings.database)
    try:
        found = people.everyone(con)
    finally:
        con.close()
    service = request.app.state.workspaces
    workspaces = service.everything()
    entries = dict(zip([w.id for w in workspaces], service.listing(workspaces), strict=True))
    watch = request.app.state.watch
    answer = []
    for person in found:
        theirs = [entries[w.id] for w in workspaces if w.person == person.id]
        watch.listed(person.email, {w["name"]: w["server"] for w in theirs})
        answer.append(
            {
                "email": person.email,
                "workspaces": [shown(w) for w in theirs],
            }
        )
    return answer
