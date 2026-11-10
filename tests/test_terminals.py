"""The Terminal tab's shells (backend/rcman/terminals.py), driven without the WebSocket: a
shell on a pseudo-terminal in a workspace of the host runtime, what is typed reaching it and
what it prints coming back, and what ends it - its own session and nothing else.

The host runtime's shells are the host's processes, so these tests look for them in the
host's ``/proc``; each test's ``sleep`` runs for a length of its own, so tests that run at the
same time never take each other's for theirs. The route that carries a terminal over a
WebSocket is tests/test_terminal.py's.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
import shlex
import subprocess
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rcman import db, people, terminals
from rcman import runtime as rt
from rcman.main import create_app

from tests.conftest import ALICE, made_workspace
from tests.hang_guard import HANG_GUARD, tier_guard
from tests.time_doubles import FakeTime
from tests.until import until


class Screen:
    """What a terminal sent, as it arrives: its ``send``."""

    def __init__(self):
        self.data = bytearray()
        self.changed = threading.Condition()

    async def send(self, chunk: bytes) -> None:
        with self.changed:
            self.data += chunk
            self.changed.notify_all()

    def wait_for(self, pattern: bytes) -> re.Match:
        """The first match of ``pattern`` in what was sent, once it is there."""
        with self.changed:
            found = self.changed.wait_for(lambda: re.search(pattern, bytes(self.data)), HANG_GUARD)
            assert found, bytes(self.data)[-4000:]
        return found


def person_id(settings, email: str) -> int:
    con = db.connect(settings.database)
    try:
        return people.by_email(con, email).id
    finally:
        con.close()


def a_length() -> str:
    """A part of a ``sleep``'s length no other test's has, by which its process is found: a
    fraction of a second, added to a length past the test's end (``sleep_argv``)."""
    return f"0.{secrets.randbelow(10**9):09d}"


def sleep_argv(length: str) -> list[str]:
    """A ``sleep`` that outlasts the test (rule (g)), so that what ends it is the test's or the
    product's doing, never its own end; ``length`` (``a_length``) is added to it, which finds
    it."""
    return ["sleep", f"{3 * tier_guard():g}", length]


def sleep_of(length: str) -> str:
    """``sleep_argv``, as a shell runs it."""
    return shlex.join(sleep_argv(length))


def sleeping(length: str) -> list[int]:
    """The host's processes that are ``sleep_argv(length)``."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if argv[0] == b"sleep" and length.encode() in argv[1:]:
            found.append(int(entry.name))
    return found


def alive(pid: int) -> bool:
    """Whether ``pid`` runs and is no zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[0] != "Z"


def stat_fields(pid: int) -> list[str]:
    """``/proc/<pid>/stat`` after the program's name: its state is the first."""
    return Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()


def start_time(pid: int) -> int:
    return int(stat_fields(pid)[19])


def session_of(pid: int) -> int:
    return int(stat_fields(pid)[3])


class Opened:
    """A terminal opened through the app's ``terminals``, on the test client's loop."""

    def __init__(self, client: TestClient, workspace: int, person: int, cols=80, rows=24):
        self.client = client
        self.screen = Screen()
        self.terminal = client.portal.call(
            client.app.state.terminals.open, workspace, person, cols, rows, self.screen.send
        )

    def type(self, text: str) -> None:
        self.client.portal.call(self.terminal.write, text.encode())

    def ask(self, command: str, pattern: bytes) -> re.Match:
        """Type ``command`` and wait for ``pattern`` in what comes back."""
        self.type(command + "\r")
        return self.screen.wait_for(pattern)

    def end(self, reason: str = terminals.TERMINAL_LOST) -> None:
        self.client.portal.call(self.client.app.state.terminals.end, self.terminal, reason)

    def ended(self) -> None:
        self.client.portal.call(asyncio.wait_for, self.terminal.wait_ended(), HANG_GUARD)

    def shell(self) -> int:
        """The shell's process, as its pidfile names it."""
        return int(Path(self.terminal.pidfile).read_text().split()[0])


@pytest.fixture
def workspace(client):
    return made_workspace(client, ALICE, "alpha")


@pytest.fixture
def alice(settings):
    return person_id(settings, ALICE)


def test_closing_ends_the_shell_and_its_jobs_and_not_what_left_its_session(
    client, workspace, alice
):
    background, nohup, own = a_length(), a_length(), a_length()
    opened = Opened(client, workspace["id"], alice)
    left = []
    try:
        opened.ask(
            f"{sleep_of(background)} & nohup {sleep_of(nohup)} >/dev/null 2>&1 & "
            f"setsid {sleep_of(own)} >/dev/null 2>&1 & echo started-$((2+2))",
            rb"started-4",
        )
        [in_background] = until(lambda: sleeping(background))
        [held] = until(lambda: sleeping(nohup))
        [left_alone] = until(lambda: sleeping(own))
        left.append(left_alone)
        shell = opened.shell()
        assert alive(shell) and session_of(held) == shell  # the control: one session
        assert session_of(left_alone) == left_alone
        opened.end()
        for pid in (shell, in_background, held):
            assert not alive(pid), pid  # gone by the time the end returns
        assert alive(left_alone)  # setsid left the terminal's session
        assert not Path(opened.terminal.pidfile).exists()
    finally:
        opened.end()
        for pid in left:
            os.kill(pid, 9)


def test_a_shell_that_exits_ends_its_terminal_and_its_jobs(client, workspace, alice):
    length = a_length()
    opened = Opened(client, workspace["id"], alice)
    opened.type(f"{sleep_of(length)} >/dev/null 2>&1 & exit 3\r")
    opened.ended()
    assert opened.terminal.reason is None  # it ended by itself
    assert opened.terminal.exit_code == 3
    client.portal.call(client.app.state.terminals.end, opened.terminal, terminals.TERMINAL_LOST)
    assert opened.terminal.reason is None  # an end afterwards changes nothing
    assert not sleeping(length)
    assert not Path(opened.terminal.pidfile).exists()


# --- pidfiles -----------------------------------------------------------------------------


@pytest.fixture
def channel(client, workspace):
    """The workspace's layout and its channel's ``run``, as the terminals use them."""
    service = client.app.state.workspaces
    found = service.get(workspace["id"])
    layout = service.layout_of(found)
    Path(layout.run, rt.TERMINALS).mkdir(parents=True, exist_ok=True)
    return layout, service.run_in(found)


@pytest.fixture
def victim():
    """A process in a session of its own, the leader, as a terminal's shell is."""
    proc = subprocess.Popen(["setsid", *sleep_argv(a_length())], stdin=subprocess.DEVNULL)
    until(lambda: alive(proc.pid) and session_of(proc.pid) == proc.pid)
    yield proc
    proc.kill()
    proc.wait()


def planted(layout, text: str) -> str:
    path = f"{layout.run}/{rt.TERMINALS}/{secrets.token_hex(8)}.pid"
    Path(path).write_text(text)
    return path


def test_a_pidfile_ends_its_session_only_while_it_names_the_leader(channel, victim):
    """A pidfile is the workspace's to write: one that names the leader with another start
    time, as a reused pid would be, signals nothing; the leader's own ends its session."""
    layout, run = channel
    pid, start = victim.pid, start_time(victim.pid)
    other = planted(layout, f"{pid} {start + 1}\n")
    terminals.end_session(layout, run, other)
    assert alive(pid) and not Path(other).exists()
    path = planted(layout, f"{pid} {start}\n")
    terminals.end_session(layout, run, path)
    assert victim.wait(timeout=HANG_GUARD) is not None
    assert not Path(path).exists()


# --- sweeping -----------------------------------------------------------------------------


def _left_behind(service, workspace: dict, length: str) -> subprocess.Popen:
    """A terminal's shell as an earlier rcman would have left it: launched as rcman launches
    one, on a pseudo-terminal of its own that nothing reads, its pidfile written, with a
    ``nohup sleep_of(length)`` in its session."""
    found = service.get(workspace["id"])
    layout = service.layout_of(found)
    Path(layout.run, rt.TERMINALS).mkdir(parents=True, exist_ok=True)
    pidfile = f"{layout.run}/{rt.TERMINALS}/{secrets.token_hex(8)}.pid"
    launch = service.runtime.terminal_argv(found.container, layout, pidfile)
    master, slave = os.openpty()
    proc = subprocess.Popen(launch.argv, stdin=slave, stdout=slave, stderr=slave, env=launch.env)
    os.close(slave)
    proc.master = master
    proc.pidfile = pidfile
    proc.length = length
    until(lambda: Path(pidfile).exists() and Path(pidfile).read_text().endswith("\n"))
    os.write(master, f"nohup {sleep_of(length)} >/dev/null 2>&1 &\r".encode())
    until(lambda: sleeping(length))
    return proc


@pytest.fixture
def left_behind():
    """``_left_behind``, with what it made ended when the test ends, whatever the test did."""
    made = []

    def make(service, workspace: dict, length: str) -> subprocess.Popen:
        made.append(proc := _left_behind(service, workspace, length))
        return proc

    yield make
    for proc in made:
        with contextlib.suppress(OSError):
            os.close(proc.master)
        proc.kill()
        proc.wait()
        for pid in sleeping(proc.length):
            with contextlib.suppress(OSError):
                os.kill(pid, 9)


def test_restoring_ends_the_terminals_an_earlier_rcman_left(settings, runtime, left_behind):
    length = a_length()
    with TestClient(create_app(settings, runtime=runtime)) as first:
        assert first.app.state.workspaces.restored.wait(HANG_GUARD)
        workspace = made_workspace(first, ALICE, "alpha")
    old = left_behind(first.app.state.workspaces, workspace, length)  # as if it crashed
    assert sleeping(length)  # the control
    with TestClient(create_app(settings, runtime=runtime)) as second:
        assert second.app.state.workspaces.restored.wait(HANG_GUARD)
        assert not sleeping(length)
        assert old.wait(timeout=HANG_GUARD) is not None
        assert not Path(old.pidfile).exists()


# --- what else ends a terminal ------------------------------------------------------------


def test_a_rebuild_ends_the_terminal_of_its_workspace_with_a_reason_of_its_own(
    client, workspace, alice
):
    opened = Opened(client, workspace["id"], alice)
    service = client.app.state.workspaces
    ending = threading.Thread(target=service.on_rebuilding, args=(service.get(workspace["id"]),))
    ending.start()
    ending.join(HANG_GUARD)
    opened.ended()
    assert opened.terminal.reason == terminals.WORKSPACE_REBUILDING
    assert client.app.state.terminals.live(workspace["id"]) is None


# --- the graces and deadlines --------------------------------------------------------------------


def test_a_session_that_ignores_the_hangup_is_killed_once_its_grace_is_over(channel, monkeypatch):
    """On the terminals' time, which only the test moves (tests/time_doubles.py): no terminal is
    open, so the ending is the only thing that reads it."""
    layout, run = channel
    length = a_length()
    leader = subprocess.Popen(
        ["setsid", "sh", "-c", f"trap '' HUP; exec {sleep_of(length)}"], stdin=subprocess.DEVNULL
    )
    fake = FakeTime()
    monkeypatch.setattr(terminals, "time", fake)
    try:
        until(lambda: sleeping(length) == [leader.pid] and session_of(leader.pid) == leader.pid)
        start = fake.mark()
        ending = threading.Thread(
            target=terminals.end_leader,
            args=(layout, run, leader.pid, start_time(leader.pid), False),
            daemon=True,
        )
        ending.start()
        fake.wait_sleep_begun(after=start)  # after the hangup, a look: its grace is taken
        fake.advance(terminals.END_GRACE_SECONDS)
        ending.join(HANG_GUARD)
        assert not ending.is_alive()
        assert leader.wait(timeout=HANG_GUARD) == -9
    finally:
        leader.kill()
        leader.wait()
