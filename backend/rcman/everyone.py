"""Everyone's workspaces and servers, for administrators, to look at only.

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


def everyone(request: Request) -> list[dict]:
    """Each person, with their workspaces and what is known of each one's server."""
    con = db.connect(request.app.state.settings.database)
    try:
        found = people.everyone(con)
    finally:
        con.close()
    service = request.app.state.workspaces
    workspaces = service.everything()
    shown = dict(zip([w.id for w in workspaces], service.listing(workspaces), strict=True))
    watch = request.app.state.watch
    answer = []
    for person in found:
        theirs = [shown[w.id] for w in workspaces if w.person == person.id]
        watch.listed(person.email, {w["name"]: w["server"] for w in theirs})
        answer.append(
            {
                "email": person.email,
                "workspaces": [
                    {
                        "id": w["id"],
                        "name": w["name"],
                        "state": w["state"],
                        "server": w["server"]
                        and {key: w["server"].get(key) for key in SERVER_FIELDS},
                        "server_error": w["server_error"],
                        "claude_version": w["claude_version"],
                    }
                    for w in theirs
                ],
            }
        )
    return answer
