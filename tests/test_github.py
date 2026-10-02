"""The GitHub token routes: the token reaches rc-helper on stdin, and nowhere else."""

import fcntl
import json
import logging
import os
import sqlite3

import pytest

from helpers import rc_helper
from tests.conftest import ALICE, BOB, as_user, changing
from tests.test_git import Recording

TOKEN = "github_pat_SECRET-TOKEN-0451"


@pytest.fixture
def recorded(app, client):
    recording = Recording(app.state.helper)
    app.state.helper = recording
    return recording


def status(client, who=ALICE) -> dict:
    r = client.get("/api/github", headers=as_user(who))
    assert r.status_code == 200, r.text
    return r.json()


def save(client, token=TOKEN, who=ALICE):
    return client.post("/api/github/token", json={"token": token}, headers=changing(who))


def test_a_token_is_stored_reported_and_removed(client):
    assert status(client) == {"token": False}
    r = save(client)
    assert (r.status_code, r.json()) == (200, {"token": True})
    assert status(client) == {"token": True}
    r = client.delete("/api/github/token", headers=changing(ALICE))
    assert (r.status_code, r.json()) == (200, {"token": False})
    assert status(client) == {"token": False}


def test_the_token_reaches_the_helper_on_stdin_only(client, recorded):
    assert save(client).status_code == 200
    [made] = recorded.calls
    assert made["linux"] == "alice"
    assert made["args"] == ["github-token"]
    assert made["stdin"] == TOKEN + "\n"


def test_each_person_has_their_own_token(client, helper_homes):
    assert save(client, who=ALICE).status_code == 200
    assert status(client, BOB) == {"token": False}
    assert status(client, ALICE) == {"token": True}  # positive control
    assert TOKEN in (helper_homes.homes["alice"] / ".git-credentials").read_text()
    assert not (helper_homes.homes["bob"] / ".git-credentials").exists()


def audit_rows(settings) -> list[tuple]:
    return sqlite3.connect(settings.database).execute("select * from audit").fetchall()


def leaks(secret: str, settings, caplog, capsys, responses) -> list[str]:
    """Where ``secret`` turned up: the audit trail, any log record, the process's own
    output, or a response."""
    found = []
    if secret in json.dumps(audit_rows(settings)):
        found.append("audit")
    if secret in caplog.text:
        found.append("log")
    out, err = capsys.readouterr()
    if secret in out + err:
        found.append("output")
    found += [f"response {r.status_code}" for r in responses if secret in r.text]
    return found


def hold_the_home(helper_homes, linux):
    """The person's home folder locked as a delete locks it, so the helper refuses."""
    fd = os.open(helper_homes.homes[linux].resolve(), os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def test_the_token_is_in_no_audit_row_log_line_or_problem(
    client, settings, helper_homes, caplog, capsys
):
    caplog.set_level(logging.DEBUG)
    responses = [save(client)]
    fd = hold_the_home(helper_homes, "alice")
    try:
        responses.append(save(client))
    finally:
        os.close(fd)
    responses.append(save(client, TOKEN + " with a space"))
    assert [r.status_code for r in responses] == [200, 409, 422]
    assert responses[1].json()["code"] == "folder_busy"  # the helper refused
    # Positive controls: each request was audited, and logged as a JSON line.
    recorded = [(r[4], r[6]) for r in audit_rows(settings)]
    assert recorded == [
        ("github.token", "ok"),
        ("github.token", "folder_busy"),
        ("github.token", "validation_failed"),
    ]
    assert '"action": "github.token"' in caplog.text
    assert leaks(TOKEN, settings, caplog, capsys, responses) == []


def test_the_leak_check_finds_a_token_recorded_as_a_target(app, settings, caplog, capsys):
    """Planted twin: a route that records the token as its target is caught."""
    from fastapi import Depends
    from fastapi.testclient import TestClient
    from rcman.audit import Recorder

    @app.post("/api/planted-token")
    def planted(body: dict, record=Depends(Recorder("planted.token"))):
        record.target = body["token"]
        return {}

    caplog.set_level(logging.DEBUG)
    with TestClient(app) as client:
        r = client.post("/api/planted-token", json={"token": TOKEN}, headers=changing(ALICE))
    assert r.status_code == 200
    assert leaks(TOKEN, settings, caplog, capsys, [r]) == ["audit", "log"]


def test_a_refused_token_is_not_echoed(client, recorded):
    refused = "not one line\n" + TOKEN
    r = save(client, refused)
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")
    assert r.json()["errors"] == [{"field": "token", "message": rc_helper.GITHUB_TOKEN.sentence}]
    assert TOKEN not in r.text
    assert recorded.calls == []  # the helper is not asked


def test_the_token_field_is_the_helpers_rule(app):
    from tests.test_problems import request_fields

    [token] = [f for f in request_fields(app) if f.path == "/api/github/token"]
    assert token.name == "token"
    assert token.schema["pattern"] == rc_helper.GITHUB_TOKEN.anchored
