"""Port forwarding (backend/rcman/forward.py, backend/rcman/routers/forward.py), on the host
runtime: Caddy's ask, the hand-off and its ways in, the forwarding server's checks of every
request, what it passes to the app and back, its caps, its sessions and how they end, and its
audit.

The forwarding server listens on the settings' ``forward_socket``, in a held folder of a short
path, as the installer's Caddy reaches it: each request here is one Caddy would pass on, on a
connection the test keeps open between requests as Caddy's pool does. The workspaces' plans are
given a forwarded port directly; the app on it is ``Upstream``, on the host's 127.0.0.1, which
the host runtime's relays reach.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import http.client
import json
import logging
import os
import queue
import re
import select
import socket
import sqlite3
import threading
import time
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from rcman import db, devcontainer, forward
from rcman.forwarding_settings import DEFAULTS
from rcman.ratelimit import TokenBuckets

from devtools import held_folders as hf
from tests.conftest import ALICE, BOB, ORIGIN, as_user, changing, made_workspace
from tests.test_terminals import alive, wait_until

DOMAIN = "rcman.example.com"  # the host of the tests' public origin (tests/conftest.py ORIGIN)
SETTINGS_PATH = "/api/forwarding/settings"
# How long the tests wait for an answer, or for something to end.
ANSWERS = 15.0
# The ask site's address, as Caddy passes it on in Host: 127.0.0.1 and the installer's ask port,
# here one below 1024, which no test binds.
ASK = "127.0.0.1:999"
# A body more than a pipe holds, answered after the client has half-closed its connection.
BIG = bytes(range(256)) * 4096
HALF_CLOSED_FOR = 0.5
# How long the app's endless answer goes on at most, a chunk each TICK, and how long a test reads
# it to see that it streams on.
ENDLESS_FOR, TICK, STREAMS_FOR = 4 * ANSWERS, 0.02, 1.0
# How long a relay takes to start in the test that holds every request under way at once, and how
# many requests come at once there.
STARTS_SLOWLY, AT_ONCE = 0.5, 4
# What a browser sends with a navigation it starts itself, and with one from rcman's page.
TYPED = {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
FROM_RCMAN = {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "navigate",
              "Sec-Fetch-Dest": "document"}  # fmt: skip


# --- the app in the workspace --------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    """The app on the forwarded port: it records each request and answers it with what it
    was sent, as JSON; ``/cookies`` sets cookies, rcman's names among them; ``/framed`` sends a
    policy of its own; ``/big`` answers BIG once HALF_CLOSED_FOR has passed, and closes;
    ``/endless`` answers chunk after chunk until its connection closes; ``/held`` says it has the
    request (``arrived``) and answers once the test lets it (``gate``); ``/once`` answers as if it
    kept the connection, and closes it; ``/drop`` closes it unanswered; and an upgrade to a
    WebSocket is answered 101 (at ``/held-ws`` once the test lets it), then every byte that comes
    is sent back."""

    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _answer(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        seen = {"method": self.command, "path": self.path, "headers": self.headers.items(),
                "body": body.decode(errors="replace")}  # fmt: skip
        self.server.seen.append(seen)
        if self.headers.get("Upgrade", "").lower() == "websocket":
            self._echo()
        elif self.path.startswith("/cookies"):
            self._send(b"cookies", [
                ("Set-Cookie", f"{forward.SESSION_COOKIE}=planted; Path=/; Secure"),
                ("Set-Cookie", "__Host-rcman-auth=planted; Path=/; Secure"),
                ("Set-Cookie", "__HOST-RCMAN-return=planted; Path=/; Secure"),
                ("Set-Cookie", "app=kept; Path=/"),
            ])  # fmt: skip
        elif self.path.startswith("/framed"):
            self._send(b"framed", [("Content-Security-Policy", "default-src 'self'")])
        elif self.path.startswith("/big"):
            time.sleep(HALF_CLOSED_FOR)
            self._send(BIG, [("Connection", "close")])
        elif self.path.startswith("/endless"):
            self._endless()
        elif self.path.startswith("/held"):
            self.server.arrived.release()
            self.server.gate.acquire(timeout=ANSWERS)
            self._send(b"held", [])
        elif self.path.startswith("/once"):
            self._send(b"once", [])
            self.close_connection = True  # without saying so
        elif self.path.startswith("/drop"):
            self.close_connection = True  # read, and dropped unanswered
        else:
            self._send(json.dumps(seen).encode(), [("Content-Type", "application/json")])

    do_GET = do_POST = do_PUT = do_DELETE = _answer

    def _send(self, body: bytes, headers: list[tuple[str, str]]) -> None:
        self.send_response(200)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _endless(self) -> None:
        """Chunk after chunk, until its connection is closed or ENDLESS_FOR has passed."""
        self.send_response(200)
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.close_connection = True
        deadline = time.monotonic() + ENDLESS_FOR
        with contextlib.suppress(OSError):
            while time.monotonic() < deadline:
                self.wfile.write(b"5\r\ntick\n\r\n")
                time.sleep(TICK)
            self.wfile.write(b"0\r\n\r\n")

    def _echo(self) -> None:
        if self.path.startswith("/held"):
            self.server.arrived.release()
            self.server.gate.acquire(timeout=ANSWERS)
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.end_headers()
        self.close_connection = True
        while data := self.rfile.read1(65536):
            self.wfile.write(data)


class Upstream(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.seen: list[dict] = []
        self.arrived = threading.Semaphore(0)  # a held request has come
        self.gate = threading.Semaphore(0)  # a held request may be answered

    @property
    def port(self) -> int:
        return self.server_address[1]


@pytest.fixture
def upstream():
    """The app (Handler), listening on a port the kernel gives; ended afterwards."""
    served = Upstream()
    thread = threading.Thread(target=served.serve_forever, daemon=True)
    thread.start()
    try:
        yield served
    finally:
        served.shutdown()
        served.server_close()


# --- the service, with its forwarding server ----------------------------------------------


@pytest.fixture
def folder():
    with hf.held("rcfw-") as made:
        yield made


@pytest.fixture
def settings(settings, folder):
    """The tests' settings with the forwarding server's socket, which a socket's path must keep
    short, in a held folder."""
    return settings.model_copy(update={"forward_socket": folder / "forward.sock"})


@pytest.fixture
def on(client):
    """Forwarding turned on, as an administrator saves it."""
    r = client.put(SETTINGS_PATH, json={**DEFAULTS.model_dump(), "enabled": True},
                   headers=changing(ALICE))  # fmt: skip
    assert r.status_code == 200, r.text


def turned_off(client) -> None:
    r = client.put(SETTINGS_PATH, json=DEFAULTS.model_dump(), headers=changing(ALICE))
    assert r.status_code == 200, r.text


class Clock:
    """The forwarding server's clock, moved by the test."""

    def __init__(self):
        self.now = time.monotonic()

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock(client):
    made = Clock()
    client.app.state.forward.clock = made
    return made


def forwarded(
    client, who: str, ports: list[int], name: str = "work", labels: dict | None = None
) -> list[str]:
    """A ready workspace of ``who``'s whose plan forwards ``ports``, each with its label of
    ``labels`` where it has one: the host of each."""
    workspace = made_workspace(client, who, name)
    service = client.app.state.workspaces
    found = service.get(workspace["id"])
    user = service.runtime.layout(found.container).user
    plan = devcontainer.Plan(
        image="planted", user_name=user, run_as=user, uid=os.getuid(), gid=os.getgid(),
        ports=tuple(devcontainer.Port(p, (labels or {}).get(p)) for p in ports),
    )  # fmt: skip
    con = db.connect(client.app.state.settings.database)
    try:
        con.execute(
            "update workspaces set environment = ? where id = ?", (plan.to_json(), found.id)
        )
        con.commit()
    finally:
        con.close()
    return [f"{p}-{found.id}.ws.{DOMAIN}" for p in ports]


def workspace_of(host: str) -> int:
    return int(host.split(".")[0].split("-")[1])


def set_state(client, host: str, state: str) -> None:
    con = db.connect(client.app.state.settings.database)
    try:
        con.execute("update workspaces set state = ? where id = ?", (state, workspace_of(host)))
        con.commit()
    finally:
        con.close()


def set_rebuild_state(client, host: str, state: str | None) -> None:
    con = db.connect(client.app.state.settings.database)
    try:
        con.execute(
            "update workspaces set rebuild_state = ? where id = ?", (state, workspace_of(host))
        )
        con.commit()
    finally:
        con.close()


# --- requests, as Caddy passes them on ----------------------------------------------------


@dataclass
class Answer:
    status: int
    headers: list[tuple[str, str]]
    body: bytes

    def header(self, name: str) -> str | None:
        found = self.all(name)
        return found[-1] if found else None

    def all(self, name: str) -> list[str]:
        return [v for k, v in self.headers if k.lower() == name.lower()]

    def cookie(self, name: str) -> str | None:
        """The value a Set-Cookie of ``name`` sets, if one does."""
        for line in self.all("set-cookie"):
            pair = line.partition(";")[0]
            if pair.partition("=")[0].strip() == name:
                return pair.partition("=")[2]
        return None

    def cookie_line(self, name: str) -> str | None:
        for line in self.all("set-cookie"):
            if line.partition("=")[0].strip() == name:
                return line
        return None

    def json(self):
        return json.loads(self.body)


class Forward(http.client.HTTPConnection):
    """A connection to the forwarding server's socket, kept open between requests as Caddy's
    pooled ones are."""

    def __init__(self, path: Path):
        super().__init__("forward", timeout=ANSWERS)
        self.unix = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(ANSWERS)
        self.sock.connect(str(self.unix))

    def ask(self, method: str, target: str, headers: dict, body: bytes | str | None = None):
        self.request(method, target, body=body, headers=headers)
        answer = self.getresponse()
        return Answer(answer.status, answer.getheaders(), answer.read())


@pytest.fixture
def conn(client):
    made = Forward(client.app.state.settings.forward_socket)
    try:
        yield made
    finally:
        made.close()


def asked(conn: Forward, domain: str, at: str = ASK) -> int:
    """What the ask answers for ``domain``, asked at ``at``."""
    query = urllib.parse.urlencode({"domain": domain})
    return conn.ask("GET", f"{forward.ASK_PATH}?{query}", {"Host": at}).status


def hand_off(client, host: str, who: str = ALICE, dest: str | None = "document"):
    headers = as_user(who) | ({"Sec-Fetch-Dest": dest} if dest else {})
    return client.get(forward.HAND_OFF_PATH, params={"host": host}, headers=headers)


TOKEN = re.compile(r'name="token" value="([^"]+)"')


def token_of(client, host: str, who: str = ALICE) -> str:
    r = hand_off(client, host, who)
    assert r.status_code == 200, r.text
    return TOKEN.search(r.text)[1]


def enter(conn: Forward, host: str, token: str, cookie: str | None = None,
          origin: str | None = ORIGIN) -> Answer:  # fmt: skip
    headers = {"Host": host, "Content-Type": "application/x-www-form-urlencoded", **FROM_RCMAN}
    if origin is not None:
        headers["Origin"] = origin
    if cookie:
        headers["Cookie"] = cookie
    return conn.ask("POST", forward.ENTER_PATH, headers, urllib.parse.urlencode({"token": token}))


def let_in(client, conn: Forward, host: str, who: str = ALICE, path: str = "/") -> str:
    """``who`` let in to ``host`` as a browser is, from a navigation to ``path``: the Cookie
    that carries the session."""
    first = conn.ask("GET", path, {"Host": host, **TYPED})
    assert first.status == 303, first.body
    returning = first.cookie(forward.RETURN_COOKIE)
    assert returning
    answer = enter(conn, host, token_of(client, host, who), f"{forward.RETURN_COOKIE}={returning}")
    assert answer.status == 303, answer.body
    assert answer.header("location") == path
    return f"{forward.SESSION_COOKIE}={answer.cookie(forward.SESSION_COOKIE)}"


def own(host: str, cookie: str, **more) -> dict:
    """The headers of a request from one of the host's own pages."""
    return {"Host": host, "Cookie": cookie, "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty", **more}  # fmt: skip


class Socket:
    """An upgrade to a WebSocket through the forwarding server, then raw bytes both ways."""

    def __init__(self, path: Path, host: str, cookie: str, origin: str | None = None,
                 site: str = "same-origin"):  # fmt: skip
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(ANSWERS)
        self.sock.connect(str(path))
        lines = [
            "GET /ws HTTP/1.1", f"Host: {host}", "Upgrade: websocket", "Connection: Upgrade",
            "Sec-WebSocket-Version: 13", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
            f"Origin: {origin or f'https://{host}'}", f"Sec-Fetch-Site: {site}",
            "Sec-Fetch-Mode: websocket", "Sec-Fetch-Dest: empty", f"Cookie: {cookie}",
        ]  # fmt: skip
        self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        self.held = b""
        while b"\r\n\r\n" not in self.held:
            data = self.sock.recv(65536)
            assert data, self.held
            self.held += data
        head, _, self.held = self.held.partition(b"\r\n\r\n")
        self.head = head.decode()
        self.status = int(self.head.split()[1])

    def echo(self, data: bytes) -> bytes:
        self.sock.sendall(data)
        while len(self.held) < len(data):
            got = self.sock.recv(65536)
            assert got, len(self.held)
            self.held += got
        back, self.held = self.held[: len(data)], self.held[len(data) :]
        return back

    def ended(self) -> bool:
        """Whether the server has closed it: waits for its end, at most ANSWERS."""
        try:
            while self.sock.recv(65536):
                pass
        except (TimeoutError, OSError):
            return False
        return True

    def close(self) -> None:
        self.sock.close()


@pytest.fixture
def sockets():
    made: list[Socket] = []

    def make(*args, **kwargs) -> Socket:
        made.append(opened := Socket(*args, **kwargs))
        return opened

    yield make
    for opened in made:
        opened.close()


def audit_rows(client) -> list[dict]:
    con = sqlite3.connect(client.app.state.settings.database)
    con.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in con.execute("select * from audit order by id")]
    finally:
        con.close()


def rows_of(client, action: str) -> list[tuple]:
    return [(r["email"], r["target"], r["result"]) for r in audit_rows(client)
            if r["action"] == action]  # fmt: skip


def live_relays(client, host: str) -> list[int]:
    """The relays to the workspace's ports that run, by their pidfiles, which a relay removes
    as it ends."""
    service = client.app.state.workspaces
    found = service.get(workspace_of(host))
    run = Path(service.layout_of(found).run)
    shells = []
    for pidfile in run.glob("forward-*.pid"):
        with contextlib.suppress(FileNotFoundError):
            shells.append(pidfile.read_text().strip())
    return [int(s) for s in shells if s and alive(int(s))]


def kept(client, host: str, port: int) -> None:
    """Wait until a relay to the port is kept idle: the server keeps it once it has passed the
    answer back, a moment after the client has it."""
    idle = client.app.state.forward._idle
    wait_until(lambda: idle.get((workspace_of(host), port)), ANSWERS)


def raw_request(path: Path, head: str, body: bytes = b"") -> socket.socket:
    """A connection to the forwarding server that has sent ``head`` and ``body``, as they are."""
    made = socket.socket(socket.AF_UNIX)
    made.settimeout(ANSWERS)
    made.connect(str(path))
    made.sendall(head.encode() + body)
    return made


def head_of(host: str, cookie: str, target: str, method: str = "GET", more=()) -> str:
    """A request's head, as from one of the host's own pages."""
    lines = [f"{method} {target} HTTP/1.1", f"Host: {host}", f"Cookie: {cookie}",
             "Sec-Fetch-Site: same-origin", "Sec-Fetch-Mode: cors", "Sec-Fetch-Dest: empty",
             *more]  # fmt: skip
    return "\r\n".join(lines) + "\r\n\r\n"


def upgrade_head(host: str, cookie: str, target: str) -> str:
    """An upgrade to a WebSocket, as from one of the host's own pages."""
    lines = [
        f"GET {target} HTTP/1.1", f"Host: {host}", "Upgrade: websocket", "Connection: Upgrade",
        "Sec-WebSocket-Version: 13", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==",
        f"Origin: https://{host}", "Sec-Fetch-Site: same-origin", "Sec-Fetch-Mode: websocket",
        "Sec-Fetch-Dest: empty", f"Cookie: {cookie}",
    ]  # fmt: skip
    return "\r\n".join(lines) + "\r\n\r\n"


def read_to_end(sock: socket.socket, within: float = ANSWERS) -> tuple[bytes, bool]:
    """What comes on ``sock`` for at most ``within`` seconds, and whether it ended meanwhile."""
    got, deadline = b"", time.monotonic() + within
    sock.settimeout(within)
    try:
        while time.monotonic() < deadline:
            data = sock.recv(65536)
            if not data:
                return got, True
            got += data
    except ConnectionResetError:
        return got, True
    except TimeoutError:
        pass
    return got, False


def read_head(sock: socket.socket) -> bytes:
    """An answer's head, or what came before the connection ended."""
    got = b""
    while b"\r\n\r\n" not in got and (data := sock.recv(65536)):
        got += data
    return got


# --- Caddy's ask ------------------------------------------------------------------------------


def test_ask_200_only_for_canonical_ready_wanted_host_on_the_ask_host(client, on, conn):
    [host] = forwarded(client, ALICE, [6080])
    assert asked(conn, host) == 404  # never marked as wanted
    assert hand_off(client, host).status_code == 200  # the owner opens it: marked
    assert asked(conn, host) == 200  # the planted positive: the same host, once marked
    for elsewhere in (host, DOMAIN, "127.0.0.2:999", "localhost:999"):
        assert asked(conn, host, at=elsewhere) == 404, elsewhere  # only on the ask's address
    [other] = forwarded(client, ALICE, [6090], "other")
    client.app.state.forward.mark_wanted(other.replace("6090", "6091"))  # no port of its plan
    assert asked(conn, other.replace("6090", "6091")) == 404
    set_state(client, host, "failed")  # not ready
    assert asked(conn, host) == 404
    set_state(client, host, "ready")
    assert asked(conn, host) == 200


def test_ask_refuses_noncanonical_spellings_and_when_off(client, on, conn):
    [host] = forwarded(client, ALICE, [6080])
    workspace = workspace_of(host)
    assert hand_off(client, host).status_code == 200
    assert asked(conn, host) == 200  # the positive the spellings are read against
    spellings = [
        f"06080-{workspace}.ws.{DOMAIN}", f"6080-0{workspace}.ws.{DOMAIN}",
        f"6080-{workspace}.WS.{DOMAIN}", f"6080-{workspace}.ws.{DOMAIN.upper()}",
        f"6080-{workspace}.ws.{DOMAIN}.", f"6080-{workspace}.ws.{DOMAIN}:443",
        f"+6080-{workspace}.ws.{DOMAIN}", f"6080.{workspace}.ws.{DOMAIN}",
        f"6080-{workspace}.ws.other.example.com", f"x6080-{workspace}.ws.{DOMAIN}",
        f"6080-{workspace}.{DOMAIN}", f"6080-{workspace}-x.ws.{DOMAIN}", f" {host}", "",
    ]  # fmt: skip
    for spelling in spellings:
        client.app.state.forward.mark_wanted(spelling)
        assert asked(conn, spelling) == 404, spelling
    for query in (f"domain={host}&domain={host}", f"domain={host}&x=1", f"name={host}", ""):
        assert conn.ask("GET", f"{forward.ASK_PATH}?{query}", {"Host": ASK}).status == 404, query
    turned_off(client)
    assert asked(conn, host) == 404


def test_ask_bucket_overflow_answers_404_for_new_hosts_only(client, on, conn):
    first, second = forwarded(client, ALICE, [6080, 6081])
    for host in (first, second):
        assert hand_off(client, host).status_code == 200
    forwarding = client.app.state.forward
    forwarding.new_hosts = TokenBuckets(1, 1e-9)  # room for one new host, and none after
    assert asked(conn, first) == 200  # new: it takes the one
    assert asked(conn, second) == 404  # new, and none is left
    assert asked(conn, first) == 200  # approved before: it takes none
    forwarding.new_hosts = TokenBuckets(1, 1e-9)  # the control: with one left, it is approved
    assert asked(conn, second) == 200


def test_ask_path_on_a_forward_host_is_refused(client, on, conn, upstream):
    [host] = forwarded(client, ALICE, [upstream.port])
    assert hand_off(client, host).status_code == 200
    target = f"{forward.ASK_PATH}?domain={host}"
    assert conn.ask("GET", target, {"Host": ASK}).status == 200  # the positive
    cookie = let_in(client, conn, host)
    for path in (target, "/.rcman/", "/.rcman/anything", "/.rcman"):
        assert conn.ask("GET", path, own(host, cookie)).status == 404, path
    assert conn.ask("GET", "/page", own(host, cookie)).status == 200  # the app's own path
    assert [s["path"] for s in upstream.seen] == ["/page"]  # none of rcman's reached it


# --- the listing: each port's address, and its host marked as wanted -----------------------


def listed(client, who: str) -> dict:
    r = client.get("/api/workspaces", headers=as_user(who))
    assert r.status_code == 200, r.text
    return r.json()


def entry_of(answer: dict, host: str) -> dict:
    return next(w for w in answer["workspaces"] if w["id"] == workspace_of(host))


def test_the_listing_gives_each_port_its_address_and_label_and_marks_its_host_wanted(
    client, on, conn
):
    hosts = forwarded(client, BOB, [680, 300], labels={680: "Pebble Emulator (noVNC)"})
    assert [asked(conn, h) for h in hosts] == [404, 404]  # not marked before the listing
    answer = listed(client, BOB)
    assert answer["forwarding"] is True
    assert entry_of(answer, hosts[0])["ports"] == [
        {"port": 680, "label": "Pebble Emulator (noVNC)", "url": f"https://{hosts[0]}/"},
        {"port": 300, "label": None, "url": f"https://{hosts[1]}/"},
    ]
    assert [asked(conn, h) for h in hosts] == [200, 200]  # shown to their owner: marked


def test_the_listing_shows_no_port_and_marks_none_while_forwarding_is_off(client, conn):
    [host] = forwarded(client, BOB, [680])
    answer = listed(client, BOB)
    assert answer["forwarding"] is False
    assert entry_of(answer, host)["ports"] == []
    client.put(SETTINGS_PATH, json={**DEFAULTS.model_dump(), "enabled": True},
               headers=changing(ALICE))  # fmt: skip
    assert asked(conn, host) == 404  # the listing while off marked nothing
    assert entry_of(listed(client, BOB), host)["ports"] != []  # the control: once on, shown
    assert asked(conn, host) == 200


def test_a_workspace_with_no_port_or_not_ready_lists_none(client, on, conn):
    plain = made_workspace(client, BOB, "plain")["id"]
    found = next(w for w in listed(client, BOB)["workspaces"] if w["id"] == plain)
    assert found["ports"] == []
    [host] = forwarded(client, BOB, [680])
    set_state(client, host, "failed")
    assert entry_of(listed(client, BOB), host)["ports"] == []
    set_state(client, host, "ready")
    assert asked(conn, host) == 404  # the failed one was not marked
    assert entry_of(listed(client, BOB), host)["ports"] != []  # the control: ready, shown


def test_while_a_rebuild_replaces_the_container_its_ports_are_listed_but_not_marked(
    client, on, conn
):
    [host] = forwarded(client, BOB, [680])
    set_rebuild_state(client, host, "switching")
    assert [p["port"] for p in entry_of(listed(client, BOB), host)["ports"]] == [680]
    set_rebuild_state(client, host, None)
    assert asked(conn, host) == 404  # not marked while it could not be reached
    listed(client, BOB)
    assert asked(conn, host) == 200  # the control: the same listing once it runs marks it


def test_an_administrator_listing_everyone_never_marks_another_persons_host(client, on, conn):
    [host] = forwarded(client, BOB, [680])
    mine = forwarded(client, ALICE, [680], "mine")[0]
    assert all(w["id"] != workspace_of(host) for w in listed(client, ALICE)["workspaces"])
    for path in ("/api/servers/everyone", "/api/attention"):  # an administrator's views
        r = client.get(path, headers=as_user(ALICE))
        assert r.status_code == 200, (path, r.text)
        assert "ports" not in r.text and host not in r.text, path
    assert asked(conn, mine) == 200  # the administrator's own listing marked their own
    assert asked(conn, host) == 404  # and never Bob's
    listed(client, BOB)  # the planted twin: the same host, shown to its owner, is marked
    assert asked(conn, host) == 200


def test_the_listing_marks_only_the_hosts_of_the_viewers_own_workspaces(client, on):
    forwarding = client.app.state.forward
    marked: list[str] = []
    real = forwarding.mark_wanted
    forwarding.mark_wanted = lambda host: (marked.append(host), real(host))[1]
    try:
        bobs = forwarded(client, BOB, [680])
        alices = forwarded(client, ALICE, [700], "mine")
        marked.clear()
        listed(client, ALICE)
        client.get("/api/servers/everyone", headers=as_user(ALICE))
        assert marked == alices  # positive: one was marked, and only the viewer's own
        listed(client, BOB)
        assert marked == alices + bobs
    finally:
        forwarding.mark_wanted = real


# --- the hand-off ----------------------------------------------------------------------------


def test_hand_off_requires_document_dest_and_owner_marks_and_audits(client, on, conn):
    [host] = forwarded(client, ALICE, [6080])
    for dest in (None, "iframe", "empty", "document, iframe"):
        r = hand_off(client, host, dest=dest)
        assert (r.status_code, r.json()["code"]) == (403, "hand_off_refused"), dest
    assert asked(conn, host) == 404  # none of those marked it
    r = hand_off(client, host, who=BOB)
    assert (r.status_code, r.json()["code"]) == (404, "port_not_found")  # not his
    assert asked(conn, host) == 404
    r = hand_off(client, host)
    assert r.status_code == 200 and TOKEN.search(r.text)
    assert asked(conn, host) == 200  # marked as wanted
    assert rows_of(client, "forward.hand_off") == [
        *[(ALICE, host, "hand_off_refused")] * 4,
        (BOB, host, "port_not_found"),
        (ALICE, host, "ok"),
    ]  # fmt: skip
    r = hand_off(client, "not a port's address")
    assert (r.status_code, r.json()["code"]) == (404, "port_not_found")
    assert rows_of(client, "forward.hand_off")[-1] == (ALICE, None, "port_not_found")


def test_admin_cannot_hand_off_to_anothers_port(client, on, conn):
    [host] = forwarded(client, BOB, [6080])
    r = hand_off(client, host, who=ALICE)  # an administrator
    assert (r.status_code, r.json()["code"]) == (404, "port_not_found")
    assert asked(conn, host) == 404  # and it was not marked
    assert hand_off(client, host, who=BOB).status_code == 200  # the control: its owner
    assert asked(conn, host) == 200


def test_hand_off_is_refused_while_forwarding_is_off(client, conn):
    [host] = forwarded(client, ALICE, [6080])
    r = hand_off(client, host)
    assert (r.status_code, r.json()["code"]) == (404, "forwarding_off")


def test_mint_caps_answer_429(client, on, monkeypatch):
    [host] = forwarded(client, ALICE, [6080])
    monkeypatch.setattr(forward, "MOST_OUTSTANDING_TOKENS", 2)
    assert [hand_off(client, host).status_code for _ in range(2)] == [200, 200]
    r = hand_off(client, host)  # two ways in are out, unspent
    assert (r.status_code, r.json()["code"]) == (429, "too_many_hand_offs")
    assert int(r.headers["Retry-After"]) >= 1
    assert hand_off(client, host, who=BOB).status_code == 404  # another's count is his own
    monkeypatch.setattr(forward, "MOST_OUTSTANDING_TOKENS", 100)
    client.app.state.forward.mints = TokenBuckets(1, 1e-9)  # one mint, and none after
    assert hand_off(client, host).status_code == 200
    r = hand_off(client, host)
    assert (r.status_code, r.json()["code"]) == (429, "too_many_hand_offs")
    assert int(r.headers["Retry-After"]) >= 1
    assert rows_of(client, "forward.hand_off")[-1] == (ALICE, host, "too_many_hand_offs")


def test_hand_off_page_strict_origin_no_store_no_frames(client, on):
    [host] = forwarded(client, ALICE, [6080])
    r = hand_off(client, host)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert r.headers["Referrer-Policy"] == "strict-origin"
    assert r.headers["Cache-Control"] == "no-store"
    assert r.headers["X-Frame-Options"] == "DENY"
    policy = r.headers["Content-Security-Policy"]
    assert "frame-ancestors 'none'" in policy and "default-src 'none'" in policy
    nonce = re.search(r"script-src 'nonce-([A-Za-z0-9_-]+)'", policy)[1]
    assert f'<script nonce="{nonce}">' in r.text
    assert f"form-action https://{host}" in policy
    form = re.search(r'<form method="post" action="([^"]+)"', r.text)
    assert form[1] == f"https://{host}{forward.ENTER_PATH}"
    assert "<noscript>" in r.text and '<button type="submit">' in r.text
    assert "<script>" not in r.text  # every script carries the nonce


# --- the way in --------------------------------------------------------------------------


def test_enter_refuses_null_or_foreign_origin(client, on, conn, upstream):
    [host] = forwarded(client, ALICE, [upstream.port])
    token = token_of(client, host)
    for origin in ("null", "https://evil.example.com", f"https://{host}", None, ORIGIN + "/"):
        r = enter(conn, host, token, origin=origin)
        assert (r.status, json.loads(r.body)["code"]) == (403, "origin_refused"), origin
        assert r.cookie(forward.SESSION_COOKIE) is None
    r = enter(conn, host, token)  # the control: from rcman's own origin, the way in still works
    assert r.status == 303 and r.cookie(forward.SESSION_COOKIE)


def test_token_single_use_expiry_binding(client, on, conn, clock, upstream):
    first, second = forwarded(client, ALICE, [upstream.port, upstream.port + 1])
    token = token_of(client, first)
    assert enter(conn, first, token).status == 303  # the positive
    again = enter(conn, first, token)
    assert (again.status, json.loads(again.body)["code"]) == (403, "forward_token_refused")
    token = token_of(client, first)
    clock.advance(forward.TOKEN_SECONDS + 1)
    late = enter(conn, first, token)
    assert (late.status, json.loads(late.body)["code"]) == (403, "forward_token_refused")
    token = token_of(client, first)
    elsewhere = enter(conn, second, token)  # handed out for the first port
    assert (elsewhere.status, json.loads(elsewhere.body)["code"]) == (403, "forward_token_refused")
    assert enter(conn, first, token).status == 403  # spent by the attempt
    assert [r[2] for r in rows_of(client, "forward.session")] == [
        "ok", "forward_token_refused", "forward_token_refused", "forward_token_refused",
        "forward_token_refused",
    ]  # fmt: skip


def returning(path: str) -> str:
    """A return cookie's value naming ``path``, as a page of the host could set one itself."""
    encoded = base64.urlsafe_b64encode(path.encode()).rstrip(b"=").decode()
    return f"{forward.RETURN_COOKIE}={encoded}"


def test_the_return_cookie_holds_the_path_alone():
    """Nothing in it is checked but the path, so nothing else is put in it."""
    value = forward.return_value("/a/b?c=1")
    assert value == forward.return_value("/a/b?c=1")
    assert value == base64.urlsafe_b64encode(b"/a/b?c=1").rstrip(b"=").decode()
    assert forward.returned_path(value) == "/a/b?c=1"


def test_return_path_from_cookie_rejects_double_slash_and_backslash_and_is_cleared(
    client, on, conn, upstream
):
    [host] = forwarded(client, ALICE, [upstream.port])
    for path, lands in (("//evil.example.com/x", "/"), ("/\\evil.example.com/x", "/"),
                        ("/fine/path?x=1&y=%2F", "/fine/path?x=1&y=%2F")):  # fmt: skip
        first = conn.ask("GET", path, {"Host": host, **TYPED})
        assert first.status == 303 and first.header("location") == (
            f"{ORIGIN}{forward.HAND_OFF_PATH}?host={host}"
        )
        line = first.cookie_line(forward.RETURN_COOKIE)
        assert all(a in line for a in ("Secure", "HttpOnly", "Path=/", "SameSite=Lax")), line
        assert "Domain" not in line
        cookie = f"{forward.RETURN_COOKIE}={first.cookie(forward.RETURN_COOKIE)}"
        r = enter(conn, host, token_of(client, host), cookie)
        assert (r.status, r.header("location")) == (303, lands), path
        cleared = r.cookie_line(forward.RETURN_COOKIE)
        assert cleared and "Max-Age=0" in cleared, path
    for planted, lands in ((returning("//evil.example.com"), "/"), (returning("/\\x"), "/"),
                           (f"{forward.RETURN_COOKIE}=junk", "/"),
                           (returning("/own?x=1"), "/own?x=1")):  # fmt: skip
        r = enter(conn, host, token_of(client, host), planted)
        assert (r.status, r.header("location")) == (303, lands), planted


# --- every request ------------------------------------------------------------------------


def test_identity_per_request_on_a_pooled_connection(client, on, conn, upstream):
    [hers] = forwarded(client, ALICE, [upstream.port], "hers")
    [his] = forwarded(client, BOB, [upstream.port], "his")
    alice = let_in(client, conn, hers, ALICE)
    bob = let_in(client, conn, his, BOB)
    pooled = conn.sock
    assert conn.ask("GET", "/a", own(hers, alice)).status == 200
    assert conn.ask("GET", "/b", own(his, bob)).status == 200
    assert conn.ask("GET", "/c", own(hers, bob)).status == 303  # his session is not for hers
    assert conn.ask("GET", "/d", {"Host": hers, **TYPED}).status == 303  # no session at all
    assert conn.ask("GET", "/e", own(hers, alice)).status == 200
    assert conn.sock is pooled  # one connection throughout
    assert [s["path"] for s in upstream.seen] == ["/a", "/b", "/e"]


def test_sibling_fetch_post_ws_refused_own_host_passes(client, on, conn, upstream, sockets):
    [hers] = forwarded(client, ALICE, [upstream.port], "hers")
    [his] = forwarded(client, BOB, [upstream.port], "his")
    cookie = let_in(client, conn, hers, ALICE)
    path = client.app.state.settings.forward_socket
    sibling = own(hers, cookie, **{"Sec-Fetch-Site": "same-site", "Origin": f"https://{his}"})
    for method in ("GET", "POST"):
        r = conn.ask(method, "/data", sibling, b"x" if method == "POST" else None)
        assert (r.status, json.loads(r.body)["code"]) == (403, "fetch_refused"), method
    assert sockets(path, hers, cookie, origin=f"https://{his}", site="same-site").status == 403
    assert upstream.seen == []
    # The planted positive: the same three from the host's own pages.
    mine = own(hers, cookie, Origin=f"https://{hers}")
    assert conn.ask("GET", "/data", mine).status == 200
    assert conn.ask("POST", "/data", mine, b"x").status == 200
    opened = sockets(path, hers, cookie)
    assert opened.status == 101 and opened.echo(b"ping") == b"ping"


def test_top_level_navigation_from_rcman_passes(client, on, conn, upstream):
    [host] = forwarded(client, ALICE, [upstream.port])
    cookie = let_in(client, conn, host)
    for site in ("same-site", "cross-site", "none", "same-origin"):
        headers = {"Host": host, "Cookie": cookie, **FROM_RCMAN, "Sec-Fetch-Site": site}
        assert conn.ask("GET", "/page", headers).status == 200, site
    for changed in ({"Sec-Fetch-Dest": "iframe"}, {"Sec-Fetch-Mode": "no-cors"},
                    {"Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}):  # fmt: skip
        headers = {"Host": host, "Cookie": cookie, **FROM_RCMAN, **changed}
        r = conn.ask("GET", "/page", headers)
        assert (r.status, json.loads(r.body)["code"]) == (403, "fetch_refused"), changed
    headers = {"Host": host, "Cookie": cookie, **FROM_RCMAN, "Origin": ORIGIN}
    r = conn.ask("POST", "/page", headers, b"x")  # a form of another site's page
    assert (r.status, json.loads(r.body)["code"]) == (403, "fetch_refused")


def test_without_sec_fetch_site_non_get_needs_own_origin(client, on, conn, upstream):
    [host] = forwarded(client, ALICE, [upstream.port])
    cookie = let_in(client, conn, host)
    bare = {"Host": host, "Cookie": cookie}
    for origin in (None, "https://evil.example.com", ORIGIN, "null"):
        headers = bare | ({"Origin": origin} if origin else {})
        r = conn.ask("POST", "/form", headers, b"x")
        assert (r.status, json.loads(r.body)["code"]) == (403, "fetch_refused"), origin
    assert conn.ask("POST", "/form", bare | {"Origin": f"https://{host}"}, b"x").status == 200
    assert conn.ask("GET", "/page", bare).status == 200  # a GET needs none
    assert [s["path"] for s in upstream.seen] == ["/form", "/page"]


# --- what passes to the app, and back --------------------------------------------------------


def test_cookies_and_identity_headers_never_reach_upstream(client, on, conn, upstream):
    [host] = forwarded(client, ALICE, [upstream.port])
    cookie = let_in(client, conn, host)
    jar = "; ".join([cookie, "__Host-rcman-auth=a", "__Host-rcman-auth_0=b", "_oauth2_proxy=c",
                     "_oauth2_proxy_csrf=d", f"{forward.RETURN_COOKIE}=e", "app=kept"])  # fmt: skip
    identity = {"X-Forwarded-Email": ALICE, "X-Forwarded-User": "alice", "X-Forwarded-Groups": "g",
                "X-Auth-Request-Email": ALICE, "X-Auth-Request-User": "alice",
                "X-Forwarded-Preferred-Username": "alice"}  # fmt: skip
    r = conn.ask("GET", "/page", own(host, jar, **identity))
    assert r.status == 200
    [seen] = upstream.seen
    names = {name.lower() for name, _ in seen["headers"]}
    assert not names & {n.lower() for n in identity}, names
    assert [v for k, v in seen["headers"] if k.lower() == "cookie"] == ["app=kept"]
    secret = cookie.partition("=")[2]
    assert secret not in json.dumps(seen)
    assert "sec-fetch-site" in names  # the positive: what is not rcman's still passes


def test_host_origin_rewritten(client, on, conn, upstream):
    [host] = forwarded(client, ALICE, [upstream.port])
    cookie = let_in(client, conn, host)
    mine = own(host, cookie, Origin=f"https://{host}")
    assert conn.ask("POST", "/form", mine, b"x").status == 200
    assert (
        conn.ask(
            "GET",
            "/page",
            {"Host": host, "Cookie": cookie, "Origin": "https://elsewhere.example.com"},
        ).status
        == 200
    )
    first, second = ({k.lower(): v for k, v in s["headers"]} for s in upstream.seen)
    local = f"localhost:{upstream.port}"
    assert (first["host"], first["origin"]) == (local, f"http://{local}")
    assert (second["host"], second["origin"]) == (local, "https://elsewhere.example.com")


def test_upstream_cannot_set_rcman_cookies(client, on, conn, upstream):
    [host] = forwarded(client, ALICE, [upstream.port])
    cookie = let_in(client, conn, host)
    r = conn.ask("GET", "/cookies", own(host, cookie))
    assert r.status == 200
    assert r.all("set-cookie") == ["app=kept; Path=/"]
    assert conn.ask("GET", "/page", own(host, cookie)).status == 200  # its session is untouched


def test_frame_ancestors_self_added(client, on, conn, upstream):
    [host] = forwarded(client, ALICE, [upstream.port])
    cookie = let_in(client, conn, host)
    for path, own_policy in (("/framed", ["default-src 'self'"]), ("/page", [])):
        r = conn.ask("GET", path, own(host, cookie))
        assert r.status == 200
        assert sorted(r.all("content-security-policy")) == sorted(
            [*own_policy, "frame-ancestors 'self'"]
        ), path


def test_101_spliced_raw_ws_open_close_audited(client, on, conn, upstream, sockets):
    [host] = forwarded(client, ALICE, [upstream.port])
    cookie = let_in(client, conn, host)
    opened = sockets(client.app.state.settings.forward_socket, host, cookie)
    assert opened.status == 101, opened.head
    raw = bytes(range(256)) * 800 + b"\x00\r\n\r\nGET / HTTP/1.1\r\n\r\n\xff"
    assert opened.echo(raw) == raw  # byte for byte, both ways, far more than a frame
    assert rows_of(client, "websocket.open") == [(ALICE, host, "ok")]
    opened.close()
    wait_until(lambda: rows_of(client, "websocket.close"), ANSWERS)
    assert rows_of(client, "websocket.close") == [(ALICE, host, "ok")]
    wait_until(lambda: not live_relays(client, host), ANSWERS)
    assert raw.decode(errors="replace") not in json.dumps(audit_rows(client))


def test_a_request_whose_client_half_closes_still_gets_its_whole_answer_and_the_relay_ends(
    client, on, conn, upstream
):
    """The relay carries no half-close: the forwarding server keeps a relay's input open until
    the app's answer is passed back, or the app has closed (project-knowledge/runtime.md,
    "Forwarded ports"), so a client that ends its sending side first still gets all of it."""
    [host] = forwarded(client, ALICE, [upstream.port])
    cookie = let_in(client, conn, host)
    raw = socket.socket(socket.AF_UNIX)
    raw.settimeout(ANSWERS)
    raw.connect(str(client.app.state.settings.forward_socket))
    try:
        head = f"GET /big HTTP/1.1\r\nHost: {host}\r\nCookie: {cookie}\r\n" + (
            "Sec-Fetch-Site: same-origin\r\nSec-Fetch-Mode: cors\r\nSec-Fetch-Dest: empty\r\n\r\n"
        )
        raw.sendall(head.encode())
        raw.shutdown(socket.SHUT_WR)  # before the app answers (HALF_CLOSED_FOR)
        got = b""
        while data := raw.recv(1 << 16):
            got += data
    finally:
        raw.close()
    head, _, body = got.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 200"), head
    assert body == BIG
    wait_until(lambda: not live_relays(client, host), ANSWERS)  # and the relay ended after it


# --- caps --------------------------------------------------------------------------------------


def test_caps_answer_503(client, on, conn, upstream, sockets, monkeypatch):
    hers, other = forwarded(client, ALICE, [upstream.port, upstream.port + 1])
    [second] = forwarded(client, ALICE, [upstream.port], "second")
    cookie, other_cookie, second_cookie = (let_in(client, conn, h) for h in (hers, other, second))
    path = client.app.state.settings.forward_socket
    monkeypatch.setattr(forward, "MOST_RELAYS_PER_WORKSPACE", 1)  # the planted overflow
    held = sockets(path, hers, cookie)  # holds the workspace's one relay
    assert held.status == 101
    r = Forward(path).ask("GET", "/page", own(hers, cookie))
    assert (r.status, json.loads(r.body)["code"]) == (503, "too_many_relays")
    assert int(r.header("retry-after")) >= 1
    # Another workspace has room of its own; its relay is kept for the next request.
    assert conn.ask("GET", "/page", own(second, second_cookie)).status == 200
    monkeypatch.setattr(forward, "MOST_RELAYS_PER_WORKSPACE", 100)
    monkeypatch.setattr(forward, "MOST_RELAYS_PER_PERSON", 1)
    # Her WebSocket holds her one: the relay kept idle is ended to make room, and is not enough.
    r = Forward(path).ask("GET", "/page", own(other, other_cookie))
    assert (r.status, json.loads(r.body)["code"]) == (503, "too_many_relays")
    held.close()
    wait_until(lambda: not live_relays(client, hers) and not live_relays(client, second), ANSWERS)
    assert Forward(path).ask("GET", "/page", own(second, second_cookie)).status == 200


# --- sessions ------------------------------------------------------------------------------


def settled(client) -> None:
    """Wait until the server has counted the end of every exchange it answered: the browser has
    the answer a moment before the session is told, and the clock is moved only after."""
    sessions = client.app.state.forward._sessions
    wait_until(lambda: all(s.busy == len(s.splices) for s in list(sessions.values())), ANSWERS)


def test_an_idle_pooled_connection_does_not_keep_a_session_alive(
    client, on, conn, upstream, sockets, clock
):
    [host] = forwarded(client, ALICE, [upstream.port])
    idle = client.app.state.forwarding_settings.settings().idle_seconds
    cookie = let_in(client, conn, host)
    assert conn.ask("GET", "/page", own(host, cookie)).status == 200
    settled(client)
    clock.advance(idle + 1)  # the connection stays open, and nothing is asked on it
    assert conn.ask("GET", "/page", own(host, cookie)).status == 303
    # The planted positive: a WebSocket open throughout keeps its session alive.
    cookie = let_in(client, conn, host)
    opened = sockets(client.app.state.settings.forward_socket, host, cookie)
    assert opened.status == 101
    settled(client)
    clock.advance(idle + 1)
    assert conn.ask("GET", "/page", own(host, cookie)).status == 200
    assert opened.echo(b"still") == b"still"


def test_absolute_lifetime_closes_open_websockets_and_next_request_hands_off(
    client, on, conn, upstream, sockets, clock
):
    [host] = forwarded(client, ALICE, [upstream.port])
    lifetime = client.app.state.forwarding_settings.settings().absolute_seconds
    cookie = let_in(client, conn, host)
    opened = sockets(client.app.state.settings.forward_socket, host, cookie)
    assert opened.status == 101 and opened.echo(b"x") == b"x"
    clock.advance(lifetime + 1)
    assert opened.ended()  # closed by the server, with no request to notice it
    assert conn.ask("GET", "/page", own(host, cookie)).status == 303
    wait_until(lambda: rows_of(client, "websocket.close"), ANSWERS)
    assert rows_of(client, "websocket.close") == [(ALICE, host, "session_ended")]


def test_forwarding_off_closes_an_open_splice(client, on, conn, upstream, sockets):
    [host] = forwarded(client, ALICE, [upstream.port])
    cookie = let_in(client, conn, host)
    opened = sockets(client.app.state.settings.forward_socket, host, cookie)
    assert opened.status == 101 and opened.echo(b"x") == b"x"
    turned_off(client)
    assert opened.ended()
    wait_until(lambda: rows_of(client, "websocket.close"), ANSWERS)
    assert rows_of(client, "websocket.close") == [(ALICE, host, "forwarding_off")]
    r = conn.ask("GET", "/page", own(host, cookie))
    assert (r.status, json.loads(r.body)["code"]) == (404, "forwarding_off")
    wait_until(lambda: not live_relays(client, host), ANSWERS)
    turned_off(client)  # off again: harmless
    r = client.put(SETTINGS_PATH, json={**DEFAULTS.model_dump(), "enabled": True},
                   headers=changing(ALICE))  # fmt: skip
    assert r.status_code == 200
    assert conn.ask("GET", "/page", own(host, cookie)).status == 303  # every session went


def test_the_off_listener_from_another_thread_closes_open_splices_and_does_no_harm_again(
    client, on, conn, upstream, sockets
):
    """The settings' save calls it in its request's thread, under the save's lock: it only
    passes the work to the server's loop, and saves nothing."""
    [host] = forwarded(client, ALICE, [upstream.port])
    cookie = let_in(client, conn, host)
    opened = sockets(client.app.state.settings.forward_socket, host, cookie)
    assert opened.status == 101
    forwarding = client.app.state.forward
    called = threading.Thread(target=forwarding.off)
    called.start()
    called.join(ANSWERS)
    assert opened.ended()
    forwarding.off()  # again, with nothing left: no harm
    forwarding.off()
    assert conn.ask("GET", "/page", own(host, cookie)).status == 303  # its session went too
    assert client.app.state.forwarding_settings.settings().enabled  # and nothing was saved


def test_a_session_made_as_forwarding_turns_off_is_refused(client, on, conn, upstream, monkeypatch):
    """A way in that passed its checks just before forwarding was turned off makes no
    session: the setting is read again as the session is made."""
    [host] = forwarded(client, ALICE, [upstream.port])
    forwarding = client.app.state.forward
    checked = forwarding.refusal

    def turning_off(person, target):
        found = checked(person, target)
        # Meanwhile, as the check runs in a thread of its own, an administrator saves it off.
        client.app.state.forwarding_settings.save(DEFAULTS)
        return found

    token = token_of(client, host)
    monkeypatch.setattr(forwarding, "refusal", turning_off)
    r = enter(conn, host, token)
    assert (r.status, json.loads(r.body)["code"]) == (404, "forwarding_off")
    assert r.cookie(forward.SESSION_COOKIE) is None


def test_removed_person_or_deleted_workspace_loses_access_at_once(
    client, on, conn, upstream, sockets
):
    [hers] = forwarded(client, ALICE, [upstream.port], "hers")
    [his] = forwarded(client, BOB, [upstream.port], "his")
    path = client.app.state.settings.forward_socket
    alice, bob = let_in(client, conn, hers, ALICE), let_in(client, conn, his, BOB)
    her_socket, his_socket = sockets(path, hers, alice), sockets(path, his, bob)
    assert her_socket.status == his_socket.status == 101
    r = client.delete(f"/api/workspaces/{workspace_of(hers)}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert her_socket.ended()
    assert conn.ask("GET", "/page", own(hers, alice)).status != 200
    assert his_socket.echo(b"his") == b"his"  # the control: his is untouched
    assert conn.ask("GET", "/page", own(his, bob)).status == 200
    listed = client.get("/api/people", headers=as_user(ALICE)).json()["people"]
    bob_id = next(p["id"] for p in listed if p["email"] == BOB)
    r = client.delete(f"/api/people/{bob_id}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert his_socket.ended()
    assert conn.ask("GET", "/page", own(his, bob)).status != 200
    closes = {target: result for _, target, result in rows_of(client, "websocket.close")}
    assert closes == {hers: "workspace_gone", his: "workspace_gone"}


def test_forwarding_is_refused_until_the_workspaces_are_restored(client, on, conn, upstream):
    """The restore at rcman's start sweeps every running workspace, which would end a relay
    started meanwhile (project-knowledge/runtime.md, "Forwarded ports")."""
    [host] = forwarded(client, ALICE, [upstream.port])
    cookie = let_in(client, conn, host)
    token = token_of(client, host)
    service = client.app.state.workspaces
    service.restored.clear()  # as before the restore is done
    try:
        r = conn.ask("GET", "/page", own(host, cookie))
        assert (r.status, json.loads(r.body)["code"]) == (503, "forwarding_starting")
        assert asked(conn, host) == 404
        r = enter(conn, host, token)
        assert (r.status, json.loads(r.body)["code"]) == (503, "forwarding_starting")
        assert upstream.seen == []
    finally:
        service.restored.set()
    assert conn.ask("GET", "/page", own(host, cookie)).status == 200  # the control
    assert asked(conn, host) == 200


# --- what is under way when a session ends ----------------------------------------------------

LIFETIME = "past its lifetime"
ENDINGS = ["turned off", LIFETIME]


def end_session(client, clock, ending: str) -> None:
    if ending == LIFETIME:
        clock.advance(client.app.state.forwarding_settings.settings().absolute_seconds + 1)
    else:
        turned_off(client)


@pytest.mark.parametrize("ending", ENDINGS)
def test_an_answer_still_streaming_when_its_session_ends_is_cut_off(
    client, on, conn, upstream, clock, ending
):
    [host] = forwarded(client, BOB, [upstream.port])
    cookie = let_in(client, conn, host, BOB)
    streaming = raw_request(client.app.state.settings.forward_socket,
                            head_of(host, cookie, "/endless"))  # fmt: skip
    try:
        got, ended = read_to_end(streaming, STREAMS_FOR)
        # The planted positive: left alone, it streams on.
        assert got.startswith(b"HTTP/1.1 200") and b"tick" in got and not ended, got[:200]
        end_session(client, clock, ending)
        _, ended = read_to_end(streaming)
        assert ended
    finally:
        streaming.close()
    wait_until(lambda: not live_relays(client, host), ANSWERS)


@pytest.mark.parametrize("ending", ENDINGS)
def test_an_upgrade_the_app_switches_after_its_session_ended_is_closed_unswitched(
    client, on, conn, upstream, clock, ending
):
    [host] = forwarded(client, BOB, [upstream.port])
    cookie = let_in(client, conn, host, BOB)
    path = client.app.state.settings.forward_socket
    # The planted positive: held the same way, while its session lasts, it is switched.
    first = raw_request(path, upgrade_head(host, cookie, "/held-ws"))
    try:
        assert upstream.arrived.acquire(timeout=ANSWERS)
        upstream.gate.release()
        assert read_head(first).startswith(b"HTTP/1.1 101")
    finally:
        first.close()
    late = raw_request(path, upgrade_head(host, cookie, "/held-ws"))
    try:
        assert upstream.arrived.acquire(timeout=ANSWERS)  # the app has it, and has not answered
        end_session(client, clock, ending)
        upstream.gate.release()
        got, ended = read_to_end(late)
        assert ended and b" 101 " not in got, got[:200]
    finally:
        late.close()
    assert rows_of(client, "websocket.open") == [(BOB, host, "ok")]  # the first alone opened
    wait_until(lambda: not live_relays(client, host), ANSWERS)


def test_turning_forwarding_off_ends_the_relays_kept_idle(client, on, conn, upstream):
    [host] = forwarded(client, ALICE, [upstream.port])
    cookie = let_in(client, conn, host)
    assert conn.ask("GET", "/page", own(host, cookie)).status == 200
    kept(client, host, upstream.port)
    assert live_relays(client, host)  # the planted positive: kept for the next request
    turned_off(client)
    wait_until(lambda: not live_relays(client, host), forward.POOL_IDLE_SECONDS / 2)


def test_a_persons_sessions_are_capped_ending_their_oldest_for_the_host_first(
    client, on, conn, upstream, monkeypatch
):
    monkeypatch.setattr(forward, "MOST_SESSIONS_PER_PERSON", 2)
    [hers] = forwarded(client, ALICE, [upstream.port], "hers")
    [other] = forwarded(client, ALICE, [upstream.port], "other")
    [his] = forwarded(client, BOB, [upstream.port], "his")
    his_cookie = let_in(client, conn, his, BOB)
    oldest, older = let_in(client, conn, other), let_in(client, conn, hers)
    for target, cookie in ((other, oldest), (hers, older), (his, his_cookie)):
        assert conn.ask("GET", "/page", own(target, cookie)).status == 200  # two of hers fit
    newest = let_in(client, conn, hers)
    # Her older one for the same host makes room, though another of hers is older still.
    assert conn.ask("GET", "/page", own(hers, older)).status == 303
    assert conn.ask("GET", "/page", own(other, oldest)).status == 200
    assert conn.ask("GET", "/page", own(hers, newest)).status == 200
    # With none left for the host, the oldest of all makes room.
    [elsewhere] = forwarded(client, ALICE, [upstream.port], "elsewhere")
    last = let_in(client, conn, elsewhere)
    assert conn.ask("GET", "/page", own(other, oldest)).status == 303
    assert conn.ask("GET", "/page", own(hers, newest)).status == 200
    assert conn.ask("GET", "/page", own(elsewhere, last)).status == 200
    assert conn.ask("GET", "/page", own(his, his_cookie)).status == 200  # his count is his own


# --- relays: their caps at once, and one kept that has ended --------------------------------


def test_requests_at_once_beyond_a_cap_of_one_are_refused_but_one(
    client, on, conn, upstream, monkeypatch
):
    """The room for a relay is taken before anything is awaited, so that requests at once are
    held to the cap as requests one by one are; and room taken for a relay that does not start
    is given back."""
    host, other = forwarded(client, ALICE, [upstream.port, upstream.port + 1])
    cookie, other_cookie = let_in(client, conn, host), let_in(client, conn, other)
    forwarding = client.app.state.forward
    path = client.app.state.settings.forward_socket
    monkeypatch.setattr(forward, "MOST_RELAYS_PER_WORKSPACE", 1)
    launch = forwarding._launch

    def slowly(target):
        time.sleep(STARTS_SLOWLY)  # every request is under way before the first relay runs
        return launch(target)

    monkeypatch.setattr(forwarding, "_launch", slowly)
    answered: queue.Queue = queue.Queue()

    def one() -> None:
        with contextlib.closing(Forward(path)) as made:
            answered.put(made.ask("GET", "/held", own(host, cookie)).status)

    threads = [threading.Thread(target=one, daemon=True) for _ in range(AT_ONCE)]
    for thread in threads:
        thread.start()
    early, deadline = [], time.monotonic() + ANSWERS
    while len(early) < AT_ONCE - 1 and (left := deadline - time.monotonic()) > 0:
        with contextlib.suppress(queue.Empty):
            early.append(answered.get(timeout=left))
    for _ in range(AT_ONCE):  # whatever reached the app is answered
        upstream.gate.release()
    for thread in threads:
        thread.join(ANSWERS)
    late = [answered.get_nowait() for _ in range(answered.qsize())]
    assert early == [503] * (AT_ONCE - 1), (early, late)  # while the one held its room
    assert late == [200], late
    kept(client, host, upstream.port)

    def failing(target):
        raise forward.rt.RuntimeFailed("planted")

    # The relay kept idle is ended to make room for one that does not start; then the room is
    # free again, which a new relay takes.
    monkeypatch.setattr(forwarding, "_launch", failing)
    r = conn.ask("GET", "/page", own(other, other_cookie))
    assert (r.status, json.loads(r.body)["code"]) == (502, "port_unreachable")
    assert not [r for r in forwarding._relays if isinstance(r, forward.Slot)]
    monkeypatch.setattr(forwarding, "_launch", launch)
    assert conn.ask("GET", "/page", own(host, cookie)).status == 200  # its room was given back


def test_a_kept_relay_that_ended_unnoticed_is_tried_again_once_on_a_new_one(
    client, on, conn, upstream, monkeypatch
):
    with socket.socket() as closed:
        closed.bind(("127.0.0.1", 0))  # held, and listened on by nothing
        dead = closed.getsockname()[1]
        host, nothing = forwarded(client, ALICE, [upstream.port, dead])
        cookie, other = let_in(client, conn, host), let_in(client, conn, nothing)
        forwarding = client.app.state.forward

        async def unwatched(relay, key):
            await asyncio.Event().wait()

        # A kept relay's end goes unnoticed until it is taken, as when the app closes the
        # connection just as a request comes.
        monkeypatch.setattr(forwarding, "_watch", unwatched)
        monkeypatch.setattr(forward.Relay, "usable", lambda self: not self.closed)
        launch, started = forwarding._launch, []
        monkeypatch.setattr(forwarding, "_launch", lambda t: started.append(t) or launch(t))
        assert conn.ask("GET", "/once", own(host, cookie)).status == 200  # then the app closes
        wait_until(lambda: not live_relays(client, host), ANSWERS)  # and the relay ends with it
        kept(client, host, upstream.port)  # yet kept
        assert started
        started.clear()
        r = conn.ask("GET", "/page", own(host, cookie))
        assert r.status == 200, r.body
        assert len(started) == 1  # a new one, once
        assert [s["path"] for s in upstream.seen] == ["/once", "/page"]
        # A request that is not GET, HEAD or OPTIONS, its head written whole to a kept relay the
        # app then drops it on unanswered, is not sent again: the app may have acted on it.
        kept(client, host, upstream.port)  # the new one
        started.clear()
        r = conn.ask("POST", "/drop", own(host, cookie, Origin=f"https://{host}"), b"")
        assert (r.status, json.loads(r.body)["code"]) == (502, "port_unreachable")
        assert [s["path"] for s in upstream.seen].count("/drop") == 1 and started == []
        # The control: a new relay that gets no answer is not tried again.
        started.clear()
        r = conn.ask("GET", "/page", own(nothing, other))
        assert (r.status, json.loads(r.body)["code"]) == (502, "port_unreachable")
        assert len(started) == 1


def test_a_request_not_replayable_whose_head_write_fails_on_a_kept_relay_is_not_tried_again(
    client, on, conn, upstream, monkeypatch
):
    """A head handed to a kept relay may reach the app even where its write then fails, as one
    larger than the pipe does before ``drain`` raises: a POST is not sent again. The planted
    positive: a GET whose write fails the same way is tried again once, on a new relay."""
    [host] = forwarded(client, ALICE, [upstream.port])
    cookie = let_in(client, conn, host)
    forwarding = client.app.state.forward
    launch, started = forwarding._launch, []
    monkeypatch.setattr(forwarding, "_launch", lambda t: started.append(t) or launch(t))

    async def failing(self, data):
        raise BrokenPipeError("planted")

    for method, again in (("GET", 1), ("POST", 0)):
        assert conn.ask("GET", "/page", own(host, cookie)).status == 200  # a relay kept
        kept(client, host, upstream.port)
        started.clear()
        with monkeypatch.context() as patched:
            patched.setattr(forward.Relay, "write", failing)
            headers = own(host, cookie, Origin=f"https://{host}")
            r = conn.ask(method, "/data", headers, b"" if method == "POST" else None)
        assert (r.status, json.loads(r.body)["code"]) == (502, "port_unreachable"), method
        assert len(started) == again, method
    assert "/data" not in [s["path"] for s in upstream.seen]


# --- slow requests and long heads -------------------------------------------------------------


def test_a_refused_request_is_answered_without_waiting_long_for_its_body(
    client, on, conn, upstream, monkeypatch
):
    monkeypatch.setattr(forward, "DRAIN_SECONDS", 0.5)
    [host] = forwarded(client, ALICE, [upstream.port])
    cookie = let_in(client, conn, host)
    cross = own(host, cookie, **{"Sec-Fetch-Site": "cross-site"})
    # The planted positive: its body whole, it is answered and its connection kept.
    pooled = conn.sock
    r = conn.ask("POST", "/data", cross, b"x" * 1000)
    assert (r.status, json.loads(r.body)["code"]) == (403, "fetch_refused")
    assert r.header("connection") is None
    assert conn.ask("GET", forward.HEALTH_PATH, {"Host": host}).status == 200
    assert conn.sock is pooled
    more = ["Sec-Fetch-Site: cross-site", "Content-Length: 1000"]
    dripping = raw_request(client.app.state.settings.forward_socket,
                           head_of(host, cookie, "/data", "POST", more), b"x" * 10)  # fmt: skip
    try:
        got, ended = read_to_end(dripping)
        assert got.startswith(b"HTTP/1.1 403") and ended, got
        assert b"connection: close" in got.lower()
    finally:
        dripping.close()
    assert upstream.seen == []


def test_the_way_in_does_not_wait_long_for_its_body(client, on, conn, upstream, monkeypatch):
    monkeypatch.setattr(forward, "BODY_SECONDS", 0.5)
    [host] = forwarded(client, ALICE, [upstream.port])
    token = token_of(client, host)
    head = "\r\n".join([
        f"POST {forward.ENTER_PATH} HTTP/1.1", f"Host: {host}", f"Origin: {ORIGIN}",
        "Content-Type: application/x-www-form-urlencoded", "Content-Length: 500",
        *(f"{k}: {v}" for k, v in FROM_RCMAN.items()),
    ]) + "\r\n\r\n"  # fmt: skip
    dripping = raw_request(client.app.state.settings.forward_socket, head, b"token=")
    try:
        got, ended = read_to_end(dripping)
        assert got.startswith(b"HTTP/1.1 408") and ended, got
    finally:
        dripping.close()
    assert enter(conn, host, token).status == 303  # the control: its body whole, it lets in


def test_a_connection_with_nothing_on_it_or_a_head_that_never_ends_is_closed(
    client, on, monkeypatch
):
    monkeypatch.setattr(forward, "IDLE_CONNECTION_SECONDS", 0.5)
    monkeypatch.setattr(forward, "HEAD_SECONDS", 0.5)
    path = client.app.state.settings.forward_socket
    with contextlib.closing(Forward(path)) as made:  # the planted positive: a request in time
        assert made.ask("GET", forward.HEALTH_PATH, {"Host": "x"}).status == 200
    for head in ("", "GET / HTTP/1.1\r\nHost: x"):
        waiting = raw_request(path, head)
        try:
            got, ended = read_to_end(waiting)
            assert ended and got == b"", (head, got)
        finally:
            waiting.close()


def test_a_head_dripped_a_byte_at_a_time_is_cut_off_at_its_one_deadline(client, monkeypatch):
    """HEAD_SECONDS runs from the head's first byte: bytes that keep coming do not move it."""
    monkeypatch.setattr(forward, "HEAD_SECONDS", 1.0)
    dripping = raw_request(client.app.state.settings.forward_socket, "GET / HTTP/1.1\r\nX: ")
    started, closed = time.monotonic(), False
    try:
        while not closed and time.monotonic() - started < ANSWERS:
            try:
                dripping.sendall(b"a")
            except OSError:
                closed = True
                break
            ready, _, _ = select.select([dripping], [], [], forward.HEAD_SECONDS / 4)
            closed = bool(ready) and dripping.recv(65536) == b""
    finally:
        dripping.close()
    assert closed and time.monotonic() - started < ANSWERS


def test_a_head_longer_than_the_most_is_answered_431(client, conn):
    path = client.app.state.settings.forward_socket
    # The planted positive: a head well over h11's own default, under the most, is taken.
    fits = "a" * (forward.MOST_HEAD // 2)
    assert conn.ask("GET", forward.HEALTH_PATH, {"Host": "x", "X-Long": fits}).status == 200
    long = f"GET {forward.HEALTH_PATH} HTTP/1.1\r\nHost: x\r\nX-Long: "
    long += "a" * (forward.MOST_HEAD + forward.READ_CHUNK) + "\r\n\r\n"
    made = socket.socket(socket.AF_UNIX)
    made.settimeout(ANSWERS)
    made.connect(str(path))
    try:
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            made.sendall(long.encode())
        got, ended = read_to_end(made)
        assert got.startswith(b"HTTP/1.1 431") and ended, got[:200]
    finally:
        made.close()


# --- no way in in any address, log or audit row ------------------------------------------------


def test_no_token_in_any_url_log_or_audit(client, on, conn, upstream, caplog):
    caplog.set_level(logging.DEBUG)
    [host] = forwarded(client, ALICE, [upstream.port])
    first = conn.ask("GET", "/page?q=1", {"Host": host, **TYPED})
    page = hand_off(client, host)
    token = TOKEN.search(page.text)[1]
    returned = f"{forward.RETURN_COOKIE}={first.cookie(forward.RETURN_COOKIE)}"
    answer = enter(conn, host, token, returned)
    session = answer.cookie(forward.SESSION_COOKIE)
    carried = own(host, f"{forward.SESSION_COOKIE}={session}")
    assert conn.ask("GET", "/page?q=1", carried).status == 200
    places = {
        "the address it was handed off from": first.header("location"),
        "the address of the hand-off": str(page.url),
        "where the way in sends": answer.header("location"),
        "the audit": json.dumps(audit_rows(client)),
        "the log": caplog.text,
        "the app": json.dumps(upstream.seen),
    }

    def holding(secret: str) -> list[str]:
        return [where for where, text in places.items() if secret in text]

    for secret in (token, session):
        assert holding(secret) == [], secret
    # The planted positive: the same search finds the way in where it is, and in a log line.
    assert token in page.text
    logging.getLogger("rcman.forward").warning("planted %s", token)
    places["the log"] = caplog.text
    assert holding(token) == ["the log"]


# --- the audit of the forwarding server's handlers -------------------------------------------

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "audit.md"


def unaccounted(handlers: dict, guide: str) -> list[str]:
    """The handlers that record nothing and are not exempt, and the exemptions whose reason the
    audit guide does not give, wherever its lines break."""
    guide = " ".join(guide.split())
    silent = [f"{name}: records nothing, and is not exempt" for name, handler in handlers.items()
              if not handler.actions and not handler.exempt]  # fmt: skip
    unsaid = [f"{name}: reason not in the guide" for name, handler in handlers.items()
              if handler.exempt and handler.exempt not in guide]  # fmt: skip
    return silent + unsaid


def test_every_handler_of_the_forwarding_server_is_audited_or_exempt_with_a_reason(client):
    served = set(client.app.state.forward.handlers)
    assert served == set(forward.HANDLERS)  # each way both: the table names what is served
    assert any(h.actions for h in forward.HANDLERS.values())  # non-vacuity
    assert any(h.exempt for h in forward.HANDLERS.values())
    assert unaccounted(forward.HANDLERS, GUIDE.read_text(encoding="utf-8")) == []


def test_the_forward_audit_guard_finds_a_silent_handler_and_an_unsaid_reason():
    """Planted twin."""
    guide = "The guide gives the planted reason."
    fine = {"a": forward.Handler(("planted.call",)), "b": forward.Handler((), "planted reason")}
    assert unaccounted(fine, guide) == []
    silent = {**fine, "c": forward.Handler(())}
    assert unaccounted(silent, guide) == ["c: records nothing, and is not exempt"]
    unsaid = {**fine, "d": forward.Handler((), "a reason the guide does not give")}
    assert unaccounted(unsaid, guide) == ["d: reason not in the guide"]


def test_each_audited_handler_writes_its_rows(client, on, conn, upstream, sockets):
    """Driving every handler that records: each of its actions is written."""
    [host] = forwarded(client, ALICE, [upstream.port])
    cookie = let_in(client, conn, host)
    opened = sockets(client.app.state.settings.forward_socket, host, cookie)
    assert opened.status == 101
    opened.close()
    wait_until(lambda: rows_of(client, "websocket.close"), ANSWERS)
    actions = {r["action"] for r in audit_rows(client)}
    wanted = {a for h in forward.HANDLERS.values() for a in h.actions}
    assert wanted and wanted <= actions, wanted - actions
