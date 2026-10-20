"""The Terminal tab's shells: one interactive shell per workspace, on a pseudo-terminal of
rcman's own, carried to the page by the WebSocket route (routers/terminal.py).

A shell is started by the runtime's ``terminal_argv`` (runtime.py): ``setsid --ctty`` makes
the pty the controlling terminal of what it starts - the podman client, or the shell itself on
the host runtime - and the launcher in the workspace writes the shell's id and start time to a
pidfile of the terminal's own in the runtime folder, then execs the shell. What is typed is
written to the pty as it comes, and what the shell prints is sent as it is read: the reader
leaves the event loop's watch on the pty while a send is awaited, so a page that reads slowly
slows the shell, and nothing is queued. Nothing typed or shown is kept, recorded or logged.

A terminal is open once its shell has written its pidfile: on Podman, ending the client does
not end its exec, so a shell that had not named itself could not be found to end. A terminal
ends when its shell does, when it is replaced, and when it is told to; ending it closes the pty,
ends the program on it, and then, through the workspace's channel, ends the shell's session -
every process in it, by the leader its pidfile named - and nothing that left the session.
Opening, ending and sweeping a workspace's terminals take turns under its lock, and a terminal
that replaces another is started only once the other is ended. What earlier terminals left -
sessions whose leader still runs and that no live terminal has - is swept as each terminal
opens, and when rcman starts, in a container that kept running (``Workspaces.on_kept_running``).
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import fcntl
import logging
import os
import re
import secrets
import struct
import termios
import time
from collections.abc import Awaitable, Callable

from rcman import runtime as rt
from rcman.audit import (
    SERVICE_CLOSING,
    SIGNED_OUT,
    TERMINAL_LOST,
    TERMINAL_REPLACED,
    WORKSPACE_GONE,
    WORKSPACE_REBUILDING,
)
from rcman.problems import Problem
from rcman.runtime import Launch, Layout, RuntimeFailed
from rcman.workspaces import Workspace, answered

log = logging.getLogger("rcman.terminals")
log.setLevel(logging.INFO)

__all__ = [
    "SERVICE_CLOSING",
    "SIGNED_OUT",
    "TERMINAL_LOST",
    "TERMINAL_REPLACED",
    "WORKSPACE_GONE",
    "WORKSPACE_REBUILDING",
    "Terminal",
    "Terminals",
    "end_leader",
    "end_session",
    "parsed_pidfile",
    "process_table",
    "processes",
    "read_pidfile",
    "sweep",
]

# The most of what the shell printed that is read, and sent, at once.
READ_BYTES = 64 * 1024
# A pidfile is two numbers: the shell's id and its start time, in clock ticks since boot.
MAX_PIDFILE_BYTES = 64
MAX_PID = 1 << 22  # the most the kernel gives (PID_MAX_LIMIT)
MAX_START = (1 << 64) - 1
PIDFILE = re.compile(rb"([1-9][0-9]{0,9}) (0|[1-9][0-9]{0,19})\n")
# How long the program on the pty is given to end once the pty is closed, and once more after
# each signal; and the shell's session after each signal.
END_GRACE_SECONDS = 2.0
POLL_SECONDS = 0.05
# How long a shell is given to write its pidfile, after which its terminal is not opened.
PIDFILE_SECONDS = 30.0
# How long what a shell that exited printed last is still passed on, past its exit, while a job
# of its holds the pty.
EXIT_DRAIN_SECONDS = 1.0
# The longest a call from another thread (a deletion, a sign-out, the restore) waits for what
# it asked of the terminals.
FROM_THREAD_SECONDS = 180.0

Run = Callable[..., object]
Send = Callable[[bytes], Awaitable[None]]


def parsed_pidfile(raw: bytes) -> tuple[int, int] | None:
    """The shell's id and start time a pidfile holds, written as the launcher writes them; None
    for anything else."""
    if len(raw) > MAX_PIDFILE_BYTES:
        return None
    found = PIDFILE.fullmatch(raw)
    if found is None:
        return None
    pid, start = int(found[1]), int(found[2])
    if pid > MAX_PID or start > MAX_START:
        return None
    return pid, start


# Every process's ``/proc/<pid>/stat``, each as its path and its content, each followed by a
# NUL, which neither holds: a program's name may hold a newline, and with it what would read as
# a record of its own.
_STATS = (
    'for f in /proc/[0-9]*/stat; do printf "%s\\0" "$f"; {cat} "$f" 2>/dev/null; printf "\\0"; done'
)


def processes(listed: bytes) -> dict[int, tuple[int, int]]:
    """Each process but a zombie, by id, with its session's id and its start time, from what
    ``_STATS`` printed."""
    found = {}
    parts = listed.split(b"\0")
    for path, stat in zip(parts[0::2], parts[1::2], strict=False):
        pid = path.removeprefix(b"/proc/").removesuffix(b"/stat")
        if not pid.isdigit() or not stat.startswith(pid + b" ("):
            continue
        fields = stat.rpartition(b")")[2].split()
        try:
            if fields[0] == b"Z":
                continue
            found[int(pid)] = (int(fields[3]), int(fields[19]))
        except (ValueError, IndexError):
            continue
    return found


def process_table(layout: Layout, run: Run) -> dict[int, tuple[int, int]]:
    """Every process in the workspace (``processes``), as the workspace sees them."""
    script = _STATS.format(cat=" ".join(layout.tool("cat")))
    return processes(run([layout.shell, "-c", script]).out)


def _members(
    table: dict[int, tuple[int, int]], pid: int, start: int, leader_gone: bool
) -> list[int]:
    """The processes of the session whose leader is ``pid``, started at ``start``: none unless
    the leader is that very process, the leader of its session. With ``leader_gone``, which only a
    terminal's own cleanup gives, a leader that is gone still names its session: the cleanup
    knows the leader from its open, and a leader that ended is a shell that ended, which ends the
    terminal, and so its cleanup, within seconds; the kernel gives a session's id to no other
    process while any process is in the session, so the processes with that id are the
    terminal's. What it cannot tell is a session that ended in those seconds and a new one led by
    a process given the same id since, which the kernel makes unlikely by giving ids in turn."""
    leader = table.get(pid)
    if leader is None and not leader_gone:
        return []
    if leader is not None and leader != (pid, start):
        return []  # another process has the id now, or it left its session
    return sorted(p for p, (session, _) in table.items() if session == pid)


def end_leader(layout: Layout, run: Run, pid: int, start: int, leader_gone: bool) -> None:
    """End the session ``pid`` leads, started at ``start``: SIGHUP to every process in it, then
    SIGKILL to those left (``_members``). The leader is checked once, first; once it has been
    seen, the session is its own while it lasts, however soon the leader ends."""
    members = _members(process_table(layout, run), pid, start, leader_gone)
    for signal in ("HUP", "KILL"):
        if not members:
            return
        run([*layout.tool("kill"), f"-{signal}", *map(str, members)])
        deadline = time.monotonic() + END_GRACE_SECONDS
        while True:
            members = _members(process_table(layout, run), pid, start, leader_gone=True)
            if not members or time.monotonic() >= deadline:
                break
            time.sleep(POLL_SECONDS)


def read_pidfile(layout: Layout, run: Run, pidfile: str) -> tuple[int, int] | None:
    read = run([*layout.tool("cat"), pidfile])
    return parsed_pidfile(read.out) if read.code == 0 else None


def end_session(layout: Layout, run: Run, pidfile: str) -> None:
    """End the session of the shell ``pidfile`` names, if it names one as the launcher wrote
    it and that shell still leads it, and remove the pidfile. A pidfile of anything else
    signals nothing."""
    named = read_pidfile(layout, run, pidfile)
    if named is not None:
        end_leader(layout, run, *named, leader_gone=False)
    run([*layout.tool("rm"), "-f", pidfile])


# Every pidfile in the folder, one per line; nothing where there is none.
_LISTED = 'for f in "$1"/*.pid; do [ -f "$f" ] && echo "$f"; done; :'


def sweep(layout: Layout, run: Run, keep: set[str]) -> None:
    """End the sessions of the workspace's pidfiles but those in ``keep``, the live terminals',
    each only while its shell still leads it."""
    folder = f"{layout.run}/{rt.TERMINALS}"
    listed = run([layout.shell, "-c", _LISTED, "sweep", folder])
    for path in listed.out.decode("utf-8", "replace").splitlines():
        name = path.rpartition("/")[2]
        if path == f"{folder}/{name}" and rt.TERMINAL_PIDFILE.fullmatch(name) and path not in keep:
            end_session(layout, run, path)


def _set_size(fd: int, cols: int, rows: int) -> None:
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def _woken(future: asyncio.Future) -> Callable[[], None]:
    def wake() -> None:
        if not future.done():
            future.set_result(None)

    return wake


class Terminal:
    """One shell on a pty: what is written to it, what it prints, and its end."""

    def __init__(self, workspace: Workspace, person: int, layout: Layout, run: Run, pidfile: str):
        self.workspace = workspace
        self.person = person
        self.layout = layout
        self.run = run
        self.pidfile = pidfile
        self.process: asyncio.subprocess.Process | None = None
        self.master: int | None = None
        # Why it was ended, or None while it was not, or for a shell that ended by itself.
        self.reason: str | None = None
        self.exit_code: int | None = None
        # The shell's id and start time, as its pidfile gave them once it was written.
        self.leader: tuple[int, int] | None = None
        self.cleaned = False
        self._ended = asyncio.Event()
        self._tasks: list[asyncio.Task] = []
        self._writable: asyncio.Future | None = None
        self._cleaning: asyncio.Task | None = None
        self._send: Send | None = None

    async def start(self, launch: Launch, cols: int, rows: int, send: Send) -> None:
        """Start ``launch`` on a new pty of ``cols`` by ``rows``, sending what it prints."""
        master, slave = os.openpty()
        try:
            _set_size(slave, cols, rows)
            self.process = await asyncio.create_subprocess_exec(
                *launch.argv, stdin=slave, stdout=slave, stderr=slave, env=launch.env
            )
        except BaseException:
            os.close(master)
            raise
        finally:
            os.close(slave)
        os.set_blocking(master, False)
        self.master = master
        self._send = send
        output = asyncio.create_task(self._output())
        self._tasks = [output, asyncio.create_task(self._exit(output))]

    async def _output(self) -> None:
        loop = asyncio.get_running_loop()
        fd = self.master
        while True:
            readable = loop.create_future()
            loop.add_reader(fd, _woken(readable))
            try:
                await readable
            finally:
                if self.master == fd:
                    loop.remove_reader(fd)
            try:
                chunk = os.read(fd, READ_BYTES)
            except BlockingIOError:
                continue
            except OSError as error:  # EIO: nothing holds the pty's other end any more
                if error.errno != errno.EIO:
                    log.warning("a terminal of workspace %s could not be read", self.workspace.id)
                chunk = b""
            if not chunk:
                self._ended.set()
                return
            try:
                await self._send(chunk)
            except Exception:  # noqa: BLE001 - the page is gone; its socket says so
                return

    async def _exit(self, output: asyncio.Task) -> None:
        self.exit_code = await self.process.wait()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(output), EXIT_DRAIN_SECONDS)
        self._ended.set()

    async def write(self, data: bytes) -> None:
        """Write what was typed to the shell, waiting while the pty is full."""
        loop = asyncio.get_running_loop()
        view = memoryview(data)
        while view and self.master is not None:
            fd = self.master
            try:
                view = view[os.write(fd, view) :]
            except BlockingIOError:
                self._writable = loop.create_future()
                loop.add_writer(fd, _woken(self._writable))
                try:
                    await self._writable
                finally:
                    if self.master == fd:
                        loop.remove_writer(fd)
            except OSError:  # the shell is gone
                return

    def resize(self, cols: int, rows: int) -> None:
        if self.master is not None:
            _set_size(self.master, cols, rows)

    def request_end(self, reason: str) -> None:
        """Mark it to end for ``reason``, unless it ended already."""
        if not self._ended.is_set():
            self.reason = reason
            self._ended.set()

    async def wait_ended(self) -> None:
        await self._ended.wait()

    async def cleanup(self) -> None:
        """End it: the pty, the program on it, the shell's session. Once, whoever asks."""
        if self._cleaning is None:
            self._cleaning = asyncio.ensure_future(self._clean())
        await asyncio.shield(self._cleaning)

    async def _clean(self) -> None:
        loop = asyncio.get_running_loop()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        fd, self.master = self.master, None
        if fd is not None:
            loop.remove_reader(fd)
            loop.remove_writer(fd)
            if self._writable is not None and not self._writable.done():
                self._writable.set_result(None)
            os.close(fd)
        await self._end_program()
        try:
            await asyncio.to_thread(self._end_session)
        except Exception as error:  # noqa: BLE001 - a workspace that is gone has nothing left
            log.warning("the terminal of workspace %s left a session: %s", self.workspace.id, error)
        self.cleaned = True
        self._ended.set()

    def _end_session(self) -> None:
        """The shell's session, by the leader its pidfile named when it opened, which may have
        ended as the pty closed; by its pidfile, if it never named one."""
        if self.leader is None:
            end_session(self.layout, self.run, self.pidfile)
            return
        end_leader(self.layout, self.run, *self.leader, leader_gone=True)
        self.run([*self.layout.tool("rm"), "-f", self.pidfile])

    def _read_pidfile(self) -> tuple[int, int] | None:
        with answered():  # the channel's failures as the workspace's problems
            return read_pidfile(self.layout, self.run, self.pidfile)

    async def wait_for_pidfile(self, timeout: float) -> bool:
        """Wait until the launcher has written the pidfile, which then names the shell's
        session; False if the program on the pty ended first, or ``timeout`` passed."""
        deadline = time.monotonic() + timeout
        while self.process.returncode is None and time.monotonic() < deadline:
            self.leader = await asyncio.to_thread(self._read_pidfile)
            if self.leader is not None:
                return True
            await asyncio.sleep(POLL_SECONDS)
        return False

    async def _end_program(self) -> None:
        """End the program on the pty: closing the pty hangs it up; then SIGTERM, then
        SIGKILL, each with END_GRACE_SECONDS to end."""
        process = self.process
        if process is None:
            return
        for signal in (None, process.terminate, process.kill):
            if process.returncode is not None:
                break
            if signal is not None:
                with contextlib.suppress(ProcessLookupError):
                    signal()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(process.wait(), END_GRACE_SECONDS)


class Terminals:
    """Every terminal rcman has open: at most one per workspace."""

    def __init__(self, service):
        self.service = service
        self._live: dict[int, Terminal] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        # How many times each person signed out: a terminal opened across a sign-out is ended.
        self._sign_outs: dict[int, int] = {}
        service.on_removing = self.removing
        service.on_rebuilding = self.rebuilding
        service.on_kept_running = self.kept_running

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """The loop the terminals run on, which other threads' calls are passed to."""
        self._loop = loop

    def _lock(self, workspace_id: int) -> asyncio.Lock:
        return self._locks.setdefault(workspace_id, asyncio.Lock())

    def live(self, workspace_id: int) -> Terminal | None:
        return self._live.get(workspace_id)

    def sign_outs_of(self, person: int) -> int:
        """How many times the person has signed out: what ``open`` is given, taken when their
        socket was accepted, so that a sign-out from then on ends the terminal it opens."""
        return self._sign_outs.get(person, 0)

    async def open(
        self,
        workspace_id: int,
        person: int,
        cols: int,
        rows: int,
        send: Send,
        sign_outs: int | None = None,
    ) -> Terminal:
        """Open a terminal on the person's ready workspace, ending the one it has, and what
        terminals before it left, first. It is open once its shell has written its pidfile, so
        an end at any moment afterwards finds the session to end; one whose shell does not, or
        whose opening is cancelled, is ended at once. One that a sign-out of its person crossed is
        returned ended."""
        if sign_outs is None:
            sign_outs = self.sign_outs_of(person)
        async with self._lock(workspace_id):
            found = await asyncio.to_thread(self.service.ready, person, workspace_id)
            old = self._live.get(workspace_id)
            if old is not None:
                old.request_end(TERMINAL_REPLACED)
                await old.cleanup()
                self._forget(old)
            layout = await asyncio.to_thread(self.service.layout_of, found)
            run = self.service.run_in(found)
            folder = f"{layout.run}/{rt.TERMINALS}"
            pidfile = f"{folder}/{secrets.token_hex(8)}.pid"

            def prepared() -> Launch:
                with answered():
                    sweep(layout, run, set())  # no other terminal of it is live: see above
                    made = run([*layout.tool("mkdir"), "-p", folder])
                    if made.code != 0:
                        raise RuntimeFailed(made.err.decode("utf-8", "replace").strip())
                    return self.service.runtime.terminal_argv(found.container, layout, pidfile)

            launch = await asyncio.to_thread(prepared)
            terminal = Terminal(found, person, layout, run, pidfile)
            try:
                await terminal.start(launch, cols, rows, send)
            except OSError as error:
                log.warning("a terminal of workspace %s did not start: %s", workspace_id, error)
                raise Problem("workspace_failed") from None
            try:
                written = await terminal.wait_for_pidfile(PIDFILE_SECONDS)
            except BaseException:
                await asyncio.shield(terminal.cleanup())
                raise
            if not written:
                log.warning("the shell of a terminal of workspace %s did not start", workspace_id)
                await terminal.cleanup()
                raise Problem("workspace_failed")
            if self.sign_outs_of(person) != sign_outs:
                terminal.request_end(SIGNED_OUT)
                await terminal.cleanup()
                return terminal
            self._live[workspace_id] = terminal
            return terminal

    def _forget(self, terminal: Terminal) -> None:
        if self._live.get(terminal.workspace.id) is terminal:
            del self._live[terminal.workspace.id]

    async def end(self, terminal: Terminal, reason: str) -> None:
        """End ``terminal`` for ``reason``, and wait until it is."""
        terminal.request_end(reason)
        if terminal.cleaned:
            return
        async with self._lock(terminal.workspace.id):
            await terminal.cleanup()
            self._forget(terminal)

    async def end_workspace(self, workspace_id: int, reason: str) -> None:
        """End the workspace's terminal, once one being opened is open."""
        async with self._lock(workspace_id):
            found = self._live.get(workspace_id)
            if found is not None:
                found.request_end(reason)
                await found.cleanup()
                self._forget(found)

    async def end_person(self, person: int, reason: str) -> None:
        """End the person's terminals; one being opened meanwhile ends as it opens."""
        self._sign_outs[person] = self._sign_outs.get(person, 0) + 1
        for terminal in [t for t in self._live.values() if t.person == person]:
            await self.end(terminal, reason)

    async def close_all(self, reason: str) -> None:
        """End every terminal; one that fails to end is logged, and the others are ended."""
        terminals = list(self._live.values())
        ended = await asyncio.gather(
            *(self.end(t, reason) for t in terminals), return_exceptions=True
        )
        for terminal, error in zip(terminals, ended, strict=True):
            if isinstance(error, BaseException):
                log.error(
                    "the terminal of workspace %s did not end: %r", terminal.workspace.id, error
                )

    async def sweep(self, workspace_id: int) -> None:
        """End what terminals that are no longer open left in the workspace."""
        async with self._lock(workspace_id):
            found = await asyncio.to_thread(self.service.get, workspace_id)
            if found is None:
                return
            layout = await asyncio.to_thread(self.service.layout_of, found)
            keep = {t.pidfile for t in self._live.values() if t.workspace.id == workspace_id}
            await asyncio.to_thread(sweep, layout, self.service.run_in(found), keep)

    # --- from other threads ----------------------------------------------------------------

    def _from_thread(self, call, *args) -> None:
        """``call(*args)`` on the terminals' loop, waited for; nothing where no loop runs, as
        in a process with no terminals."""
        loop = self._loop
        if loop is None or loop.is_closed() or not loop.is_running():
            return
        with contextlib.suppress(RuntimeError):
            if asyncio.get_running_loop() is loop:
                loop.create_task(call(*args))  # the loop cannot wait for itself
                return
        future = asyncio.run_coroutine_threadsafe(call(*args), loop)
        try:
            future.result(FROM_THREAD_SECONDS)
        except Exception:  # noqa: BLE001 - logged; what called it goes on
            log.exception("%s%s did not finish", call.__name__, args)

    def removing(self, workspace: Workspace) -> None:
        """The workspace is about to be removed: end its terminal while it is there."""
        self._from_thread(self.end_workspace, workspace.id, WORKSPACE_GONE)

    def rebuilding(self, workspace: Workspace) -> None:
        """The workspace's container is about to be replaced by a rebuild: end its terminal
        while the old one is still there."""
        self._from_thread(self.end_workspace, workspace.id, WORKSPACE_REBUILDING)

    def kept_running(self, workspace: Workspace) -> None:
        """The workspace's container ran on while rcman did not: sweep it."""
        self._from_thread(self.sweep, workspace.id)

    def signed_out(self, person: int) -> None:
        """The person signed out of rcman: end their terminals."""
        self._from_thread(self.end_person, person, SIGNED_OUT)
