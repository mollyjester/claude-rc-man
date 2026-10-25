"""Port forwarding: the forwarding server, its sessions, the ways in the hand-off hands out, the
hosts marked as wanted, Caddy's ask, and the caps on relays.

A workspace's forwarded port (``Workspaces.ports_of``, its plan's ``ports``) is reached by its
owner's browser at ``https://<port>-<workspace id>.ws.<DOMAIN>/``, DOMAIN the host of
``RCMAN_PUBLIC_ORIGIN``: Caddy's wildcard site (installer/templates/Caddyfile) passes every such
request to this server, on the unix socket ``forward_socket``, and Caddy's ask site passes it the
question whether it may get a certificate for a host, at ``ASK_PATH`` with the ask site's own
address, ``127.0.0.1:<ask port>``, as the Host. Only canonical hosts are a port's (``Hosts``), so
that no port has two addresses, and two certificates. The person's own listing of their
workspaces (routers/workspaces.py) gives each port's address (``shown``), which marks its host as
wanted, as the hand-off does; nothing that lists another person's workspaces marks one.

The server is asyncio and h11 (``Forwarding``), started and stopped by the app's lifespan
(main.py); it answers nothing but its health (``HEALTH_PATH``, on any Host) until the restore at
rcman's start is done (``Workspaces.restored``), whose sweep would end a relay started
meanwhile. Every request is judged on its own, never by the connection it came on, since Caddy
pools its connections across hosts and people:

- forwarding is on (forwarding_settings.py), and the request comes from the host's own pages
  or is a top-level navigation (``fetch_refusal``);
- it carries a session cookie (``SESSION_COOKIE``) of a session for that host; a navigation
  without one is sent to the hand-off (routers/forward.py), its path kept in ``RETURN_COOKIE``;
- the session's person is active and owns the workspace, which is ready with the port in its
  plan (``refusal``).

It is then passed to the app through a relay (``Runtime.port_argv``): the cookies and headers
of rcman's own and of the sign-in removed, the Host and an Origin of the host's own made the
app's own ``localhost:<port>``; what comes back loses any cookie of rcman's names and gains a
policy that lets only the port's own pages frame it. An upgrade the app answers 101 is spliced
raw both ways. A relay ends by itself once its input ends, and so loses what is still in its
pipe and carries no half-close (project-knowledge/runtime.md, "Forwarded ports"): its input is
closed only once the app's answer has been passed back whole, or the app has closed. Relays an
answer leaves open are kept for the next request to the same port, each a while
(``POOL_IDLE_SECONDS``); the relays a workspace and a person have at once are capped.

A session lasts while it is used: an exchange under way and an open WebSocket keep it, a
connection Caddy keeps open with nothing on it does not. It ends after the idle timeout with
nothing going on, at its lifetime, when forwarding is turned off, when its workspace is deleted
or rebuilt or its person removed, and when its person makes more than they may have at once
(the oldest); its WebSockets are closed with it, and so are the connections of its exchanges
under way, an answer still streaming among them. Whatever awaits anything reads the session's
end again before it goes on, so that nothing is passed on, nor switched, once it has ended.

A request's body is waited for a short while only: the way in's for BODY_SECONDS, and the rest of
a short one an answer of the server's own leaves unread for DRAIN_SECONDS, after which the
answer closes the connection, as it does for a longer one. A connection with no
request on it, or a head not whole HEAD_SECONDS after its first byte, is closed, and a head too
long answered 431.

Python standard library, h11 and the backend only.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import html
import json
import logging
import os
import re
import secrets
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import h11

from rcman import audit, db, people, problems
from rcman import runtime as rt
from rcman.headers import SECURITY_HEADERS
from rcman.identity import Identity
from rcman.problems import Problem
from rcman.ratelimit import TokenBuckets
from rcman.workspaces import READY, REBUILD_SWITCHING

log = logging.getLogger("rcman.forward")
log.setLevel(logging.INFO)

HEALTH_PATH = "/.rcman/health"
ASK_PATH = "/.rcman/ask"
ENTER_PATH = "/.rcman/enter"
RESERVED = "/.rcman"  # the paths of rcman's own on a forward host: no app's path is under it
HAND_OFF_PATH = "/api/forward/hand-off"  # routers/forward.py, on rcman's own origin
SESSION_COOKIE = "__Host-rcman-forward"
RETURN_COOKIE = "__Host-rcman-return"
# A port's host: <port>-<workspace id>.ws.<DOMAIN>, in decimal with no leading zero.
FORWARD_HOST = r"([1-9][0-9]{0,4})-([1-9][0-9]{0,9})\.ws\."
# The ask site's address, as Caddy passes it on: the loopback and the installer's ask port.
ASK_HOST = re.compile(r"127\.0\.0\.1:[1-9][0-9]{0,4}")

# How long a host stays marked as wanted once its owner is shown its address or opens it.
WANTED_SECONDS = 15 * 60
# How long a way in the hand-off hands out may be spent, and how long a spent or expired one is
# remembered after that, so that spending it again is told and recorded as such.
TOKEN_SECONDS = 60
# How long the path a navigation was sent to the hand-off from is kept for its return.
RETURN_SECONDS = 10 * 60
MOST_RETURN_PATH = 2048  # a longer one returns to /
# The most unspent ways in a person may hold at once, and how many they may be handed in a
# burst and then each minute.
MOST_OUTSTANDING_TOKENS = 8
MINTS_BURST, MINTS_PER_MINUTE = 20, 10
# New hosts Caddy may get a certificate for: Let's Encrypt gives a registered domain 50 new
# certificates a week, for every host under it alike, and rcman takes well under that.
NEW_HOSTS_BURST, NEW_HOSTS_PER_WEEK = 10, 20
WEEK = 7 * 24 * 3600
# The most relays a workspace, and a person, may have at once, idle ones among them; how many
# idle relays are kept for each port, and for how long.
MOST_RELAYS_PER_WORKSPACE = 32
MOST_RELAYS_PER_PERSON = 64
MOST_IDLE_PER_PORT = 4
POOL_IDLE_SECONDS = 30
# The most connections the server takes at once, Caddy's and any local user's.
MOST_CONNECTIONS = 1024
# What a refusal for want of room says to wait, in seconds.
RETRY_AFTER_SECONDS = 5
# How often the sessions are looked over for their ends.
REAP_SECONDS = 1
# How long a relay is given to end once its input is closed, before its client is killed.
RELAY_ENDS_WITHIN = 10
# How long a WebSocket whose client has gone waits for the app to close its side, so that what
# the client sent last leaves the relay's pipe before its input is closed.
SPLICE_DRAIN_SECONDS = 1
# How long a thread's call waits for the loop to end what it ended.
FROM_THREAD_SECONDS = 30
READ_CHUNK = 1 << 16
MOST_ENTER_BODY = 1024
# The longest request head taken, and the longest answer head an app may send; a longer request
# head is answered 431.
MOST_HEAD = 1 << 16
# How long the way in waits for its body; how long a connection may have no request on it,
# longer than Caddy keeps an idle pooled connection to an upstream (two minutes), so that Caddy
# ends it first; and how long a request's head may take once it has begun.
BODY_SECONDS = 10
# How long an answer of the server's own waits for the rest of a short body, MOST_DRAINED at
# most, so as to keep its connection; a longer one, or one that takes longer, closes it.
DRAIN_SECONDS = 1
MOST_DRAINED = 1 << 16
IDLE_CONNECTION_SECONDS = 150
HEAD_SECONDS = 30
# The most sessions a person may have at once; one more ends their oldest.
MOST_SESSIONS_PER_PERSON = 32

# Headers that are the connection's own, never passed on.
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-connection", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "upgrade",
}  # fmt: skip
# Who the sign-in says is asking: never for an app.
IDENTITY = {
    "x-forwarded-email", "x-forwarded-user", "x-forwarded-groups",
    "x-forwarded-preferred-username", "x-forwarded-access-token",
}  # fmt: skip
IDENTITY_PREFIX = "x-auth-request-"
# Cookies of rcman's and of the sign-in's, which an app never sees nor sets.
OWN_COOKIES = ("__host-rcman-", "_oauth2_proxy")
FRAMING = ("content-security-policy", "frame-ancestors 'self'")
SAFE = ("GET", "HEAD")
# What may be sent again on a new relay once a kept one ended before it said anything: the app
# cannot have acted on these, or on any request whose head was never handed to the relay.
REPLAYABLE = ("GET", "HEAD", "OPTIONS")


@dataclass(frozen=True)
class Handler:
    """A handler of the forwarding server: the audit actions it records, and why it records
    nothing, or not everything (named in docs/audit.md)."""

    actions: tuple[str, ...]
    exempt: str | None = None


# Every handler the server has, by the name ``Forwarding.handlers`` serves it under;
# tests/test_forward.py holds the two together, and every exemption to the audit guide.
HANDLERS = {
    "health": Handler((), "whether it answers at all"),
    "ask": Handler((), "Caddy's question whether it may get a certificate for a port's address"),
    "refused": Handler((), "its refusal of a request for what is no port's"),
    "enter": Handler(("forward.session",)),
    "proxy": Handler(
        ("websocket.open", "websocket.close"),
        "plain proxied HTTP to a forwarded port is not recorded request by request, since it is "
        "the app's own traffic, as many requests as its pages make",
    ),
}


@dataclass(frozen=True)
class Target:
    """A forwarded port, by its canonical host."""

    host: str  # <port>-<workspace>.ws.<DOMAIN>
    port: int
    workspace: int


@dataclass
class Token:
    """A way in the hand-off handed out, by the hash of its value."""

    identity: Identity
    target: Target
    expires: float
    spent: bool = False


@dataclass(eq=False)
class Session:
    identity: Identity
    target: Target
    key: str  # the hash of its cookie's value
    started: float
    last: float  # when it was last used
    busy: int = 0  # exchanges under way and WebSockets open
    splices: set = field(default_factory=set)
    exchanges: set = field(default_factory=set)  # the connections' tasks of exchanges under way
    ended: bool = False


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def safe_return(path: str) -> str:
    """``path`` if it is one of the host's own, else ``/``: it must begin with ``/`` and not
    with ``//`` or ``/\\``, which a browser takes for another host."""
    if (
        path.startswith("/")
        and not path.startswith(("//", "/\\"))
        and len(path) <= MOST_RETURN_PATH
        and all(0x21 <= ord(c) <= 0x7E for c in path)
    ):
        return path
    return "/"


def return_value(path: str) -> str:
    """The return cookie's value for ``path``: the path in URL-safe base64 without padding,
    which a cookie holds as it is. A page of the host can set it too, so what it names is held to
    ``safe_return`` again where it is read."""
    return base64.urlsafe_b64encode(safe_return(path).encode()).rstrip(b"=").decode()


def returned_path(value: str | None) -> str:
    """The path a return cookie's value names, held to ``safe_return``; ``/`` for anything
    else."""
    if not value:
        return "/"
    try:
        path = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
    except (binascii.Error, ValueError):
        return "/"
    return safe_return(path)


def cookies_of(headers: list[tuple[bytes, bytes]]) -> list[tuple[str, str]]:
    """Every cookie the request's Cookie headers carry, as (name, value)."""
    found = []
    for name, value in headers:
        if name == b"cookie":
            for pair in value.decode("latin-1").split(";"):
                key, _, held = pair.strip().partition("=")
                if key:
                    found.append((key, held))
    return found


def own_cookie(name: str) -> bool:
    return name.strip().lower().startswith(OWN_COOKIES)


def head_size(request: h11.Request) -> int:
    """About how long the request's head was: its line and its headers, each with its
    separators."""
    line = len(request.method) + len(request.target) + 11
    return line + sum(len(name) + len(value) + 4 for name, value in request.headers) + 2


class Hosts:
    """The hosts of the forwarded ports under the public origin's domain."""

    def __init__(self, public_origin: str):
        parts = urllib.parse.urlsplit(public_origin)
        self.scheme = parts.scheme
        self.domain = (parts.hostname or "").lower()
        default = {"https": 443, "http": 80}.get(parts.scheme)
        self.suffix = "" if parts.port in (None, default) else f":{parts.port}"
        self._host = re.compile(FORWARD_HOST + re.escape(self.domain))

    def target(self, host: str) -> Target | None:
        """The port ``host`` names, if it is a port's canonical host."""
        found = self._host.fullmatch(host)
        if found is None or int(found[1]) > 65535:
            return None
        return Target(host, int(found[1]), int(found[2]))

    def of_header(self, host: str) -> Target | None:
        """The port a request's Host names: its canonical host, with the public origin's port
        where that is not the scheme's own."""
        if self.suffix:
            if not host.endswith(self.suffix):
                return None
            host = host[: -len(self.suffix)]
        return self.target(host)

    def origin(self, target: Target) -> str:
        return f"{self.scheme}://{target.host}{self.suffix}"


class Relay:
    """One connection to a forwarded port: the relay's process (``Runtime.port_argv``), whose
    input goes to the port and whose output is what the port answers, and the HTTP state of the
    connection."""

    def __init__(self, process: asyncio.subprocess.Process, target: Target, person: int):
        self.process = process
        self.target = target
        self.person = person
        self.http = h11.Connection(h11.CLIENT, max_incomplete_event_size=MOST_HEAD)
        self.watch: asyncio.Task | None = None
        self.closed = False
        self.reused = False  # taken from those kept idle
        self.head_written = False  # the request's head of this exchange was handed to it
        self.heard = False  # the app has sent something on it in this exchange

    async def write(self, data: bytes | None) -> None:
        if data:
            self.process.stdin.write(data)
            await self.process.stdin.drain()

    async def read(self) -> bytes:
        data = await self.process.stdout.read(READ_CHUNK)
        self.heard = self.heard or bool(data)
        return data

    def usable(self) -> bool:
        return (
            not self.closed
            and self.process.returncode is None
            and (self.watch is None or not self.watch.done())
        )

    async def close(self) -> None:
        """End it: its input closed, which ends the relay, and its client killed if it does not
        end in time. Any end is ordinary: a relay's exit code means nothing."""
        if self.closed:
            return
        self.closed = True
        if self.watch is not None and self.watch is not asyncio.current_task():
            self.watch.cancel()
        with contextlib.suppress(Exception):
            self.process.stdin.close()
        try:
            await asyncio.wait_for(self.process.wait(), RELAY_ENDS_WITHIN)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                self.process.kill()
            await self.process.wait()


@dataclass(eq=False)
class Slot:
    """Room held under the caps for a relay being started, counted as one of them."""

    target: Target
    person: int

    async def close(self) -> None:
        pass


class Splice:
    """A WebSocket, spliced raw between the browser's connection and a relay."""

    def __init__(self, session: Session):
        self.session = session
        self.result = audit.OK
        self.ended = asyncio.Event()
        self.finished = asyncio.Event()

    def end(self, result: str) -> None:
        if not self.ended.is_set():
            self.result = result
            self.ended.set()


class Exchange:
    """One request on a browser's connection, as Caddy passed it on."""

    def __init__(self, conn: h11.Connection, reader, writer, request: h11.Request):
        self.conn, self.reader, self.writer, self.request = conn, reader, writer, request
        self.method = request.method.decode("ascii", "replace")
        self.target = request.target.decode("ascii", "replace")
        self.path, _, self.query = self.target.partition("?")
        self.host = self.header("host") or ""
        upgrade = (self.header("upgrade") or "").lower()
        connection = {t.strip().lower() for t in (self.header("connection") or "").split(",")}
        self.upgrade = self.method == "GET" and upgrade == "websocket" and "upgrade" in connection
        self.body_done = False
        self.body_sent = False  # some of the body has been passed to a relay

    def header(self, name: str) -> str | None:
        """The header's value; several are joined, which no value checked here equals."""
        key = name.encode()
        values = [v.decode("latin-1") for k, v in self.request.headers if k == key]
        return ",".join(values) if values else None

    async def send(self, event) -> None:
        data = self.conn.send(event)
        if data:
            self.writer.write(data)
            await self.writer.drain()

    async def next_event(self):
        while True:
            event = self.conn.next_event()
            if event is h11.NEED_DATA:
                self.conn.receive_data(await self.reader.read(READ_CHUNK))
                continue
            return event

    async def body(self, most: int) -> bytes | None:
        """The request's body, at most ``most`` bytes; None for a longer one."""
        held = b""
        while not self.body_done:
            event = await self.next_event()
            if type(event) is h11.Data:
                held += event.data
                if len(held) > most:
                    return None
            else:  # its end, or the pause before a switch of protocol
                self.body_done = True
        return held

    def settled(self) -> bool:
        """Whether the request's body has all come: what has come of it is passed over, and
        nothing more is waited for."""
        while not self.body_done:
            try:
                event = self.conn.next_event()
            except h11.RemoteProtocolError:
                return False
            if type(event) is h11.Data:
                continue
            if type(event) is not h11.EndOfMessage and event is not h11.PAUSED:
                return False  # it has not all come, or will not
            self.body_done = True
        return True

    async def answer(self, status: int, headers: list, body: bytes = b"") -> None:
        """An answer of the server's own, the connection kept only where the request's body has
        come whole, or comes within DRAIN_SECONDS, MOST_DRAINED at most."""
        close = not self.settled()
        if close:
            try:
                close = await asyncio.wait_for(self.body(MOST_DRAINED), DRAIN_SECONDS) is None
            except (TimeoutError, h11.RemoteProtocolError):
                close = True
        sent = [*headers, ("content-length", str(len(body)))]
        if close:
            sent.append(("connection", "close"))
        await self.send(h11.Response(status_code=status, headers=sent))
        if body and self.method != "HEAD":
            await self.send(h11.Data(data=body))
        await self.send(h11.EndOfMessage())

    async def bare(self, status: int) -> None:
        """An answer of the status alone, and the connection closed after it."""
        await self.send(h11.Response(status_code=status, headers=[
            ("content-length", "0"), ("connection", "close"), *own_headers()]))  # fmt: skip
        await self.send(h11.EndOfMessage())

    async def problem(self, code: str, headers: list | None = None) -> None:
        status = problems.STATUS[code]
        body = json.dumps(problems.body(code, status, problems.MESSAGES[code])).encode()
        await self.answer(status, [*own_headers(), ("content-type", problems.MEDIA_TYPE),
                                   *(headers or [])], body)  # fmt: skip


def own_headers() -> list[tuple[str, str]]:
    """The headers every answer of the server's own carries."""
    return [*SECURITY_HEADERS.items(), ("cache-control", "no-store")]


def cookie_line(name: str, value: str, max_age: int | None) -> str:
    line = f"{name}={value}; Secure; HttpOnly; Path=/; SameSite=Lax"
    return line if max_age is None else f"{line}; Max-Age={max_age}"


class Capped(Exception):
    """No room for another relay."""


def _after(service, name: str, own: Callable) -> None:
    """``own`` called after what the workspaces' hook ``name`` calls already."""
    before = getattr(service, name)

    def both(*args):
        try:
            before(*args)
        finally:
            own(*args)

    setattr(service, name, both)


class Forwarding:
    """The forwarding server and what it keeps in memory (``app.state.forward``): sessions,
    ways in, wanted hosts, approved hosts and relays. Lost when rcman restarts."""

    def __init__(self, settings, service, store, clock: Callable[[], float] = time.monotonic):
        self.settings, self.service, self.store = settings, service, store
        self.hosts = Hosts(settings.public_origin)
        self.clock = clock
        self._lock = threading.Lock()  # the ways in, the wanted and the approved hosts
        self._tokens: dict[str, Token] = {}
        self._wanted: dict[str, float] = {}
        self._approved: dict[str, None] = {}
        self.new_hosts = TokenBuckets(NEW_HOSTS_BURST, NEW_HOSTS_PER_WEEK / WEEK)
        self.mints = TokenBuckets(MINTS_BURST, MINTS_PER_MINUTE / 60)
        # The rest is the loop's alone.
        self._sessions: dict[str, Session] = {}
        self._relays: set[Relay] = set()
        self._idle: dict[tuple[int, int], list[Relay]] = {}
        self._touched: set[int] = set()  # workspaces a relay was started in
        self._connections: set[asyncio.Task] = set()
        self._background: set[asyncio.Task] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._server: asyncio.base_events.Server | None = None
        self._reaper: asyncio.Task | None = None
        self._path: Path | None = None
        self.handlers = {
            "health": self._health,
            "ask": self._ask,
            "refused": self._refused,
            "enter": self._enter,
            "proxy": self._proxy,
        }
        store.on_off(self.off)
        _after(service, "on_removing", lambda w: self._end_from_thread(
            lambda s: s.target.workspace == w.id, audit.WORKSPACE_GONE, w.id))  # fmt: skip
        _after(service, "on_rebuilding", lambda w: self._end_from_thread(
            lambda s: s.target.workspace == w.id, audit.WORKSPACE_REBUILDING, w.id))  # fmt: skip
        _after(service, "on_person_deleting", lambda p: self._end_from_thread(
            lambda s: s.identity.person == p, audit.WORKSPACE_GONE, None, person=p))  # fmt: skip

    @property
    def serving(self) -> bool:
        return self._server is not None

    # --- starting and stopping ----------------------------------------------------------

    async def start(self, path: Path) -> None:
        """Listen on ``path``, a unix socket anyone may connect to, in a folder only Caddy's
        group may enter (installer/install.py)."""
        self._loop = asyncio.get_running_loop()
        self._loop_thread = threading.get_ident()
        self._path = path
        self._server = await asyncio.start_unix_server(self._serve, path=str(path))
        os.chmod(path, 0o666)
        self._reaper = asyncio.create_task(self._reap_loop())

    async def stop(self) -> None:
        """Stop listening, close every WebSocket and relay, and sweep what relays the
        workspaces used may have left."""
        if self._server is None:
            return
        self._server.close()
        if self._reaper is not None:
            self._reaper.cancel()
        for session in list(self._sessions.values()):
            self._end_session(session, audit.SERVICE_CLOSING)
        for task in list(self._connections):
            task.cancel()
        await asyncio.gather(*self._connections, *self._background, return_exceptions=True)
        await asyncio.gather(*(r.close() for r in list(self._relays)), return_exceptions=True)
        self._relays.clear()
        self._idle.clear()
        if self._path is not None:
            with contextlib.suppress(OSError):
                self._path.unlink()
        touched, self._touched = sorted(self._touched), set()
        await asyncio.to_thread(self._sweep, touched)
        self._server = None

    def _sweep(self, workspace_ids: list[int]) -> None:
        for workspace_id in workspace_ids:
            found = self.service.get(workspace_id)
            if found is not None:
                self.service.sweep_forwards(found)

    # --- what other threads tell it -----------------------------------------------------

    def off(self) -> None:
        """Forwarding was turned off (forwarding_settings.Store.on_off): every session ends and
        every WebSocket closes, on the server's loop. Called in the saving request's thread,
        under the save's lock: it saves nothing, waits for nothing, and does no harm again."""
        loop = self._loop
        if loop is None:
            return
        with contextlib.suppress(RuntimeError):  # a loop that has closed has nothing open
            loop.call_soon_threadsafe(self._all_off)

    def _all_off(self) -> None:
        for session in list(self._sessions.values()):
            self._end_session(session, audit.FORWARDING_OFF)
        idle = [relay for kept in self._idle.values() for relay in kept]
        self._idle.clear()
        for relay in idle:
            self._in_background(self._drop(relay))
        with self._lock:
            self._tokens.clear()

    def _in_background(self, work) -> None:
        """``work`` run on the loop, held until it is done."""
        task = asyncio.get_running_loop().create_task(work)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def _end_from_thread(self, which, result: str, workspace: int | None,
                         person: int | None = None) -> None:  # fmt: skip
        """End the sessions ``which`` picks, their WebSockets and the idle relays of the
        workspace or the person, and wait for them, from a thread of the workspaces'."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return

        async def ending():
            await self._end_where(which, result, workspace, person)

        if threading.get_ident() == self._loop_thread:
            loop.create_task(ending())
            return
        try:
            asyncio.run_coroutine_threadsafe(ending(), loop).result(FROM_THREAD_SECONDS)
        except Exception:  # noqa: BLE001 - logged; the sweep that follows ends what is left
            log.exception("the forwarded ports were not all closed")

    async def _end_where(self, which, result, workspace, person) -> None:
        ended = [s for s in list(self._sessions.values()) if which(s)]
        splices = [splice for s in ended for splice in s.splices]
        for session in ended:
            self._end_session(session, result)
        for key, idle in list(self._idle.items()):
            if key[0] == workspace or (person is not None and idle and idle[0].person == person):
                for relay in self._idle.pop(key, []):
                    await self._drop(relay)
        for splice in splices:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(splice.finished.wait(), FROM_THREAD_SECONDS)

    # --- the hand-off's side (routers/forward.py), in a request's thread ------------------

    def mark_wanted(self, host: str) -> None:
        """Mark ``host`` as wanted: its owner was shown its address (``shown``), or opened it
        (``hand_off``). Caddy gets a certificate only for a host marked in the last
        WANTED_SECONDS."""
        if self.hosts.target(host) is None:
            return
        with self._lock:
            now = self.clock()
            for held in [h for h, until in self._wanted.items() if until <= now]:
                del self._wanted[held]
            self._wanted[host] = now + WANTED_SECONDS

    def on(self) -> bool:
        """Whether ports are forwarded now: the server is serving and forwarding is turned on."""
        return self.serving and self.store.settings().enabled

    def shown(self, person: int, workspace) -> list[dict]:
        """The forwarded ports of ``workspace``, ``person``'s own, as the workspace page shows
        them to its owner: each port, its label (None where it has none) and its address. None
        while forwarding is off, for a workspace that is not ready, or for another person's. The
        host of each is marked as wanted, for its owner was shown its address, unless a rebuild
        replaces the container just now, while which its ports are listed but cannot be
        reached. Only a person's own listing calls this, so no one marks another's host."""
        if workspace.person != person or workspace.state != READY or not self.on():
            return []
        reachable = workspace.rebuild_state != REBUILD_SWITCHING
        found = []
        for port in self.service.forwarded_ports(workspace):
            target = self.hosts.target(f"{port.port}-{workspace.id}.ws.{self.hosts.domain}")
            if target is None:
                continue
            if reachable:
                self.mark_wanted(target.host)
            found.append(
                {"port": port.port, "label": port.label, "url": f"{self.hosts.origin(target)}/"}
            )
        return found

    def hand_off(self, identity: Identity, host: str) -> str:
        """A way in to ``host`` for ``identity``, its owner, which is marked as wanted: a value
        spent once, by the port's host, within TOKEN_SECONDS. Only its hash is kept."""
        if not self.serving or not self.store.settings().enabled:
            raise Problem("forwarding_off")
        target = self.hosts.target(host)
        if target is None:
            raise Problem("port_not_found")
        code = self.refusal(identity.person, target)
        if code is not None:
            raise Problem(code)
        self.mark_wanted(host)
        with self._lock:
            now = self.clock()
            self._prune_tokens(now)
            held = [
                t
                for t in self._tokens.values()
                if t.identity.person == identity.person and not t.spent and t.expires > now
            ]
            if len(held) >= MOST_OUTSTANDING_TOKENS:
                raise Problem("too_many_hand_offs", headers={"Retry-After": str(TOKEN_SECONDS)})
            if not self.mints.take(str(identity.person)):
                wait = self.mints.seconds_until_allowed(str(identity.person))
                raise Problem("too_many_hand_offs", headers={"Retry-After": str(wait)})
            value = secrets.token_urlsafe(32)
            self._tokens[_hash(value)] = Token(identity, target, now + TOKEN_SECONDS)
        return value

    def _prune_tokens(self, now: float) -> None:
        for key in [k for k, t in self._tokens.items() if t.expires + TOKEN_SECONDS <= now]:
            del self._tokens[key]

    def refusal(self, person: int, target: Target) -> str | None:
        """Why ``person`` may not reach ``target`` now, as a problem's code, or None: they must be
        active and own its workspace, which must be ready, not in a rebuild's switch, with the
        port in its plan. Read from the database."""
        con = db.connect(self.settings.database)
        try:
            found = people.by_id(con, person)
        finally:
            con.close()
        if found is None or found.state != people.ACTIVE:
            return "not_allowed"
        return None if self._open(target, person) else "port_not_found"

    def _open(self, target: Target, person: int | None = None) -> bool:
        """Whether the port is one a ready workspace forwards, ``person``'s where one is
        given."""
        workspace = self.service.get(target.workspace)
        if workspace is None or (person is not None and workspace.person != person):
            return False
        if workspace.state != READY or workspace.rebuild_state == REBUILD_SWITCHING:
            return False
        return target.port in self.service.ports_of(workspace)

    # --- the server -------------------------------------------------------------------------

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        self._connections.add(task)
        try:
            conn = h11.Connection(h11.SERVER, max_incomplete_event_size=MOST_HEAD)
            if len(self._connections) > MOST_CONNECTIONS:
                writer.write(conn.send(h11.Response(status_code=503, headers=[
                    ("content-length", "0"), ("connection", "close"),
                    ("retry-after", str(RETRY_AFTER_SECONDS))])))  # fmt: skip
                await writer.drain()
                return
            await self._connection(conn, reader, writer)
        except (ConnectionError, h11.ProtocolError, asyncio.CancelledError):
            pass
        except Exception:  # noqa: BLE001 - one connection's failure is that connection's
            log.exception("a connection to the forwarding server failed")
        finally:
            self._connections.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def _connection(self, conn, reader, writer) -> None:
        """Request after request on a connection: one that brings none for
        IDLE_CONNECTION_SECONDS, or whose head is not whole HEAD_SECONDS after its first byte,
        is closed; a head longer than MOST_HEAD is answered 431."""
        while True:
            exchange = None
            deadline = None  # HEAD_SECONDS from the head's first byte, however many follow
            while exchange is None:
                try:
                    event = conn.next_event()
                except h11.RemoteProtocolError as refused:
                    await self._refuse_head(conn, writer, refused.error_status_hint)
                    return
                if event is h11.NEED_DATA:
                    if deadline is None:
                        within = IDLE_CONNECTION_SECONDS
                    else:
                        within = deadline - asyncio.get_running_loop().time()
                    try:
                        data = await asyncio.wait_for(reader.read(READ_CHUNK), max(within, 0))
                    except TimeoutError:
                        return
                    if data and deadline is None:
                        deadline = asyncio.get_running_loop().time() + HEAD_SECONDS
                    conn.receive_data(data)
                    continue
                if type(event) is not h11.Request:
                    return  # the connection ended
                if head_size(event) > MOST_HEAD:
                    await self._refuse_head(conn, writer, 431)
                    return
                exchange = Exchange(conn, reader, writer, event)
            handler = self.handlers[self.route(exchange)]
            switched = await handler(exchange)
            if switched or conn.our_state is not h11.DONE or conn.their_state is not h11.DONE:
                return
            conn.start_next_cycle()

    @staticmethod
    async def _refuse_head(conn, writer, status: int) -> None:
        """The status alone, for a request whose head was not taken, and the connection closed."""
        if conn.our_state not in (h11.IDLE, h11.SEND_RESPONSE):
            return
        with contextlib.suppress(h11.LocalProtocolError):
            writer.write(conn.send(h11.Response(status_code=status, headers=[
                ("content-length", "0"), ("connection", "close")])))  # fmt: skip
            await writer.drain()

    def route(self, x: Exchange) -> str:
        """The handler a request is for."""
        if x.path == HEALTH_PATH:
            return "health"
        if ASK_HOST.fullmatch(x.host):
            return "ask"
        if self.hosts.of_header(x.host) is None or not x.target.startswith("/"):
            return "refused"
        if x.path == RESERVED or x.path.startswith(RESERVED + "/"):
            return "enter" if x.path == ENTER_PATH and x.method == "POST" else "refused"
        return "proxy"

    async def _health(self, x: Exchange) -> bool:
        body = json.dumps({"status": "ok"}).encode()
        await x.answer(200, [*own_headers(), ("content-type", "application/json")], body)
        return False

    async def _refused(self, x: Exchange) -> bool:
        await x.problem("not_found")
        return False

    async def _ask(self, x: Exchange) -> bool:
        """200 only for a canonical host of a ready workspace's port, marked as wanted, while
        forwarding is on; one never approved before also takes one of ``new_hosts``. 404 for
        anything else."""
        allowed = x.method == "GET" and x.path == ASK_PATH and await self._approves(x.query)
        await x.answer(200 if allowed else 404, [("cache-control", "no-store")])
        return False

    async def _approves(self, query: str) -> bool:
        try:
            fields = urllib.parse.parse_qsl(query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            return False
        if len(fields) != 1 or fields[0][0] != "domain":
            return False
        target = self.hosts.target(fields[0][1])
        if target is None or not self.store.settings().enabled:
            return False
        if not self.service.restored.is_set():
            return False
        with self._lock:
            until = self._wanted.get(target.host)
            if until is None or until <= self.clock():
                return False
        if not await asyncio.to_thread(self._open, target):
            return False
        with self._lock:
            if target.host in self._approved:
                return True
            if not self.new_hosts.take("new hosts"):
                return False
            self._approved[target.host] = None
        return True

    async def _enter(self, x: Exchange) -> bool:
        """The way in, spent: a session for its host, and back to where the browser was."""
        target = self.hosts.of_header(x.host)
        if x.target != ENTER_PATH or target is None:
            await x.problem("not_found")
            return False
        if x.header("origin") != self.settings.public_origin:
            await x.problem("origin_refused")
            return False
        if not self.store.settings().enabled:
            await x.problem("forwarding_off")
            return False
        if not self.service.restored.is_set():
            await x.problem("forwarding_starting", [("retry-after", str(RETRY_AFTER_SECONDS))])
            return False
        kind = (x.header("content-type") or "").partition(";")[0].strip().lower()
        try:
            body = await asyncio.wait_for(x.body(MOST_ENTER_BODY), BODY_SECONDS)
        except TimeoutError:
            await x.bare(408)
            return False
        value = None
        if kind == "application/x-www-form-urlencoded" and body is not None:
            with contextlib.suppress(ValueError, UnicodeDecodeError):
                fields = urllib.parse.parse_qsl(body.decode("ascii"), strict_parsing=True)
                if len(fields) == 1 and fields[0][0] == "token":
                    value = fields[0][1]
        token = self._spend(value) if value else None
        if token is None:
            await x.problem("forward_token_refused")
            return False
        record = audit.Record("forward.session", token.identity, target.host)
        spent, token.spent = token.spent, True  # once, whatever the attempt comes to
        if spent or token.expires <= self.clock() or token.target != target:
            await self._audit(record, "forward_token_refused")
            await x.problem("forward_token_refused")
            return False
        code = await asyncio.to_thread(self.refusal, token.identity.person, target)
        if code is not None:
            await self._audit(record, code)
            await x.problem(code)
            return False
        # Read again with nothing awaited before the session is made: forwarding turned off
        # since the checks above ends every session there is, and this one would be missed.
        if not self.store.settings().enabled:
            await self._audit(record, "forwarding_off")
            await x.problem("forwarding_off")
            return False
        value = secrets.token_urlsafe(32)
        now = self.clock()
        key = _hash(value)
        person = token.identity.person
        held = [s for s in self._sessions.values() if s.identity.person == person]
        # Their older ones for the same host first, then the oldest of all, as they were made.
        held.sort(key=lambda s: s.target != target)
        for oldest in held[: max(0, len(held) - MOST_SESSIONS_PER_PERSON + 1)]:
            self._end_session(oldest, audit.SESSION_ENDED)
        self._sessions[key] = Session(token.identity, target, key, now, now)
        await self._audit(record, audit.OK)
        back = None
        for name, held in cookies_of(x.request.headers):
            if name == RETURN_COOKIE:
                back = held
        await x.answer(303, [
            *own_headers(),
            ("location", returned_path(back)),
            ("set-cookie", cookie_line(SESSION_COOKIE, value, None)),
            ("set-cookie", cookie_line(RETURN_COOKIE, "", 0)),
        ])  # fmt: skip
        return False

    def _spend(self, value: str) -> Token | None:
        """The way in ``value`` is, if one was handed out and is still remembered."""
        with self._lock:
            self._prune_tokens(self.clock())
            return self._tokens.get(_hash(value))

    async def _audit(self, record: audit.Record, result: str) -> None:
        def write():
            con = db.connect(self.settings.database)
            try:
                audit.write(con, record, result)
            finally:
                con.close()

        await asyncio.to_thread(write)

    # --- a request to a port ----------------------------------------------------------------

    def fetch_refusal(self, x: Exchange, target: Target) -> str | None:
        """Whether the browser says the request comes from the host's own pages, or is a
        top-level navigation: ``fetch_refused`` if not."""
        site = x.header("sec-fetch-site")
        origin = x.header("origin")
        own = self.hosts.origin(target)
        safe = x.method in SAFE and not x.upgrade
        if site is not None:
            navigation = (
                x.header("sec-fetch-mode") == "navigate"
                and x.header("sec-fetch-dest") == "document"
            )
            if site not in ("same-origin", "none") and not (
                site in ("same-site", "cross-site") and navigation and safe
            ):
                return "fetch_refused"
        elif x.method not in SAFE and origin != own:
            return "fetch_refused"
        if (x.upgrade or x.method not in SAFE) and origin is not None and origin != own:
            return "fetch_refused"
        return None

    def session_of(self, x: Exchange, target: Target) -> Session | None:
        """The live session for the host that the request's cookies carry, if any."""
        settings = self.store.settings()
        now = self.clock()
        for name, value in cookies_of(x.request.headers):
            if name != SESSION_COOKIE:
                continue
            session = self._sessions.get(_hash(value))
            if session is None or session.target != target:
                continue
            if self._expired(session, now, settings):
                self._end_session(session, audit.SESSION_ENDED)
                continue
            return session
        return None

    @staticmethod
    def _expired(session: Session, now: float, settings) -> bool:
        if now - session.started >= settings.absolute_seconds:
            return True
        return session.busy == 0 and now - session.last >= settings.idle_seconds

    def _end_session(self, session: Session, result: str) -> None:
        """The session ended: its WebSockets closed, recorded with ``result``, and the
        connections of its exchanges under way, but the one that ends it, closed with them."""
        session.ended = True
        self._sessions.pop(session.key, None)
        for splice in list(session.splices):
            splice.end(result)
        current = asyncio.current_task()
        for task in list(session.exchanges):
            if task is not current:
                task.cancel()

    def _gone(self, session: Session) -> bool:
        """Whether the session has ended, ending it where forwarding was turned off or its
        lifetime has passed: read again after anything is awaited."""
        if session.ended:
            return True
        settings = self.store.settings()
        if not settings.enabled:
            self._end_session(session, audit.FORWARDING_OFF)
        elif self.clock() - session.started >= settings.absolute_seconds:
            self._end_session(session, audit.SESSION_ENDED)
        return session.ended

    async def _proxy(self, x: Exchange) -> bool:
        target = self.hosts.of_header(x.host)
        if not self.store.settings().enabled:
            await x.problem("forwarding_off")
            return False
        if not self.service.restored.is_set():
            await x.problem("forwarding_starting", [("retry-after", str(RETRY_AFTER_SECONDS))])
            return False
        refused = self.fetch_refusal(x, target)
        if refused is not None:
            await x.problem(refused)
            return False
        session = self.session_of(x, target)
        if session is None:
            if x.method in SAFE and not x.upgrade:
                await self._to_hand_off(x, target)
            else:
                await x.problem("forward_not_signed_in")
            return False
        task = asyncio.current_task()
        session.busy += 1
        session.exchanges.add(task)
        session.last = self.clock()
        try:
            code = await asyncio.to_thread(self.refusal, session.identity.person, target)
            if code is not None:
                self._end_session(session, audit.WORKSPACE_GONE)
                await x.problem(code)
                return False
            if self._gone(session):
                return True  # the browser's connection ends with it
            return await self._exchange(x, session, target)
        finally:
            session.exchanges.discard(task)
            session.busy -= 1
            session.last = self.clock()

    async def _to_hand_off(self, x: Exchange, target: Target) -> None:
        """A navigation with no session: to the hand-off, on rcman's origin, with where it was
        going kept for its return."""
        headers = [
            *own_headers(),
            ("location", f"{self.settings.public_origin}{HAND_OFF_PATH}?host={target.host}"),
        ]
        if x.header("sec-fetch-dest") in (None, "document"):
            value = return_value(x.target)
            headers.append(("set-cookie", cookie_line(RETURN_COOKIE, value, RETURN_SECONDS)))
        await x.answer(303, headers)

    def upstream_headers(self, x: Exchange, target: Target) -> list[tuple[bytes, bytes]]:
        """The request's headers as the app is given them."""
        own = self.hosts.origin(target).encode()
        local = f"localhost:{target.port}".encode()
        named = {t.strip().lower() for t in (x.header("connection") or "").split(",") if t}
        sent = [(b"host", local)]
        for name, value in x.request.headers:
            text = name.decode("latin-1")
            if text in HOP_BY_HOP or text in named or text == "host":
                continue
            if text in IDENTITY or text.startswith(IDENTITY_PREFIX):
                continue
            if text == "cookie":
                kept = [f"{k}={v}" for k, v in cookies_of([(name, value)]) if not own_cookie(k)]
                if not kept:
                    continue
                value = "; ".join(kept).encode("latin-1")
            if text == "origin" and value == own:
                value = b"http://" + local
            sent.append((name, value))
        if x.upgrade:
            sent += [(b"connection", b"upgrade"), (b"upgrade", b"websocket")]
        return sent

    @staticmethod
    def downstream_headers(headers, upgrade: bool) -> list[tuple[bytes, bytes]]:
        """The app's answer's headers as the browser is given them."""
        named = {t.strip().lower() for n, v in headers if n == b"connection"
                 for t in v.decode("latin-1").split(",")}  # fmt: skip
        sent = []
        for name, value in headers:
            text = name.decode("latin-1")
            if text in HOP_BY_HOP or text in named or text == "transfer-encoding":
                continue
            if text == "set-cookie" and own_cookie(value.decode("latin-1").partition("=")[0]):
                continue
            sent.append((name, value))
        if upgrade:
            sent += [(b"connection", b"upgrade"), (b"upgrade", b"websocket")]
        sent.append((FRAMING[0].encode(), FRAMING[1].encode()))
        return sent

    async def _exchange(self, x: Exchange, session: Session, target: Target) -> bool:
        """The request passed to the app and its answer back; whether the browser's connection
        is done with. A relay kept idle that turns out to have ended before it said anything is
        tried again once, on a new one, where nothing of the request's body was spent on it and
        the request is one the app cannot have acted on (REPLAYABLE), or its head was never
        handed to the relay."""
        fresh = False
        while True:
            try:
                relay = await self._take(session, target, fresh)
            except Capped:
                await x.problem("too_many_relays", [("retry-after", str(RETRY_AFTER_SECONDS))])
                return False
            except (rt.RuntimeFailed, OSError) as failed:
                log.warning("a relay to %s did not start: %s", target.host, failed)
                await x.problem("port_unreachable")
                return False
            if self._gone(session):
                await self._drop(relay)
                return True
            try:
                switched = await self._through(x, relay)
            except (h11.ProtocolError, ConnectionError, asyncio.IncompleteReadError, OSError):
                await self._drop(relay)
                replayable = x.method in REPLAYABLE or not relay.head_written
                if relay.reused and not relay.heard and not x.body_sent and replayable:
                    fresh = True
                    continue
                if x.conn.our_state is h11.SEND_RESPONSE:
                    with contextlib.suppress(Exception):
                        await x.problem("port_unreachable")
                    return False
                return True
            except BaseException:
                await self._drop(relay)
                raise
            break
        if switched is not None:
            await self._splice(x, relay, session, target, switched)
            return True
        await self._release(relay)
        return False

    async def _through(self, x: Exchange, relay: Relay):
        """The request through ``relay`` and the app's answer back (``_pass``): its 101, where it
        switched, which ``_splice`` passes back."""
        request = h11.Request(method=x.request.method, target=x.request.target,
                              headers=self.upstream_headers(x, relay.target))  # fmt: skip
        pump = passing = None
        try:
            head = relay.http.send(request)
            # Handed to the relay from here, even where its write fails part way: the app may
            # have it, and act on it.
            relay.head_written = True
            await relay.write(head)
            pump = asyncio.create_task(self._pump(x, relay))
            passing = asyncio.create_task(self._pass(x, relay))
            await asyncio.wait({pump, passing}, return_when=asyncio.FIRST_COMPLETED)
            if pump.done() and not pump.cancelled() and pump.exception() is not None:
                raise pump.exception()
            return await passing
        finally:
            # The app may answer before it has read the whole body; and an exchange whose
            # session ends is cancelled here.
            started = [task for task in (pump, passing) if task is not None]
            for task in started:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*started, return_exceptions=True)

    async def _pump(self, x: Exchange, relay: Relay) -> None:
        """The request's body, from the browser's connection to the app, and its end."""
        while not x.body_done:
            event = await x.next_event()
            if type(event) is h11.Data:
                x.body_sent = True
                await relay.write(relay.http.send(h11.Data(data=event.data)))
            elif type(event) is h11.EndOfMessage or event is h11.PAUSED:
                x.body_done = True
            else:
                raise h11.RemoteProtocolError("the request ended before its body")
        await relay.write(relay.http.send(h11.EndOfMessage()))

    async def _up_event(self, relay: Relay):
        while True:
            event = relay.http.next_event()
            if event is h11.NEED_DATA:
                relay.http.receive_data(await relay.read())
                continue
            return event

    async def _pass(self, x: Exchange, relay: Relay):
        """The app's answer, back to the browser: its 101, where it switched to a WebSocket,
        which is passed back by ``_splice``; None once an answer was passed back whole."""
        while True:
            event = await self._up_event(relay)
            if type(event) is h11.InformationalResponse:
                if event.status_code == 101:
                    if not x.upgrade:
                        raise h11.RemoteProtocolError("a switch of protocol nobody asked for")
                    return event
                if event.status_code != 100:  # 100 is Caddy's own to answer
                    await x.send(h11.InformationalResponse(
                        status_code=event.status_code, headers=self.downstream_headers(
                            event.headers, False), reason=event.reason))  # fmt: skip
                continue
            if type(event) is h11.Response:
                break
            raise h11.RemoteProtocolError("no answer")
        headers = self.downstream_headers(event.headers, False)
        await x.send(h11.Response(status_code=event.status_code, reason=event.reason,
                                  headers=headers))  # fmt: skip
        while True:
            event = await self._up_event(relay)
            if type(event) is h11.Data:
                await x.send(h11.Data(data=event.data))
            elif type(event) is h11.EndOfMessage:
                await x.send(h11.EndOfMessage())
                return None
            else:
                raise h11.RemoteProtocolError("the answer ended before its body")

    async def _splice(self, x: Exchange, relay: Relay, session: Session, target: Target,
                      switched: h11.InformationalResponse) -> None:  # fmt: skip
        """The app's 101 passed back, then every byte both ways until either side ends, or the
        session does. A session that has ended meanwhile is switched for nothing: the browser's
        connection closes with the relay."""
        if self._gone(session):
            await self._drop(relay)
            return
        opened = audit.Opened("websocket", session.identity, self.settings.database)
        opened.target = target.host
        splice = Splice(session)
        session.splices.add(splice)
        # From here the splice is ended, not its connection cancelled, so that its close is
        # recorded with why.
        session.exchanges.discard(asyncio.current_task())
        try:
            await asyncio.to_thread(opened.opened)
            if self._gone(session) or splice.ended.is_set():
                return
            headers = self.downstream_headers(switched.headers, True)
            await x.send(h11.InformationalResponse(status_code=101, headers=headers))
            await self._raw(x, relay, splice)
        finally:
            session.splices.discard(splice)
            await self._drop(relay)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(opened.closed, splice.result)
            splice.finished.set()

    async def _raw(self, x: Exchange, relay: Relay, splice: Splice) -> None:
        from_app, _ = relay.http.trailing_data
        from_browser, _ = x.conn.trailing_data

        async def to_browser():
            if from_app:
                x.writer.write(from_app)
                await x.writer.drain()
            while data := await relay.read():
                x.writer.write(data)
                await x.writer.drain()

        async def to_app():
            await relay.write(from_browser)
            while data := await x.reader.read(READ_CHUNK):
                await relay.write(data)

        down, up = asyncio.create_task(to_browser()), asyncio.create_task(to_app())
        ended = asyncio.create_task(splice.ended.wait())
        tasks = {down, up, ended}
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if up.done() and not down.done() and not ended.done():
                # The browser has gone: what it sent last leaves the relay's pipe first.
                await asyncio.wait({down, ended}, timeout=SPLICE_DRAIN_SECONDS,
                                   return_when=asyncio.FIRST_COMPLETED)  # fmt: skip
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    # --- relays ---------------------------------------------------------------------------

    async def _take(self, session: Session, target: Target, fresh: bool = False) -> Relay:
        """A relay to the port: one kept idle, unless ``fresh``, or a new one where there is
        room."""
        key = (target.workspace, target.port)
        idle = [] if fresh else self._idle.get(key, [])
        while idle:
            relay = idle.pop()
            if relay.usable():
                relay.watch.cancel()
                relay.watch = None
                relay.reused, relay.heard, relay.head_written = True, False, False
                return relay
            await self._drop(relay)
        person = session.identity.person
        slot = await self._reserve(target, person)
        try:
            launch = await asyncio.to_thread(self._launch, target)
            process = await asyncio.create_subprocess_exec(
                *launch.argv, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, env=launch.env,
            )  # fmt: skip
        finally:
            self._relays.discard(slot)
        relay = Relay(process, target, person)
        self._relays.add(relay)
        self._touched.add(target.workspace)
        return relay

    async def _reserve(self, target: Target, person: int) -> Slot:
        """Room for one more relay of the workspace's and the person's, idle ones ended to make
        it, held at once: nothing is awaited between the last count and the room being taken, so
        that requests at once are held to the caps as requests one by one are."""
        caps = (
            (lambda r: r.target.workspace == target.workspace, MOST_RELAYS_PER_WORKSPACE),
            (lambda r: r.person == person, MOST_RELAYS_PER_PERSON),
        )
        for which, most in caps:
            if self._count(which) >= most:
                await self._make_room(which)
        if any(self._count(which) >= most for which, most in caps):
            raise Capped
        slot = Slot(target, person)
        self._relays.add(slot)
        return slot

    def _count(self, which) -> int:
        return sum(1 for r in self._relays if which(r))

    async def _make_room(self, which) -> None:
        """The idle relays of those ``which`` picks, ended."""
        for key, idle in list(self._idle.items()):
            for relay in [r for r in idle if which(r)]:
                idle.remove(relay)
                await self._drop(relay)
            if not idle:
                self._idle.pop(key, None)

    def _launch(self, target: Target) -> rt.Launch:
        """What starts a relay to the port, as the runtime runs one, in a thread: never as a
        process group's leader, which busybox's ``setsid`` would fork for
        (project-knowledge/runtime.md, "Forwarded ports")."""
        workspace = self.service.get(target.workspace)
        if workspace is None:
            raise rt.RuntimeFailed("the workspace is not there")
        layout = self.service.layout_of(workspace)
        return self.service.runtime.port_argv(
            workspace.container, target.port, rt.forward_pidfile(layout)
        )

    async def _release(self, relay: Relay) -> None:
        """A relay whose exchange is done: kept idle for the next request to its port, where
        the app keeps the connection; ended otherwise."""
        http = relay.http
        if http.our_state is not h11.DONE or http.their_state is not h11.DONE:
            await self._drop(relay)
            return
        http.start_next_cycle()
        key = (relay.target.workspace, relay.target.port)
        idle = self._idle.setdefault(key, [])
        if len(idle) >= MOST_IDLE_PER_PORT or not relay.usable():
            await self._drop(relay)
            return
        relay.watch = asyncio.create_task(self._watch(relay, key))
        idle.append(relay)

    async def _watch(self, relay: Relay, key) -> None:
        """An idle relay, ended when the app closes it or says anything, or after
        POOL_IDLE_SECONDS."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(relay.process.stdout.read(1), POOL_IDLE_SECONDS)
        idle = self._idle.get(key, [])
        if relay in idle:
            idle.remove(relay)
        await self._drop(relay)

    async def _drop(self, relay: Relay) -> None:
        self._relays.discard(relay)
        await relay.close()

    # --- sessions' ends ---------------------------------------------------------------------

    async def _reap_loop(self) -> None:
        while True:
            await asyncio.sleep(REAP_SECONDS)
            try:
                self.reap()
            except Exception:  # noqa: BLE001 - the next round tries again
                log.exception("the forward sessions were not looked over")

    def reap(self) -> None:
        """End the sessions past their lifetime, closing their WebSockets, and forget those
        idle for too long, the ways in no longer remembered and the marks expired."""
        settings = self.store.settings()
        now = self.clock()
        for session in list(self._sessions.values()):
            if self._expired(session, now, settings):
                self._end_session(session, audit.SESSION_ENDED)
        with self._lock:
            self._prune_tokens(now)
            for host in [h for h, until in self._wanted.items() if until <= now]:
                del self._wanted[host]


def html_page(title: str, lead: str, button: str, action: str, token: str, nonce: str) -> str:
    """The hand-off's page: a form that posts the way in to the port's ``action``, sent by a
    script that carries ``nonce``, or by its button where scripts do not run."""
    e = html.escape
    return (
        "<!doctype html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{e(title)}</title></head><body>"
        f'<form method="post" action="{e(action)}">'
        f'<input type="hidden" name="token" value="{e(token)}">'
        f"<p>{e(lead)}</p>"
        f'<noscript><button type="submit">{e(button)}</button></noscript>'
        "</form>"
        f'<script nonce="{e(nonce)}">document.forms[0].submit();</script>'
        "</body></html>\n"
    )
