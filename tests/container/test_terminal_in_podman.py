"""The Terminal tab (backend/rcman/terminals.py, routers/terminal.py) against real rootless
Podman: the tests' engine (tests/podman_service.py), with the suites' sizes
(tests/container/sizes.py).

The workspaces run the default workspace image, built on the engine from the installer's build
file, and are made through the app, in its test client, as a person makes them. What the host
runtime's tests cannot show is shown here: the bytes reaching the shell through
``podman --remote exec`` on its own tty, a resize reaching it there, ^P^Q passing through, the
shell running in the person's range as its root, and nothing of the terminal left in the
container or in Podman once it is closed.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from rcman import db, people
from rcman.main import create_app

from tests.conftest import ALICE, ORIGIN, changing, made_workspace
from tests.container import sizes
from tests.container import test_runtime_in_podman as base
from tests.container.conftest import engine_runtime, engine_settings
from tests.container.execs import exec_ids
from tests.test_terminal import opened, path, resize
from tests.test_terminals import a_length, alive, sleeping, wait_until


@pytest.fixture(scope="module")
def client(podman_service, default_image, engine_tools, engine_store, tmp_path_factory):
    folder = tmp_path_factory.mktemp("rcman")
    settings = engine_settings(
        folder / "rcman.db", engine_store, public_origin=ORIGIN, tools_dir=engine_tools,
        workspace_image=default_image,
    )  # fmt: skip
    db.migrate(settings.database)
    con = db.connect(settings.database)
    try:
        alice = people.add(con, ALICE, True)
    finally:
        con.close()
    podman = engine_runtime(podman_service, default_image, engine_tools, engine_store, folder)
    app = create_app(settings, runtime=podman, builder_slot=sizes.BUILDER_SLOT)
    with TestClient(app) as made:
        assert made.app.state.workspaces.restored.wait(120)
        made.alice, made.engine = alice, podman_service
        try:
            yield made
        finally:
            for workspace in made.app.state.workspaces.everything():
                made.app.state.workspaces.delete(workspace)
            podman.remove_account(alice.id)


@pytest.fixture(scope="module")
def workspace(client):
    return made_workspace(client, ALICE, "terminal")


def test_what_is_typed_reaches_the_shell_and_comes_back(client, workspace):
    with opened(client, ALICE, workspace["id"]) as peer:
        peer.ask("echo $((6*7))", rb"[\r\n]42\r\n")


def test_a_resize_reaches_the_shell_in_the_container(client, workspace):
    """The podman client passes a resize to the container's tty by a request of its own, apart
    from the stream of what is typed, so a command typed just after a resize can still read the
    old size there. What shows the resize arrived is the SIGWINCH the kernel sends the tty's
    foreground process when the size changes: a waiting process reads the size as it gets it."""
    with opened(client, ALICE, workspace["id"], cols=90, rows=20) as peer:
        peer.ask("stty size", rb"[\r\n]20 90\r\n")
        peer.ask(
            "sh -c 'resized() { echo size-$(stty size)-end; exit 0; }; trap resized WINCH;"
            " echo waiting-$((3+4)); while :; do sleep 0.1; done'",
            rb"waiting-7",
        )  # the trap is set before the resize is sent
        peer.session.send_json(resize(150, 45))
        peer.read(lambda p: re.search(rb"size-45 150-end", bytes(p.output)))


def test_ctrl_p_ctrl_q_reach_the_shell_and_do_not_detach(client, workspace):
    with opened(client, ALICE, workspace["id"]) as peer:
        peer.session.send_bytes(
            b"stty raw -echo; echo raw-$((2+5)); head -c 2 | od -An -tx1; stty sane\r"
        )
        peer.read(lambda p: b"raw-7" in bytes(p.output))  # raw before the two bytes come
        peer.session.send_bytes(b"\x10\x11")
        peer.read(lambda p: re.search(rb"10 11", bytes(p.output)))
        peer.ask("echo still-$((1+1))", rb"still-2")  # podman did not detach


def test_the_shell_is_root_of_the_persons_range_and_not_rcman_on_the_host(client, workspace):
    length = a_length()
    with opened(client, ALICE, workspace["id"]) as peer:
        found = peer.ask(
            f"sleep {length} & echo id-$(id -u)-$(cat /proc/self/uid_map | tr -s ' ' ,)-end",
            rb"id-(\d+)-(\S+)-end",
        )
        assert found[1] == b"0"  # root of the person's user namespace, in the default image
        assert (
            found[2].strip(b",")
            == f"0,{client.alice.slot * sizes.TEST_RANGE},{sizes.TEST_RANGE}".encode()
        )
        [pid] = wait_until(lambda: sleeping(length))
        uid = next(
            int(line.split()[1])
            for line in Path(f"/proc/{pid}/status").read_text().splitlines()
            if line.startswith("Uid:")
        )
        assert uid not in (0, os.getuid())
        peer.ask("apt-get --version | head -n 1 | grep -c ^apt", rb"[\r\n]1\r\n")


def test_closing_leaves_no_process_and_no_exec_of_its_own(client, workspace):
    service = client.app.state.workspaces
    found = service.get(workspace["id"])
    service.run_in(found)(["true"])  # the channel's own exec, open before and after
    before = exec_ids(client.engine, found.container)
    length = a_length()
    with opened(client, ALICE, workspace["id"]) as peer:
        peer.ask(f"nohup sleep {length} >/dev/null 2>&1 & echo held-$((2+2))", rb"held-4")
        [pid] = wait_until(lambda: sleeping(length))
        terminal = client.app.state.terminals.live(workspace["id"])
        rss = next(
            int(line.split()[1]) * 1024
            for line in Path(f"/proc/{terminal.process.pid}/status").read_text().splitlines()
            if line.startswith("VmRSS:")
        )
        assert 0 < rss < base.CLIENT_MAX_RSS_BYTES, rss  # what the podman client holds
        assert len(exec_ids(client.engine, found.container)) > len(
            before
        )  # the control: its exec is there, running
    wait_until(lambda: not alive(pid), timeout=30)
    wait_until(lambda: set(exec_ids(client.engine, found.container)) <= set(before), timeout=30)
    shell = service.layout_of(found).shell
    table = service.run_in(found)([shell, "-c", "cat /proc/[0-9]*/cmdline"])
    assert table.code == 0 and table.out  # the control: the table was read
    assert length.encode() not in table.out  # nothing of its session is left in the container


def test_a_terminal_closed_as_soon_as_it_opens_leaves_no_shell_and_no_exec(client, workspace):
    """Ending the podman client does not end its exec: a terminal is open only once its shell
    has named itself, so one closed at once still ends the shell in the container."""
    service = client.app.state.workspaces
    found = service.get(workspace["id"])
    service.run_in(found)(["true"])  # the channel's own exec, open before and after
    before = set(exec_ids(client.engine, found.container))
    shell = service.layout_of(found).shell
    for _ in range(5):
        with client.websocket_connect(path(workspace["id"]), headers=changing(ALICE)) as session:
            session.send_json(resize())
        # closed at once: the page went as the terminal opened
    wait_until(lambda: client.app.state.terminals.live(workspace["id"]) is None, timeout=60)
    wait_until(lambda: set(exec_ids(client.engine, found.container)) <= before, timeout=30)
    table = service.run_in(found)([shell, "-c", "cat /proc/[0-9]*/cmdline"])
    assert table.code == 0 and table.out  # the control: the table was read
    assert b"rcman-terminal" not in table.out and b"-i\0" not in table.out
