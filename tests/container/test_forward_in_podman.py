"""Reaching a port in a workspace through ``podman --remote exec``, against real rootless Podman:
the tests' engine (tests/podman_service.py), with the suites' sizes (tests/container/sizes.py),
the tools as the installer writes them and the default workspace image
(tests/container/conftest.py).

The relay is the product's own (``PodmanRuntime.port_argv``, runtime.py RELAY_SCRIPT): the
mounted busybox's shell writes its own id to a pidfile, then runs ``cat`` into ``nc``, and once
either ends it removes its pidfile and ends its process group. One payload crosses it both ways;
then its input ends, which reaches the listener as its end of file, and the relay ends. The
workspace joins a network of its own (tests/container/network.py), so nothing it listens on is
the machine's.
"""

from __future__ import annotations

import hashlib
import os
import queue
import subprocess
import threading

import pytest
from rcman import runtime as rt

from tests import podman_service as ps
from tests.container import network, sizes
from tests.container.conftest import engine_of, engine_runtime
from tests.hang_guard import CONTAINER_HANG_GUARD
from tests.until import SLOW, until

# The module's workspace and its person, clear of the other modules' numbers.
WORKSPACE = 740
SLOT = sizes.PEOPLE_SLOTS[2]
BUSYBOX = f"{rt.TOOLS_MOUNT}/bin/busybox"
# The port the workspace listens on in its own network.
PORT = 6080
# Bytes sent each way: many times what a pipe holds, so neither side can have taken it whole.
PAYLOAD = 1024 * 1024
# Run by the workspace's python3: listens on 127.0.0.1:<port>, takes one connection, reads
# <size> bytes and says how many and their sha256 ("got"), answers <size> random bytes and says
# the same of them ("answer"), then reads on to the end and says how much it read in all, and
# its sha256 ("eof").
LISTENER = r"""
import hashlib, os, socket, sys
port, size = int(sys.argv[1]), int(sys.argv[2])
made = socket.socket()
made.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
made.bind(("127.0.0.1", port))
made.listen(1)
print("listening", os.getpid(), flush=True)
peer, _ = made.accept()
digest, length = hashlib.sha256(), 0
while length < size and (chunk := peer.recv(min(65536, size - length))):
    digest.update(chunk)
    length += len(chunk)
print("got", length, digest.hexdigest(), flush=True)
back = os.urandom(size)
print("answer", len(back), hashlib.sha256(back).hexdigest(), flush=True)
peer.sendall(back)
while chunk := peer.recv(65536):
    digest.update(chunk)
    length += len(chunk)
print("eof", length, digest.hexdigest(), flush=True)
peer.close()
"""


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


class Listener:
    """LISTENER running in the workspace, and the lines it says, as they come."""

    def __init__(self, podman, name: str):
        engine = engine_of(podman)
        self.podman, self.name, self.pid = podman, name, None
        self.proc = subprocess.Popen(
            engine.argv("exec", name, "python3", "-c", LISTENER, str(PORT), str(PAYLOAD)),
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

    def said(self, word: str) -> list[str]:
        """What the listener said after ``word``, waiting for it."""
        while True:
            try:
                line = self.lines.get(timeout=CONTAINER_HANG_GUARD)
            except queue.Empty:
                raise AssertionError(f"the listener never said {word!r}") from None
            if line is None:
                said = self.proc.stderr.read().decode(errors="replace")
                raise AssertionError(f"the listener ended before {word!r}: {said}")
            if line[0] == word:
                return line[1:]

    def end(self) -> None:
        if self.pid is not None:
            self.podman.exec(self.name, [BUSYBOX, "kill", "-KILL", str(self.pid)])
        ended(self.proc)


def ended(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait()
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream:
            stream.close()


def pidfile() -> str:
    """The relay's pidfile, named as ``forward_pidfile`` names one, by the port: in the runtime
    folder, the container's tmpfs."""
    return f"{rt.RUN_MOUNT}/forward-{PORT:016x}.pid"


def pidfile_there(podman, name: str) -> bool:
    return podman.exec(name, [BUSYBOX, "test", "-e", pidfile()]).code == 0


def written(stream, data: bytes) -> None:
    stream.write(data)
    stream.flush()


def read_exactly(stream, size: int) -> bytes:
    parts, left = [], size
    while left and (part := stream.read(min(left, 65536))):
        parts.append(part)
        left -= len(part)
    return b"".join(parts)


def test_the_busybox_relay_over_exec_is_byte_exact_both_ways_and_ends_with_its_input(
    podman, workspace
):
    """One payload, binary and more than a pipe holds, crosses the relay both ways intact, its
    pidfile there while it runs; closing its input reaches the listener as its end of file, and
    the relay then ends, with nothing more on its output, and removes its pidfile."""
    listener, relay = None, None
    try:
        listener = Listener(podman, workspace)
        launch = podman.port_argv(workspace, PORT, pidfile())
        relay = subprocess.Popen(
            launch.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=launch.env, cwd=engine_of(podman).folder,
        )  # fmt: skip
        sent = os.urandom(PAYLOAD)
        writer = threading.Thread(target=written, args=(relay.stdin, sent), daemon=True)
        writer.start()
        back = read_exactly(relay.stdout, PAYLOAD)
        writer.join(timeout=CONTAINER_HANG_GUARD)
        assert listener.said("got") == [str(PAYLOAD), hashlib.sha256(sent).hexdigest()]
        assert listener.said("answer") == [str(len(back)), hashlib.sha256(back).hexdigest()]
        assert len(back) == PAYLOAD
        assert pidfile_there(podman, workspace)  # the positive: it is there while the relay runs
        relay.stdin.close()
        assert listener.said("eof") == [str(PAYLOAD), hashlib.sha256(sent).hexdigest()]
        relay.wait(timeout=CONTAINER_HANG_GUARD)  # it ended with its input; its code means nothing
        assert relay.stdout.read() == b""
        until(lambda: not pidfile_there(podman, workspace), SLOW)
    finally:
        if relay is not None:
            ended(relay)
        if listener is not None:
            listener.end()
        podman.exec(workspace, [BUSYBOX, "rm", "-f", pidfile()])
