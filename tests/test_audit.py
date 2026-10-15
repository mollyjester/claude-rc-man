"""The audit trail: recording, coverage of every change, retention."""

import datetime as dt
import re
import sqlite3
from pathlib import Path

from fastapi.routing import APIRoute
from rcman import audit, db, routes

from tests.conftest import ALICE, BOB, CALLS, changing, concrete, made_workspace

REPO = Path(__file__).resolve().parent.parent
CHANGING = {"POST", "PUT", "PATCH", "DELETE"}


def rows(settings):
    con = sqlite3.connect(settings.database)
    con.row_factory = sqlite3.Row
    return [dict(r) for r in con.execute("select * from audit order by id")]


# --- coverage: structural ---------------------------------------------------------


def changing_routes(app):
    return [c for c in routes.api_routes(app) if c.methods & CHANGING]


def test_every_changing_route_is_audited_or_exempt_with_a_reason(app):
    routes = changing_routes(app)
    assert routes  # non-vacuity
    unaccounted = [
        f"{sorted(r.methods)} {r.path}"
        for r in routes
        if audit.action_of(r.route) is None and (r.path, *sorted(r.methods)) not in audit.EXEMPT
    ]
    assert unaccounted == []


def exemption_problems(present, exempt, guide) -> list[str]:
    """Exemptions whose route is gone, and those whose reason the guide does not give."""
    stale = [f"{key}: no such route" for key in exempt if key not in present]
    undocumented = [
        f"{key}: reason not in the guide" for key, r in exempt.items() if r not in guide
    ]
    return stale + undocumented


def test_exemptions_are_stale_checked_and_documented(app):
    present = {(r.path, *sorted(r.methods)) for r in changing_routes(app)}
    assert audit.EXEMPT  # non-vacuity
    guide = (REPO / "docs" / "audit.md").read_text(encoding="utf-8")
    assert exemption_problems(present, audit.EXEMPT, guide) == []


def test_the_exemption_check_fails_on_a_stale_or_undocumented_exemption():
    """Planted twin: an exemption for a route that is gone, or with a reason the guide
    does not give, is caught."""
    present = {("/api/planted", "POST")}
    exempt = {("/api/planted", "POST"): "planted reason"}
    guide = "The guide gives the planted reason."
    assert exemption_problems(present, exempt, guide) == []
    gone = {**exempt, ("/api/gone", "POST"): "planted reason"}
    assert exemption_problems(present, gone, guide) == ["('/api/gone', 'POST'): no such route"]
    unsaid = {("/api/planted", "POST"): "a reason the guide does not give"}
    assert exemption_problems(present, unsaid, guide) == [
        "('/api/planted', 'POST'): reason not in the guide"
    ]


def test_every_action_is_listed_in_the_guide(app):
    actions = {audit.action_of(r.route) for r in changing_routes(app)} - {None}
    guide = (REPO / "docs" / "audit.md").read_text(encoding="utf-8")
    listed = set(
        re.findall(r"^\| `([a-z]+\.[a-z_]+)` \|", guide, re.MULTILINE)
    )  # the actions table
    assert actions and actions == listed


def documented_results(guide):
    """The results the guide's results table lists, besides error codes."""
    return set(
        re.findall(r"^\| `([a-z_]+)` \| [^|]+ \|$", guide.split("## How long")[0], re.MULTILINE)
    )


def test_every_result_other_than_an_error_code_is_listed_in_the_guide():
    guide = (REPO / "docs" / "audit.md").read_text(encoding="utf-8")
    assert set(audit.RESULTS) == {"ok", "unrecognised_screen", "committed_not_pushed"}
    assert documented_results(guide) == set(audit.RESULTS)


def test_the_results_check_fails_on_a_drifted_guide():
    """Planted twin: a results table missing a row, or with one too many, is caught."""
    rows = [f"| `{result}` | meaning |" for result in audit.RESULTS]
    right = "| Result | Meaning |\n|---|---|\n" + "\n".join(rows) + "\n"
    assert documented_results(right) == set(audit.RESULTS)
    missing = right.replace(rows[-1] + "\n", "")
    assert missing != right and documented_results(missing) != set(audit.RESULTS)
    extra = right + "| `forged` | meaning |\n"
    assert documented_results(extra) != set(audit.RESULTS)


def test_a_route_without_a_recorder_is_detected():
    """Planted twin: a changing route with no recorder is reported."""
    from fastapi import FastAPI

    planted = FastAPI()

    @planted.post("/planted")
    def handler():
        return {}

    route = next(r for r in planted.routes if isinstance(r, APIRoute))
    assert audit.action_of(route) is None


# --- coverage: behavioural -----------------------------------------------------------


def test_driving_every_audited_route_writes_its_row(app, client, settings):
    calls = {
        k: v for k, v in CALLS.items() if k[0] in CHANGING and (k[1], k[0]) not in audit.EXEMPT
    }
    audited = {
        (sorted(r.methods)[0], r.path): audit.action_of(r.route)
        for r in changing_routes(app)
        if audit.action_of(r.route)
    }
    assert set(calls) == set(audited)
    for (method, path), kwargs in calls.items():
        before = len(rows(settings))
        client.request(method, concrete(path), headers=changing(ALICE), **kwargs)
        after = rows(settings)
        assert len(after) == before + 1, (method, path)
        assert after[-1]["action"] == audited[(method, path)]
        assert after[-1]["email"] == ALICE


def test_a_refused_change_is_recorded_with_its_error_code(client, settings):
    made = made_workspace(client, ALICE, "alpha")
    client.post("/api/servers/stop", json={"workspace": made["id"]}, headers=changing(ALICE))
    last = rows(settings)[-1]
    assert (last["action"], last["result"], last["target"]) == (
        "server.stop",
        "unknown_server",
        "alpha",
    )


def test_workspace_changes_are_recorded_with_the_workspaces_name(client, settings):
    made = made_workspace(client, ALICE, "alpha")
    client.delete(f"/api/workspaces/{made['id']}", headers=changing(ALICE))
    client.post("/api/workspaces", json={"name": "Not one!"}, headers=changing(ALICE))
    recorded = [(r["action"], r["target"], r["result"]) for r in rows(settings)[-3:]]
    assert recorded == [
        ("workspace.create", "alpha", "ok"),
        ("workspace.delete", "alpha", "ok"),
        ("workspace.create", None, "validation_failed"),
    ]


def test_people_changes_are_recorded_with_the_address(client, settings):
    client.post("/api/people", json={"email": "Dave@Example.com"}, headers=changing(ALICE))
    dave = next(
        p
        for p in client.get("/api/people", headers=changing(ALICE)).json()["people"]
        if p["email"] == "dave@example.com"
    )
    client.delete(f"/api/people/{dave['id']}", headers=changing(ALICE))
    recorded = [(r["action"], r["target"], r["result"]) for r in rows(settings)[-2:]]
    assert recorded == [
        ("person.add", "dave@example.com", "ok"),
        ("person.delete", "dave@example.com", "ok"),
    ]


def test_a_refused_settings_change_is_recorded_as_validation_failed(client, settings):
    r = client.patch("/api/me/settings", json={"prefix": "not valid!"}, headers=changing(BOB))
    assert (r.status_code, r.json()["code"]) == (422, "validation_failed")
    last = rows(settings)[-1]
    assert (last["action"], last["result"]) == ("settings.update", "validation_failed")
    assert last["target"] is None  # what was refused is not recorded


def test_the_refused_target_check_fails_on_a_route_that_records_too_early(app, settings):
    """Planted twin: a route that sets its target before the body is validated is caught."""
    from fastapi import Depends
    from fastapi.testclient import TestClient
    from pydantic import BaseModel

    class Body(BaseModel):
        n: int

    def early(record=Depends(audit.Recorder("planted.early"))):
        record.target = "recorded before validation"
        return record

    @app.post("/api/planted-early")
    def planted(body: Body, record=Depends(early)):
        return {}

    with TestClient(app) as client:
        r = client.post("/api/planted-early", json={"n": "not a number"}, headers=changing(BOB))
    assert r.status_code == 422
    last = rows(settings)[-1]
    assert (last["action"], last["result"]) == ("planted.early", "validation_failed")
    assert last["target"] is not None  # the check above would fail here


def test_every_failure_is_recorded_with_the_code_it_is_answered_with(app, settings):
    """Whatever ends an audited request, its row carries the code of the problem sent back."""
    from fastapi import Depends, HTTPException
    from fastapi.testclient import TestClient
    from pydantic import BaseModel
    from rcman.problems import Problem

    class Body(BaseModel):
        n: int

    @app.post("/api/planted/{kind}")
    def planted(kind: str, body: Body, record=Depends(audit.Recorder("planted.call"))):
        if kind == "problem":
            raise Problem("admin_only")
        if kind == "missing":
            raise HTTPException(404)
        if kind == "boom":
            raise RuntimeError("planted")
        return {}

    cases = [
        ("ok", {"n": 1}, "ok"),
        ("ok", {"n": "not a number"}, "validation_failed"),
        ("problem", {"n": 1}, "admin_only"),
        ("missing", {"n": 1}, "not_found"),
        ("boom", {"n": 1}, "internal_error"),
    ]
    with TestClient(app, raise_server_exceptions=False) as client:
        for kind, body, code in cases:
            r = client.post(f"/api/planted/{kind}", json=body, headers=changing(BOB))
            answered = "ok" if r.status_code == 200 else r.json()["code"]
            assert answered == code, (kind, body, r.text)
            last = rows(settings)[-1]
            assert (last["action"], last["result"]) == ("planted.call", code), (kind, body)


def test_a_successful_change_is_recorded_as_ok(client, settings):
    client.patch("/api/me/settings", json={"prefix": "pfx"}, headers=changing(BOB))
    last = rows(settings)[-1]
    assert (last["action"], last["result"], last["email"]) == ("settings.update", "ok", BOB)


def test_a_settings_change_records_the_settings_sent(client, settings):
    import json

    for body in (
        {"prefix": "pfx"},
        {"author_name": "Сергей Лихарев", "author_email": ""},
        {"author_email": "bob@work.example"},
    ):
        r = client.patch("/api/me/settings", json=body, headers=changing(BOB))
        assert r.status_code == 200, r.text
        last = rows(settings)[-1]
        assert (last["action"], last["result"]) == ("settings.update", "ok")
        assert json.loads(last["target"]) == body
    assert "Сергей" in rows(settings)[-2]["target"]  # as written, not escaped


def test_audit_events_are_logged_as_json(client, caplog):
    import json

    caplog.set_level("INFO", logger="rcman.audit")
    client.patch("/api/me/settings", json={"prefix": "pfx"}, headers=changing(BOB))
    event = json.loads(caplog.records[-1].getMessage())
    assert event["action"] == "settings.update" and event["email"] == BOB


def _service_argv(port: int, sock: Path) -> list[str]:
    """The unit's own ExecStart, with the development uvicorn and a free listener."""
    import shlex
    import sys

    unit = (REPO / "installer" / "templates" / "rcman.service").read_text(encoding="utf-8")
    exec_start = next(
        line.partition("=")[2] for line in unit.splitlines() if line.startswith("ExecStart=")
    )
    argv = shlex.split(exec_start)
    assert argv[0] == "@CURRENT@/.venv/bin/uvicorn"
    argv[0] = str(Path(sys.executable).parent / "uvicorn")
    for option, value in (("--port", str(port)), ("--uds", str(sock))):
        if option in argv:
            argv[argv.index(option) + 1] = value
    assert not any("@" in a for a in argv), argv
    return argv


def _request(port, sock, method, path, body=None, headers=None):
    """One HTTP request to the service, over its unix socket or its TCP port."""
    import http.client
    import socket

    if sock is not None:

        class Connection(http.client.HTTPConnection):
            def connect(self):
                self.sock = socket.socket(socket.AF_UNIX)
                self.sock.settimeout(5)
                self.sock.connect(str(sock))

        conn = Connection("rcman")
    else:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read().decode()
    finally:
        conn.close()


def service_output(tmp_path, settings, method, path, body, headers) -> list[str]:
    """The lines the service writes, started as the unit starts it, around one request.

    The request is sent once the service answers, and must succeed."""
    import json
    import os
    import socket
    import subprocess
    import time

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    sock = tmp_path / "rcman.sock"
    argv = _service_argv(port, sock)
    if "--uds" not in argv:
        sock = None
    env = {
        "PATH": os.environ["PATH"],
        "RCMAN_PUBLIC_ORIGIN": settings.public_origin,
        "RCMAN_ALLOWED_EMAILS": str(settings.allowed_emails),
        "RCMAN_DATABASE": str(settings.database),
    }
    output = tmp_path / "service.log"
    with output.open("wb") as log:
        proc = subprocess.Popen(argv, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                if _request(port, sock, "GET", "/api/health")[0] == 200:
                    break
            except OSError:
                pass
            assert time.monotonic() < deadline, output.read_text()
            time.sleep(0.1)
        status, text = _request(
            port,
            sock,
            method,
            path,
            body=json.dumps(body),
            headers={**headers, "Content-Type": "application/json"},
        )
        assert status == 200, text
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    lines = output.read_text().splitlines()
    assert any("Uvicorn running" in line for line in lines), lines  # positive control
    return lines


def json_events(lines: list[str]) -> list[dict]:
    """The lines that are each one JSON object, as those objects."""
    import json

    events = []
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def test_audit_events_reach_the_service_output(tmp_path, settings):
    """Started as the unit starts it, an audit event is a line on the service's own output."""
    lines = service_output(
        tmp_path, settings, "PATCH", "/api/me/settings", {"prefix": "pfx"}, changing(BOB)
    )
    events = [e for e in json_events(lines) if e.get("action") == "settings.update"]
    assert len(events) == 1, lines
    assert events[0]["email"] == BOB and events[0]["result"] == "ok"


# --- retention ------------------------------------------------------------------------


def seed(path, ages_days, now):
    db.migrate(path)
    con = db.connect(path)
    for age in ages_days:
        at = (now - dt.timedelta(days=age)).isoformat()
        con.execute(
            "insert into audit (at, email, action, target, result) values (?, 'e', 'a', 't', 'ok')",
            (at,),
        )
    con.commit()
    return con


def test_retention_deletes_rows_older_than_the_limit(tmp_path):
    now = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
    con = seed(tmp_path / "a.db", [1, 5, 10, 40], now)
    deleted = audit.apply_retention(con, now=now, days=7, max_rows=100)
    assert deleted == 2
    assert con.execute("select count(*) from audit").fetchone()[0] == 2


def test_retention_caps_the_row_count_keeping_the_newest(tmp_path):
    now = dt.datetime(2026, 10, 1, tzinfo=dt.UTC)
    con = seed(tmp_path / "a.db", [4, 3, 2, 1], now)
    audit.apply_retention(con, now=now, days=30, max_rows=2)
    ages = sorted(
        (now - dt.datetime.fromisoformat(r[0])).days for r in con.execute("select at from audit")
    )
    assert ages == [1, 2]


def test_retention_runs_when_the_app_starts(settings, runtime):
    from fastapi.testclient import TestClient
    from rcman.main import create_app

    now = dt.datetime.now(dt.UTC)
    seed(settings.database, [settings.audit_retention_days + 1, 0], now).close()
    with TestClient(create_app(settings, runtime=runtime)):
        pass
    assert [r["at"] for r in rows(settings)] == [now.isoformat()]  # the expired row only goes


def test_what_git_and_github_routes_are_sent_is_never_recorded_or_logged(client, settings, caplog):
    workspace = made_workspace(client, ALICE, "hers")
    caplog.set_level("DEBUG")
    message, token, files = "SECRET-COMMIT-MESSAGE-7", "a-token-never-recorded", ["SECRET-FILE.txt"]
    requests = [
        ("/api/git/stage", {"workspace": workspace["id"], "files": files}, "git.stage", None),
        ("/api/git/commit", {"workspace": workspace["id"], "message": message}, "git.commit", None),
        ("/api/github/token", {"token": token}, "github.token", "ok"),
    ]
    results = []
    for url, body, _, _ in requests:
        r = client.post(url, json=body, headers=changing(ALICE))
        results.append(r.json().get("code", "ok") if r.status_code >= 400 else "ok")
    recorded = [(r["action"], r["target"], r["result"]) for r in rows(settings)[-3:]]
    assert recorded == [
        ("git.stage", "hers", results[0]),
        ("git.commit", "hers", results[1]),
        ("github.token", None, "ok"),
    ]
    assert results[:2] == ["not_a_repository", "author_not_set"]  # they were refused, not made up
    assert caplog.records  # positive control: the audit events were logged
    for secret in (message, token, *files):
        assert all(secret not in str(v) for row in rows(settings) for v in row.values())
        assert secret not in caplog.text
