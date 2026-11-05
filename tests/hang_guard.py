"""The hang guard's values, the marker that gives the container tests theirs, the hang guards
for product calls, and what the guard does to the processes a test it ends has started.

Not a test module. The hang guard is pytest-timeout, with the hooks tests/conftest.py adds. Its
two values are in pyproject's ``[tool.pytest.ini_options]``, read from there so that each is
one: ``timeout``, which every test gets (HOST_GUARD), and ``container_timeout``, which the tests
carrying the derived container marker get instead (CONTAINER_GUARD) through a ``timeout`` marker
that ``mark`` adds. HANG_GUARD, in a host test, and CONTAINER_HANG_GUARD, in a container test,
are what a test hands a product call as its timeout for something expected to happen. The rules
are in project-knowledge/tests.md, "The hang guard".
"""

from __future__ import annotations

import os
import signal
import sys
import tomllib
from pathlib import Path

from tests.container_marker import MARKER

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def configured(pyproject: Path = PYPROJECT) -> dict[str, object]:
    """pytest's settings in ``pyproject``."""
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]


HOST_GUARD: float = float(configured()["timeout"])
CONTAINER_GUARD: float = float(configured()["container_timeout"])
# Half its tier's guard: a call given it early in a test ends, and the test fails as any other
# test does, its teardown run, well before the guard would end the test's process. HANG_GUARD is
# the host tests', CONTAINER_HANG_GUARD the container tests'.
HANG_GUARD: float = HOST_GUARD / 2
CONTAINER_HANG_GUARD: float = CONTAINER_GUARD / 2

CONTAINER_TIMEOUT = "container_timeout"


def mark(items, container_guard: float) -> None:
    """Give every item that carries the container marker (tests/container_marker.py, which
    must have marked them) the guard ``container_guard``."""
    import pytest

    for item in items:
        if item.get_closest_marker(MARKER) is not None:
            item.add_marker(pytest.mark.timeout(container_guard))


def start_time(pid: int) -> int | None:
    """When ``pid`` started, in clock ticks since boot (field 22 of its stat), or None for one
    that is gone: with the pid, what names one process, since a pid is given again."""
    try:
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def _stats() -> dict[int, tuple[int, int]]:
    """Each process's parent and start time, by /proc now."""
    found: dict[int, tuple[int, int]] = {}
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                fields = Path(f"/proc/{entry}/stat").read_text().rsplit(")", 1)[1].split()
                found[int(entry)] = (int(fields[1]), int(fields[19]))
            except (OSError, ValueError, IndexError):
                continue
    return found


def descendants(pid: int) -> dict[int, int]:
    """Every process below ``pid``, by the parents /proc gives now, with its start time; not
    ``pid`` itself."""
    stats = _stats()
    children: dict[int, list[int]] = {}
    for child, (parent, _start) in stats.items():
        children.setdefault(parent, []).append(child)
    found: dict[int, int] = {}
    todo = list(children.get(pid, []))
    while todo:
        child = todo.pop()
        if child not in found:
            found[child] = stats[child][1]
            todo.extend(children.get(child, []))
    return found


# Looks at the processes below this one: one forked before its parent was stopped is found by
# the next look, and none is left to fork once a look finds nothing new.
KILL_PASSES = 5


def _signal(pidfd: int, signum: int) -> bool:
    """Whether ``signum`` reached the process, or it had ended and been reaped; a failure of
    another kind is said on stderr."""
    try:
        signal.pidfd_send_signal(pidfd, signum)
    except ProcessLookupError:  # ended and reaped since
        return True
    except OSError as error:  # one the user may not signal
        print(f"hang guard: signal {signum} not sent: {error}", file=sys.stderr)
        return False
    return True


def stop_descendants() -> dict[int, int]:
    """Stop every process below this one, and hold each by a pidfd, by pid, for ``kill``. Only
    this process's own: under pytest-xdist a worker's, never the controller's or another
    worker's. A pidfd is taken only while the process's start time is still the one the look
    found, so that a pid given again is never signalled; each process is stopped as it is
    found, and the processes below are looked at again until a look finds nothing new, up to
    KILL_PASSES looks, so that none forks past the kill. A stopped process stays where it was:
    a test waiting on one stays waiting while its stacks are taken. One the user may not signal
    is neither stopped nor held; the kernel lets a SIGKILL through wherever it let the SIGSTOP,
    so only one whose credentials changed in between is left stopped, and said. Nothing here
    waits."""
    held: dict[int, int] = {}
    passed: set[int] = set()  # ones the user may not signal, which are neither stopped nor killed
    try:
        for _ in range(KILL_PASSES):
            new = False
            for pid, start in descendants(os.getpid()).items():
                if pid in held or pid in passed:
                    continue
                try:
                    pidfd = os.pidfd_open(pid)
                except OSError:
                    continue  # gone since the look
                if start_time(pid) != start:  # another process's pid by now
                    os.close(pidfd)
                    continue
                new = True
                if _signal(pidfd, signal.SIGSTOP):
                    held[pid] = pidfd
                else:
                    passed.add(pid)
                    os.close(pidfd)
            if not new:
                break
    except BaseException:
        kill(held)
        raise
    return held


def kill(held: dict[int, int]) -> None:
    """SIGKILL every process ``stop_descendants`` holds, and let go of its pidfd."""
    try:
        for pidfd in held.values():
            _signal(pidfd, signal.SIGKILL)
    finally:
        for pidfd in held.values():
            os.close(pidfd)
