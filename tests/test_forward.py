"""Port forwarding (backend/rcman/forward.py, backend/rcman/routers/forward.py), on the host
runtime: Caddy's ask, the listing and the hand-off, the forwarding server's check of where a
request comes from, what it passes to the app and back, a WebSocket spliced through it, its
sessions ended when idle, at their lifetime and when forwarding is turned off, no way in in any
address, log or audit row, and its audit.

The forwarding server listens on the settings' ``forward_socket``, in a held folder of a short
path, as the installer's Caddy reaches it: each request here is one Caddy would pass on, on a
connection the test keeps open between requests as Caddy's pool does. The workspaces' plans are
given a forwarded port directly; the app on it is ``Upstream``, on the host's 127.0.0.1, which
the host runtime's relays reach.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import logging
import os
import re
import socket
import sqlite3
import threading
import urllib.parse
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from rcman import db, devcontainer, forward
from rcman.forwarding_settings import DEFAULTS

from devtools import held_folders as hf
from tests.conftest import ALICE, BOB, ORIGIN, as_user, changing, made_workspace
from tests.hang_guard import HANG_GUARD
from tests.test_terminals import alive
from tests.time_doubles import Clock
from tests.until import until

DOMAIN = "rcman.example.com"  # the host of the tests' public origin (tests/conftest.py ORIGIN)
SETTINGS_PATH = "/api/forwarding/settings"
# The ask site's address, as Caddy passes it on in Host: 127.0.0.1 and the installer's ask port,
# here one below 1024, which no test binds.
ASK = "127.0.0.1:999"
# What a browser sends with a navigation it starts itself, and with one from rcman's page.
TYPED = {"Sec-Fetch-Site": "none", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "document"}
FROM_RCMAN = {"Sec-Fetch-Site": "same-site", "Sec-Fetch-Mode": "navigate",
              "Sec-Fetch-Dest": "document"}  # fmt: skip


# --- the app in the workspace --------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    """The app on the forwarded port: it records each request and answers it with what it
    was sent, as JSON; ``/cookies`` sets cookies, rcman's names among them; and an upgrade to a
    WebSocket is answered 101, then every byte that comes is sent back."""

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
        else:
            self._send(json.dumps(seen).encode(), [("Content-Type", "application/json")])

    do_GET = do_POST = _answer

    def _send(self, body: bytes, headers: list[tuple[str, str]]) -> None:
        self.send_response(200)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _echo(self) -> None:
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

    def json(self):
        return json.loads(self.body)


class Forward(http.client.HTTPConnection):
    """A connection to the forwarding server's socket, kept open between requests as Caddy's
    pooled ones are."""

    def __init__(self, path: Path):
        super().__init__("forward", timeout=HANG_GUARD)
        self.unix = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(HANG_GUARD)
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


def asked(conn: Forward, domain: str) -> int:
    """What the ask answers for ``domain``, asked at the ask's address."""
    query = urllib.parse.urlencode({"domain": domain})
    return conn.ask("GET", f"{forward.ASK_PATH}?{query}", {"Host": ASK}).status


def hand_off(client, host: str):
    headers = as_user(ALICE) | {"Sec-Fetch-Dest": "document"}
    return client.get(forward.HAND_OFF_PATH, params={"host": host}, headers=headers)


TOKEN = re.compile(r'name="token" value="([^"]+)"')


def token_of(client, host: str) -> str:
    r = hand_off(client, host)
    assert r.status_code == 200, r.text
    return TOKEN.search(r.text)[1]


def enter(conn: Forward, host: str, token: str, cookie: str) -> Answer:
    headers = {"Host": host, "Content-Type": "application/x-www-form-urlencoded", **FROM_RCMAN,
               "Origin": ORIGIN, "Cookie": cookie}  # fmt: skip
    return conn.ask("POST", forward.ENTER_PATH, headers, urllib.parse.urlencode({"token": token}))


def let_in(client, conn: Forward, host: str) -> str:
    """Alice let in to ``host`` as a browser is, from a navigation to its root: the Cookie
    that carries the session."""
    path = "/"
    first = conn.ask("GET", path, {"Host": host, **TYPED})
    assert first.status == 303, first.body
    returning = first.cookie(forward.RETURN_COOKIE)
    assert returning
    answer = enter(conn, host, token_of(client, host), f"{forward.RETURN_COOKIE}={returning}")
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
        self.sock.settimeout(HANG_GUARD)
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
        """Whether the server has closed it: waits for its end, each read within the hang guard."""
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


# --- the hand-off ----------------------------------------------------------------------------


def test_a_hand_off_gives_a_way_in_marks_the_host_and_is_audited(client, on, conn):
    [host] = forwarded(client, ALICE, [6080])
    assert asked(conn, host) == 404  # Caddy's ask: never marked as wanted
    r = hand_off(client, host)
    assert r.status_code == 200 and TOKEN.search(r.text)
    assert asked(conn, host) == 200  # the owner opened it: marked as wanted
    assert rows_of(client, "forward.hand_off") == [(ALICE, host, "ok")]


# --- every request ------------------------------------------------------------------------


def test_sibling_fetch_post_ws_refused_own_host_passes(client, on, conn, upstream, sockets):
    [hers] = forwarded(client, ALICE, [upstream.port], "hers")
    [his] = forwarded(client, BOB, [upstream.port], "his")
    cookie = let_in(client, conn, hers)
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
    [seen] = ({k.lower(): v for k, v in s["headers"]} for s in upstream.seen)
    local = f"localhost:{upstream.port}"
    assert (seen["host"], seen["origin"]) == (local, f"http://{local}")


def test_upstream_cannot_set_rcman_cookies(client, on, conn, upstream):
    [host] = forwarded(client, ALICE, [upstream.port])
    cookie = let_in(client, conn, host)
    r = conn.ask("GET", "/cookies", own(host, cookie))
    assert r.status == 200
    assert r.all("set-cookie") == ["app=kept; Path=/"]
    assert conn.ask("GET", "/page", own(host, cookie)).status == 200  # its session is untouched


def test_101_spliced_raw_ws_open_close_audited(client, on, conn, upstream, sockets):
    [host] = forwarded(client, ALICE, [upstream.port])
    cookie = let_in(client, conn, host)
    opened = sockets(client.app.state.settings.forward_socket, host, cookie)
    assert opened.status == 101, opened.head
    raw = bytes(range(256)) * 800 + b"\x00\r\n\r\nGET / HTTP/1.1\r\n\r\n\xff"
    assert opened.echo(raw) == raw  # byte for byte, both ways, far more than a frame
    assert rows_of(client, "websocket.open") == [(ALICE, host, "ok")]
    opened.close()
    until(lambda: rows_of(client, "websocket.close"))
    assert rows_of(client, "websocket.close") == [(ALICE, host, "ok")]
    until(lambda: not live_relays(client, host))
    assert raw.decode(errors="replace") not in json.dumps(audit_rows(client))


# --- sessions ------------------------------------------------------------------------------


@pytest.fixture
def clock(client):
    made = Clock()
    client.app.state.forward.clock = made
    return made


def settled(client) -> None:
    """Wait until the server has counted the end of every exchange it answered: the browser has
    the answer a moment before the session is told, and the clock is moved only after."""
    sessions = client.app.state.forward._sessions
    until(lambda: all(s.busy == len(s.splices) for s in list(sessions.values())))


def test_a_session_ends_when_idle_and_at_its_lifetime(client, on, conn, upstream, sockets, clock):
    [host] = forwarded(client, ALICE, [upstream.port])
    lifetimes = client.app.state.forwarding_settings.settings()
    cookie = let_in(client, conn, host)
    assert conn.ask("GET", "/page", own(host, cookie)).status == 200
    settled(client)
    clock.advance(lifetimes.idle_seconds + 1)
    assert conn.ask("GET", "/page", own(host, cookie)).status == 303  # idle: handed off again
    # A WebSocket open throughout keeps its session from idling, but not past its lifetime.
    cookie = let_in(client, conn, host)
    opened = sockets(client.app.state.settings.forward_socket, host, cookie)
    assert opened.status == 101 and opened.echo(b"x") == b"x"
    assert conn.ask("GET", "/page", own(host, cookie)).status == 200
    settled(client)
    clock.advance(lifetimes.absolute_seconds + 1)
    assert conn.ask("GET", "/page", own(host, cookie)).status == 303  # past its lifetime


def test_forwarding_off_closes_an_open_splice(client, on, conn, upstream, sockets):
    [host] = forwarded(client, ALICE, [upstream.port])
    cookie = let_in(client, conn, host)
    opened = sockets(client.app.state.settings.forward_socket, host, cookie)
    assert opened.status == 101 and opened.echo(b"x") == b"x"
    turned_off(client)
    assert opened.ended()
    until(lambda: rows_of(client, "websocket.close"))
    assert rows_of(client, "websocket.close") == [(ALICE, host, "forwarding_off")]
    r = conn.ask("GET", "/page", own(host, cookie))
    assert (r.status, json.loads(r.body)["code"]) == (404, "forwarding_off")
    until(lambda: not live_relays(client, host))
    r = client.put(SETTINGS_PATH, json={**DEFAULTS.model_dump(), "enabled": True},
                   headers=changing(ALICE))  # fmt: skip
    assert r.status_code == 200
    assert conn.ask("GET", "/page", own(host, cookie)).status == 303  # every session went


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
    until(lambda: rows_of(client, "websocket.close"))
    actions = {r["action"] for r in audit_rows(client)}
    wanted = {a for h in forward.HANDLERS.values() for a in h.actions}
    assert wanted and wanted <= actions, wanted - actions
