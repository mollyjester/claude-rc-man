"""The hang guard (tests/hang_guard.py, pytest-timeout as pyproject configures it, with the hooks
tests/conftest.py adds): a test that hangs is ended, whatever it is blocked in, its teardown
included after its call failed, the processes it started are ended with it, and the run says
which test it was; a test that finishes is not touched.

Each case is a child pytest over a planted test file, with the project's own configuration
(pyproject.toml) and tests/conftest.py loaded as a plugin, in one process as make
test-container runs it, and with a pytest-xdist worker as make test does. A
planted test that blocks for ever is run with a guard of a second, so that it does not wait
long; the guard's method, and everything else, is the project's.
"""

from __future__ import annotations

import ast
import contextlib
import os
import select
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.collected import child_env
from tests.hang_guard import (
    CONTAINER_GUARD,
    CONTAINER_HANG_GUARD,
    EXEC_GUARD,
    GUARD_VARIABLE,
    HANG_GUARD,
    HOST_GUARD,
    PYPROJECT,
    configured,
    start_time,
)
from tests.typescript import FRONTEND, module_numbers, parse_ts

REPO = PYPROJECT.parent

# A guard that expires soon: the planted tests below that block never finish.
SHORT_GUARD = "1"

# Blocked in a worker thread it waits for, and where no signal reaches it, as in code that masks
# signals or retries a call they interrupt: a guard that relies on interrupting the test with a
# signal never ends this one. The worker thread is not a daemon: a run that only failed the test,
# and left the thread waiting, would itself not end where the interpreter waits for such a thread
# when it exits, as Python 3.13 and later do after an interrupted join.
HUNG = """
import signal
import threading


def test_blocked_in_a_worker_thread_where_no_signal_reaches():
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGALRM})
    worker = threading.Thread(target=threading.Event().wait, daemon=False)
    worker.start()
    worker.join()
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


@pytest.mark.parametrize("mode", MODES)
def test_a_hung_test_is_ended_and_named(tmp_path, mode):
    """Planted twin: a test that never finishes, under a short guard."""
    with child(tmp_path, HUNG, "--timeout", SHORT_GUARD, *MODES[mode]) as (code, output):
        ended_by_the_guard(
            mode, code, output, "test_blocked_in_a_worker_thread_where_no_signal_reaches"
        )


def test_a_test_whose_call_failed_is_still_guarded_in_its_teardown(tmp_path):
    """Planted twin: pytest-timeout on its own stops guarding a test once its call fails."""
    planted = child(tmp_path, FAILS_THEN_BLOCKS_IN_TEARDOWN, "--timeout", SHORT_GUARD)
    with planted as (code, output):
        ended_by_the_guard(
            "one process", code, output, "test_fails_then_blocks_in_teardown",
            "blocks_in_its_teardown",
        )  # fmt: skip
        assert f"{PLANTED} F" in output, output  # the call failed first


def test_the_processes_a_hung_test_started_are_ended_with_it(tmp_path):
    """Planted twin: the guard ends the test's process, and with it the child the test waits
    on, which would otherwise outlive it."""
    pidfile = tmp_path / "child.pid"
    source = WAITS_ON_A_CHILD.format(pidfile=str(pidfile))
    with child(tmp_path, source, "--timeout", SHORT_GUARD, *MODES["xdist"]) as (code, output):
        ended_by_the_guard("xdist", code, output, "test_waits_on_a_child_that_never_ends")
        pid, start = pidfile.read_text().split()  # the plant took effect: the child started
        assert ends(int(pid), int(start)), output


def test_a_test_that_finishes_is_not_touched_by_the_projects_guard(tmp_path):
    """Positive control, with the project's guard as it is: the guard is on, as the run's
    header says, and the test passes."""
    with child(tmp_path, FINISHES) as (code, output):
        assert code == 0, output
        assert f"timeout: {HOST_GUARD}s" in output and "timeout method: thread" in output, output
        assert "1 passed" in output, output


def test_each_tiers_hang_guard_is_outlived_by_its_guard_whose_method_is_thread():
    """A product call given its tier's hang guard early in a test ends before the guard of the
    tier would end the test's process; the method is the one that ends a test whatever it is
    blocked in (project-knowledge/tests.md, "The hang guard")."""
    assert 0 < HANG_GUARD < HOST_GUARD
    assert 0 < CONTAINER_HANG_GUARD < CONTAINER_GUARD
    # A program the container tests run in a container ends by its own bound, the tier's hang
    # guard, before the exec's kill, which comes before the test's guard.
    assert CONTAINER_HANG_GUARD < EXEC_GUARD < CONTAINER_GUARD
    assert configured()["timeout_method"] == "thread"


# The execs that run, in a container, a program the container tests start, each handing it the
# tier's hang guard in GUARD_VARIABLE: by file, the class whose ``exec`` it is.
HANDING_EXECS = {
    "tests/container/test_install_in_container.py": "Installation",
}


def handing_problems(source: str, owner: str) -> list[str]:
    """What is wrong with ``owner.exec`` in ``source``: it hands the program it runs
    CONTAINER_HANG_GUARD in GUARD_VARIABLE, its ``until``'s bound there, and kills it at
    EXEC_GUARD, beyond that bound, so that the program's own ends first and says what it
    waited for."""
    method = next(
        (
            node
            for cls in ast.parse(source).body
            if isinstance(cls, ast.ClassDef) and cls.name == owner
            for node in cls.body
            if isinstance(node, ast.FunctionDef) and node.name == "exec"
        ),
        None,
    )
    if method is None:
        return [f"{owner}.exec is not there"]
    problems = []
    handed = [
        ast.unparse(part.value)
        for node in ast.walk(method)
        if isinstance(node, ast.JoinedStr)
        and any(
            isinstance(p, ast.FormattedValue) and ast.unparse(p.value) == "GUARD_VARIABLE"
            for p in node.values
        )
        for part in node.values
        if isinstance(part, ast.FormattedValue) and ast.unparse(part.value) != "GUARD_VARIABLE"
    ]
    if handed != ["CONTAINER_HANG_GUARD"]:
        problems.append(f"{owner}.exec hands {handed or 'nothing'} in {GUARD_VARIABLE}")
    kills = [
        ast.unparse(k.value)
        for node in ast.walk(method)
        if isinstance(node, ast.Call)
        for k in node.keywords
        if k.arg == "timeout"
    ]
    if kills != ["EXEC_GUARD"]:
        problems.append(f"{owner}.exec kills at {kills or 'nothing'}, not EXEC_GUARD")
    return problems


def test_a_program_run_in_a_container_ends_by_its_own_bound_first():
    """The bounds nest: a program the container tests run in a container is handed the tier's
    hang guard, which its ``until`` gives up at with what it waited for, and the exec that runs
    it kills it beyond that, at EXEC_GUARD, which the test of each tier's value holds within the
    test's guard (project-knowledge/tests.md, "The hang guard")."""
    for path, owner in HANDING_EXECS.items():
        assert handing_problems((REPO / path).read_text(encoding="utf-8"), owner) == [], path
    # Planted twin: the installation's exec killing at the bound it hands, as a bare timeout that
    # would always come first.
    source = (REPO / "tests/container/test_install_in_container.py").read_text(encoding="utf-8")
    killed = source.replace("timeout=EXEC_GUARD", "timeout=CONTAINER_HANG_GUARD")
    assert killed != source  # the plant took
    assert handing_problems(killed, "Installation") == [
        "Installation.exec kills at ['CONTAINER_HANG_GUARD'], not EXEC_GUARD"
    ]


# The frontend tests' hang guards (frontend/src/test/hangGuard.ts), in milliseconds, each tier a
# test's and a step's, half of it.
FRONTEND_GUARDS = FRONTEND / "src" / "test" / "hangGuard.ts"
# The component tests' own step (Vitest in jsdom), not the host's: a render and a fake fetch come
# far within it on a loaded machine, and a failing findBy* says so in seconds, not minutes.
UNIT_STEP_MS = 20_000


def frontend_guard_problems(values: dict[str, float]) -> list[str]:
    """What is wrong with the frontend's hang guards, as hangGuard.ts gives them: the
    component tests' own, a test's and a step's, and no other."""
    wanted = {
        "UNIT_TEST_GUARD_MS": 2 * UNIT_STEP_MS,
        "UNIT_HANG_GUARD_MS": UNIT_STEP_MS,
    }
    problems = [
        f"{n} is {values.get(n)}, not {v:g}" for n, v in wanted.items() if values.get(n) != v
    ]
    problems += [f"{n} is no tier's guard" for n in sorted(set(values) - set(wanted))]
    return problems


def test_the_frontends_hang_guards_are_their_tiers():
    tree = parse_ts({"hangGuard.ts": FRONTEND_GUARDS.read_text(encoding="utf-8")})["hangGuard.ts"]
    values = module_numbers(tree)
    assert values  # positive control: the guards were read
    assert frontend_guard_problems(values) == []
    planted = {**values, "UNIT_HANG_GUARD_MS": 15_000}  # planted twin: a step at another value
    assert frontend_guard_problems(planted) == ["UNIT_HANG_GUARD_MS is 15000, not 20000"]
