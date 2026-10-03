"""The tmux driver used to record the real `claude` and to drive the fake."""

import sys

import pytest

from devtools import tmuxctl
from helpers import rc_helper


@pytest.fixture
def pane(tmp_path):
    with tmuxctl.Pane(socket_dir=tmp_path) as p:
        yield p


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
    assert pane.screen() == ["ready", "^Cinterrupted"]  # the tty echoes ^C


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
    with tmuxctl.Pane(socket_dir=tmp_path) as p:
        p.run([sys.executable, "-c", "import time; time.sleep(30)"], cwd=tmp_path, env={})
        socket = p.socket
    assert not tmuxctl.server_running(socket)


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
    monkeypatch.setattr(rc_helper, "_zombie_status", lambda pid: 5 << 8)  # wait status
    assert pane.exit_status() == 5


def test_zombie_status_reads_the_kernel_exit_code(tmp_path):
    import os
    import subprocess
    import time

    child = subprocess.Popen([sys.executable, "-c", "raise SystemExit(7)"])
    for _ in range(100):  # wait for it to become a zombie, without reaping it
        if rc_helper._zombie_status(child.pid) is not None:
            break
        time.sleep(0.05)
    status = rc_helper._zombie_status(child.pid)
    child.wait()
    assert status is not None and os.waitstatus_to_exitcode(status) == 7
    assert rc_helper._zombie_status(child.pid) is None  # reaped: no longer readable


def test_a_closed_pane_can_run_again_at_once(tmp_path):
    with tmuxctl.Pane(socket_dir=tmp_path) as p:
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
