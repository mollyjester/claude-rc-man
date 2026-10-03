"""How the backend calls rc-helper and rc-admin, and maps their errors."""

from pathlib import Path

import pytest
from rcman import helpers_client as hc

from helpers import rc_admin, rc_helper


def test_production_helper_runs_through_sudo_as_the_user(tmp_path):
    seen = {}

    def fake_run(argv, **kw):
        import subprocess

        seen["argv"], seen["input"] = argv, kw.get("input")
        return subprocess.CompletedProcess(argv, 0, stdout='{"servers": []}', stderr="")

    run = hc.sudo_helper(Path("/opt/rcman/helpers"), runner=fake_run)
    assert run("alice", ["list"], stdin="secret") == (0, {"servers": []})
    assert seen["argv"] == ["sudo", "-n", "-u", "alice", "/opt/rcman/helpers/rc-helper", "list"]
    assert seen["input"] == "secret"


def test_production_admin_runs_through_sudo_as_root():
    seen = {}

    def fake_run(argv, **kw):
        import subprocess

        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout='{"users": []}', stderr="")

    run = hc.sudo_admin(Path("/opt/rcman/helpers"), runner=fake_run)
    assert run(["list"]) == (0, {"users": []})
    assert seen["argv"] == ["sudo", "-n", "/opt/rcman/helpers/rc-admin", "list"]


def test_unparseable_output_is_helper_unavailable():
    def fake_run(argv, **kw):
        import subprocess

        return subprocess.CompletedProcess(
            argv, 1, stdout="sudo: a password is required", stderr=""
        )

    code, out = hc.sudo_helper(Path("/x"), runner=fake_run)("alice", ["list"])
    assert out["error"]["code"] == "helper_unavailable"


def test_every_helper_error_code_has_a_status():
    assert set(hc.HELPER_STATUS) == set(rc_helper.MESSAGES)


def test_every_admin_error_code_has_a_status():
    assert set(hc.ADMIN_STATUS) == set(rc_admin.MESSAGES)


@pytest.mark.parametrize("status", [*hc.HELPER_STATUS.values(), *hc.ADMIN_STATUS.values()])
def test_statuses_are_client_or_server_errors(status):
    assert 400 <= status < 600


def test_a_helper_that_times_out_is_a_problem():
    import subprocess

    from rcman.identity import Identity
    from rcman.problems import Problem

    seen = {}

    def fake_run(argv, **kw):
        seen["timeout"] = kw["timeout"]
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    run = hc.sudo_helper(Path("/x"), runner=fake_run)
    with pytest.raises(Problem) as raised:
        hc.call_helper(run, Identity("a@example.com", "alice", False), ["list"], timeout=7)
    assert (raised.value.code, raised.value.status) == ("helper_timeout", 504)
    assert seen["timeout"] == 7
    hc.call_helper(
        hc.sudo_helper(Path("/x"), runner=lambda argv, **kw: seen.update(kw) or ok(argv)),
        Identity("a@example.com", "alice", False),
        ["list"],
    )
    assert seen["timeout"] == hc.HELPER_TIMEOUT  # the default when none is given


def ok(argv):
    import subprocess

    return subprocess.CompletedProcess(argv, 0, stdout='{"servers": []}', stderr="")


# A stand-in for sudo: it runs a child that outlives it unless told to stop, and,
# like sudo, relays SIGTERM to that child; SIGKILL it cannot relay.
SUDO_LIKE = """
import signal, subprocess, sys, time
child = subprocess.Popen([sys.executable, "-c",
    "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)",
    sys.argv[1]])
signal.signal(signal.SIGTERM, lambda *a: (child.terminate(), child.wait(), sys.exit(143)))
child.wait()
"""


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


def test_a_timed_out_helper_does_not_leave_its_child_running(tmp_path):
    import os
    import signal
    import subprocess
    import sys

    # Planted twin: subprocess.run's own timeout kills the sudo stand-in outright,
    # and the child it started keeps running.
    planted = tmp_path / "planted.pid"
    with pytest.raises(subprocess.TimeoutExpired):
        subprocess.run([sys.executable, "-c", SUDO_LIKE, str(planted)], timeout=1)
    alive, pid = child_alive(planted, deadline=1.0)
    assert alive  # the defect is real for a plain timeout
    os.kill(pid, signal.SIGKILL)

    pid_file = tmp_path / "child.pid"
    with pytest.raises(subprocess.TimeoutExpired):
        hc.run_bounded(
            [sys.executable, "-c", SUDO_LIKE, str(pid_file)],
            input=None,
            capture_output=True,
            text=True,
            timeout=1,
        )
    alive, pid = child_alive(pid_file)
    if alive:
        os.kill(pid, signal.SIGKILL)
    assert not alive


def test_run_bounded_returns_what_the_process_printed():
    import sys

    proc = hc.run_bounded(
        [sys.executable, "-c", "import sys; print(sys.stdin.read().upper()); sys.exit(3)"],
        input="hi",
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert (proc.returncode, proc.stdout.strip()) == (3, "HI")


def test_the_production_runners_bound_their_calls_this_way():
    import inspect

    assert inspect.signature(hc.sudo_helper).parameters["runner"].default is hc.run_bounded
    assert inspect.signature(hc.sudo_admin).parameters["runner"].default is hc.run_bounded


def test_an_admin_call_that_times_out_is_a_problem():
    import subprocess

    from rcman.problems import Problem

    seen = {}

    def fake_run(argv, **kw):
        seen["timeout"] = kw["timeout"]
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    run = hc.sudo_admin(Path("/x"), runner=fake_run)
    with pytest.raises(Problem) as raised:
        hc.call_admin(run, ["claude-status"], timeout=9)
    assert (raised.value.code, raised.value.status) == ("helper_timeout", 504)
    assert seen["timeout"] == 9
    hc.call_admin(
        hc.sudo_admin(Path("/x"), runner=lambda argv, **kw: seen.update(kw) or ok(argv)),
        ["list"],
    )
    assert seen["timeout"] == hc.ADMIN_TIMEOUT  # the default when none is given


# Like SUDO_LIKE, but its child ignores SIGTERM, as a stuck helper might, and
# keeps the output pipe open.
SUDO_LIKE_STUBBORN = """
import signal, subprocess, sys
child = subprocess.Popen([sys.executable, "-c",
    "import os, signal, sys, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
    "open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)",
    sys.argv[1]])
signal.signal(signal.SIGTERM, lambda *a: child.terminate())
child.wait()
"""


def test_a_helper_that_ignores_sigterm_does_not_hold_the_call(tmp_path, monkeypatch):
    import os
    import signal
    import subprocess
    import sys
    import time

    monkeypatch.setattr(hc, "TERMINATE_GRACE_SECONDS", 0.5)
    pid_file = tmp_path / "child.pid"
    started = time.monotonic()
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            hc.run_bounded(
                [sys.executable, "-c", SUDO_LIKE_STUBBORN, str(pid_file)],
                input=None,
                capture_output=True,
                text=True,
                timeout=1,
            )
        elapsed = time.monotonic() - started
    finally:
        if pid_file.exists() and pid_file.read_text():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass
    assert elapsed < 1 + 0.5 + 2  # the timeout, the grace period and a margin


# A child Python whose locale is not UTF-8: C, with the coercion to C.UTF-8 and
# UTF-8 mode both off, so its text streams default to ASCII.
NOT_UTF8 = {"LC_ALL": "C", "LANG": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"}
ROUND_TRIP = """
import json, locale, sys
from rcman import helpers_client as hc
echo = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())"]
text = json.loads(sys.argv[1])  # in ASCII, so the locale cannot change it
proc = hc.run_bounded(echo, input=text, capture_output=True, text=True, timeout=10)
print(json.dumps({"locale": locale.getpreferredencoding(False), "out": proc.stdout}))
"""


@pytest.mark.parametrize("text", ["café", "café €", "Сергей ünï 𝄞"])
def test_what_a_helper_is_given_and_answers_is_utf8_whatever_the_locale(text):
    import json
    import os
    import subprocess
    import sys

    env = {**{k: v for k, v in os.environ.items() if not k.startswith("LC_")}, **NOT_UTF8}
    proc = subprocess.run(
        [sys.executable, "-c", ROUND_TRIP, json.dumps(text)],
        env=env,
        capture_output=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")[-2000:]
    seen = json.loads(proc.stdout)
    assert seen["locale"].lower().replace("-", "") not in ("utf8",)  # the plant took effect
    assert seen["out"] == text


def test_a_stop_is_waited_for_until_the_helper_has_ended_the_server():
    """A stop waits STOP_TIMEOUT for Ctrl+C, then END_GRACE_SECONDS after each of
    SIGTERM and SIGKILL; the usual timeout outlasts all of it, with time to spare."""
    assert rc_helper.STOP_TIMEOUT > 0 and rc_helper.END_GRACE_SECONDS > 0
    assert hc.HELPER_TIMEOUT > rc_helper.STOP_TIMEOUT + 2 * rc_helper.END_GRACE_SECONDS
