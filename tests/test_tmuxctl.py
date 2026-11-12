"""The tmux driver used to record the real `claude` and to drive the fake."""

import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from devtools import held_folders, tmuxctl
from tests.hang_guard import HANG_GUARD
from tests.until import until

# Waits, in the program a test runs, until it is signalled or killed.
UNTIL_SIGNALLED = "import signal; signal.pause()"


@pytest.fixture
def pane():
    with held_folders.tmux_sockets("rcp-") as sockets, tmuxctl.Pane(socket_dir=sockets) as p:
        yield p


def test_a_program_runs_with_its_arguments_unread_by_a_shell_its_folder_and_only_its_environment(
    pane, tmp_path
):
    (tmp_path / "sub").mkdir()
    code = (
        "import os, sys\n"
        "print(sys.argv[1]); print(os.getcwd())\n"
        "print(sorted(k for k in os.environ if k.startswith('RCT_')))\n"
        "raise SystemExit(3)\n"
    )
    pane.run(
        [sys.executable, "-c", code, "$HOME; echo x"], cwd=tmp_path / "sub", env={"RCT_A": "1"}
    )
    assert pane.wait_exit(timeout=HANG_GUARD) == 3
    assert pane.screen() == ["$HOME; echo x", str(tmp_path / "sub"), "['RCT_A']"]


def test_typed_text_reaches_the_program(pane, tmp_path):
    pane.run([sys.executable, "-c", "print('got', input('> '))"], cwd=tmp_path, env={})
    pane.wait_for(r"^>$", timeout=HANG_GUARD)  # trailing space is trimmed
    pane.type_line("abc#def")
    pane.wait_exit(timeout=HANG_GUARD)
    assert pane.screen() == ["> abc#def", "got abc#def"]


def test_interrupt_delivers_sigint(pane, tmp_path):
    code = (
        "import signal\n"
        "signal.signal(signal.SIGINT, lambda *a: (print('interrupted'), exit(0)))\n"
        "print('ready'); signal.pause()\n"
    )
    pane.run([sys.executable, "-c", code], cwd=tmp_path, env={})
    pane.wait_for(r"^ready$", timeout=HANG_GUARD)
    pane.interrupt()
    assert pane.wait_exit(timeout=HANG_GUARD) == 0
    # The tty echoes ^C, but may send SIGINT first, so the two come in either order.
    screen = pane.screen()
    assert screen[0] == "ready"
    rest = "".join(screen[1:])
    assert "^C" in rest and "interrupted" in rest
    assert rest.replace("^C", "", 1).replace("interrupted", "", 1) == ""


def test_wait_for_times_out_with_the_screen_in_the_message(pane, tmp_path):
    pane.run(
        [sys.executable, "-c", f"print('nothing useful'); {UNTIL_SIGNALLED}"],
        cwd=tmp_path,
        env={},
    )
    pane.wait_for(r"nothing useful", timeout=HANG_GUARD)
    with pytest.raises(TimeoutError, match="nothing useful"):
        pane.wait_for(r"never appears", timeout=0)  # it never does: given up at any speed


def test_closing_kills_the_tmux_server_in_its_held_folder(tmp_path):
    """The server process itself ends, not only its socket file; its folder is held, so a run
    killed by a signal leaves it for the next run to stop."""
    pid = None
    try:
        with held_folders.tmux_sockets("rcp-") as sockets, tmuxctl.Pane(socket_dir=sockets) as p:
            p.run([sys.executable, "-c", UNTIL_SIGNALLED], cwd=tmp_path, env={})
            folder = p.socket.parent
            assert held_folders.made_with("rcp-")(folder.name) and folder.parent == held_folders.TMP
            assert not held_folders.unheld(folder)
            server = ["tmux", "-S", str(p.socket), "display-message", "-p", "#{pid}"]
            pid = int(subprocess.run(server, capture_output=True, text=True, check=True).stdout)
            assert alive(pid)  # positive control
            socket = p.socket
        until(lambda: not alive(pid))
        assert not tmuxctl.server_running(socket)
    finally:
        if pid is not None:
            try:
                comm = (Path("/proc") / str(pid) / "comm").read_text().strip()
            except OSError:
                comm = ""
            if comm.startswith("tmux"):  # the server names itself "tmux: server"
                os.kill(pid, signal.SIGKILL)


def alive(pid: int) -> bool:
    """Whether ``pid`` runs now: there, and not a zombie."""
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().split(")")[-1].split()[0] != "Z"
    except FileNotFoundError, ProcessLookupError:
        return False
