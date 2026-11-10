"""The Terminal tab's WebSocket route (backend/rcman/routers/terminal.py): who may open a
terminal and on what, the frames it carries, what ends it, and what is recorded of it.

Most cases drive the app in the test's process, through the test client's WebSocket; a
service that stops is driven through real uvicorn, started with the options of the unit's own
ExecStart, since the test client does not send the handshake. The shells are the host runtime's,
so they are looked for in the host's /proc (tests/test_terminals.py's helpers).
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
import uuid
from pathlib import Path

import anyio
import pytest
from rcman.routers import terminal as route

from tests.conftest import ALICE, BOB, changing, made_workspace
from tests.hang_guard import HANG_GUARD, tier_guard
from tests.test_terminals import a_length, alive, sleep_of, sleeping
from tests.until import until

REPO = Path(__file__).resolve().parent.parent
PROTOCOL = route.PROTOCOL


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

    def _next(self) -> dict:
        async def receive():
            with anyio.fail_after(tier_guard()):
                return await self.session._send_rx.receive()

        return self.session.portal.call(receive)

    def read(self, until) -> None:
        """Take what the server sends until ``until(self)`` holds or the socket closes; each
        message is waited for within the hang guard."""
        while not until(self):
            assert self.close is None, (self.close, bytes(self.output)[-500:], self.frames)
            message = self._next()
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

    def closed(self) -> dict:
        """The close the server sent, once it has."""
        self.read(lambda p: p.close is not None)
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
    return until(lambda: len(found := terminal_rows(settings)) >= count and found)


def shell_of(peer: Peer, length: str) -> list[int]:
    """Start ``sleep_of(length)`` in the background and wait for it."""
    peer.ask(f"{sleep_of(length)} & echo started-$((1+2))", rb"started-3")
    return until(lambda: sleeping(length))


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


def test_another_persons_workspace_is_not_found(client, workspace, settings):
    theirs, theirs_close = refused(client, BOB, workspace["id"])
    assert theirs["type"] == PROTOCOL["problem"]
    assert theirs["problem"]["code"] == "workspace_not_found"
    assert theirs_close["code"] == PROTOCOL["close_problem"] + 404
    assert terminal_rows(settings)[-1] == ("terminal.open", None, "workspace_not_found")


# --- the shell -----------------------------------------------------------------------------


def test_the_shell_runs_as_the_workspaces_user_at_the_pages_size_until_replaced(
    client, workspace, settings
):
    service = client.app.state.workspaces
    found = service.get(workspace["id"])
    own = service.runtime.exec(found.container, ["id", "-u"]).out.decode().strip()
    assert own.isdigit()
    with opened(client, ALICE, workspace["id"], cols=100, rows=30) as first:
        first.ask("echo uid-$(id -u)-$((1+1))", rf"uid-{own}-2".encode())
        first.ask("stty size", rb"[\r\n]30 100\r\n")
        first.session.send_json(resize(120, 40))
        first.ask("stty size", rb"[\r\n]40 120\r\n")
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


def test_the_shell_exiting_closes_the_terminal_and_nothing_typed_is_recorded_or_logged(
    client, workspace, settings, caplog
):
    caplog.set_level("DEBUG")
    secret = "KEYSTROKES-NEVER-RECORDED-" + uuid.uuid4().hex
    with opened(client, ALICE, workspace["id"]) as peer:
        peer.ask(f"echo {secret}-$((2+3))", f"{secret}-5".encode())
        peer.session.send_bytes(b"exit 7\r")
        close = peer.closed()
    assert peer.frames[-1] == {"type": PROTOCOL["exit"], "code": 7}
    assert close["code"] == PROTOCOL["close_normal"]
    assert wait_rows(settings, 2) == [
        ("terminal.open", "alpha", "ok"),
        ("terminal.close", "alpha", "ok"),
    ]
    assert caplog.records  # the control: the events were logged
    for row in rows(settings):
        assert all(secret not in str(value) for value in row.values())
    assert secret not in caplog.text


def test_deleting_the_workspace_closes_its_terminal_and_ends_its_session(
    client, workspace, settings
):
    length = a_length()
    with opened(client, ALICE, workspace["id"]) as peer:
        [pid] = shell_of(peer, length)
        r = client.delete(f"/api/workspaces/{workspace['id']}", headers=changing(ALICE))
        assert r.status_code == 200, r.text
        close = peer.closed()
        assert peer.frames[-1] == {"type": PROTOCOL["closed"], "reason": "workspace_gone"}
        assert close["code"] == PROTOCOL["close_normal"]
        assert not alive(pid)
    assert wait_rows(settings, 2)[-1] == ("terminal.close", "alpha", "workspace_gone")


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
        capture_output=True, text=True, timeout=HANG_GUARD, check=True,
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


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextlib.contextmanager
def served(tmp_path: Path):
    """The screenshots' backend (devtools/world.py) under real uvicorn with the unit's
    options, on a port of its own; its output in a file."""
    port = free_port()
    argv = [
        str(Path(sys.executable).parent / "uvicorn"),
        "--factory",
        "devtools.world:create_app",
        *unit_options(),
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
    ]
    origin = f"http://127.0.0.1:{port}"
    env = {
        "PATH": os.environ["PATH"],
        "RCMAN_PUBLIC_ORIGIN": origin,
    }
    output = tmp_path / f"uvicorn-{port}.log"
    with output.open("wb") as log:
        proc = subprocess.Popen(argv, cwd=REPO, env=env, stdout=log, stderr=subprocess.STDOUT)
    server = Served(proc, port, origin, output)
    try:
        until(server.answers)
        yield server
    finally:
        if proc.poll() is None:
            proc.terminate()
        proc.wait(timeout=HANG_GUARD)


class Served:
    def __init__(self, proc, port, origin, output):
        self.proc, self.port, self.origin, self.output = proc, port, origin, output

    def http(self, method: str, url: str, body=None, who=ALICE) -> tuple[int, dict]:
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=HANG_GUARD)
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

        until(ready)
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


def connect(server: Served, workspace: int):
    from websockets.sync.client import connect as ws_connect

    return ws_connect(
        server.url(workspace),
        origin=server.origin,
        additional_headers={"X-Forwarded-Email": ALICE},
        proxy=None,
        open_timeout=HANG_GUARD,
    )


def test_a_service_that_stops_closes_its_terminals_first(tmp_path):
    """Under the WebSocket implementation the route names, which the unit's ExecStart gives."""
    options = unit_options()
    assert options[options.index("--ws") + 1] == route.WEBSOCKETS
    length = a_length()
    with served(tmp_path) as server:
        workspace = server.workspace("alpha")
        with connect(server, workspace) as ws:
            ws.send(json.dumps(resize()))
            ws.send(f"{sleep_of(length)} &\r".encode())
            [pid] = until(lambda: sleeping(length))
            server.proc.send_signal(signal.SIGTERM)
            with contextlib.suppress(Exception):
                while True:
                    ws.recv(timeout=HANG_GUARD)
            assert ws.close_code == PROTOCOL["close_closing"]
        server.proc.wait(timeout=HANG_GUARD)
        assert not alive(pid)
        closes = [e for e in server.events() if e.get("action") == "terminal.close"]
        assert [e["result"] for e in closes] == ["service_closing"]
