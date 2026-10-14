"""The tmux driver used to record the real `claude` and to drive the fake."""

import sys
from pathlib import Path

import pytest

from devtools import held_folders, tmuxctl
from helpers import screens


@pytest.fixture
def pane():
    with held_folders.tmux_sockets("rcp-") as sockets, tmuxctl.Pane(socket_dir=sockets) as p:
        yield p


def test_the_panes_tmux_server_is_in_a_held_folder_of_its_own(pane, tmp_path):
    """So a run killed by a signal leaves it for the next run to stop."""
    pane.run(["sleep", "60"], cwd=tmp_path, env={})
    folder = pane.socket.parent
    assert held_folders.made_with("rcp-")(folder.name) and folder.parent == held_folders.TMP
    assert not held_folders.unheld(folder)


def test_screen_shows_command_output_and_exit_status(pane, tmp_path):
    pane.run([sys.executable, "-c", "print('hello'); raise SystemExit(3)"], cwd=tmp_path, env={})
    assert pane.wait_exit(timeout=10) == 3
    assert pane.screen() == ["hello"]


def test_environment_is_exactly_what_was_given(pane, tmp_path):
    code = "import os; print(sorted(k for k in os.environ if k.startswith('RCT_')))"
    pane.run([sys.executable, "-c", code], cwd=tmp_path, env={"RCT_A": "1"})
    pane.wait_exit(timeout=10)
    assert pane.screen() == ["['RCT_A']"]


def test_working_directory_is_set(pane, tmp_path):
    (tmp_path / "sub").mkdir()
    pane.run([sys.executable, "-c", "import os; print(os.getcwd())"], cwd=tmp_path / "sub", env={})
    pane.wait_exit(timeout=10)
    assert pane.screen() == [str(tmp_path / "sub")]


def test_arguments_are_not_shell_interpreted(pane, tmp_path):
    pane.run(
        [sys.executable, "-c", "import sys; print(sys.argv[1])", "$HOME; echo x"],
        cwd=tmp_path,
        env={},
    )
    pane.wait_exit(timeout=10)
    assert pane.screen() == ["$HOME; echo x"]


def test_typed_text_reaches_the_program(pane, tmp_path):
    pane.run([sys.executable, "-c", "print('got', input('> '))"], cwd=tmp_path, env={})
    pane.wait_for(r"^>$", timeout=10)  # trailing space is trimmed
    pane.type_line("abc#def")
    pane.wait_exit(timeout=10)
    assert pane.screen() == ["> abc#def", "got abc#def"]


def test_interrupt_delivers_sigint(pane, tmp_path):
    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGINT, lambda *a: (print('interrupted'), exit(0)))\n"
        "print('ready'); time.sleep(30)\n"
    )
    pane.run([sys.executable, "-c", code], cwd=tmp_path, env={})
    pane.wait_for(r"^ready$", timeout=10)
    pane.interrupt()
    assert pane.wait_exit(timeout=10) == 0
    # The tty echoes ^C, but may send SIGINT first, so the two come in either order.
    screen = pane.screen()
    assert screen[0] == "ready"
    rest = "".join(screen[1:])
    assert "^C" in rest and "interrupted" in rest
    assert rest.replace("^C", "", 1).replace("interrupted", "", 1) == ""


def test_wait_for_times_out_with_the_screen_in_the_message(pane, tmp_path):
    pane.run(
        [sys.executable, "-c", "print('nothing useful'); import time; time.sleep(30)"],
        cwd=tmp_path,
        env={},
    )
    pane.wait_for(r"nothing useful", timeout=10)
    with pytest.raises(TimeoutError, match="nothing useful"):
        pane.wait_for(r"never appears", timeout=0.5)


def test_wait_exit_times_out_while_running(pane, tmp_path):
    pane.run([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path, env={})
    with pytest.raises(TimeoutError):
        pane.wait_exit(timeout=0.5)


def test_screen_trims_trailing_blank_lines_and_spaces(pane, tmp_path):
    pane.run(
        [sys.executable, "-c", "print(); print('a   '); print(); print()"], cwd=tmp_path, env={}
    )
    pane.wait_exit(timeout=10)
    assert pane.screen() == ["", "a"]


def test_closing_kills_the_tmux_server(tmp_path):
    """The server process itself ends, not only its socket file."""
    import os
    import signal
    import subprocess

    pid_file = tmp_path / "server.pid"
    pid = None
    try:
        with held_folders.tmux_sockets("rcp-") as sockets, tmuxctl.Pane(socket_dir=sockets) as p:
            p.run([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path, env={})
            server = ["tmux", "-S", str(p.socket), "display-message", "-p", "#{pid}"]
            pid = int(subprocess.run(server, capture_output=True, text=True, check=True).stdout)
            pid_file.write_text(str(pid))
            assert child_alive(pid_file, deadline=0.5) == (True, pid)  # positive control
            socket = p.socket
        assert child_alive(pid_file, deadline=10.0) == (False, pid)
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
    pane.wait_exit(timeout=10)
    pane.rerun([sys.executable, "-c", "print('second'); raise SystemExit(4)"], cwd=tmp_path, env={})
    assert pane.wait_exit(timeout=10) == 4
    assert pane.screen() == ["second"]


def test_death_by_signal_reports_128_plus_the_signal(pane, tmp_path):
    pane.run(
        [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"],
        cwd=tmp_path,
        env={},
    )
    assert pane.wait_exit(timeout=10) == 128 + 15


def test_exit_status_of_an_unreaped_process_comes_from_proc(monkeypatch, pane, tmp_path):
    """tmux can report a pane dead before it has reaped the process (a zombie)."""
    pane.run([sys.executable, "-c", "raise SystemExit(5)"], cwd=tmp_path, env={})
    pane.wait_exit(timeout=10)
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
    import time

    child = subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"])
    for _ in range(100):  # wait for it to become a zombie, without reaping it
        if screens._zombie_status(child.pid) is not None:
            break
        time.sleep(0.05)
    status = screens._zombie_status(child.pid)
    child.wait()
    assert status is not None and os.waitstatus_to_exitcode(status) == 7
    assert screens._zombie_status(child.pid) is None  # reaped: no longer readable


def test_a_closed_pane_can_run_again_at_once(tmp_path):
    with held_folders.tmux_sockets("rcp-") as sockets, tmuxctl.Pane(socket_dir=sockets) as p:
        for i in range(5):
            p.run([sys.executable, "-c", f"print({i})"], cwd=tmp_path, env={})
            p.wait_exit(timeout=10)
            assert p.screen() == [str(i)]
            p.close()


def test_signal_reaches_the_program_itself(pane, tmp_path):
    import signal

    code = (
        "import signal, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        "print('ready'); time.sleep(30)\n"
    )
    pane.run([sys.executable, "-c", code], cwd=tmp_path, env={})
    pane.wait_for(r"^ready$", timeout=10)
    pane.interrupt()  # ignored: it is the signal that ends it
    pane.signal(signal.SIGTERM)
    assert pane.wait_exit(timeout=10) == 128 + signal.SIGTERM


def child_alive(pid_file, deadline=5.0):
    import time

    start = time.monotonic()
    while not pid_file.exists() or not pid_file.read_text():
        assert time.monotonic() - start < deadline, "the child never started"
        time.sleep(0.05)
    pid = int(pid_file.read_text())
    while time.monotonic() - start < deadline:
        try:
            with open(f"/proc/{pid}/stat") as stat:
                if stat.read().split(")")[-1].split()[0] == "Z":
                    return False, pid
        except (FileNotFoundError, ProcessLookupError):
            return False, pid
        time.sleep(0.1)
    return True, pid


def test_child_alive_reads_a_child_that_exits_while_its_stat_is_read(tmp_path, monkeypatch):
    # A process that exits between opening /proc/<pid>/stat and reading it makes
    # the read fail with ESRCH; that child is gone, not a failure of the test.
    import io
    import sys

    class Vanishing(io.StringIO):
        def read(self, *args):
            raise ProcessLookupError(3, "No such process")

    pid_file = tmp_path / "child.pid"
    pid_file.write_text("4242")
    monkeypatch.setattr(sys.modules[__name__], "open", lambda *a, **kw: Vanishing(), raising=False)
    assert child_alive(pid_file, deadline=1.0) == (False, 4242)
