"""What relays to forwarded ports leave behind (backend/rcman/forwards.py), on the host runtime:
a real relay through ``HostRuntime.port_argv`` to a listener that holds every connection open,
its client holding its input open, so that only a sweep ends it; each place a workspace sweeps
(a deletion, a rebuild, a restore that finds it running) ends it, and a
decoy pidfile naming a bystander signals nothing; and what ``sweep`` refuses to act on.

The host runtime's relays are the host's processes, so these tests look for them in the host's
``/proc``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import os
import secrets
import socket
import stat
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
from tests.test_terminals import a_length, alive, wait_until

# How long a relay is given to end once it is told to.
ANSWERS = 15.0
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
        wait_until(lambda: Path(self.pidfile).exists() and Path(self.pidfile).read_text())
        self.shell = int(Path(self.pidfile).read_text())
        # Its shell, the two subshells of its pipeline, cat and nc.
        self.parts = wait_until(lambda: len(children_of(self.shell)) >= 4 and children_of(
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


@pytest.fixture
def bystander():
    """A process of the host no relay is: ``sleep`` of a length of its own."""
    proc = subprocess.Popen(["sleep", a_length()], stdin=subprocess.DEVNULL)
    wait_until(lambda: alive(proc.pid))
    yield proc
    proc.kill()
    proc.wait()


def decoy(service, workspace: dict, pid: int) -> Path:
    """A relay's pidfile, by its name, that names ``pid``, which is no relay."""
    layout = service.layout_of(service.get(workspace["id"]))
    path = Path(rt.forward_pidfile(layout))
    path.write_text(f"{pid}\n")
    return path


def left(service, workspace: dict, holder: int, relays, bystander):
    """A relay left running in the workspace and a decoy pidfile naming the bystander."""
    relay = relays(service, workspace, holder)
    planted = decoy(service, workspace, bystander.pid)
    assert relay.proc.poll() is None  # held open by both sides, it does not end by itself
    return relay, planted


def assert_swept(relay: Relay, planted: Path, bystander) -> None:
    wait_until(relay.gone)
    assert not Path(relay.pidfile).exists()
    assert alive(bystander.pid) and not planted.exists()


def test_a_relay_held_open_by_both_sides_is_ended_by_a_sweep_and_a_decoy_is_not(
    client, holder, relays, bystander
):
    workspace = made_workspace(client, ALICE, "alpha")
    service = client.app.state.workspaces
    relay, planted = left(service, workspace, holder, relays, bystander)
    found = service.get(workspace["id"])
    forwards.sweep(service.layout_of(found), service.run_in(found))
    assert_swept(relay, planted, bystander)


def test_a_deletion_ends_the_relays_left_in_the_workspace(client, holder, relays, bystander):
    workspace = made_workspace(client, ALICE, "alpha")
    service = client.app.state.workspaces
    relay, planted = left(service, workspace, holder, relays, bystander)
    pidfile_folder = Path(relay.pidfile).parent
    service.delete(service.get(workspace["id"]))
    wait_until(relay.gone)  # the host runtime's removal ends no process: the sweep did
    assert alive(bystander.pid) and not pidfile_folder.exists()


def test_a_rebuild_ends_the_relays_left_in_the_old_container(client, holder, relays, bystander):
    workspace = made_workspace(client, ALICE, "alpha")
    service = client.app.state.workspaces
    relay, planted = left(service, workspace, holder, relays, bystander)
    service._switch(service.get(workspace["id"]), threading.Event())  # where the switch starts
    assert_swept(relay, planted, bystander)


def test_restoring_a_workspace_that_kept_running_ends_the_relays_left_in_it(
    settings, runtime, holder, relays, bystander
):
    with TestClient(create_app(settings, runtime=runtime)) as first:
        assert first.app.state.workspaces.restored.wait(30)
        workspace = made_workspace(first, ALICE, "alpha")
        relay, planted = left(first.app.state.workspaces, workspace, holder, relays, bystander)
    assert relay.proc.poll() is None  # the control: rcman's end did not end it
    with TestClient(create_app(settings, runtime=runtime)) as second:
        assert second.app.state.workspaces.restored.wait(30)
        assert_swept(relay, planted, bystander)


def test_a_failing_sweep_is_logged_and_stops_nothing(client, monkeypatch, caplog):
    workspace = made_workspace(client, ALICE, "alpha")
    service = client.app.state.workspaces

    def broken(*args, **kwargs):
        raise RuntimeError("the sweep broke")

    monkeypatch.setattr(forwards, "sweep", broken)
    service.delete(service.get(workspace["id"]))
    assert service.get(workspace["id"]) is None
    assert "were not swept" in caplog.text and "the sweep broke" in caplog.text


# --- what a sweep refuses to act on ----------------------------------------------------------


class Recorded:
    """A ``run`` that runs on the host and records each argv."""

    def __init__(self):
        self.asked: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float = 60.0) -> rt.Result:
        self.asked.append(argv)
        proc = subprocess.run(
            argv, capture_output=True, env={"PATH": "/usr/bin:/bin"}, timeout=ANSWERS
        )
        return rt.Result(proc.returncode, proc.stdout, proc.stderr)

    def _ends(self) -> list[list[str]]:
        """The arguments of each run of the script that ends and removes."""
        return [argv[4:] for argv in self.asked if argv[3:4] == ["end"]]

    def signalled(self) -> list[int]:
        """The processes the sweep was told to end."""
        found = []
        for args in self._ends():
            pairs = args[: args.index("--")]
            found += [int(pid) for pid in pairs[0::2]]
        return found

    def removed(self) -> list[str]:
        """The pidfiles the sweep was told to remove once it had read them."""
        return [path for args in self._ends() for path in args[args.index("--") + 1 :]]


@pytest.fixture
def layout(tmp_path):
    folder = tmp_path / "run"
    folder.mkdir()
    host = rt.HostRuntime(tmp_path, tmp_path, tmp_path, tmp_path)
    return dataclasses.replace(host.layout("x"), run=str(folder))


@pytest.fixture
def relay(layout, holder):
    """A relay on the host, as ``HostRuntime.port_argv`` starts one, to the holder, with its
    client's input held open; its shell's id, once its pidfile names it."""
    pidfile = rt.forward_pidfile(layout)
    proc = subprocess.Popen(
        rt.relay_command(layout, pidfile, holder), stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin"},
    )  # fmt: skip
    try:
        wait_until(lambda: Path(pidfile).exists() and Path(pidfile).read_text())
        proc.pidfile = pidfile
        yield proc
    finally:
        for pid in [proc.pid, *children_of(proc.pid)]:
            with contextlib.suppress(OSError):
                os.kill(pid, 9)
        proc.wait()
        proc.stdin.close()


def test_the_harness_sees_a_relay_it_ends(layout, relay):
    """The positive the refusals below are read against: in the same harness, a relay's
    pidfile has its shell and the processes under it ended, and is removed."""
    parts = wait_until(lambda: len(children_of(relay.pid)) >= 4 and children_of(relay.pid))
    run = Recorded()
    forwards.sweep(layout, run)
    assert sorted(run.signalled()) == sorted([relay.pid, *parts])
    assert run.removed() == [relay.pidfile] and not Path(relay.pidfile).exists()
    assert relay.wait(timeout=ANSWERS) == -9
    wait_until(lambda: not any(alive(p) for p in parts))


def test_a_relay_removes_its_own_pidfile_whichever_side_ends_it(layout, holder):
    """Its input ending, and the app closing its side, each end the relay and remove its
    pidfile; the sweep is left only what a relay that is killed leaves. Each absence is read
    after the pidfile was seen while the relay ran."""
    with socket.socket() as closing:
        closing.bind(("127.0.0.1", 0))
        closing.listen(1)
        for side in ("its input", "the app"):
            go = threading.Event()

            def close_one(go=go):
                peer, _ = closing.accept()
                go.wait(ANSWERS)
                peer.close()

            port = holder
            if side == "the app":
                port = closing.getsockname()[1]
                threading.Thread(target=close_one, daemon=True).start()
            pidfile = rt.forward_pidfile(layout)
            proc = subprocess.Popen(
                rt.relay_command(layout, pidfile, port), stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, env={"PATH": "/usr/bin:/bin"},
            )  # fmt: skip
            try:
                # The positive: it is there while the relay runs.
                held = Path(pidfile)
                wait_until(lambda held=held: held.exists() and held.read_text())
                if side == "its input":
                    proc.stdin.close()
                else:
                    go.set()
                proc.wait(timeout=ANSWERS)
                wait_until(lambda held=held: not held.exists(), ANSWERS)
            finally:
                go.set()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                with contextlib.suppress(OSError):
                    proc.stdin.close()
                Path(pidfile).unlink(missing_ok=True)


def test_a_pidfile_is_read_only_as_echo_writes_one(layout, relay):
    """What names the relay's shell, but not as ``echo $$`` writes it, an id and a newline, names
    nothing; the control, written so, ends it."""
    pid = relay.pid
    for case, text in {"no newline": f"{pid}", "two newlines": f"{pid}\n\n",
                       "a space": f"{pid} \n"}.items():  # fmt: skip
        Path(relay.pidfile).write_text(text)
        run = Recorded()
        forwards.sweep(layout, run)
        assert run.signalled() == [] and relay.poll() is None, case
        assert not Path(relay.pidfile).exists(), case
    Path(relay.pidfile).write_text(f"{pid}\n")
    run = Recorded()
    forwards.sweep(layout, run)
    assert pid in run.signalled() and relay.wait(timeout=ANSWERS) == -9


def test_a_process_that_ends_once_its_argv_is_read_does_not_stop_the_sweep(layout, relay, tmp_path):
    """The argv is read once: a process that ends just after its argv was read, as one may at
    any moment, cannot make what is printed of it disagree with its length, and the real relay
    in the same sweep is ended."""
    gone = subprocess.Popen(["sleep", a_length()], stdin=subprocess.DEVNULL)
    wait_until(lambda: alive(gone.pid))
    head = tmp_path / "head"  # the layout's head, which ends ``gone`` once its argv is read
    head.write_text(
        "#!/bin/sh\n"
        f'/usr/bin/head "$@"; code=$?\n'
        f'case "$3" in /proc/{gone.pid}/cmdline) kill -9 {gone.pid} ;; esac\n'
        "exit $code\n"
    )
    head.chmod(0o755)
    tools = {**layout.tools, "head": [str(head)]}
    ending = dataclasses.replace(layout, tools=tools)
    planted = Path(rt.forward_pidfile(layout))
    planted.write_text(f"{gone.pid}\n")
    try:
        run = Recorded()
        forwards.sweep(ending, run)
        assert gone.wait(timeout=ANSWERS) == -9  # the plant took: it ended once read
        assert relay.pid in run.signalled() and relay.wait(timeout=ANSWERS) == -9
        assert not planted.exists() and not Path(relay.pidfile).exists()
    finally:
        if gone.poll() is None:
            gone.kill()
        gone.wait()


def planting_shell(tmp_path, layout, plant: str):
    """The layout with a shell that runs ``plant`` before the sweep's first script, with $4 the
    runtime folder and $$ the very shell that then runs the script, so that what it plants is
    at the name a file of that shell's own would have."""
    shell = tmp_path / "sh"
    shell.write_text(
        f'#!/bin/sh\nif [ "$3" = sweep ]; then {plant}; fi\nexec {layout.shell} "$@"\n'
    )
    shell.chmod(0o755)
    return dataclasses.replace(layout, shell=str(shell))


def test_what_the_workspace_plants_where_a_file_of_the_sweep_could_be_changes_nothing(
    layout, relay, bystander, tmp_path
):
    """No file of its own: a regular file at the name the sweep's would have, holding the argv
    of a relay for a pidfile that names the bystander, does not make the bystander a relay; a
    fifo there does not stall the sweep; and the real relay is ended both times."""
    pidfile = rt.forward_pidfile(layout)
    fake = b"\0".join(a.encode() for a in rt.relay_shell(layout, pidfile, 80)) + b"\0"
    argv = tmp_path / "argv"
    argv.write_bytes(fake)
    assert forwards.is_relay(layout, fake, pidfile)  # the plant would pass for the relay's argv
    regular = planting_shell(tmp_path, layout, f'cp {argv} "$4/sweep-$$.tmp"')
    Path(pidfile).write_text(f"{bystander.pid}\n")
    run = Recorded()
    forwards.sweep(regular, run)
    assert alive(bystander.pid) and bystander.pid not in run.signalled()
    assert relay.pid in run.signalled() and relay.wait(timeout=ANSWERS) == -9
    assert list(Path(layout.run).glob("sweep-*.tmp"))  # the plant took


def test_a_fifo_where_a_file_of_the_sweep_could_be_does_not_stall_it(layout, relay, tmp_path):
    fifo = planting_shell(tmp_path, layout, 'mkfifo "$4/sweep-$$.tmp"')
    run = Recorded()
    forwards.sweep(fifo, run)  # Recorded's runs end within their timeout, or the test fails
    assert relay.pid in run.signalled() and relay.wait(timeout=ANSWERS) == -9
    planted = list(Path(layout.run).glob("sweep-*.tmp"))
    assert planted and stat.S_ISFIFO(planted[0].lstat().st_mode)  # the plant took


def test_empty_pidfiles_do_not_count_toward_the_bound(layout, relay):
    empties = [Path(rt.forward_pidfile(layout)) for _ in range(forwards.MAX_PIDFILES + 5)]
    for path in empties:
        path.write_text("")
    run = Recorded()
    forwards.sweep(layout, run)
    assert relay.pid in run.signalled() and relay.wait(timeout=ANSWERS) == -9
    assert all(p.exists() for p in empties)


def test_a_pidfile_naming_what_is_not_its_relay_signals_nothing_and_is_removed(layout, bystander):
    pid = bystander.pid
    other = rt.forward_pidfile(layout)
    cases = {
        "a bystander": f"{pid}\n",
        "junk": "a relay\n",
        "two numbers": f"{pid} 1\n",
        "a sign": f"+{pid}\n",
        "no pid": "0\n",
        "a leading zero": f"0{pid}\n",
        "beyond any pid": f"{forwards.MAX_PID + 1}\n",
        "oversized": f"{pid}\n" + " " * forwards.MAX_PIDFILE_BYTES,
        "a large file of digits": "1" * (1 << 20),
        "no newline": f"{pid}",
    }
    for case, text in cases.items():
        run = Recorded()
        path = rt.forward_pidfile(layout)
        Path(path).write_text(text)
        forwards.sweep(layout, run)
        assert alive(pid) and run.signalled() == [], case
        assert not Path(path).exists(), case
    # A relay's argv, but for another pidfile, and its port, under a shell that only waits.
    waiting = subprocess.Popen(
        ["sh", "-c", "sleep 3600; :", *rt.relay_shell(layout, other, 80)],
        stdin=subprocess.DEVNULL,
    )
    try:
        wait_until(lambda: alive(waiting.pid))
        run = Recorded()
        path = rt.forward_pidfile(layout)
        Path(path).write_text(f"{waiting.pid}\n")
        forwards.sweep(layout, run)
        assert alive(waiting.pid) and run.signalled() == []
    finally:
        for child in children_of(waiting.pid):
            with contextlib.suppress(OSError):
                os.kill(child, 9)
        waiting.kill()
        waiting.wait()


def test_an_empty_pidfile_is_left_for_the_relay_that_is_writing_it(layout):
    path = Path(rt.forward_pidfile(layout))
    path.write_text("")
    run = Recorded()
    forwards.sweep(layout, run)
    assert path.exists() and run.signalled() == [] and run.removed() == []
    path.write_text("1\n")  # the control: once written, it is read and removed
    forwards.sweep(layout, run)
    assert not path.exists()


def test_a_linked_pidfile_is_removed_unread_and_its_target_is_left(layout, tmp_path, bystander):
    target = tmp_path / "target"
    target.write_text(f"{bystander.pid}\n")
    link = Path(rt.forward_pidfile(layout))
    link.symlink_to(target)
    run = Recorded()
    forwards.sweep(layout, run)
    assert not link.is_symlink() and target.read_text() == f"{bystander.pid}\n"
    assert alive(bystander.pid) and run.signalled() == [] and run.removed() == []


def test_pidfiles_beyond_the_bound_are_removed_unread_and_nothing_is_signalled(layout, bystander):
    paths = [Path(rt.forward_pidfile(layout)) for _ in range(forwards.MAX_PIDFILES + 10)]
    for path in paths:
        path.write_text(f"{bystander.pid}\n")
    run = Recorded()
    forwards.sweep(layout, run)
    assert not any(p.exists() for p in paths)
    assert len(run.removed()) == forwards.MAX_PIDFILES  # the rest went unread
    assert len(run.asked) == 2  # one script to read, one to end and remove
    assert alive(bystander.pid) and run.signalled() == []


def test_a_process_that_is_no_longer_the_one_read_is_not_signalled(layout, bystander):
    """The script that ends checks each process's start time again: a pid given with another
    start time, as a pid the kernel has given again would have, is not signalled."""
    run = Recorded()
    run([layout.shell, "-c", forwards._script(forwards._END, layout), "end",
         str(bystander.pid), "1", "--"])  # fmt: skip
    assert alive(bystander.pid)
    stat = Path(f"/proc/{bystander.pid}/stat").read_text().rpartition(")")[2].split()
    run([layout.shell, "-c", forwards._script(forwards._END, layout), "end",
         str(bystander.pid), stat[19], "--"])  # fmt: skip
    assert bystander.wait(timeout=ANSWERS) == -9  # the control: with its own start time it is


def test_the_argv_of_the_relay_that_writes_the_pidfile_is_its_relay_and_nothing_else(layout):
    pidfile = rt.forward_pidfile(layout)
    argv = rt.relay_shell(layout, pidfile, 6080)
    cmdline = b"\0".join(a.encode() for a in argv) + b"\0"
    assert forwards.is_relay(layout, cmdline, pidfile)  # the positive
    other = rt.forward_pidfile(layout)
    under_setsid = rt.relay_command(layout, pidfile, 6080)
    for case, changed in {
        "another pidfile": b"\0".join(a.encode() for a in rt.relay_shell(layout, other, 6080))
        + b"\0",
        "setsid's own argv": b"\0".join(a.encode() for a in under_setsid) + b"\0",
        "no end": cmdline[:-1],
        "another script": cmdline.replace(b"kill 0", b"kill 1"),
        "another busybox": cmdline.replace(layout.busybox.encode(), b"/tmp/busybox"),
        "a port of leading zero": cmdline.replace(b"\x006080\x00", b"\x0006080\x00"),
        "an argument more": cmdline + b"x\0",
        "empty": b"",
    }.items():
        assert not forwards.is_relay(layout, changed, pidfile), case


def test_a_sweep_acts_only_on_relay_pidfiles_by_name(layout):
    run = Recorded()
    swept = [rt.forward_pidfile(layout), rt.forward_pidfile(layout)]
    others = [
        f"{layout.run}/forward-{secrets.token_hex(7)}.pid",
        f"{layout.run}/x.pid",
        f"{layout.run}/{secrets.token_hex(8)}.pid",
        f"{layout.run}/forward-XY.pid",
    ]
    for path in [*swept, *others]:
        Path(path).write_text("1\n")  # init: no relay, so nothing is signalled
    forwards.sweep(layout, run)
    assert not any(Path(p).exists() for p in swept)
    assert all(Path(p).exists() for p in others)
    assert run.signalled() == [] and sorted(run.removed()) == sorted(swept)


def test_what_the_first_script_printed_cut_short_acts_on_nothing(layout, caplog):
    def cut(argv, timeout=60.0):
        proc = subprocess.run(argv, capture_output=True, env={"PATH": "/usr/bin:/bin"})
        return rt.Result(proc.returncode, proc.stdout[:-3], proc.stderr)

    path = Path(rt.forward_pidfile(layout))
    path.write_text("1\n")
    forwards.sweep(layout, cut)
    assert path.exists() and "were not read" in caplog.text


def test_a_port_and_a_pidfile_that_are_not_a_relays_are_refused(layout):
    good = rt.forward_pidfile(layout)
    assert rt.relay_command(layout, good, 1)[-2:] == [good, "1"]  # the positive
    for port in (0, 65536, -1, True, "80", 80.0, None):
        with pytest.raises(ValueError):
            rt.relay_command(layout, good, port)
    for pidfile in (
        f"{layout.run}/forward-{secrets.token_hex(7)}.pid",
        f"{layout.run}/0123456789abcdef.pid",
        f"{layout.run}/sub/forward-{secrets.token_hex(8)}.pid",
        f"{Path(layout.run).parent}/forward-{secrets.token_hex(8)}.pid",
        f"{layout.run}/forward-{secrets.token_hex(8)}.pid\n",
        f"{layout.run}/../run/forward-{secrets.token_hex(8)}.pid",
    ):
        with pytest.raises(ValueError):
            rt.relay_command(layout, pidfile, 80)
    for busybox in ("", "busybox", "/opt/my busybox", "/opt/$(x)/busybox", "/opt/a;b"):
        with pytest.raises(rt.RuntimeFailed):
            rt.relay_command(dataclasses.replace(layout, busybox=busybox), good, 80)


def test_the_relays_argv_is_the_same_on_both_runtimes_but_for_its_busybox(layout):
    pidfile = f"{rt.RUN_MOUNT}/forward-{'0' * 16}.pid"
    busybox = f"{rt.TOOLS_MOUNT}/bin/busybox"
    podman = rt.relay_command(rt.PODMAN_LAYOUT, pidfile, 6080)
    assert podman == [
        busybox, "setsid", busybox, "sh", "-c",
        f'echo $$ >"$1"; {{ {busybox} cat; {busybox} rm -f "$1"; kill 0; }} | '
        f'{{ {busybox} nc 127.0.0.1 "$2"; {busybox} rm -f "$1"; kill 0; }}',
        "sh", pidfile, "6080",
    ]  # fmt: skip
    assert rt.relay_shell(rt.PODMAN_LAYOUT, pidfile, 6080) == podman[2:]
    host = rt.relay_command(layout, rt.forward_pidfile(layout), 6080)
    assert host[5] == podman[5].replace(busybox, layout.busybox)
