"""What relays to forwarded ports leave behind (backend/rcman/forwards.py), on the host runtime:
a real relay through ``HostRuntime.port_argv`` to a listener that holds every connection open,
its client holding its input open, so that only a sweep ends it; each place a workspace sweeps
(a deletion, a rebuild, a restore that finds it running) ends it.

The host runtime's relays are the host's processes, so these tests look for them in the host's
``/proc``.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rcman import forwards
from rcman import runtime as rt
from rcman.main import create_app

from tests.conftest import ALICE, made_workspace
from tests.hang_guard import HANG_GUARD
from tests.test_terminals import alive
from tests.until import until

# Run by python3 on the host: listens on 127.0.0.1 on a port the kernel gives, which it prints,
# and holds every connection it takes open, reading and dropping what comes, as an app that
# keeps its connections does.
HOLDER = r"""
import socket, sys, threading
made = socket.socket()
made.bind(("127.0.0.1", 0))
made.listen(8)
print(made.getsockname()[1], flush=True)

def hold(peer):
    while peer.recv(65536):
        pass

while True:
    peer, _ = made.accept()
    threading.Thread(target=hold, args=(peer,), daemon=True).start()
"""


@pytest.fixture
def holder():
    """The listener (HOLDER), by the port it listens on; ended afterwards."""
    proc = subprocess.Popen(
        [sys.executable, "-I", "-c", HOLDER], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE
    )
    try:
        yield int(proc.stdout.readline())
    finally:
        proc.kill()
        proc.wait()
        proc.stdout.close()


def children_of(pid: int) -> list[int]:
    """The host's processes descended from ``pid``."""
    table = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            with contextlib.suppress(OSError, ValueError, IndexError):
                stat = (entry / "stat").read_text()
                table[int(entry.name)] = int(stat.rpartition(")")[2].split()[1])
    return forwards.descendants(table, pid)


class Relay:
    """A relay to ``port`` in the workspace, as the forwarding server starts one, whose client
    holds its input open: its process, its pidfile, and its parts once it runs."""

    def __init__(self, service, workspace: dict, port: int):
        found = service.get(workspace["id"])
        layout = service.layout_of(found)
        self.pidfile = rt.forward_pidfile(layout)
        launch = service.runtime.port_argv(found.container, port, self.pidfile)
        self.proc = subprocess.Popen(
            launch.argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=launch.env
        )
        until(lambda: Path(self.pidfile).exists() and Path(self.pidfile).read_text())
        self.shell = int(Path(self.pidfile).read_text())
        # Its shell, the two subshells of its pipeline, cat and nc.
        self.parts = until(lambda: len(children_of(self.shell)) >= 4 and children_of(
            self.shell))  # fmt: skip
        assert self.shell == self.proc.pid  # setsid ran it in place
        assert alive(self.shell) and all(alive(p) for p in self.parts)  # the positive

    def gone(self) -> bool:
        return self.proc.poll() is not None and not any(alive(p) for p in self.parts)

    def end(self) -> None:
        for pid in [self.shell, *self.parts]:
            with contextlib.suppress(OSError):
                os.kill(pid, 9)
        self.proc.wait()
        self.proc.stdin.close()
        self.proc.stdout.close()


@pytest.fixture
def relays():
    """``relays(service, workspace, port)``: a Relay, ended afterwards whatever the test did."""
    made: list[Relay] = []

    def make(service, workspace: dict, port: int) -> Relay:
        made.append(relay := Relay(service, workspace, port))
        return relay

    yield make
    for relay in made:
        relay.end()


def left(service, workspace: dict, holder: int, relays) -> Relay:
    """A relay left running in the workspace."""
    relay = relays(service, workspace, holder)
    assert relay.proc.poll() is None  # held open by both sides, it does not end by itself
    return relay


def assert_swept(relay: Relay) -> None:
    until(relay.gone)
    assert not Path(relay.pidfile).exists()


def test_a_deletion_ends_the_relays_left_in_the_workspace(client, holder, relays):
    workspace = made_workspace(client, ALICE, "alpha")
    service = client.app.state.workspaces
    relay = left(service, workspace, holder, relays)
    pidfile_folder = Path(relay.pidfile).parent
    service.delete(service.get(workspace["id"]))
    until(relay.gone)  # the host runtime's removal ends no process: the sweep did
    assert not pidfile_folder.exists()


def test_a_rebuild_ends_the_relays_left_in_the_old_container(client, holder, relays):
    workspace = made_workspace(client, ALICE, "alpha")
    service = client.app.state.workspaces
    relay = left(service, workspace, holder, relays)
    service._switch(service.get(workspace["id"]), threading.Event())  # where the switch starts
    assert_swept(relay)


def test_restoring_a_workspace_that_kept_running_ends_the_relays_left_in_it(
    settings, runtime, holder, relays
):
    with TestClient(create_app(settings, runtime=runtime)) as first:
        assert first.app.state.workspaces.restored.wait(HANG_GUARD)
        workspace = made_workspace(first, ALICE, "alpha")
        relay = left(first.app.state.workspaces, workspace, holder, relays)
    assert relay.proc.poll() is None  # the control: rcman's end did not end it
    with TestClient(create_app(settings, runtime=runtime)) as second:
        assert second.app.state.workspaces.restored.wait(HANG_GUARD)
        assert_swept(relay)
