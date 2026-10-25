"""Reaching a port in a workspace through ``podman --remote exec``, against real rootless Podman:
the tests' engine (tests/podman_service.py), with the suites' sizes (tests/container/sizes.py),
the tools as the installer writes them and the default workspace image
(tests/container/conftest.py).

The relay is the product's own (``PodmanRuntime.port_argv``, runtime.py RELAY_SCRIPT): under
busybox's ``setsid``, so that it leads a process group of its own in the workspace, the mounted
busybox's shell writes its own id to a pidfile, then runs ``cat`` into ``nc``, and once
either ends - ``cat`` having read its input to the end, or ``nc`` its connection's - it removes
its pidfile and ``kill 0`` ends the relay's process group, the other with it. So the relay ends
when either side does, whoever ended it, while the other holds its side open, and leaves no
pidfile; and it carries no half-close. What a relay leaves anyway is ended by forwards.py's
sweep, through the workspace's channel. The workspace
joins a network of its own (tests/container/network.py), so nothing it listens on is the
machine's.

What is in a workspace's processes is read from its ``/proc``, by the mounted busybox's shell,
and which exec sessions run from Podman's API (tests/container/execs.py); every absence is read
by a check that first sees what it looks for while it runs.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import queue
import signal
import subprocess
import threading
import time

import pytest
from rcman import forwards
from rcman import runtime as rt

from tests import podman_service as ps
from tests.container import network, sizes
from tests.container.conftest import engine_of, engine_runtime
from tests.container.execs import exec_ids

# The module's workspace and its person, clear of the other modules' numbers.
WORKSPACE = 740
SLOT = sizes.PEOPLE_SLOTS[2]
BUSYBOX = f"{rt.TOOLS_MOUNT}/bin/busybox"
# What the relay runs, as the mounted busybox's shell runs it with ``-c``: $1 the pidfile, $2 the
# port on the workspace's loopback.
RELAY = rt.RELAY_SCRIPT.format(busybox=BUSYBOX)
# The relay that ends only with its input: what the planted positive of an app closing first
# runs, which does not end while its client keeps its input open.
ONE_SIDED = f'echo $$ >"$1"; {{ {BUSYBOX} cat; kill 0; }} | {BUSYBOX} nc 127.0.0.1 "$2"'
# The ports the workspace listens on in its own network: one per part of the tests.
EXCHANGED, PLANTED, HELD_KILLED, HELD_CLOSED, HALF_CLOSED = 6080, 6082, 6083, 6084, 6085
APP_CLOSED, APP_CLOSED_ONE_SIDED, LEFT_OVER = 6086, 6087, 6088
# Bytes sent each way: many times what a pipe holds, so neither side can have taken it whole.
PAYLOAD = 1024 * 1024
# Seconds a step may take, and how long what a relay leaves is given to go.
WITHIN = 120
GONE_WITHIN = 15
# What the bystander runs: a process of a separate exec, which no relay's end may take with it.
BYSTANDER = [BUSYBOX, "sleep", "3601"]
# Run by the workspace's python3: listens on 127.0.0.1:<port> and takes one connection. By its
# mode it then reads <size> bytes, saying how many and their sha256, answers <size> random bytes,
# saying the same of them, and reads on to the end ("answer"); or does the same but closes its
# side once it has answered, saying "closed" ("close"); or reads to the end before it answers so
# ("after-eof"); or reads to the end and keeps its side open ("hold"). It says "eof" with how much
# it read in all, and its sha256, when the end of file arrives.
LISTENER = r"""
import hashlib, os, socket, sys, time
port, mode, size = int(sys.argv[1]), sys.argv[2], int(sys.argv[3])
made = socket.socket()
made.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
made.bind(("127.0.0.1", port))
made.listen(1)
print("listening", os.getpid(), flush=True)
peer, _ = made.accept()
print("accepted", flush=True)
digest, length = hashlib.sha256(), 0

def answer():
    back = os.urandom(size)
    print("answer", len(back), hashlib.sha256(back).hexdigest(), flush=True)
    try:
        peer.sendall(back)
    except OSError as failed:
        print("unsent", failed.errno, flush=True)

if mode in ("answer", "close"):
    while length < size and (chunk := peer.recv(min(65536, size - length))):
        digest.update(chunk)
        length += len(chunk)
    print("got", length, digest.hexdigest(), flush=True)
    answer()
if mode == "close":
    peer.close()
    print("closed", flush=True)
    time.sleep(3600)
try:
    while chunk := peer.recv(65536):
        digest.update(chunk)
        length += len(chunk)
except OSError:
    pass
print("eof", length, digest.hexdigest(), flush=True)
if mode == "after-eof":
    answer()
if mode == "hold":
    time.sleep(3600)
peer.close()
"""
# Run by the mounted busybox's shell in the workspace: every process's id and argv, a line each,
# "<pid> <argv>" with the argv's separators made \1 and any newline in it \2.
PROCESSES = (
    "for d in /proc/[0-9]*; do printf '%s ' \"${d#/proc/}\"; "
    f"{BUSYBOX} tr '\\0\\n' '\\1\\2' <\"$d/cmdline\" 2>/dev/null; echo; done"
)


def pipe_size() -> int:
    """What a pipe holds on this machine."""
    read, write = os.pipe()
    try:
        return fcntl.fcntl(write, fcntl.F_GETPIPE_SZ)
    finally:
        os.close(read)
        os.close(write)


@pytest.fixture(scope="module")
def holder(podman_service, default_image, labelled):
    """The network the module's workspace joins, held by a container with no way out."""
    with network.holder(
        podman_service, default_image, labelled, f"rcman-test-forward-{os.getpid()}"
    ) as name:
        yield name


@pytest.fixture(scope="module")
def podman(podman_service, default_image, engine_tools, engine_store, engine_scratch, holder):
    return engine_runtime(
        podman_service, default_image, engine_tools, engine_store, engine_scratch,
        network=f"container:{holder}",
    )  # fmt: skip


@pytest.fixture(scope="module")
def workspace(podman):
    spec = rt.Spec(rt.container_name(WORKSPACE), slot=SLOT, workspace=WORKSPACE, person=WORKSPACE)
    podman.create(spec)
    try:
        yield spec.name
    finally:
        podman.remove(spec.name)
        podman.remove_account(spec.person)


def processes(podman, name: str) -> dict[int, list[str]]:
    """Every process of the workspace, by its id there: its argv."""
    found = podman.exec(name, [BUSYBOX, "sh", "-c", PROCESSES])
    assert found.code == 0, found
    table = {}
    for line in found.out.splitlines():
        pid, _, cmdline = line.partition(b" ")
        if not pid:
            continue
        argv = cmdline.rstrip(b"\1").split(b"\1") if cmdline else []
        table[int(pid)] = [part.decode(errors="replace") for part in argv]
    assert table, found  # the control: the table was read
    return table


# Run by the mounted busybox's shell in the workspace: every process's id and process group, a
# line each, "<pid> <pgrp>", from its stat.
GROUPS = (
    'for d in /proc/[0-9]*; do read -r stat 2>/dev/null <"$d/stat" || continue; '
    'set -- ${stat##*) }; echo "${d#/proc/} $3"; done'
)


def groups(podman, name: str) -> dict[int, int]:
    """Every process of the workspace, by its id there: its process group."""
    found = podman.exec(name, [BUSYBOX, "sh", "-c", GROUPS])
    assert found.code == 0, found
    table = {int(pid): int(group) for pid, group in (
        line.split() for line in found.out.decode().splitlines() if line.strip())}  # fmt: skip
    assert table, found  # the control: the table was read
    return table


def wrapper_argv(pidfile: str, port: int) -> list[str]:
    """The argv of the relay's shell, which busybox's ``setsid`` runs in its own place, and of
    the subshells it forks for ``cat`` and ``nc``."""
    return rt.relay_shell(rt.PODMAN_LAYOUT, pidfile, port)


def relay_parts(podman, name: str) -> dict[int, list[str]]:
    """Every process of a relay running in the workspace - its shell and subshell, ``cat`` and
    ``nc``, the mounted busybox's or another ``nc`` - by its id: its argv."""

    def of_a_relay(argv: list[str]) -> bool:
        program = os.path.basename(argv[0]) if argv else ""
        return (
            program == "nc"
            or (program == "busybox" and argv[1:2] == ["nc"])
            or argv == [BUSYBOX, "cat"]
            or argv[:4] in ([BUSYBOX, "sh", "-c", RELAY], [BUSYBOX, "sh", "-c", ONE_SIDED])
        )

    return {pid: argv for pid, argv in processes(podman, name).items() if of_a_relay(argv)}


def relay_started(podman, name: str, pidfile: str, port: int, script: str | None = None):
    """The relay to ``port`` as the forwarding server starts it (``port_argv``), or, with
    ``script``, an exec of the same shape running that script in its place."""
    launch = podman.port_argv(name, port, pidfile)
    argv = launch.argv
    if script is not None:
        argv = [script if part == RELAY else part for part in argv]
        assert argv != launch.argv  # the plant took
    return subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=launch.env, cwd=engine_of(podman).folder,
    )  # fmt: skip


def expected_parts(pidfile: str, port: int) -> list[list[str]]:
    """The argvs of a running relay's processes, sorted: its shell and the two subshells it
    forked, ``cat`` and ``nc``."""
    wrapper = wrapper_argv(pidfile, port)
    return sorted(
        [wrapper, wrapper, wrapper, [BUSYBOX, "cat"], [BUSYBOX, "nc", "127.0.0.1", str(port)]]
    )


class Listener:
    """LISTENER running in the workspace, and the lines it says, as they come."""

    def __init__(self, podman, name: str, port: int, mode: str, size: int = 0):
        engine = engine_of(podman)
        self.podman, self.name = podman, name
        self.proc = subprocess.Popen(
            engine.argv("exec", name, "python3", "-c", LISTENER, str(port), mode, str(size)),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=ps.env(engine.folder), cwd=engine.folder,
        )  # fmt: skip
        self.lines: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.pid = int(self.said("listening")[0])
        except BaseException:
            ended(self.proc)
            raise

    def _read(self) -> None:
        for line in self.proc.stdout:
            self.lines.put(line.decode().split())
        self.lines.put(None)

    def said(self, word: str, within: float = WITHIN) -> list[str]:
        """What the listener said after ``word``, waiting for it."""
        deadline = time.monotonic() + within
        while True:
            left = deadline - time.monotonic()
            assert left > 0, f"the listener never said {word!r}"
            try:
                line = self.lines.get(timeout=left)
            except queue.Empty:
                continue
            if line is None:
                said = self.proc.stderr.read().decode(errors="replace")
                raise AssertionError(f"the listener ended before {word!r}: {said}")
            if line[0] == word:
                return line[1:]

    def end(self) -> None:
        self.podman.exec(self.name, [BUSYBOX, "kill", "-KILL", str(self.pid)])
        try:
            self.proc.wait(timeout=WITHIN)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def ended(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait()
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream:
            stream.close()


def until(check, within: float = WITHIN):
    deadline = time.monotonic() + within
    while not (found := check()):
        assert time.monotonic() < deadline, f"never: {check.__doc__ or check}"
        time.sleep(0.2)
    return found


def pidfile(port: int) -> str:
    """A relay's pidfile, named as ``forward_pidfile`` names one, by the port: in the runtime
    folder, the container's tmpfs, so it goes with the container's stop."""
    return f"{rt.RUN_MOUNT}/forward-{port:016x}.pid"


def pidfile_there(podman, name: str, port: int) -> bool:
    """Whether the relay's pidfile is in the workspace's runtime folder."""
    return podman.exec(name, [BUSYBOX, "test", "-e", pidfile(port)]).code == 0


def shell_of(podman, name: str, port: int) -> int:
    """The relay's shell, as its pidfile names it."""
    read = podman.exec(name, [BUSYBOX, "cat", pidfile(port)])
    assert read.code == 0, read
    return int(read.out.decode())


def next_read(stream, within: float) -> bytes | None:
    """What ``stream`` gives next - b"" at its end of file - or None if nothing comes within
    ``within`` seconds."""
    got: queue.Queue = queue.Queue()
    threading.Thread(target=lambda: got.put(stream.read(1)), daemon=True).start()
    try:
        return got.get(timeout=within)
    except queue.Empty:
        return None


def running_execs(podman, name: str) -> set[str]:
    """The exec sessions of the workspace's container that run, as Podman's API tells each one
    (a remote exec's record stays a while after it ends)."""
    return set(exec_ids(engine_of(podman), name))


def settled(read, within: float = GONE_WITHIN):
    """What ``read`` gives once it is falsy, or what it gives last when ``within`` has passed."""
    deadline = time.monotonic() + within
    while (found := read()) and time.monotonic() < deadline:
        time.sleep(0.2)
    return found


def killed(relay: subprocess.Popen) -> None:
    relay.send_signal(signal.SIGKILL)
    relay.wait(timeout=WITHIN)


def input_closed(relay: subprocess.Popen) -> None:
    relay.stdin.close()


def written(stream, data: bytes) -> None:
    stream.write(data)
    stream.flush()


def read_exactly(stream, size: int) -> bytes:
    parts, left = [], size
    while left and (part := stream.read(min(left, 65536))):
        parts.append(part)
        left -= len(part)
    return b"".join(parts)


@pytest.fixture
def bystander(podman, workspace):
    """A process of a separate exec in the workspace, by its id, ended afterwards."""
    started = podman.exec(
        workspace, [BUSYBOX, "sh", "-c", f"( exec {' '.join(BYSTANDER)} </dev/null >/dev/null "
                    "2>&1 ) & echo $!"],
    )  # fmt: skip
    assert started.code == 0, started
    pid = int(started.out.decode().strip())
    assert processes(podman, workspace).get(pid) == BYSTANDER  # the control: it runs
    try:
        yield pid
    finally:
        podman.exec(workspace, [BUSYBOX, "kill", "-KILL", str(pid)])


def held_relay_left(podman, name: str, port: int, end) -> dict:
    """A relay to a listener that keeps its side open, its client ended by ``end`` (given the
    client): whether the listener got the relay's end of file and still runs, and the relay's
    processes, exec and pidfile still there once a short while has passed, each seen while it
    ran first, with its pidfile naming its shell. What is left is ended afterwards, so that the next
    case starts clean."""
    holding = Listener(podman, name, port, "hold")
    before = running_execs(podman, name)  # the listener's own among them
    relay = relay_started(podman, name, pidfile(port), port)
    try:
        holding.said("accepted")
        shell = shell_of(podman, name, port)
        parts = relay_parts(podman, name)
        assert sorted(parts.values()) == expected_parts(pidfile(port), port), parts
        assert parts[shell] == wrapper_argv(pidfile(port), port)  # the pidfile names the shell
        # The shell leads a process group of its own, which holds the relay's parts alone, so
        # that its ``kill 0`` takes nothing else.
        table = groups(podman, name)
        assert table[shell] == shell, table
        assert {p for p, g in table.items() if g == shell} == set(parts), (table, parts)
        [relay_exec] = running_execs(podman, name) - before  # the positive: its exec runs
        assert pidfile_there(podman, name, port)  # the positive: its pidfile is there
        end(relay)
        try:
            holding.said("eof", within=GONE_WITHIN)
            eof = True
        except AssertionError:
            eof = False
        return {
            "listener got eof": eof,
            "relay left": settled(lambda: relay_parts(podman, name)),
            "exec left": settled(lambda: relay_exec in running_execs(podman, name)),
            "pidfile left": settled(lambda: pidfile_there(podman, name, port)),
            "listener still runs": holding.pid in processes(podman, name),
        }
    finally:
        for pid in relay_parts(podman, name):
            podman.exec(name, [BUSYBOX, "kill", "-KILL", str(pid)])
        ended(relay)
        holding.end()
        until(lambda: relay_parts(podman, name) == {})
        podman.exec(name, [BUSYBOX, "rm", "-f", pidfile(port)])


def test_the_busybox_relay_over_exec_is_byte_exact_both_ways_and_ends_with_its_input(
    podman, workspace, bystander
):
    """Gate: the mounted busybox has ``nc``; bytes cross the relay both ways intact, binary and
    more than a pipe holds, its client keeping its input open until it has read the answer and
    then closing it, which reaches the listener as its end of file; the relay's pidfile names its
    shell while it runs; and once its client is killed, or only closes its input, while the
    listener keeps its side open, no ``nc``, ``cat`` or shell of the relay, no exec of it and
    no pidfile of it is left, while the listener and a bystander of another exec still run.
    Each absence is read by a check that first sees what it looks for: a ``nc`` left running on
    purpose, and the relay's processes, exec and pidfile while it runs."""
    applets = podman.exec(workspace, [BUSYBOX, "--list"])
    assert applets.code == 0 and {"nc", "cat"} <= set(applets.out.decode().split()), applets
    assert relay_parts(podman, workspace) == {}  # nothing before

    # The planted positive: a ``nc`` its listener keeps open is seen, by its id, until killed.
    holding = Listener(podman, workspace, PLANTED, "hold")
    try:
        left = podman.exec(workspace, [BUSYBOX, "sh", "-c",
                                       f"( exec {BUSYBOX} nc 127.0.0.1 {PLANTED} </dev/null "
                                       ">/dev/null 2>&1 ) & echo $!"])  # fmt: skip
        assert left.code == 0, left
        planted = int(left.out.decode().strip())
        assert holding.said("eof") == ["0", hashlib.sha256().hexdigest()]  # connected, then EOF
        assert relay_parts(podman, workspace) == {
            planted: [BUSYBOX, "nc", "127.0.0.1", str(PLANTED)]
        }  # still running, its listener's side open
        podman.exec(workspace, [BUSYBOX, "kill", "-KILL", str(planted)])
        until(lambda: relay_parts(podman, workspace) == {})
    finally:
        holding.end()

    # Both ways, byte for byte: the client sends, reads the whole answer, then ends its input.
    assert PAYLOAD > pipe_size()
    answering = Listener(podman, workspace, EXCHANGED, "answer", PAYLOAD)
    before = running_execs(podman, workspace)
    relay = relay_started(podman, workspace, pidfile(EXCHANGED), EXCHANGED)
    try:
        sent = os.urandom(PAYLOAD)
        writer = threading.Thread(target=written, args=(relay.stdin, sent), daemon=True)
        writer.start()
        back = read_exactly(relay.stdout, PAYLOAD)
        writer.join(timeout=WITHIN)
        assert answering.said("got") == [str(len(sent)), hashlib.sha256(sent).hexdigest()]
        assert answering.said("answer") == [str(len(back)), hashlib.sha256(back).hexdigest()]
        assert len(back) == PAYLOAD
        [relay_exec] = running_execs(podman, workspace) - before  # the positive: its exec runs
        assert pidfile_there(podman, workspace, EXCHANGED)  # and its pidfile is there
        relay.stdin.close()  # the end of its input reaches the listener as its end of file
        assert answering.said("eof") == [str(len(sent)), hashlib.sha256(sent).hexdigest()]
        relay.wait(timeout=WITHIN)  # the relay ended with its input; its code means nothing
        assert relay.stdout.read() == b""  # and nothing more came
        until(lambda: relay_parts(podman, workspace) == {})
        until(lambda: relay_exec not in running_execs(podman, workspace))
        until(lambda: not pidfile_there(podman, workspace, EXCHANGED))  # it removed its own
    finally:
        ended(relay)
        answering.end()
        podman.exec(workspace, [BUSYBOX, "rm", "-f", pidfile(EXCHANGED)])

    # With a listener that keeps its side open, as a server holding a connection does: what is
    # left once the client is killed, and once it only closes its input. Both are read before
    # either is held to being gone, so that a failure tells both.
    found = {
        "client killed": held_relay_left(podman, workspace, HELD_KILLED, killed),
        "input closed": held_relay_left(podman, workspace, HELD_CLOSED, input_closed),
    }
    assert all(
        f["listener got eof"] and not f["relay left"] and not f["exec left"]
        and not f["pidfile left"] and f["listener still runs"]
        for f in found.values()
    ), found  # fmt: skip
    assert processes(podman, workspace).get(bystander) == BYSTANDER  # kill 0 took only its own


def test_the_relay_does_not_carry_a_half_close(podman, workspace, bystander):
    """A documented limitation, pinned: a client that ends its input before the answer comes, as
    a half-close does, ends the relay, and the answer the listener sends after its end of file
    is not carried whole, while the relay leaves nothing, its pidfile included, and a bystander
    of another exec still runs. Its only client, rcman's forwarding server, keeps the input
    open until the answer has been passed back."""
    late = Listener(podman, workspace, HALF_CLOSED, "after-eof", PAYLOAD)
    relay = relay_started(podman, workspace, pidfile(HALF_CLOSED), HALF_CLOSED)
    try:
        late.said("accepted")
        assert relay_parts(podman, workspace)  # the control: it runs
        until(lambda: pidfile_there(podman, workspace, HALF_CLOSED))  # and its pidfile is there
        sent = os.urandom(PAYLOAD)
        back, _ = relay.communicate(sent, timeout=WITHIN)  # sends, then ends its input at once
        length, _ = late.said("eof")
        assert int(length) >= 0  # the end of file came, after however much the relay passed
        assert late.said("answer")[0] == str(PAYLOAD)  # and the listener answered in full
        assert len(back) < PAYLOAD, len(back)  # cut short
        until(lambda: relay_parts(podman, workspace) == {})
        until(lambda: not pidfile_there(podman, workspace, HALF_CLOSED))
    finally:
        ended(relay)
        late.end()
        podman.exec(workspace, [BUSYBOX, "rm", "-f", pidfile(HALF_CLOSED)])
    assert processes(podman, workspace).get(bystander) == BYSTANDER


def app_closed_left(podman, name: str, port: int, script: str | None = None) -> dict:
    """A relay to a listener that answers in full and then closes its side, while the client
    keeps its input open, as an app that closes a connection (HTTP/1.0, ``Connection: close``,
    a WebSocket's close, a crash) does: whether the client got the whole answer, then its end of
    file within GONE_WITHIN, and the relay's processes, exec and pidfile still there once a
    short while has passed, each seen while it ran first. With ``script``, an exec of the same shape
    running that script instead (the planted positive). What is left is ended afterwards."""
    closing = Listener(podman, name, port, "close", PAYLOAD)
    before = running_execs(podman, name)
    relay = relay_started(podman, name, pidfile(port), port, script)
    try:
        # The positive, before anything is sent: the relay waits for its input, so its exec
        # runs until it has been read.
        [relay_exec] = until(lambda: running_execs(podman, name) - before)
        until(lambda: pidfile_there(podman, name, port))  # and its pidfile is written
        sent = os.urandom(PAYLOAD)
        writer = threading.Thread(target=written, args=(relay.stdin, sent), daemon=True)
        writer.start()
        back = read_exactly(relay.stdout, PAYLOAD)
        writer.join(timeout=WITHIN)
        answered = closing.said("answer")
        closing.said("closed")
        end = next_read(relay.stdout, GONE_WITHIN)  # its input still open
        return {
            "whole answer": answered == [str(len(back)), hashlib.sha256(back).hexdigest()]
            and len(back) == PAYLOAD,
            "eof": end == b"",
            "hung": end is None,
            "relay left": settled(lambda: relay_parts(podman, name)),
            "exec left": settled(lambda: relay_exec in running_execs(podman, name)),
            "pidfile left": settled(lambda: pidfile_there(podman, name, port)),
        }
    finally:
        for pid in relay_parts(podman, name):
            podman.exec(name, [BUSYBOX, "kill", "-KILL", str(pid)])
        ended(relay)
        closing.end()
        until(lambda: relay_parts(podman, name) == {})
        podman.exec(name, [BUSYBOX, "rm", "-f", pidfile(port)])


def test_the_relay_ends_when_the_app_closes_first_while_its_client_keeps_its_input(
    podman, workspace, bystander
):
    """Gate: once the listener has answered and closed its side, the client, keeping its input
    open, reads the whole answer and then its end of file, and no part of the relay, no exec of
    it and no pidfile of it is left, while a bystander of another exec still runs. The planted
    positive: the relay that ends only with its input, run the same way, does not end - its
    client sees no end of file, and its pidfile stays - so the check can fail."""
    found = app_closed_left(podman, workspace, APP_CLOSED)
    assert found["whole answer"] and found["eof"], found
    assert not found["relay left"] and not found["exec left"], found
    assert not found["pidfile left"], found
    planted = app_closed_left(podman, workspace, APP_CLOSED_ONE_SIDED, ONE_SIDED)
    assert planted["whole answer"] and planted["hung"], planted  # the twin hangs
    assert planted["relay left"], planted  # its cat waits on the input still open
    assert planted["pidfile left"], planted  # and the same check sees its pidfile
    assert processes(podman, workspace).get(bystander) == BYSTANDER


def test_the_pidfile_wrapper_lets_sweep_end_a_leftover_relay(podman, workspace, bystander):
    """A relay left running - its client keeping its input open, its listener its side - is
    ended by forwards.py's sweep through the workspace's channel, as rcman sweeps it: every part
    of it and its exec gone, its pidfile removed; while a decoy pidfile, named as a relay's and
    naming the bystander, signals nothing and is removed, and the listener still runs."""
    holding = Listener(podman, workspace, LEFT_OVER, "hold")
    before = running_execs(podman, workspace)
    relay = relay_started(podman, workspace, pidfile(LEFT_OVER), LEFT_OVER)
    decoy = f"{rt.RUN_MOUNT}/forward-{'d' * 16}.pid"
    channel = podman.channel(workspace)
    try:
        holding.said("accepted")
        shell = shell_of(podman, workspace, LEFT_OVER)
        parts = relay_parts(podman, workspace)
        assert sorted(parts.values()) == expected_parts(pidfile(LEFT_OVER), LEFT_OVER), parts
        assert shell in parts  # the positive: the pidfile names the running shell
        [relay_exec] = running_execs(podman, workspace) - before
        planted = podman.exec(workspace, [BUSYBOX, "sh", "-c", f'echo {bystander} >"$1"', "sh",
                                          decoy])  # fmt: skip
        assert planted.code == 0, planted
        assert next_read(relay.stdout, 2) is None  # held open by both sides, it stays

        forwards.sweep(rt.PODMAN_LAYOUT, channel.run)
        assert relay.wait(timeout=WITHIN) is not None  # its exec ended with its shell
        until(lambda: relay_parts(podman, workspace) == {})
        until(lambda: relay_exec not in running_execs(podman, workspace))
        for path in (pidfile(LEFT_OVER), decoy):
            assert podman.exec(workspace, [BUSYBOX, "test", "-e", path]).code != 0, path
        assert processes(podman, workspace).get(bystander) == BYSTANDER
        assert holding.pid in processes(podman, workspace)
    finally:
        channel.close()
        for pid in relay_parts(podman, workspace):
            podman.exec(workspace, [BUSYBOX, "kill", "-KILL", str(pid)])
        podman.exec(workspace, [BUSYBOX, "rm", "-f", decoy, pidfile(LEFT_OVER)])
        ended(relay)
        holding.end()
