"""What administrators are warned about, built fresh on every request.

Servers on a screen rcman does not recognise, named by person and folder but
never with the screen's text, and Claude Code versions: who differs from the
approved one, and whether the approved one is the tested one. The listing and
the version report run side by side; when the version report fails, the
unrecognised screens are still reported, with the version report's error.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as WaitTimeout

from fastapi import APIRouter, Depends, Request

from rcman import everyone
from rcman.identity import admin_user
from rcman.problems import MESSAGES, Problem
from rcman.routers.claude import version_report

router = APIRouter(prefix="/api/attention", tags=["attention"], dependencies=[Depends(admin_user)])


@router.get("")
def attention(request: Request) -> dict:
    timeout = everyone.ATTENTION_VERSIONS_TIMEOUT_SECONDS
    pool = ThreadPoolExecutor(max_workers=2)
    try:
        report = pool.submit(version_report, request, timeout)
        listing = pool.submit(everyone.everyone, request)
        people = listing.result()  # a users file rcman cannot read fails the whole request
        try:
            versions, versions_error = report.result(timeout=timeout), None
        except WaitTimeout:
            versions_error = {"code": "helper_timeout", "title": MESSAGES["helper_timeout"]}
        except Problem as error:
            versions_error = {"code": error.code, "title": error.title}
        if versions_error:
            versions = {"differs": [], "untested": False}
    finally:
        pool.shutdown(wait=False)  # neither part waits for the other once it has failed
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
