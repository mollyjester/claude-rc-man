"""The Terminal tab's WebSocket route (backend/rcman/routers/terminal.py): who may open a
terminal and on what, the frames it carries, what ends it, and what is recorded of it.

Most cases drive the app in the test's process, through the test client's WebSocket; the
refusals before the upgrade is accepted, a peer that vanished and a service that stops are
driven through real uvicorn, started with the options of the unit's own ExecStart, since the
test client neither sends the handshake nor pings. The shells are the host runtime's, so they
are looked for in the host's /proc (tests/test_terminals.py's helpers).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import signal
import socket
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path

import anyio
import pytest
from rcman import audit, db, people, problems
from rcman import runtime as rt
from rcman.headers import SECURITY_HEADERS
from rcman.routers import terminal as route
from starlette.testclient import WebSocketDenialResponse

from tests.conftest import ALICE, BOB, changing, made_workspace
from tests.test_terminals import a_length, alive, sleeping, wait_until

REPO = Path(__file__).resolve().parent.parent
PROTOCOL = route.PROTOCOL
# How long a frame is waited for.
FRAME_WITHIN = 15.0


@pytest.fixture
def quick_revocation(monkeypatch):
    """Revocation looked for often, before the service starts: it reads the value as it waits,
    but a test that relies on it lists this before ``client`` all the same."""
    monkeypatch.setattr(route, "REVOCATION_SECONDS", 0.2)


def path(workspace: int) -> str:
    return f"{PROTOCOL['path']}?{PROTOCOL['query']}={workspace}"


def resize(cols: int = 80, rows: int = 24) -> dict:
    return {"type": PROTOCOL["resize"], "cols": cols, "rows": rows}


class Peer:
    """The page's end of a terminal over the test client's WebSocket."""

    def __init__(self, session):
        self.session = session
        self.output = bytearray()
        self.frames: list[dict] = []
        self.close: dict | None = None

    def _next(self, timeout: float) -> dict:
        async def receive():
            with anyio.fail_after(timeout):
                return await self.session._send_rx.receive()

        return self.session.portal.call(receive)

    def read(self, until, timeout: float = FRAME_WITHIN) -> None:
        """Take what the server sends until ``until(self)`` holds or the socket closes."""
        deadline = time.monotonic() + timeout
        while not until(self):
            assert self.close is None, (self.close, bytes(self.output)[-500:], self.frames)
            message = self._next(max(0.01, deadline - time.monotonic()))
            if message["type"] == "websocket.close":
                self.close = message
            elif message.get("bytes") is not None:
                self.output += message["bytes"]
            else:
                self.frames.append(json.loads(message["text"]))

    def ask(self, command: str, pattern: bytes) -> re.Match:
        self.session.send_bytes(command.encode() + b"\r")
        self.read(lambda p: re.search(pattern, bytes(p.output)))
        return re.search(pattern, bytes(self.output))

    def closed(self, timeout: float = FRAME_WITHIN) -> dict:
        """The close the server sent, once it has."""
        self.read(lambda p: p.close is not None, timeout)
        return self.close


@contextlib.contextmanager
def opened(client, who: str, workspace: int, cols: int = 80, rows: int = 24):
    with client.websocket_connect(path(workspace), headers=changing(who)) as session:
        session.send_json(resize(cols, rows))
        yield Peer(session)


def rows(settings) -> list[dict]:
    con = sqlite3.connect(settings.database)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("select * from audit order by id")]
    finally:
        con.close()


def terminal_rows(settings) -> list[tuple]:
    return [
        (r["action"], r["target"], r["result"])
        for r in rows(settings)
        if r["action"].startswith("terminal.")
    ]


def wait_rows(settings, count: int) -> list[tuple]:
    return wait_until(lambda: len(found := terminal_rows(settings)) >= count and found)


def shell_of(peer: Peer, length: str) -> list[int]:
    """Start ``sleep <length>`` in the background and wait for it."""
    peer.ask(f"sleep {length} & echo started-$((1+2))", rb"started-3")
    return wait_until(lambda: sleeping(length))


@pytest.fixture
def workspace(client):
    return made_workspace(client, ALICE, "alpha")


# --- what may be opened, once the upgrade is accepted ----------------------------------------


def refused(client, who: str, workspace: int) -> tuple[dict, dict]:
    with client.websocket_connect(path(workspace), headers=changing(who)) as session:
        peer = Peer(session)
        close = peer.closed()
    [frame] = peer.frames
    return frame, close


def test_another_persons_workspace_is_refused_as_one_that_is_not_there(client, workspace, settings):
    theirs, theirs_close = refused(client, BOB, workspace["id"])
    missing, missing_close = refused(client, BOB, 999)
    assert theirs["type"] == PROTOCOL["problem"]
    assert theirs["problem"]["code"] == "workspace_not_found"
    assert theirs == missing and theirs_close == missing_close
    assert theirs_close["code"] == PROTOCOL["close_problem"] + 404
    # an administrator has no terminal on another's workspace either
    bobs = made_workspace(client, BOB, "bobs")
    admin, admin_close = refused(client, ALICE, bobs["id"])
    assert (admin, admin_close) == (theirs, theirs_close)
    assert terminal_rows(settings)[-3:] == [("terminal.open", None, "workspace_not_found")] * 3


def test_a_workspace_that_is_not_ready_is_refused(client, workspace, settings):
    con = db.connect(settings.database)
    try:
        con.execute("update workspaces set state = 'failed' where id = ?", (workspace["id"],))
        con.commit()
    finally:
        con.close()
    frame, close = refused(client, ALICE, workspace["id"])
    assert frame["problem"]["code"] == "workspace_not_ready"
    assert frame["problem"]["status"] == 409 and frame["problem"]["title"]
    assert close["code"] == PROTOCOL["close_problem"] + 409
    # the person's own workspace is named in the record, ready or not
    assert terminal_rows(settings)[-1] == ("terminal.open", "alpha", "workspace_not_ready")


def test_a_query_that_is_not_a_workspace_is_refused_before_the_upgrade(client):
    for query in ("", "?workspace=0", "?workspace=one"):
        with pytest.raises(WebSocketDenialResponse) as denied:
            with client.websocket_connect(PROTOCOL["path"] + query, headers=changing(ALICE)):
                pass
        assert denied.value.status_code == 422, query
        assert denied.value.json()["code"] == "validation_failed"


# --- the shell -----------------------------------------------------------------------------


def test_the_shell_runs_as_the_workspaces_user(client, workspace):
    service = client.app.state.workspaces
    found = service.get(workspace["id"])
    own = service.runtime.exec(found.container, ["id", "-u"]).out.decode().strip()
    assert own.isdigit()
    with opened(client, ALICE, workspace["id"]) as peer:
        peer.ask("echo uid-$(id -u)-$((1+1))", rf"uid-{own}-2".encode())


def test_the_first_size_and_every_resize_reach_the_shell(client, workspace):
    with opened(client, ALICE, workspace["id"], cols=100, rows=30) as peer:
        peer.ask("stty size", rb"[\r\n]30 100\r\n")
        peer.session.send_json(resize(120, 40))
        peer.ask("stty size", rb"[\r\n]40 120\r\n")


def test_a_second_terminal_replaces_the_first(client, workspace, settings):
    with opened(client, ALICE, workspace["id"]) as first:
        first.ask("echo $((2*21))", rb"[\r\n]42\r\n")
        with opened(client, ALICE, workspace["id"]) as second:
            close = first.closed()
            assert first.frames[-1] == {
                "type": PROTOCOL["closed"],
                "reason": "terminal_replaced",
            }
            assert close["code"] == PROTOCOL["close_normal"]
            second.ask("echo $((3*7))", rb"[\r\n]21\r\n")
    recorded = wait_rows(settings, 4)
    assert recorded[0] == ("terminal.open", "alpha", "ok")
    assert sorted(recorded[1:]) == [  # the first's close and the second's open, in either order
        ("terminal.close", "alpha", "terminal_lost"),
        ("terminal.close", "alpha", "terminal_replaced"),
        ("terminal.open", "alpha", "ok"),
    ]
    assert recorded[-1] == ("terminal.close", "alpha", "terminal_lost")


def test_the_shell_exiting_closes_the_terminal(client, workspace, settings):
    with opened(client, ALICE, workspace["id"]) as peer:
        peer.ask("echo $((1+4))", rb"[\r\n]5\r\n")
        peer.session.send_bytes(b"exit 7\r")
        close = peer.closed()
    assert peer.frames[-1] == {"type": PROTOCOL["exit"], "code": 7}
    assert close["code"] == PROTOCOL["close_normal"]
    assert wait_rows(settings, 2)[-1] == ("terminal.close", "alpha", "ok")


def ended_by(client, settings, workspace, act, reason: str) -> None:
    """``act`` ends the terminal with ``reason``, and its session is gone when it closes."""
    length = a_length()
    with opened(client, ALICE, workspace["id"]) as peer:
        [pid] = shell_of(peer, length)
        act()
        close = peer.closed()
        assert peer.frames[-1] == {"type": PROTOCOL["closed"], "reason": reason}
        assert close["code"] == PROTOCOL["close_normal"]
        assert not alive(pid)
    assert wait_rows(settings, 2)[-1][2] == reason


def test_deleting_the_workspace_closes_its_terminal(client, workspace, settings):
    def delete():
        r = client.delete(f"/api/workspaces/{workspace['id']}", headers=changing(ALICE))
        assert r.status_code == 200, r.text

    ended_by(client, settings, workspace, delete, "workspace_gone")


def test_a_workspace_marked_deleting_behind_rcmans_back_closes_its_terminal(
    quick_revocation, client, workspace, settings
):
    def mark():
        con = db.connect(settings.database)
        try:
            con.execute("update workspaces set state = 'deleting' where id = ?", (workspace["id"],))
            con.commit()
        finally:
            con.close()

    ended_by(client, settings, workspace, mark, "workspace_gone")


def test_a_person_marked_deleting_behind_rcmans_back_loses_their_terminal(
    quick_revocation, client, workspace, settings
):
    def mark():
        con = db.connect(settings.database)
        try:
            people.mark_deleting(con, people.by_email(con, ALICE))
        finally:
            con.close()

    ended_by(client, settings, workspace, mark, "not_allowed")


def test_signing_out_closes_the_persons_terminal(client, workspace, settings):
    def sign_out():
        r = client.post("/api/me/sign-out", json={"keep_sessions": True}, headers=changing(ALICE))
        assert r.status_code == 200, r.text

    ended_by(client, settings, workspace, sign_out, "signed_out")


def test_closing_every_terminal_tells_each_the_service_is_closing(client, workspace, settings):
    """What the app's shutdown does for any terminal uvicorn has not closed already."""
    length = a_length()
    with opened(client, ALICE, workspace["id"]) as peer:
        [pid] = shell_of(peer, length)
        client.portal.call(client.app.state.terminals.close_all, "service_closing")
        assert not alive(pid)
        close = peer.closed()
        assert peer.frames[-1] == {"type": PROTOCOL["closed"], "reason": "service_closing"}
        assert close["code"] == PROTOCOL["close_closing"]
    assert wait_rows(settings, 2)[-1] == ("terminal.close", "alpha", "service_closing")


def test_the_apps_shutdown_closes_every_terminal(settings, runtime):
    from fastapi.testclient import TestClient
    from rcman.main import create_app

    app = create_app(settings, runtime=runtime)
    closed = []
    real = app.state.terminals.close_all

    async def close_all(reason):
        closed.append(reason)
        await real(reason)

    app.state.terminals.close_all = close_all
    with TestClient(app) as client:
        assert client.app.state.workspaces.restored.wait(30)
    assert closed == ["service_closing"]


def test_what_is_typed_and_shown_is_never_recorded_or_logged(client, workspace, settings, caplog):
    caplog.set_level("DEBUG")
    secret = "KEYSTROKES-NEVER-RECORDED-" + uuid.uuid4().hex
    with opened(client, ALICE, workspace["id"]) as peer:
        peer.ask(f"echo {secret}-$((2+3))", f"{secret}-5".encode())
    recorded = wait_rows(settings, 2)
    assert recorded == [
        ("terminal.open", "alpha", "ok"),
        ("terminal.close", "alpha", "terminal_lost"),
    ]
    assert caplog.records  # the control: the events were logged
    for row in rows(settings):
        assert all(secret not in str(value) for value in row.values())
    assert secret not in caplog.text


# --- frames --------------------------------------------------------------------------------


MALFORMED_FIRST = {
    "binary before the size": ("bytes", b"ls\r", PROTOCOL["close_policy"]),
    "not JSON": ("text", "{resize", PROTOCOL["close_unsupported"]),
    "not an object": ("text", "[1, 2]", PROTOCOL["close_unsupported"]),
    "an unknown frame": ("text", json.dumps({"type": "paste"}), PROTOCOL["close_unsupported"]),
    "no type": ("text", json.dumps({"cols": 80, "rows": 24}), PROTOCOL["close_unsupported"]),
    "too narrow": ("text", json.dumps(resize(0, 24)), PROTOCOL["close_policy"]),
    "too tall": (
        "text",
        json.dumps(resize(80, PROTOCOL["size_max"] + 1)),
        PROTOCOL["close_policy"],
    ),
    "a size as text": ("text", json.dumps({**resize(), "cols": "80"}), PROTOCOL["close_policy"]),
    "a size as a truth": ("text", json.dumps({**resize(), "rows": True}), PROTOCOL["close_policy"]),
    "a size as a fraction": (
        "text",
        json.dumps({**resize(), "rows": 2.5}),
        PROTOCOL["close_policy"],
    ),
    "no rows": ("text", json.dumps({"type": "resize", "cols": 80}), PROTOCOL["close_policy"]),
}


@pytest.mark.parametrize("case", list(MALFORMED_FIRST))
def test_a_malformed_first_frame_closes_the_socket_and_starts_no_shell(
    client, workspace, settings, case
):
    kind, data, code = MALFORMED_FIRST[case]
    with client.websocket_connect(path(workspace["id"]), headers=changing(ALICE)) as session:
        session.send({"type": "websocket.receive", kind: data})
        peer = Peer(session)
        assert peer.closed()["code"] == code
    assert client.app.state.terminals.live(workspace["id"]) is None
    assert terminal_rows(settings)[-1] == ("terminal.open", "alpha", "validation_failed")


def test_the_largest_size_is_taken():
    assert route.size_of(resize(PROTOCOL["size_max"], PROTOCOL["size_min"])) == (
        PROTOCOL["size_max"],
        PROTOCOL["size_min"],
    )


def test_a_malformed_frame_later_closes_the_socket_and_ends_the_shell(client, workspace, settings):
    length = a_length()
    with opened(client, ALICE, workspace["id"]) as peer:
        [pid] = shell_of(peer, length)
        peer.session.send_json(resize(80, 0))
        assert peer.closed()["code"] == PROTOCOL["close_policy"]
    wait_until(lambda: not alive(pid))
    assert wait_rows(settings, 2)[-1] == ("terminal.close", "alpha", "validation_failed")


# --- the protocol, as backend.md writes it ------------------------------------------------


def documented_protocol(markdown: str) -> dict[str, list[str]]:
    """The table of backend.md's Terminal section: each name and the values in its row."""
    section = markdown.split("### The terminal's protocol", 1)[-1].split("\n#", 1)[0]
    found = {}
    for line in section.splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) == 3 and re.fullmatch(r"`[a-z_]+`", cells[0]):
            found[cells[0].strip("`")] = re.findall(r"`([^`]+)`", cells[1])
    return found


def as_listed(protocol: dict) -> dict[str, list[str]]:
    return {
        name: [str(v) for v in value] if isinstance(value, (list, tuple)) else [str(value)]
        for name, value in protocol.items()
    }


def test_the_protocol_is_the_one_backend_md_writes():
    guide = (REPO / "project-knowledge" / "backend.md").read_text(encoding="utf-8")
    found = documented_protocol(guide)
    assert found  # the control: the table was read
    assert found == as_listed(PROTOCOL)


def test_the_protocol_check_fails_on_a_drifted_table():
    """Planted twin: a table with a value changed or a row missing is caught."""
    rows_ = [
        f"| `{k}` | {', '.join(f'`{v}`' for v in vs)} | x |"
        for k, vs in as_listed(PROTOCOL).items()
    ]
    right = (
        "### The terminal's protocol\n\n| Name | Value | Meaning |\n|---|---|---|\n"
        + "\n".join(rows_)
    )
    assert documented_protocol(right) == as_listed(PROTOCOL)
    changed = right.replace(f"`{PROTOCOL['close_policy']}`", "`1009`")
    assert changed != right and documented_protocol(changed) != as_listed(PROTOCOL)
    missing = right.replace(rows_[0] + "\n", "")
    assert documented_protocol(missing) != as_listed(PROTOCOL)


PAGE_PROTOCOL = REPO / "frontend" / "src" / "terminalSocket.ts"
# Loads a module of the page's by itself, with Node's own TypeScript, and prints its PROTOCOL.
LOAD_PROTOCOL = """
import { pathToFileURL } from "node:url";
const loaded = await import(pathToFileURL(process.argv[1]).href);
process.stdout.write(JSON.stringify(loaded.PROTOCOL ?? null));
"""


def page_protocol(source: Path) -> dict | None:
    """The PROTOCOL a module of the page's exports, as Node reads it."""
    proc = subprocess.run(
        ["node", "--input-type=module", "-e", LOAD_PROTOCOL, str(source)],
        capture_output=True, text=True, timeout=60, check=True,
    )  # fmt: skip
    return json.loads(proc.stdout)


def test_the_page_speaks_the_protocol_the_route_does():
    """frontend/src/terminalSocket.ts's PROTOCOL is the route's, and so backend.md's table."""
    found = page_protocol(PAGE_PROTOCOL)
    assert found  # the control: the page's module was read
    assert as_listed(found) == as_listed(PROTOCOL)


def test_the_page_protocol_check_fails_on_a_drifted_module(tmp_path):
    """Planted twin: the page's module with a value changed, or a name missing, is caught."""
    source = PAGE_PROTOCOL.read_text(encoding="utf-8")
    right = tmp_path / "right.ts"
    right.write_text(source, encoding="utf-8")
    assert as_listed(page_protocol(right)) == as_listed(PROTOCOL)  # the copy is read alike
    changed = source.replace(f"close_policy: {PROTOCOL['close_policy']},", "close_policy: 1009,")
    missing = source.replace('  query: "workspace",\n', "")
    for name, planted in (("changed", changed), ("missing", missing)):
        assert planted != source, name  # the plant took effect
        drifted = tmp_path / f"{name}.ts"
        drifted.write_text(planted, encoding="utf-8")
        assert as_listed(page_protocol(drifted)) != as_listed(PROTOCOL), name


def test_the_reasons_are_the_ones_the_audit_records():
    """A reason a terminal is closed with is what its close records: a result of the audit's
    own, or the code of the problem it is."""
    for reason in PROTOCOL["reasons"]:
        assert reason in audit.RESULTS or reason in problems.MESSAGES, reason


# --- through real uvicorn -------------------------------------------------------------------


def unit_options() -> list[str]:
    """The options the unit's ExecStart gives uvicorn, but where it listens."""
    unit = (REPO / "installer" / "templates" / "rcman.service").read_text(encoding="utf-8")
    exec_start = next(
        line.partition("=")[2] for line in unit.splitlines() if line.startswith("ExecStart=")
    )
    argv = shlex.split(exec_start)
    assert argv[:3] == ["@CURRENT@/.venv/bin/uvicorn", "--factory", "rcman.main:create_app"]
    options = argv[3:]
    if "--uds" in options:
        at = options.index("--uds")
        del options[at : at + 2]
    assert not any("@" in o for o in options), options
    return options


def test_the_service_and_the_browser_tests_backend_run_the_same_websocket_implementation():
    options = unit_options()
    assert "--ws" in options
    implementation = options[options.index("--ws") + 1]
    config = (REPO / "frontend" / "playwright.config.ts").read_text(encoding="utf-8")
    command = re.search(r"command: `(uv run uvicorn [^`]*)`", config).group(1)
    assert f"--ws {implementation}" in command
    assert implementation == route.WEBSOCKETS


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def served(tmp_path: Path, ping: float = 20.0):
    """The browser tests' backend (tests/e2e_server.py) under real uvicorn with the unit's
    options, on a port of its own, pinging every ``ping`` seconds; its output in a file."""
    port = free_port()
    world = tmp_path / f"rcman-e2e-{uuid.uuid4()}"
    argv = [
        str(Path(sys.executable).parent / "uvicorn"),
        "--factory",
        "tests.e2e_server:create_app",
        *unit_options(),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--ws-ping-interval",
        str(ping),
        "--ws-ping-timeout",
        str(ping),
    ]
    origin = f"http://127.0.0.1:{port}"
    env = {
        "PATH": os.environ["PATH"],
        "RCMAN_PUBLIC_ORIGIN": origin,
        "RCMAN_E2E_DIR": str(world),
    }
    output = tmp_path / f"uvicorn-{port}.log"
    with output.open("wb") as log:
        proc = subprocess.Popen(argv, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    server = Served(proc, port, origin, output)
    try:
        wait_until(server.answers, timeout=60)
        yield server
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=60)


class Served:
    def __init__(self, proc, port, origin, output):
        self.proc, self.port, self.origin, self.output = proc, port, origin, output

    def http(self, method: str, url: str, body=None, who=ALICE) -> tuple[int, dict]:
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            headers = {"X-Forwarded-Email": who, "Origin": self.origin}
            if body is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, url, body=json.dumps(body) if body is not None else None,
                         headers=headers)  # fmt: skip
            response = conn.getresponse()
            return response.status, json.loads(response.read() or b"null")
        finally:
            conn.close()

    def answers(self) -> bool:
        assert self.proc.poll() is None, self.output.read_text()
        try:
            return self.http("GET", "/api/health")[0] == 200
        except OSError:
            return False

    def workspace(self, name: str) -> int:
        status, made = self.http("POST", "/api/workspaces", {"name": name})
        assert status == 202, made
        number = made["workspace"]["id"]

        def ready():
            listed = self.http("GET", "/api/workspaces")[1]["workspaces"]
            state = next(w for w in listed if w["id"] == number)["state"]
            assert state in ("creating", "ready"), state
            return state == "ready"

        wait_until(ready, timeout=60)
        return number

    def url(self, workspace: int | None = None) -> str:
        query = "" if workspace is None else f"?{PROTOCOL['query']}={workspace}"
        return f"ws://127.0.0.1:{self.port}{PROTOCOL['path']}{query}"

    def events(self) -> list[dict]:
        found = []
        for line in self.output.read_text(errors="replace").splitlines():
            with contextlib.suppress(ValueError):
                event = json.loads(line)
                if isinstance(event, dict):
                    found.append(event)
        return found


def connect(server: Served, workspace: int | None = 1, headers=None, origin="own"):
    from websockets.sync.client import connect as ws_connect

    return ws_connect(
        server.url(workspace),
        origin=server.origin if origin == "own" else origin,
        additional_headers=headers if headers is not None else {"X-Forwarded-Email": ALICE},
        proxy=None,
        open_timeout=10,
    )


def test_identity_and_origin_are_refused_before_the_upgrade_with_a_problem(tmp_path):
    from websockets.exceptions import InvalidStatus

    cases = [
        ("no identity", {}, "own", 401, "not_authenticated"),
        ("a stranger", {"X-Forwarded-Email": "stranger@example.com"}, "own", 403, "not_allowed"),
        ("no origin", {"X-Forwarded-Email": ALICE}, None, 403, "origin_refused"),
        ("another origin", {"X-Forwarded-Email": ALICE}, "https://evil.example.com", 403,
         "origin_refused"),
    ]  # fmt: skip
    with served(tmp_path) as server:
        for case, headers, origin, status, code in cases:
            with pytest.raises(InvalidStatus) as denied:
                connect(server, 1, headers, origin).close()
            response = denied.value.response
            assert response.status_code == status, case
            assert response.headers["Content-Type"].startswith("application/problem+json"), case
            assert json.loads(response.body)["code"] == code, case
            for name, value in SECURITY_HEADERS.items():
                assert response.headers.get(name) == value, (case, name)
        # the control: the same upgrade with an identity and the origin is accepted
        with connect(server, 999) as accepted:
            frame = json.loads(accepted.recv(timeout=FRAME_WITHIN))
        assert frame["problem"]["code"] == "workspace_not_found"


CLIENT = """
import json, sys, time
from websockets.sync.client import connect
url, origin, email, length = sys.argv[1:]
ws = connect(url, origin=origin, additional_headers={"X-Forwarded-Email": email}, proxy=None)
ws.send(json.dumps({"type": "resize", "cols": 80, "rows": 24}))
ws.send(f"sleep {length} &\\r".encode())
print("sent", flush=True)
time.sleep(600)
"""


def test_a_peer_that_vanished_ends_its_terminal(tmp_path):
    length = a_length()
    with served(tmp_path, ping=0.5) as server:
        workspace = server.workspace("alpha")
        peer = subprocess.Popen(
            [sys.executable, "-c", CLIENT, server.url(workspace), server.origin, ALICE, length],
            cwd=REPO,
            stdout=subprocess.PIPE,
        )
        try:
            [pid] = wait_until(lambda: sleeping(length))
            peer.send_signal(signal.SIGSTOP)  # it answers no ping from now on
            wait_until(lambda: not alive(pid), timeout=30)
            wait_until(
                lambda: any(
                    e.get("action") == "terminal.close" and e.get("result") == "terminal_lost"
                    for e in server.events()
                )
            )
        finally:
            peer.kill()
            peer.wait()


def test_a_service_that_stops_closes_its_terminals_first(tmp_path):
    length = a_length()
    with served(tmp_path) as server:
        workspace = server.workspace("alpha")
        with connect(server, workspace) as ws:
            ws.send(json.dumps(resize()))
            ws.send(f"sleep {length} &\r".encode())
            [pid] = wait_until(lambda: sleeping(length))
            server.proc.send_signal(signal.SIGTERM)
            with contextlib.suppress(Exception):
                while True:
                    ws.recv(timeout=FRAME_WITHIN)
            assert ws.close_code == PROTOCOL["close_closing"]
        server.proc.wait(timeout=60)
        assert not alive(pid)
        closes = [e for e in server.events() if e.get("action") == "terminal.close"]
        assert [e["result"] for e in closes] == ["service_closing"]


# --- what else ends a terminal, and what does not -------------------------------------------


def test_a_page_that_sends_no_size_is_closed(monkeypatch, client, workspace, settings):
    monkeypatch.setattr(route, "FIRST_RESIZE_SECONDS", 0.5)
    with client.websocket_connect(path(workspace["id"]), headers=changing(ALICE)) as session:
        assert Peer(session).closed()["code"] == PROTOCOL["close_policy"]
    assert client.app.state.terminals.live(workspace["id"]) is None
    assert terminal_rows(settings)[-1] == ("terminal.open", "alpha", "validation_failed")


def test_deleting_the_person_ends_their_terminal(client, settings):
    bobs = made_workspace(client, BOB, "bobs")
    length = a_length()
    with opened(client, BOB, bobs["id"]) as peer:
        [pid] = shell_of(peer, length)
        con = db.connect(settings.database)
        try:
            bob = people.by_email(con, BOB)
        finally:
            con.close()
        r = client.delete(f"/api/people/{bob.id}", headers=changing(ALICE))
        assert r.status_code == 200, r.text
        close = peer.closed()
        assert peer.frames[-1] == {"type": PROTOCOL["closed"], "reason": "workspace_gone"}
        assert close["code"] == PROTOCOL["close_normal"]
        assert not alive(pid)


def test_a_workspace_removed_behind_rcmans_back_ends_its_terminal(client, workspace, settings):
    """What removing the container does in Podman - every process in it ended - done on the
    host runtime: the shell's end closes the terminal."""
    with opened(client, ALICE, workspace["id"]) as peer:
        peer.ask("echo $((5*5))", rb"[\r\n]25\r\n")
        terminal = client.app.state.terminals.live(workspace["id"])
        client.app.state.workspaces.runtime.remove(rt.container_name(workspace["id"]))
        os.kill(terminal.leader[0], signal.SIGKILL)
        close = peer.closed()
        assert peer.frames[-1]["type"] == PROTOCOL["exit"]
        assert close["code"] == PROTOCOL["close_normal"]
    assert not alive(terminal.leader[0])


def test_a_look_at_the_rows_that_fails_is_tried_again(
    quick_revocation, client, workspace, settings, monkeypatch, caplog
):
    real, failed = route._revoked, []

    def busy(*args):
        if len(failed) < 3:
            failed.append(1)
            raise sqlite3.OperationalError("database is locked")
        return real(*args)

    monkeypatch.setattr(route, "_revoked", busy)
    with opened(client, ALICE, workspace["id"]) as peer:
        wait_until(lambda: len(failed) == 3)
        peer.ask("echo still-$((3+3))", rb"still-6")  # the terminal is still open
        con = db.connect(settings.database)
        try:
            con.execute("update workspaces set state = 'deleting' where id = ?", (workspace["id"],))
            con.commit()
        finally:
            con.close()
        peer.closed()
        assert peer.frames[-1] == {"type": PROTOCOL["closed"], "reason": "workspace_gone"}
    assert "could not look at its rows" in caplog.text


def test_of_what_ends_a_terminal_together_its_own_end_says_most():
    assert route.most_telling(["typed", "ended"]) == "ended"
    assert route.most_telling(["typed", "watch"]) == "watch"
    assert route.most_telling(["watch", "ended", "typed"]) == "ended"
    assert route.most_telling(["typed"]) == "typed"


def test_the_shutdown_closes_the_rest_when_the_terminals_fail_to(settings, runtime):
    from fastapi.testclient import TestClient
    from rcman.main import create_app

    app = create_app(settings, runtime=runtime)

    async def failing(reason):
        raise RuntimeError("planted")

    app.state.terminals.close_all = failing
    with TestClient(app) as client:
        assert client.app.state.workspaces.restored.wait(30)
    assert app.state.workspaces._closing.is_set()  # the service was closed all the same


def test_a_sign_out_before_the_first_resize_ends_the_terminal_as_it_opens(client, workspace):
    with client.websocket_connect(path(workspace["id"]), headers=changing(ALICE)) as session:
        con = db.connect(client.app.state.settings.database)
        try:
            person = people.by_email(con, ALICE).id
        finally:
            con.close()
        # accepted, and the size not sent yet
        client.portal.call(client.app.state.terminals.end_person, person, "signed_out")
        session.send_json(resize())
        peer = Peer(session)
        close = peer.closed()
    assert peer.frames[-1] == {"type": PROTOCOL["closed"], "reason": "signed_out"}
    assert close["code"] == PROTOCOL["close_normal"]
    assert client.app.state.terminals.live(workspace["id"]) is None
