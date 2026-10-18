"""A network of its own on the tests' engine (tests/podman_service.py): a holder container with no
network but its own loopback, which workspaces join with the network ``container:<holder>``, and
listening sockets made inside it for a server the test runs itself.

Not a test module. Podman refuses a container outside a pod the network of a pod's container, and
gives a container that joins another's network a hosts file of its own, so the network is a plain
container's, and a name a workspace reaches in it is resolved by the workspace's image (as
tests/container/github_stand_in.py ``dockerfile`` does for git), not by a hosts file.

A socket is made in the holder's network by a helper that ``podman unshare`` lets enter it, and is
handed back over a unix socket, so the server serving on it is the test's own, in the test's own
process: its state (the requests it saw, a gate a test closes) is the test's to read.
"""

from __future__ import annotations

import contextlib
import json
import socket
import subprocess
import sys
import threading
from collections.abc import Iterator

from devtools import held_folders as hf
from tests import podman_service as ps

# What the holder runs: nothing, for as long as it is needed.
HOLDS = ["sleep", "infinity"]
# Seconds the helper may take to hand a socket over.
HANDED_WITHIN = 60
# Run by the helper in the holder's network: a TCP socket bound to the address and listening,
# handed over the unix socket named.
HELPER = """
import socket, sys
host, port, path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
made = socket.socket()
made.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
made.bind((host, port))
made.listen(64)
told = socket.socket(socket.AF_UNIX)
told.connect(path)
socket.send_fds(told, [b"socket"], [made.fileno()])
"""


@contextlib.contextmanager
def holder(engine, image: str, labels: list[str], name: str, *more: str) -> Iterator[str]:
    """A container of ``image`` called ``name`` with no network but its own loopback, holding
    that network for as long as the context lasts; its name. ``more`` are run's further
    options, before the image."""
    made = engine.cli(
        "run", "-d", "--pull=never", "--network=none", *labels, "--name", name, *more, image,
        *HOLDS, timeout=300
    )  # fmt: skip
    assert made.returncode == 0, made.stderr.decode(errors="replace")
    try:
        yield name
    finally:
        engine.cli("rm", "--force", "--time=0", name, timeout=300)


def pid_of(engine, container: str) -> int:
    found = engine.cli("inspect", "--format", "{{.State.Pid}}", container, timeout=60)
    assert found.returncode == 0, found.stderr
    return int(found.stdout.decode())


def namespace_of(engine, container: str) -> str:
    """The network namespace of ``container``, as ``readlink`` names it (``net:[<inode>]``)."""
    found = engine.local(
        "unshare", "readlink", f"/proc/{pid_of(engine, container)}/ns/net", timeout=60
    )
    assert found.returncode == 0, found.stderr
    return found.stdout.decode().strip()


def listening(engine, container: str, port: int, host: str = "127.0.0.1") -> socket.socket:
    """A TCP socket bound to ``host``:``port`` in ``container``'s network and listening."""
    with hf.held("rcnet-") as folder:
        path = str(folder / "s")
        with socket.socket(socket.AF_UNIX) as told:
            told.bind(path)
            told.listen(1)
            told.settimeout(HANDED_WITHIN)
            helper = subprocess.Popen(
                [*ps.local(engine.folder), "unshare", "nsenter", "-t",
                 str(pid_of(engine, container)), "-n", sys.executable, "-c", HELPER, host,
                 str(port), path],
                env=ps.env(engine.folder), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )  # fmt: skip
            try:
                connection, _ = told.accept()
            except TimeoutError:
                helper.kill()
                raise AssertionError(json.dumps(helper.communicate()[1].decode())) from None
            with connection:
                _, fds, _, _ = socket.recv_fds(connection, 16, 1)
            _, said = helper.communicate(timeout=HANDED_WITHIN)
            assert helper.returncode == 0 and len(fds) == 1, said.decode(errors="replace")
    return socket.socket(fileno=fds[0])


class Relay:
    """An HTTP proxy on the loopback of the network the test runs in that takes only
    ``CONNECT <name>:443`` and joins it to ``target``, a (host, port) on the same loopback: what a
    workspace on the engine's own network reaches a stand-in through, by git's ``http.proxy``
    (tests/container/github_stand_in.py ``dockerfile``), without anything listening on the
    machine's port 443. What it carries is the client's own TLS, end to end."""

    def __init__(self, name: str, target: tuple[str, int]):
        self.name, self.target = name, target
        self.listening = socket.socket()
        self.listening.bind(("127.0.0.1", 0))
        self.listening.listen(64)
        self.port = self.listening.getsockname()[1]
        self.refused: list[bytes] = []  # the request lines it would not carry
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self.listening.accept()
            except OSError:
                return
            threading.Thread(target=self._carry, args=(client,), daemon=True).start()

    def _carry(self, client: socket.socket) -> None:
        with client:
            head = b""
            while b"\r\n\r\n" not in head and len(head) < 8192:
                got = client.recv(4096)
                if not got:
                    return
                head += got
            line = head.split(b"\r\n", 1)[0]
            if line.split()[:2] != [b"CONNECT", f"{self.name}:443".encode()]:
                self.refused.append(line)
                client.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                return
            with socket.create_connection(self.target, timeout=30) as upstream:
                client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
                rest = head.split(b"\r\n\r\n", 1)[1]
                if rest:
                    upstream.sendall(rest)
                upstream.settimeout(None)
                back = threading.Thread(target=_pump, args=(upstream, client), daemon=True)
                back.start()
                _pump(client, upstream)
                back.join()

    def close(self) -> None:
        self.listening.close()


def _pump(source: socket.socket, sink: socket.socket) -> None:
    try:
        while data := source.recv(65536):
            sink.sendall(data)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            sink.shutdown(socket.SHUT_WR)
