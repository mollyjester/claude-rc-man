"""The hang guard (tests/hang_guard.py, pytest-timeout as pyproject configures it, with the hooks
tests/conftest.py adds): a test that hangs is ended, whatever it is blocked in, its teardown
included after its call failed, the processes it started are ended with it, and the run says
which test it was; a test that finishes is not touched.

Each case is a child pytest over a planted test file, with the project's own configuration
(pyproject.toml) and tests/conftest.py loaded as a plugin, in one process as make
test-container and make check-ui run, and with a pytest-xdist worker as make test does. A
planted test that blocks for ever is run with a guard of a second, so that it does not wait
long; the guard's method, and everything else, is the project's.
"""

from __future__ import annotations

import contextlib
import json
import os
import select
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from devtools.held_folders import state_and_threads
from tests.collected import child_env
from tests.hang_guard import (
    CONTAINER_GUARD,
    CONTAINER_HANG_GUARD,
    HANG_GUARD,
    HOST_GUARD,
    PYPROJECT,
    configured,
    start_time,
)

REPO = PYPROJECT.parent

# A guard that expires soon: the planted tests below that block never finish.
SHORT_GUARD = "1"

BLOCKED_ON_A_LOCK = """
import threading


def test_blocked_on_a_lock():
    lock = threading.Lock()
    lock.acquire()
    lock.acquire()
"""

# The worker thread is not a daemon: a run that only failed the test, and left the thread
# waiting, would itself not end where the interpreter waits for such a thread when it exits,
# as Python 3.13 and later do after an interrupted join.
BLOCKED_IN_A_WORKER_THREAD = """
import threading


def test_blocked_in_a_worker_thread():
    worker = threading.Thread(target=threading.Event().wait, daemon=False)
    worker.start()
    worker.join()
"""

# Blocked where no signal reaches it, as in code that masks signals or retries a call they
# interrupt: a guard that relies on interrupting the test with a signal never ends this one.
BLOCKED_WHERE_NO_SIGNAL_REACHES = """
import signal
import threading


def test_blocked_where_no_signal_reaches():
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    lock = threading.Lock()
    lock.acquire()
    lock.acquire()
"""

# Its call fails, which pytest-timeout on its own takes as the end of the guard.
FAILS_THEN_BLOCKS_IN_TEARDOWN = """
import threading

import pytest


@pytest.fixture
def blocks_in_its_teardown():
    yield
    lock = threading.Lock()
    lock.acquire()
    lock.acquire()


def test_fails_then_blocks_in_teardown(blocks_in_its_teardown):
    assert False, "planted"
"""

# Started without a session of its own, as most of what the tests start is; {pidfile} is where
# it says its pid and its start time.
WAITS_ON_A_CHILD = """
import subprocess
from pathlib import Path

from tests.hang_guard import start_time


def test_waits_on_a_child_that_never_ends():
    child = subprocess.Popen(["sleep", "infinity"])
    Path({pidfile!r}).write_text(f"{{child.pid}} {{start_time(child.pid)}}")
    child.wait()
"""

# The guard's stop of the processes below the test fails: the test must still be ended.
STOP_FAILS = """
import threading

from tests import hang_guard


def test_blocked_while_the_stop_fails(monkeypatch):
    def fails():
        raise PermissionError("planted: the stop failed")

    monkeypatch.setattr(hang_guard, "stop_descendants", fails)
    lock = threading.Lock()
    lock.acquire()
    lock.acquire()
"""

# What the guard needs to report the test is not to be had, or is broken: the terminal it
# writes to cannot be had, or fails every write and flush, as a closed pipe would make it. The
# test, which started a child and is blocked on a lock, so that it does not end when the child
# is killed, must still be ended, and the child with it. {pidfile} is where the child's pid and
# start time are written.
NO_TERMINAL = """
import subprocess
import threading
from pathlib import Path

from tests.hang_guard import start_time


def broken(*args, **kwargs):
    raise BrokenPipeError("planted: the terminal is broken")


def test_blocked_without_a_terminal(request, monkeypatch):
    {plant}
    child = subprocess.Popen(["sleep", "infinity"])
    Path({pidfile!r}).write_text(f"{{child.pid}} {{start_time(child.pid)}}")
    lock = threading.Lock()
    lock.acquire()
    lock.acquire()
"""
PLANTS = {
    "cannot be had": 'monkeypatch.setattr(request.config, "get_terminal_writer", broken)',
    "broken": (
        "terminal = request.config.get_terminal_writer()\n"
        "    for name in ('write', 'line', 'sep', 'flush'):\n"
        "        monkeypatch.setattr(terminal, name, broken)"
    ),
}

FINISHES = """
import threading


def test_finishes_after_its_worker_thread():
    done = threading.Event()
    worker = threading.Thread(target=done.set, daemon=False)
    worker.start()
    worker.join()
    assert done.is_set()
"""

PLANTED = "test_planted.py"

# How the targets run pytest: in one process, and with pytest-xdist workers.
MODES = {"one process": [], "xdist": ["-n", "1"]}


@contextlib.contextmanager
def in_a_session(argv: list[str], cwd: Path) -> Iterator[tuple[int, str]]:
    """The exit code and the output of ``argv``, run with the repository importable in a
    session of its own; whatever is left of that session when the context ends is killed, so
    that a guard that failed leaves nothing running."""
    # tests.* from the repository; a width that keeps each summary line whole
    env = child_env({"PYTHONPATH": str(REPO), "COLUMNS": "300"})
    proc = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output, _ = proc.communicate(timeout=HANG_GUARD)
        yield proc.returncode, output
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def child(tmp_path: Path, source: str, *args: str):
    """in_a_session for a child pytest over ``source`` as the only test file, with the
    project's configuration and tests/conftest.py; the run must end."""
    planted = tmp_path / PLANTED
    planted.write_text(source, encoding="utf-8")
    argv = [sys.executable, "-m", "pytest", "-c", str(PYPROJECT), "--rootdir", str(tmp_path)]
    argv += ["--basetemp", str(tmp_path / "basetemp"), "-p", "no:cacheprovider"]
    argv += ["-p", "tests.conftest", *args, planted.name]
    return in_a_session(argv, tmp_path)


def ends(pid: int, start: int | None) -> bool:
    """Whether the process ``pid`` that started at ``start`` (start_time), not a child of this
    process, has ended or ends within HANG_GUARD, as its pidfd tells; a pid that names another
    process by now is one that ended."""
    try:
        pidfd = os.pidfd_open(pid)
    except ProcessLookupError:
        return True
    try:
        if start_time(pid) != start:
            return True
        readable, _, _ = select.select([pidfd], [], [], HANG_GUARD)
        return bool(readable)
    finally:
        os.close(pidfd)


def ended_by_the_guard(mode: str, code: int, output: str, test: str, frame: str = "") -> None:
    """The run ended, failed, and names ``test`` as the one the guard ended: in one process by
    the guard's report and the frame it was blocked in (``frame``, by default the test's) in
    the stacks it gives; under pytest-xdist by the worker the guard ended, which the controller
    reports with the test it was running."""
    assert code == 1, output
    if mode == "xdist":
        assert f"crashed while running '{PLANTED}::{test}'" in output, output
        assert f"FAILED {PLANTED}::{test} - worker" in output, output
    else:
        assert "+ Timeout +" in output, output
        assert f", in {frame or test}\n" in output, output  # in the stacks of the threads


HUNG = {
    "test_blocked_on_a_lock": BLOCKED_ON_A_LOCK,
    "test_blocked_in_a_worker_thread": BLOCKED_IN_A_WORKER_THREAD,
    "test_blocked_where_no_signal_reaches": BLOCKED_WHERE_NO_SIGNAL_REACHES,
}


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("test", HUNG)
def test_a_hung_test_is_ended_and_named(tmp_path, test, mode):
    """Planted twin: a test that never finishes, under a short guard."""
    with child(tmp_path, HUNG[test], "--timeout", SHORT_GUARD, *MODES[mode]) as (code, output):
        ended_by_the_guard(mode, code, output, test)


@pytest.mark.parametrize("mode", MODES)
def test_a_test_whose_call_failed_is_still_guarded_in_its_teardown(tmp_path, mode):
    """Planted twin: pytest-timeout on its own stops guarding a test once its call fails."""
    planted = child(tmp_path, FAILS_THEN_BLOCKS_IN_TEARDOWN, "--timeout", SHORT_GUARD, *MODES[mode])
    with planted as (code, output):
        ended_by_the_guard(
            mode, code, output, "test_fails_then_blocks_in_teardown", "blocks_in_its_teardown"
        )
        # the call failed first
        assert "planted" in output if mode == "xdist" else f"{PLANTED} F" in output, output


@pytest.mark.parametrize("mode", MODES)
def test_the_processes_a_hung_test_started_are_ended_with_it(tmp_path, mode):
    """Planted twin: the guard ends the test's process, and with it the child the test waits
    on, which would otherwise outlive it."""
    pidfile = tmp_path / "child.pid"
    source = WAITS_ON_A_CHILD.format(pidfile=str(pidfile))
    with child(tmp_path, source, "--timeout", SHORT_GUARD, *MODES[mode]) as (code, output):
        ended_by_the_guard(mode, code, output, "test_waits_on_a_child_that_never_ends")
        pid, start = pidfile.read_text().split()  # the plant took effect: the child started
        assert ends(int(pid), int(start)), output


@pytest.mark.parametrize("mode", MODES)
def test_a_hung_test_is_ended_when_the_stop_of_its_processes_fails(tmp_path, mode):
    """Planted twin: the stop of its processes raises, and the test is ended all the same."""
    with child(tmp_path, STOP_FAILS, "--timeout", SHORT_GUARD, *MODES[mode]) as (code, output):
        ended_by_the_guard(mode, code, output, "test_blocked_while_the_stop_fails")
        if mode == "one process":
            assert "PermissionError: planted: the stop failed" in output, output


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("plant", PLANTS)
def test_a_hung_test_is_ended_when_its_terminal_fails(tmp_path, plant, mode):
    """Planted twin: the guard's report fails, and the test and its child are ended all the
    same; what failed is said on stderr."""
    pidfile = tmp_path / "child.pid"
    source = NO_TERMINAL.format(plant=PLANTS[plant], pidfile=str(pidfile))
    with child(tmp_path, source, "--timeout", SHORT_GUARD, *MODES[mode]) as (code, output):
        assert code == 1, output
        if mode == "xdist":
            assert f"crashed while running '{PLANTED}::test_blocked_without_a_terminal'" in output
        else:
            assert "BrokenPipeError: planted: the terminal is broken" in output, output
        pid, start = pidfile.read_text().split()  # the plant took effect: the child started
        assert ends(int(pid), int(start)), output


# A process below the killer that its first look at /proc misses, as one forked between the look
# and the stop of its parent would be: the next look finds it, its parent stopped.
FORKS_LATE = """
import json
import subprocess

from tests import hang_guard

parent = subprocess.Popen(
    ["sh", "-c", "sleep infinity & echo $!; wait"], stdout=subprocess.PIPE, text=True
)
late = int(parent.stdout.readline())
started = {pid: hang_guard.start_time(pid) for pid in (parent.pid, late)}
real = hang_guard.descendants
looks = []


def the_first_look_misses_the_late_one(pid):
    found = real(pid)
    looks.append(sorted(found))
    return {p: s for p, s in found.items() if len(looks) > 1 or p != late}


hang_guard.descendants = the_first_look_misses_the_late_one
hang_guard.kill(hang_guard.stop_descendants())
print(json.dumps({"started": started, "looks": looks}))
"""


def test_a_process_forked_after_the_first_look_is_killed_too(tmp_path):
    with in_a_session([sys.executable, "-c", FORKS_LATE], tmp_path) as (code, output):
        assert code == 0, output
        found = json.loads(output)
        started = {int(pid): start for pid, start in found["started"].items()}
        assert all(set(started) <= set(look) for look in found["looks"])  # both were below it
        assert len(found["looks"]) >= 2  # it looked again
        assert all(ends(pid, start) for pid, start in started.items()), output


# The kill, told of a process below it by a pid whose start time is not the process's own, as
# a pid given again since the look would be.
KILLS_WHAT_IT_IS_TOLD = """
import json
import sys

from tests import hang_guard

told = {int(pid): start for pid, start in json.loads(sys.argv[1]).items()}
hang_guard.descendants = lambda pid: told
hang_guard.kill(hang_guard.stop_descendants())
"""


def test_a_pid_given_again_is_not_killed(tmp_path):
    other = subprocess.Popen(["sleep", "infinity"], start_new_session=True)
    try:
        start = start_time(other.pid)
        for told, killed in ((start + 1, False), (start, True)):  # the second: positive control
            argv = [sys.executable, "-c", KILLS_WHAT_IT_IS_TOLD, json.dumps({other.pid: told})]
            with in_a_session(argv, tmp_path) as (code, output):
                assert code == 0, output
            if killed:
                assert ends(other.pid, start)
                assert other.wait() == -signal.SIGKILL
            else:
                assert other.poll() is None, "a process it was not told of was killed"
                assert state_and_threads(other.pid)[0] != "T", "and not stopped either"
    finally:
        other.kill()
        other.wait()


@pytest.mark.parametrize("mode", MODES)
def test_a_test_that_finishes_is_not_touched_by_the_projects_guard(tmp_path, mode):
    """Positive control, with the project's guard as it is: the guard is on, as the run's
    header says, and the test passes."""
    with child(tmp_path, FINISHES, *MODES[mode]) as (code, output):
        assert code == 0, output
        assert f"timeout: {HOST_GUARD}s" in output and "timeout method: thread" in output, output
        assert "1 passed" in output, output


def test_each_tiers_hang_guard_is_outlived_by_its_guard_whose_method_is_thread():
    """A product call given its tier's hang guard early in a test ends before the guard of the
    tier would end the test's process; the method is the one that ends a test whatever it is
    blocked in (project-knowledge/tests.md, "The hang guard")."""
    assert 0 < HANG_GUARD < HOST_GUARD
    assert 0 < CONTAINER_HANG_GUARD < CONTAINER_GUARD
    assert configured()["timeout_method"] == "thread"
