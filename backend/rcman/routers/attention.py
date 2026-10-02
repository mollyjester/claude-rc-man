"""What administrators are warned about, built fresh on every request.

Servers on a screen rcman does not recognise, named by person and folder but
never with the screen's text, and Claude Code versions: who differs from the
approved one, and whether the approved one is the tested one. The listing and
the version report run side by side; when the version report fails, the
unrecognised screens are still reported, with the version report's error.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from fastapi import APIRouter, Depends, Request

from rcman import everyone
from rcman.identity import admin_user
from rcman.problems import Problem
from rcman.routers.claude import version_report

router = APIRouter(prefix="/api/attention", tags=["attention"], dependencies=[Depends(admin_user)])


@router.get("")
def attention(request: Request) -> dict:
    with ThreadPoolExecutor(max_workers=2) as pool:
        listing = pool.submit(everyone.everyone, request)
        report = pool.submit(version_report, request)
        people = listing.result()  # a users file rcman cannot read fails the whole request
        try:
            versions, versions_error = report.result(), None
        except Problem as error:
            versions = {"differs": [], "untested": False}
            versions_error = {"code": error.code, "title": error.title}
    unrecognised = [
        {"linux": person["linux"], "path": server["path"]}
        for person in people
        for server in person.get("servers", [])
        if server["state"] == "unrecognised"
    ]
    return {
        "unrecognised": unrecognised,
        "versions_differ": versions["differs"],
        "untested": versions["untested"],
        "versions_error": versions_error,
    }
