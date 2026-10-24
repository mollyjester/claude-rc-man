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
import sys
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rcman import db, people, terminals
from rcman import runtime as rt
from rcman.main import create_app
from rcman.problems import Problem

from devtools import held_folders as hf
from tests import killed_runs as kr
from tests.conftest import ALICE, made_workspace

# How long a shell is given to answer.
ANSWERS_WITHIN = 15.0


class Screen:
    """What a terminal sent, as it arrives: its ``send``."""

    def __init__(self):
        self.data = bytearray()
        self.changed = threading.Condition()

    async def send(self, chunk: bytes) -> None:
        with self.changed:
            self.data += chunk
            self.changed.notify_all()

    def wait_for(self, pattern: bytes, timeout: float = ANSWERS_WITHIN) -> re.Match:
        """The first match of ``pattern`` in what was sent, once it is there."""
        deadline = time.monotonic() + timeout
        with self.changed:
            while True:
                found = re.search(pattern, bytes(self.data))
                if found:
                    return found
                left = deadline - time.monotonic()
                assert left > 0, bytes(self.data)[-4000:]
                self.changed.wait(left)


def person_id(settings, email: str) -> int:
    con = db.connect(settings.database)
    try:
        return people.by_email(con, email).id
    finally:
        con.close()


def a_length() -> str:
    """A length of ``sleep`` no other test uses, by which its process is found."""
    return f"{secrets.randbelow(9000) + 1000}.{secrets.randbelow(10**6):06d}"


def sleeping(length: str) -> list[int]:
    """The host's processes that are ``sleep <length>``."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        if argv[:2] == [b"sleep", length.encode()]:
            found.append(int(entry.name))
    return found


def alive(pid: int) -> bool:
    """Whether ``pid`` runs and is no zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[0] != "Z"


def wait_until(check, timeout: float = ANSWERS_WITHIN):
    deadline = time.monotonic() + timeout
    while not (found := check()):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    return found


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

    def ended(self, timeout: float = ANSWERS_WITHIN) -> None:
        self.client.portal.call(asyncio.wait_for, self.terminal.wait_ended(), timeout)

    def shell(self) -> int:
        """The shell's process, as its pidfile names it."""
        return int(Path(self.terminal.pidfile).read_text().split()[0])


@pytest.fixture
def workspace(client):
    return made_workspace(client, ALICE, "alpha")


@pytest.fixture
def alice(settings):
    return person_id(settings, ALICE)


def test_what_is_typed_reaches_the_shell_and_what_it_prints_comes_back(client, workspace, alice):
    opened = Opened(client, workspace["id"], alice)
    try:
        # the answer is computed by the shell: it is not an echo of what was typed
        opened.ask("echo $((6*7))", rb"[\r\n]42\r\n")
    finally:
        opened.end()


def test_the_terminals_size_is_the_shells_and_follows_a_resize(client, workspace, alice):
    opened = Opened(client, workspace["id"], alice, cols=100, rows=30)
    try:
        opened.ask("stty size", rb"[\r\n]30 100\r\n")
        client.portal.call(opened.terminal.resize, 132, 50)
        opened.ask("stty size", rb"[\r\n]50 132\r\n")
    finally:
        opened.end()


def test_ctrl_c_ends_the_command_in_the_foreground_and_not_the_shell(client, workspace, alice):
    length = a_length()
    opened = Opened(client, workspace["id"], alice)
    try:
        opened.type(f"sleep {length}\r")
        [pid] = wait_until(lambda: sleeping(length))
        opened.type("\x03")
        wait_until(lambda: not alive(pid))
        opened.ask("echo after-$((1+1))", rb"after-2")
        assert alive(opened.shell())
    finally:
        opened.end()


def test_closing_ends_the_shell_and_its_jobs_and_not_what_left_its_session(
    client, workspace, alice
):
    background, nohup, own = a_length(), a_length(), a_length()
    opened = Opened(client, workspace["id"], alice)
    left = []
    try:
        opened.ask(
            f"sleep {background} & nohup sleep {nohup} >/dev/null 2>&1 & "
            f"setsid sleep {own} >/dev/null 2>&1 & echo started-$((2+2))",
            rb"started-4",
        )
        [in_background] = wait_until(lambda: sleeping(background))
        [held] = wait_until(lambda: sleeping(nohup))
        [left_alone] = wait_until(lambda: sleeping(own))
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


def open_files() -> set[str]:
    return set(os.listdir("/proc/self/fd"))


def test_a_terminal_leaves_no_open_file_behind(client, workspace, alice):
    Opened(client, workspace["id"], alice).end()  # the channel it opens stays open
    before = open_files()
    opened = Opened(client, workspace["id"], alice)
    opened.ask("echo $((3*3))", rb"[\r\n]9\r\n")
    assert open_files() - before  # the control: the terminal holds some while it runs
    opened.end()
    wait_until(lambda: open_files() <= before)


def test_a_shell_that_exits_ends_its_terminal_and_its_jobs(client, workspace, alice):
    length = a_length()
    opened = Opened(client, workspace["id"], alice)
    opened.type(f"sleep {length} >/dev/null 2>&1 & exit 3\r")
    opened.ended()
    assert opened.terminal.reason is None  # it ended by itself
    assert opened.terminal.exit_code == 3
    client.portal.call(client.app.state.terminals.end, opened.terminal, terminals.TERMINAL_LOST)
    assert opened.terminal.reason is None  # an end afterwards changes nothing
    assert not sleeping(length)
    assert not Path(opened.terminal.pidfile).exists()


def test_a_second_terminal_replaces_the_first_once_the_first_is_cleaned_up(
    client, workspace, alice
):
    length = a_length()
    first = Opened(client, workspace["id"], alice)
    first.ask(f"nohup sleep {length} >/dev/null 2>&1 & echo held-$((5+5))", rb"held-10")
    [held] = wait_until(lambda: sleeping(length))
    shell = first.shell()
    second = Opened(client, workspace["id"], alice)
    try:
        # cleaned up before the second's launch, not afterwards
        assert not alive(shell) and not alive(held)
        assert first.terminal.reason == terminals.TERMINAL_REPLACED
        first.ended(timeout=1)
        second.ask("echo $((7+7))", rb"[\r\n]14\r\n")
        assert client.app.state.terminals.live(workspace["id"]) is second.terminal
    finally:
        second.end()


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
    proc = subprocess.Popen(["setsid", "sleep", a_length()], stdin=subprocess.DEVNULL)
    wait_until(lambda: alive(proc.pid) and session_of(proc.pid) == proc.pid)
    yield proc
    proc.kill()
    proc.wait()


def planted(layout, text: str) -> str:
    path = f"{layout.run}/{rt.TERMINALS}/{secrets.token_hex(8)}.pid"
    Path(path).write_text(text)
    return path


def test_a_pidfile_of_the_session_ends_it(channel, victim):
    layout, run = channel
    path = planted(layout, f"{victim.pid} {start_time(victim.pid)}\n")
    terminals.end_session(layout, run, path)
    assert victim.wait(timeout=ANSWERS_WITHIN) is not None  # the control for the cases below
    assert not Path(path).exists()


def test_a_pidfile_naming_another_process_signals_nothing(channel, victim):
    layout, run = channel
    pid, start = victim.pid, start_time(victim.pid)
    follower = subprocess.Popen(["sleep", a_length()], stdin=subprocess.DEVNULL)
    try:
        wait_until(lambda: alive(follower.pid))
        assert session_of(follower.pid) != follower.pid
        cases = {
            "wrong start time": f"{pid} {start + 1}\n",
            "not a session leader": f"{follower.pid} {start_time(follower.pid)}\n",
            "junk": "a terminal\n",
            "one number": f"{pid}\n",
            "three numbers": f"{pid} {start} 1\n",
            "a sign": f"+{pid} {start}\n",
            "no pid": f"0 {start}\n",
            "beyond any pid": f"{terminals.MAX_PID + 1} {start}\n",
            "beyond any start time": f"{pid} {'9' * 30}\n",
            "oversized": f"{pid} {start}\n" + " " * terminals.MAX_PIDFILE_BYTES,
            "empty": "",
        }
        for case, text in cases.items():
            path = planted(layout, text)
            terminals.end_session(layout, run, path)
            assert alive(pid) and alive(follower.pid), case
            assert not Path(path).exists(), case
    finally:
        follower.kill()
        follower.wait()


def test_the_pidfile_is_read_strictly():
    assert terminals.parsed_pidfile(b"12 345\n") == (12, 345)
    for raw in (b"12 345", b" 12 345\n", b"12  345\n", b"12 345\n\n", b"012 345\n", b"\xff\n"):
        assert terminals.parsed_pidfile(raw) is None, raw


# --- sweeping -----------------------------------------------------------------------------


def _left_behind(service, workspace: dict, length: str) -> subprocess.Popen:
    """A terminal's shell as an earlier rcman would have left it: launched as rcman launches
    one, on a pseudo-terminal of its own that nothing reads, its pidfile written, with a
    ``nohup sleep <length>`` in its session."""
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
    wait_until(lambda: Path(pidfile).exists() and Path(pidfile).read_text().endswith("\n"))
    os.write(master, f"nohup sleep {length} >/dev/null 2>&1 &\r".encode())
    wait_until(lambda: sleeping(length))
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


def test_a_sweep_ends_what_is_left_and_not_a_live_terminal(client, workspace, alice, left_behind):
    service = client.app.state.workspaces
    length, mine = a_length(), a_length()
    opened = Opened(client, workspace["id"], alice)
    try:
        old = left_behind(service, workspace, length)  # after the opening's own sweep
        opened.ask(f"sleep {mine} & echo mine-$((4*4))", rb"mine-16")
        client.portal.call(client.app.state.terminals.sweep, workspace["id"])
        assert not sleeping(length) and old.wait(timeout=ANSWERS_WITHIN) is not None
        assert not Path(old.pidfile).exists()
        assert sleeping(mine) and alive(opened.shell())  # the live terminal's own
        opened.ask("echo $((8+8))", rb"[\r\n]16\r\n")
    finally:
        opened.end()


def test_a_sweep_waits_for_a_terminal_being_opened(client, workspace, alice, monkeypatch):
    """Between the shell writing its pidfile and the terminal being known, the pidfile looks
    left behind: a sweep then would end the shell being opened."""
    real = terminals.Terminal.start
    started = threading.Event()

    async def slow(self, *args, **kwargs):
        await real(self, *args, **kwargs)
        await asyncio.to_thread(lambda: wait_until(lambda: Path(self.pidfile).exists()))
        started.set()
        await asyncio.sleep(1.0)

    monkeypatch.setattr(terminals.Terminal, "start", slow)
    app_terminals = client.app.state.terminals
    holder = {}
    opening = threading.Thread(
        target=lambda: holder.setdefault("opened", Opened(client, workspace["id"], alice))
    )
    opening.start()
    assert started.wait(ANSWERS_WITHIN)
    client.portal.call(app_terminals.sweep, workspace["id"])  # while it is being opened
    opening.join(ANSWERS_WITHIN)
    opened = holder["opened"]
    try:
        assert alive(opened.shell())
        opened.ask("echo $((9+9))", rb"[\r\n]18\r\n")
    finally:
        opened.end()


def test_restoring_ends_the_terminals_an_earlier_rcman_left(settings, runtime, left_behind):
    length = a_length()
    with TestClient(create_app(settings, runtime=runtime)) as first:
        assert first.app.state.workspaces.restored.wait(30)
        workspace = made_workspace(first, ALICE, "alpha")
    old = left_behind(first.app.state.workspaces, workspace, length)  # as if it crashed
    assert sleeping(length)  # the control
    with TestClient(create_app(settings, runtime=runtime)) as second:
        assert second.app.state.workspaces.restored.wait(30)
        assert not sleeping(length)
        assert old.wait(timeout=ANSWERS_WITHIN) is not None
        assert not Path(old.pidfile).exists()


# --- opening: the pidfile first, and what a sign-out or a deletion crosses ------------------


def test_a_terminal_is_open_once_its_shell_has_named_itself_and_an_end_at_once_ends_it(
    client, workspace, alice
):
    """On Podman, ending the client does not end the exec: a terminal ended before its shell
    wrote its pidfile would leave the shell running. So it is open only once the pidfile names
    the shell, and an end straight after the open finds it."""
    opened = Opened(client, workspace["id"], alice)
    named = terminals.parsed_pidfile(Path(opened.terminal.pidfile).read_bytes())
    assert named is not None and opened.terminal.leader == named  # at once, without waiting
    shell = named[0]
    assert alive(shell)
    opened.end()
    assert not alive(shell)


def test_a_shell_that_never_names_itself_is_not_opened_and_is_ended(
    client, workspace, alice, monkeypatch
):
    length = a_length()
    runtime = client.app.state.workspaces.runtime
    real = runtime.terminal_argv

    def silent(name, layout, pidfile):
        launch = real(name, layout, pidfile)
        return rt.Launch([*launch.argv[:2], "sleep", length], launch.env)  # writes no pidfile

    monkeypatch.setattr(runtime, "terminal_argv", silent)
    monkeypatch.setattr(terminals, "PIDFILE_SECONDS", 1.0)
    screen = Screen()
    with pytest.raises(Problem) as refused:
        client.portal.call(
            client.app.state.terminals.open, workspace["id"], alice, 80, 24, screen.send
        )
    assert refused.value.code == "workspace_failed"
    assert not sleeping(length)
    assert client.app.state.terminals.live(workspace["id"]) is None


def test_opening_a_terminal_ends_what_earlier_terminals_left(client, workspace, alice, left_behind):
    length = a_length()
    old = left_behind(client.app.state.workspaces, workspace, length)
    opened = Opened(client, workspace["id"], alice)
    try:
        assert not sleeping(length)
        assert old.wait(timeout=ANSWERS_WITHIN) is not None
    finally:
        opened.end()


def opening_slowly(client, monkeypatch):
    """Open terminals with a pause once the shell has named itself and before the terminal is
    known: an event set when the pause begins."""
    real = terminals.Terminal.wait_for_pidfile
    paused = threading.Event()

    async def slow(self, timeout):
        written = await real(self, timeout)
        paused.set()
        await asyncio.sleep(1.0)
        return written

    monkeypatch.setattr(terminals.Terminal, "wait_for_pidfile", slow)
    return paused


def opened_in_the_background(client, workspace_id, person) -> tuple[threading.Thread, dict]:
    holder = {}
    thread = threading.Thread(
        target=lambda: holder.setdefault("opened", Opened(client, workspace_id, person))
    )
    thread.start()
    return thread, holder


def test_a_deletion_while_a_terminal_opens_ends_it(client, workspace, alice, monkeypatch):
    paused = opening_slowly(client, monkeypatch)
    thread, holder = opened_in_the_background(client, workspace["id"], alice)
    assert paused.wait(ANSWERS_WITHIN)
    client.portal.call(
        client.app.state.terminals.end_workspace, workspace["id"], terminals.WORKSPACE_GONE
    )
    thread.join(ANSWERS_WITHIN)
    opened = holder["opened"]
    assert opened.terminal.reason == terminals.WORKSPACE_GONE
    assert opened.terminal.cleaned and not alive(opened.terminal.leader[0])


def test_a_rebuild_ends_the_terminal_of_its_workspace_with_a_reason_of_its_own(
    client, workspace, alice
):
    opened = Opened(client, workspace["id"], alice)
    service = client.app.state.workspaces
    ending = threading.Thread(target=service.on_rebuilding, args=(service.get(workspace["id"]),))
    ending.start()
    ending.join(ANSWERS_WITHIN)
    opened.ended()
    assert opened.terminal.reason == terminals.WORKSPACE_REBUILDING
    assert client.app.state.terminals.live(workspace["id"]) is None


def test_a_sign_out_while_a_terminal_opens_ends_it(client, workspace, alice, monkeypatch):
    paused = opening_slowly(client, monkeypatch)
    thread, holder = opened_in_the_background(client, workspace["id"], alice)
    assert paused.wait(ANSWERS_WITHIN)
    client.portal.call(client.app.state.terminals.end_person, alice, terminals.SIGNED_OUT)
    thread.join(ANSWERS_WITHIN)
    opened = holder["opened"]
    assert opened.terminal.reason == terminals.SIGNED_OUT
    assert opened.terminal.cleaned and not alive(opened.terminal.leader[0])
    assert client.app.state.terminals.live(workspace["id"]) is None


def test_closing_every_terminal_goes_on_past_one_that_fails(client, alice, monkeypatch):
    first = made_workspace(client, ALICE, "first")
    second = made_workspace(client, ALICE, "second")
    ones = [Opened(client, w["id"], alice) for w in (first, second)]
    shells = [o.shell() for o in ones]
    real = terminals.Terminals.end

    async def failing(self, terminal, reason):
        if terminal is ones[0].terminal:
            raise RuntimeError("planted")
        await real(self, terminal, reason)

    monkeypatch.setattr(terminals.Terminals, "end", failing)
    client.portal.call(client.app.state.terminals.close_all, terminals.SERVICE_CLOSING)
    assert not alive(shells[1])  # the other was ended all the same
    monkeypatch.setattr(terminals.Terminals, "end", real)
    ones[0].end()


# --- what the shell is given and what is read of the workspace's processes ------------------


def test_the_shell_holds_no_file_of_rcmans(client, workspace, alice):
    opened = Opened(client, workspace["id"], alice)
    try:
        # no command substitution and no pipe, which would hold a file of the shell's own
        found = opened.ask(
            "echo fds-$((1+1)); ls /proc/$$/fd; echo end-$((1+2))", rb"fds-2\r\n([0-9 \r\n]*)end-3"
        )
        held = set(found[1].split())
        assert {b"0", b"1", b"2"} <= held <= {b"0", b"1", b"2", b"255"}  # bash keeps 255
    finally:
        opened.end()


def test_a_process_named_with_a_newline_is_read_as_one(channel):
    """A program's name may hold a newline, and with it what would read as another process's
    record: the table is read one file at a time, so it cannot."""
    layout, run = channel
    forged = "z\n1 (x) R 0 1 1"  # a record of process 1 in a session of its own, were it read
    proc = subprocess.Popen(
        [sys.executable, "-c",
         f"open('/proc/self/comm', 'w').write({forged!r}); import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        wait_until(lambda: "\n" in Path(f"/proc/{proc.pid}/comm").read_text().rstrip("\n"))
        table = terminals.process_table(layout, run)
        assert table[proc.pid] == (session_of(proc.pid), start_time(proc.pid))
        assert table[1] == (session_of(1), start_time(1))
    finally:
        proc.kill()
        proc.wait()


def test_a_process_whose_first_thread_has_ended_is_in_the_table(channel, tmp_path):
    """Its stat reads Z while another thread of it runs, and it is still a process to end."""
    layout, run = channel
    with kr.FirstThreadEnded(tmp_path) as helper:
        pid = helper.start()
        # the control: its first thread has ended
        assert hf.state_and_threads(pid) == kr.FIRST_THREAD_ENDED
        table = terminals.process_table(layout, run)
        assert table.get(pid) == (session_of(pid), start_time(pid))


def test_the_table_drops_a_zombie_and_keeps_one_whose_other_threads_run():
    def stat(pid: int, state: str, threads: int) -> bytes:
        fields = [state, "1", str(pid), str(pid), *["0"] * 13, str(threads), "0", "77"]
        return f"/proc/{pid}/stat\0{pid} (x) {' '.join(fields)}\0".encode()

    listed = stat(10, "S", 1) + stat(11, "Z", 2) + stat(12, "Z", 1)
    assert terminals.processes(listed) == {10: (10, 77), 11: (11, 77)}


def test_ending_a_session_ends_a_member_whose_first_thread_has_ended(channel, tmp_path):
    layout, run = channel
    length = a_length()
    with kr.FirstThreadEnded(tmp_path) as helper:
        leader = subprocess.Popen(
            ["setsid", "sh", "-c", f"{shlex.join(helper.argv)} & echo $!; exec sleep {length}"],
            pass_fds=(helper.read_end,), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        )  # fmt: skip
        member = None
        try:
            member = int(leader.stdout.readline())
            kr.wait_for_state(member, kr.FIRST_THREAD_ENDED)
            wait_until(lambda: session_of(leader.pid) == leader.pid)
            assert session_of(member) == leader.pid  # the control: it is of the leader's session
            terminals.end_leader(layout, run, leader.pid, start_time(leader.pid), leader_gone=False)
            assert leader.wait(timeout=ANSWERS_WITHIN) is not None
            assert hf.state_and_threads(member) in (None, kr.ENDED)  # ended, all its threads
        finally:
            leader.kill()
            if member is not None and hf.state_and_threads(member) not in (None, kr.ENDED):
                os.kill(member, 9)
            leader.wait()
            leader.stdout.close()


def test_a_pidfile_whose_shell_is_gone_signals_nothing_but_for_its_own_terminal(channel):
    """Planted twin of the cleanup's own case: once the leader is gone, its session is ended
    only by the terminal that knew it, never by a sweep or a pidfile alone."""
    layout, run = channel
    length = a_length()
    leader = subprocess.Popen(
        ["setsid", "sh", "-c", f"sleep {length} & sleep 1"], stdin=subprocess.DEVNULL
    )
    wait_until(lambda: alive(leader.pid) and session_of(leader.pid) == leader.pid)
    start = start_time(leader.pid)
    try:
        [member] = wait_until(lambda: sleeping(length))
        leader.wait(timeout=ANSWERS_WITHIN)
        assert session_of(member) == leader.pid  # the control: its session outlives its leader
        terminals.end_session(layout, run, planted(layout, f"{leader.pid} {start}\n"))
        assert alive(member)
        terminals.end_leader(layout, run, leader.pid, start, leader_gone=True)
        assert not alive(member)
    finally:
        for pid in sleeping(length):
            os.kill(pid, 9)


def test_a_channel_that_fails_while_the_shell_names_itself_is_a_problem(
    client, workspace, alice, monkeypatch
):
    """A failure of the channel while the pidfile is read is the workspace's, in rcman's words,
    and the shell started is ended."""
    from rcman.channel import ChannelBroken

    def broken(*args):
        raise ChannelBroken("planted")

    monkeypatch.setattr(terminals, "read_pidfile", broken)
    started = []
    real = terminals.Terminal.start

    async def start(self, *args, **kwargs):
        await real(self, *args, **kwargs)
        started.append(self)

    monkeypatch.setattr(terminals.Terminal, "start", start)
    screen = Screen()
    with pytest.raises(Problem) as refused:
        client.portal.call(
            client.app.state.terminals.open, workspace["id"], alice, 80, 24, screen.send
        )
    assert refused.value.code == "workspace_failed"
    [terminal] = started
    assert terminal.cleaned and terminal.process.returncode is not None
