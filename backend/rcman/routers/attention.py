"""What administrators are warned about, built fresh on every request.

Servers on a screen rcman does not recognise, named by person and folder but
never with the screen's text, and Claude Code versions: who differs from the
approved one, and whether the approved one is the tested one.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from rcman import everyone
from rcman.identity import admin_user
from rcman.routers.claude import version_report

router = APIRouter(prefix="/api/attention", tags=["attention"], dependencies=[Depends(admin_user)])


@router.get("")
def attention(request: Request) -> dict:
    unrecognised = [
        {"linux": person["linux"], "path": server["path"]}
        for person in everyone.everyone(request)
        for server in person.get("servers", [])
        if server["state"] == "unrecognised"
    ]
    versions = version_report(request)
    return {
        "unrecognised": unrecognised,
        "versions_differ": versions["differs"],
        "untested": versions["untested"],
    }
