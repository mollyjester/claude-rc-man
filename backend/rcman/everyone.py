"""Everyone's servers, for administrators.

One ``rc-helper list`` per person in the users file, run as that person, side
by side. A person whose call fails or takes too long is listed with the error
instead of their servers, so one person never hides or stalls the others.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait

from fastapi import Request

from helpers import users_file as uf
from rcman.helpers_client import call_helper
from rcman.identity import Identity
from rcman.problems import MESSAGES, Problem

EVERYONE_WORKERS = 8
EVERYONE_TIMEOUT_SECONDS = 15.0
# What is passed on about each server: never its screen, which may show anything
# the person's Claude showed, nor its claude.ai address, which is theirs alone.
SERVER_FIELDS = ("path", "state", "session_name", "exit_code")


def _error(code: str, title: str) -> dict:
    return {"code": code, "title": title}


def everyone(request: Request) -> list[dict]:
    """Each person in the users file with their servers, or the error listing them gave."""
    try:
        users = uf.load(request.app.state.settings.users_file)
    except uf.UsersFileError:
        raise Problem("users_file_invalid") from None
    if not users:
        return []
    run = request.app.state.helper
    timeout = EVERYONE_TIMEOUT_SECONDS
    pool = ThreadPoolExecutor(max_workers=min(len(users), EVERYONE_WORKERS))
    try:
        futures = {
            pool.submit(
                call_helper,
                run,
                Identity(u.email, u.linux, u.admin),
                ["list"],
                timeout=timeout,
            ): u
            for u in users
        }
        wait(futures, timeout=timeout)
    finally:
        pool.shutdown(wait=False, cancel_futures=True)  # the calls' own timeout ends them
    people = []
    for future, user in futures.items():
        entry = {"linux": user.linux, "email": user.email}
        if not future.done() or future.cancelled():  # still running, or still queued
            entry["error"] = _error("helper_timeout", MESSAGES["helper_timeout"])
        elif isinstance(future.exception(), Problem):
            entry["error"] = _error(future.exception().code, future.exception().title)
        elif future.exception() is not None:
            entry["error"] = _error("internal_error", MESSAGES["internal_error"])
        else:
            entry["servers"] = [
                {key: server.get(key) for key in SERVER_FIELDS}
                for server in future.result()["servers"]
            ]
        people.append(entry)
    return people
