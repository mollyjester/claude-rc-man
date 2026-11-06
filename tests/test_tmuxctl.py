"""The tmux driver used to record the real `claude` and to drive the fake."""

import sys
from pathlib import Path

import pytest

from devtools import held_folders, tmuxctl
from helpers import screens
from tests.hang_guard import HANG_GUARD
from tests.until import until

# Waits, in the program a test runs, until it is signalled or killed.
UNTIL_SIGNALLED = "import signal; signal.pause()"


@pytest.fixture
def pane():
    with held_folders.tmux_sockets("rcp-") as sockets, tmuxctl.Pane(socket_dir=sockets) as p:
        yield p


def test_the_panes_tmux_server_is_in_a_held_folder_of_its_own(pane, tmp_path):
    """So a run killed by a signal leaves it for the next run to stop."""
    pane.run(["sleep", f"{3 * HANG_GUARD:g}"], cwd=tmp_path, env={})
    folder = pane.socket.parent
    assert held_folders.made_with("rcp-")(folder.name) and folder.parent == held_folders.TMP
    assert not held_folders.unheld(folder)


def test_screen_shows_command_output_and_exit_status(pane, tmp_path):
    pane.run([sys.executable, "-c", "print('hello'); raise SystemExit(3)"], cwd=tmp_path, env={})
    assert pane.wait_exit(timeout=HANG_GUARD) == 3
    assert pane.screen() == ["hello"]


def test_environment_is_exactly_what_was_given(pane, tmp_path):
    code = "import os; print(sorted(k for k in os.environ if k.startswith('RCT_')))"
    pane.run([sys.executable, "-c", code], cwd=tmp_path, env={"RCT_A": "1"})
    pane.wait_exit(timeout=HANG_GUARD)
    assert pane.screen() == ["['RCT_A']"]


def test_working_directory_is_set(pane, tmp_path):
    (tmp_path / "sub").mkdir()
    pane.run([sys.executable, "-c", "import os; print(os.getcwd())"], cwd=tmp_path / "sub", env={})
    pane.wait_exit(timeout=HANG_GUARD)
    assert pane.screen() == [str(tmp_path / "sub")]


def test_arguments_are_not_shell_interpreted(pane, tmp_path):
    pane.run(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", "$HOME; echo x"],
        cwd=tmp_path,
        env={},
    )
    pane.wait_exit(timeout=HANG_GUARD)
    assert pane.screen() == ["$HOME; echo x"]


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


def test_wait_exit_times_out_while_running(pane, tmp_path):
    pane.run([sys.executable, "-c", UNTIL_SIGNALLED], cwd=tmp_path, env={})
    with pytest.raises(TimeoutError):
        pane.wait_exit(timeout=0)  # it never exits: given up at any speed


def test_screen_trims_trailing_blank_lines_and_spaces(pane, tmp_path):
    pane.run(
        [sys.executable, "-c", "print(); print('a   '); print(); print()"], cwd=tmp_path, env={}
    )
    pane.wait_exit(timeout=HANG_GUARD)
    assert pane.screen() == ["", "a"]


def test_closing_kills_the_tmux_server(tmp_path):
    """The server process itself ends, not only its socket file."""
    import os
    import signal
    import subprocess

    pid = None
    try:
        with held_folders.tmux_sockets("rcp-") as sockets, tmuxctl.Pane(socket_dir=sockets) as p:
            p.run([sys.executable, "-c", UNTIL_SIGNALLED], cwd=tmp_path, env={})
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


def test_rerun_replaces_the_program_on_a_cleared_screen(pane, tmp_path):
    pane.run([sys.executable, "-c", "print('first')"], cwd=tmp_path, env={})
    pane.wait_exit(timeout=HANG_GUARD)
    pane.rerun([sys.executable, "-c", "print('second'); raise SystemExit(4)"], cwd=tmp_path, env={})
    assert pane.wait_exit(timeout=HANG_GUARD) == 4
    assert pane.screen() == ["second"]


def test_death_by_signal_reports_128_plus_the_signal(pane, tmp_path):
    pane.run(
        [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"],
        cwd=tmp_path,
        env={},
    )
    assert pane.wait_exit(timeout=HANG_GUARD) == 128 + 15


def test_exit_status_of_an_unreaped_process_comes_from_proc(monkeypatch, pane, tmp_path):
    """tmux can report a pane dead before it has reaped the process (a zombie)."""
    pane.run([sys.executable, "-c", "raise SystemExit(5)"], cwd=tmp_path, env={})
    pane.wait_exit(timeout=HANG_GUARD)
    real = pane._tmux

    def unreaped(*args, **kw):
        out = real(*args, **kw)
        if args[:1] == ("display-message",):
            dead, _status, _signal, pid = out.strip().split(":")
            return f"{dead}:::{pid}\n"  # tmux has not recorded a status yet
        return out

    monkeypatch.setattr(pane, "_tmux", unreaped)
    monkeypatch.setattr(screens, "_zombie_status", lambda pid: 5 << 8)  # wait status
    assert pane.exit_status() == 5


def test_zombie_status_reads_the_kernel_exit_code(tmp_path):
    import os
    import subprocess

    child = subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"])
    until(lambda: screens._zombie_status(child.pid) is not None)  # a zombie, not reaped
    status = screens._zombie_status(child.pid)
    child.wait()
    assert status is not None and os.waitstatus_to_exitcode(status) == 7
    assert screens._zombie_status(child.pid) is None  # reaped: no longer readable


def test_a_closed_pane_can_run_again_at_once(tmp_path):
    with held_folders.tmux_sockets("rcp-") as sockets, tmuxctl.Pane(socket_dir=sockets) as p:
        for i in range(5):
            p.run([sys.executable, "-c", f"print({i})"], cwd=tmp_path, env={})
            p.wait_exit(timeout=HANG_GUARD)
            assert p.screen() == [str(i)]
            p.close()


def test_signal_reaches_the_program_itself(pane, tmp_path):
    import signal

    code = (
        "import signal\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "print('ready'); signal.pause()\n"
    )
    pane.run([sys.executable, "-c", code], cwd=tmp_path, env={})
    pane.wait_for(r"^ready$", timeout=HANG_GUARD)
    pane.interrupt()  # ignored: it is the signal that ends it
    pane.signal(signal.SIGTERM)
    assert pane.wait_exit(timeout=HANG_GUARD) == 128 + signal.SIGTERM


def alive(pid: int) -> bool:
    """Whether ``pid`` runs now: there, and not a zombie."""
    try:
        with open(f"/proc/{pid}/stat") as stat:
            return stat.read().split(")")[-1].split()[0] != "Z"
    except (FileNotFoundError, ProcessLookupError):
        return False


def test_alive_reads_a_child_that_exits_while_its_stat_is_read(monkeypatch):
    # A process that exits between opening /proc/<pid>/stat and reading it makes
    # the read fail with ESRCH; that child is gone, not a failure of the test.
    import io
    import sys

    class Vanishing(io.StringIO):
        def read(self, *args):
            raise ProcessLookupError(3, "No such process")

    import os

    assert alive(os.getpid())  # positive control: this one runs
    monkeypatch.setattr(sys.modules[__name__], "open", lambda *a, **kw: Vanishing(), raising=False)
    assert alive(os.getpid()) is False
