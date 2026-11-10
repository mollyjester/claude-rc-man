"""The audit trail: recording, coverage of every change, retention."""

import datetime as dt
import re
import sqlite3
from pathlib import Path

from fastapi.routing import APIRoute
from rcman import audit, db, routes

from tests.conftest import ALICE, BOB, CALLS, changing, concrete, made_workspace
from tests.hang_guard import HANG_GUARD
from tests.until import until

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


def test_every_websocket_route_records_when_it_opens_and_when_it_closes(app):
    found = routes.websocket_routes(app)
    assert found  # non-vacuity
    for route in found:
        action = audit.action_of(route)
        assert audit.actions_of(route) == (f"{action}.open", f"{action}.close"), route.path


def test_a_websocket_route_without_a_session_is_detected():
    """Planted twin: a WebSocket route recording nothing, or recording as a request does, has
    no session; one with a Session has its two actions."""
    from fastapi import Depends, FastAPI, WebSocket

    planted = FastAPI()

    @planted.websocket("/api/none")
    async def none(websocket: WebSocket):
        await websocket.close()

    @planted.websocket("/api/session")
    async def session(websocket: WebSocket, record=Depends(audit.Session("planted"))):
        await websocket.close()

    found = {r.path: audit.actions_of(r) for r in routes.websocket_routes(planted)}
    assert found == {"/api/none": (), "/api/session": ("planted.open", "planted.close")}


def recorded_actions(app) -> set[str]:
    """Every action rcman records: each route's, a request's one (a changing request's, or a
    navigation's that changes something, identity.NAVIGATIONS) and a WebSocket's two, and the
    forwarding server's handlers' (forward.HANDLERS)."""
    from rcman import forward

    return {
        action
        for r in [*(c.route for c in routes.api_routes(app)), *routes.websocket_routes(app)]
        for action in audit.actions_of(r)
    } | {action for handler in forward.HANDLERS.values() for action in handler.actions}


def guide_actions(guide: str) -> set[str]:
    """The actions the guide's actions table lists."""
    return set(re.findall(r"^\| `([a-z]+\.[a-z_]+)` \|", guide, re.MULTILINE))


def undocumented_actions(app) -> set[str]:
    """The actions rcman records that the guide's table does not list, and those it lists that
    rcman does not record."""
    guide = (REPO / "docs" / "audit.md").read_text(encoding="utf-8")
    return recorded_actions(app) ^ guide_actions(guide)


def test_every_action_is_listed_in_the_guide(app):
    actions = recorded_actions(app)
    # One of each kind: a change, a WebSocket, a navigation and the forwarding server's.
    assert {"terminal.open", "workspace.create", "forward.hand_off", "websocket.close"} <= actions
    assert guide_actions((REPO / "docs" / "audit.md").read_text(encoding="utf-8"))
    assert undocumented_actions(app) == set()


def test_the_actions_check_finds_an_action_the_guide_does_not_list(app):
    """Planted twin: a route that records an action the guide does not list is caught."""
    from fastapi import Depends

    @app.post("/api/planted-recorded")
    def planted(record=Depends(audit.Recorder("planted.call"))):
        return {}

    assert undocumented_actions(app) == {"planted.call"}


def documented_results(guide):
    """The results the guide's results table lists, besides error codes."""
    return set(
        re.findall(r"^\| `([a-z_]+)` \| [^|]+ \|$", guide.split("## How long")[0], re.MULTILINE)
    )


def test_every_result_other_than_an_error_code_is_listed_in_the_guide():
    guide = (REPO / "docs" / "audit.md").read_text(encoding="utf-8")
    assert set(audit.RESULTS) == {
        "ok",
        "unrecognised_screen",
        "committed_not_pushed",
        "pulled_not_pushed",
        "terminal_lost",
        "terminal_replaced",
        "workspace_gone",
        "workspace_rebuilding",
        "workspace_stopped",
        "signed_out",
        "service_closing",
        "forwarding_off",
        "session_ended",
    }
    assert documented_results(guide) == set(audit.RESULTS)


def test_the_results_check_fails_on_a_drifted_guide():
    """Planted twin: a results table missing a row is caught."""
    rows = [f"| `{result}` | meaning |" for result in audit.RESULTS]
    right = "| Result | Meaning |\n|---|---|\n" + "\n".join(rows) + "\n"
    assert documented_results(right) == set(audit.RESULTS)
    missing = right.replace(rows[-1] + "\n", "")
    assert documented_results(missing) != set(audit.RESULTS)


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


def audited_routes(app) -> dict[tuple[str, str], str]:
    """Every changing route that records, by (method, path), with its action."""
    return {
        (sorted(r.methods)[0], r.path): audit.action_of(r.route)
        for r in changing_routes(app)
        if audit.action_of(r.route)
    }


def audited_calls() -> dict:
    """The valid request of every changing route that is not exempt."""
    return {k: v for k, v in CALLS.items() if k[0] in CHANGING and (k[1], k[0]) not in audit.EXEMPT}


def test_driving_every_audited_route_writes_its_row(app, client, settings):
    calls, audited = audited_calls(), audited_routes(app)
    assert set(calls) == set(audited)
    for (method, path), kwargs in calls.items():
        before = len(rows(settings))
        client.request(method, concrete(path), headers=changing(ALICE), **kwargs)
        after = rows(settings)
        assert len(after) == before + 1, (method, path)
        assert after[-1]["action"] == audited[(method, path)]
        assert after[-1]["email"] == ALICE


def test_the_driving_check_finds_an_audited_route_with_no_request(app):
    """Planted twin: an audited route no valid request drives is told apart from the calls."""
    from fastapi import Depends

    @app.post("/api/planted-undriven")
    def planted(record=Depends(audit.Recorder("planted.call"))):
        return {}

    assert set(audited_routes(app)) - set(audited_calls()) == {("POST", "/api/planted-undriven")}


def test_a_refused_change_is_recorded_with_its_error_code(client, settings):
    made = made_workspace(client, ALICE, "alpha")
    client.post("/api/servers/stop", json={"workspace": made["id"]}, headers=changing(ALICE))
    last = rows(settings)[-1]
    assert (last["action"], last["result"], last["target"]) == (
        "server.stop",
        "unknown_server",
        "alpha",
    )


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
                self.sock.settimeout(HANG_GUARD)
                self.sock.connect(str(sock))

        conn = Connection("rcman")
    else:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=HANG_GUARD)
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

        def healthy() -> bool:
            try:
                return _request(port, sock, "GET", "/api/health")[0] == 200
            except OSError:
                return False

        try:
            until(healthy)
        except AssertionError as never:
            raise AssertionError(output.read_text()) from never
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
        proc.wait(timeout=HANG_GUARD)
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


def test_what_git_and_github_routes_are_sent_is_never_answered_recorded_or_logged(
    client, settings, caplog
):
    workspace = made_workspace(client, ALICE, "hers")
    caplog.set_level("DEBUG")
    message, token, files = "SECRET-COMMIT-MESSAGE-7", "a-token-never-recorded", ["SECRET-FILE.txt"]
    requests = [
        ("/api/git/stage", {"workspace": workspace["id"], "files": files}),
        ("/api/git/commit", {"workspace": workspace["id"], "message": message}),
        ("/api/github/token", {"token": token}),
    ]
    answers = [client.post(url, json=body, headers=changing(ALICE)).text for url, body in requests]
    recorded = [(r["action"], r["target"]) for r in rows(settings)[-3:]]
    assert recorded == [("git.stage", "hers"), ("git.commit", "hers"), ("github.token", None)]
    assert caplog.records  # positive control: the audit events were logged
    for secret in (message, token, *files):
        assert all(secret not in str(v) for row in rows(settings) for v in row.values())
        assert secret not in caplog.text
    assert all(token not in answer for answer in answers)
