"""A real run of the suite, killed as a harness timeout or a closed terminal kills one.

Not a test module. KilledRun runs ``python -m pytest -n 2`` over tiny test files a test
writes, in a session of its own, with the environment the tests were started with
(tests/collected.py's child_env), the repository on PYTHONPATH and a TMPDIR of its own.
The tiny tests say what they made with report() and wait in hold() until the run is
killed with kill() or released with finish(). Every wait has a deadline, and the run
itself is ended by ``timeout`` past it, so a run whose test was itself killed ends too.
FirstThreadEnded runs a process whose first thread has ended while another runs, which
counts as running (held_folders.running).

Everything read from a report is checked before it is used: a report is read only
once it is complete, and a path in it only if it lies under the test's own tmp_path or
in /tmp under one of the test's own prefixes, which stem() gives. scrub(), the only
removal here, refuses anything else. The rules a killed run's leftovers follow are in
project-knowledge/tests.md, "What a killed run leaves".
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import IO

from devtools import held_folders as hf
from tests.collected import child_env

REPO = Path(__file__).resolve().parent.parent
# Seconds any wait on a killed run may take; the run itself is ended past it.
DEADLINE = 120.0
# What a stem's name begins with.
STEM = "rcks-"
ENV_REPORTS = "RCMAN_KILLED_RUN_REPORTS"
ENV_RELEASE = "RCMAN_KILLED_RUN_RELEASE"
ENV_DEADLINE = "RCMAN_KILLED_RUN_DEADLINE"
PARTIAL = ".partial"


class UnsafePath(ValueError):
    """A path read from a report, or given to scrub(), is not the test's own."""


# --- in the run: what the tiny tests call ---------------------------------------------


def report(name: str, data: dict) -> None:
    """Write ``data`` as the report ``name``: under another name first, then renamed, so
    a report is never seen part-written."""
    folder = Path(os.environ[ENV_REPORTS])
    partial = folder / f".{name}{PARTIAL}"
    partial.write_text(json.dumps(data), encoding="utf-8")
    os.rename(partial, folder / f"{name}.json")


def shell_reporter(folder: Path) -> str:
    """A shell function, ``report NAME JSON``, for a script a test plants, that writes
    as report() does."""
    q = shlex.quote(str(folder))
    return (
        "report() {\n"
        f'  printf "%s" "$2" > {q}/."$1"{PARTIAL} && mv -f {q}/."$1"{PARTIAL} {q}/"$1".json\n'
        "}\n"
    )


def hold() -> None:
    """Wait until the run is released, or its deadline passes."""
    release = Path(os.environ[ENV_RELEASE])
    deadline = float(os.environ[ENV_DEADLINE])
    while not release.exists():
        if time.time() > deadline:
            raise TimeoutError("the killed run was never killed or released")
        time.sleep(0.05)


def shell_hold() -> str:
    """Shell lines, for a script a test plants in a run, that wait as hold() does."""
    release = shlex.quote(os.environ[ENV_RELEASE])
    ticks = int(max(0.0, float(os.environ[ENV_DEADLINE]) - time.time()) * 10)
    return f"n=0\nwhile [ ! -e {release} ] && [ $n -lt {ticks} ]; do sleep 0.1; n=$((n+1)); done\n"


# --- outside the run: reading what it reported -------------------------------------------


def complete_report(path: Path) -> dict | None:
    """The report at ``path`` once it is complete: a regular file, not a link, holding a
    JSON object; None until then."""
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            return None
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def checked(text, prefixes: Iterable[str], tmp_path: Path, *, follow: bool = True) -> Path:
    """``text`` as a path, if it is absolute and resolves to strictly under
    ``tmp_path``, itself a folder a removal may take, or to in /tmp under a name that
    begins with one of ``prefixes`` and is longer than it; otherwise UnsafePath. With
    ``follow`` false only what holds the path is resolved, so a link there is taken as
    itself, as a removal takes it."""
    try:  # tmp_path itself must be one a removal could take, so never "/", "." or a home
        hf.removable(tmp_path)
    except hf.UnsafeRemoval as error:
        raise UnsafePath(f"{tmp_path!r} cannot be a test's tmp_path: {error}") from error
    prefixes = tuple(prefixes)
    for prefix in prefixes:
        if not isinstance(prefix, str) or not prefix or "/" in prefix:
            raise UnsafePath(f"{prefix!r} is not a prefix of names in /tmp")
    if not isinstance(text, str) or not text or not os.path.isabs(text):
        raise UnsafePath(f"{text!r} is not an absolute path")
    if any(part in (".", "..") for part in text.split("/")):
        raise UnsafePath(f"{text!r} holds . or ..")
    given = Path(text)
    if given.name == "":
        raise UnsafePath(f"{text!r} is /")
    real = Path(os.path.realpath(given if follow else given.parent))
    if not follow:
        real = real / given.name
    if Path(os.path.realpath(tmp_path)) in real.parents:
        return given
    tmp = Path(os.path.realpath(hf.TMP))
    if tmp in real.parents:
        name = real.relative_to(tmp).parts[0]
        if any(name.startswith(p) and len(name) > len(p) for p in prefixes):
            return given
    raise UnsafePath(
        f"{text!r} is neither under {tmp_path} nor named with {list(prefixes)} in {tmp}"
    )


class Reports:
    """The reports of one run, in ``folder``, whose paths must be the test's own."""

    def __init__(
        self, folder: Path, prefixes: Iterable[str], tmp_path: Path, alive=None, output=None
    ):
        self.folder = folder
        self.prefixes = tuple(prefixes)
        self.tmp_path = tmp_path
        self._alive = alive
        self._output = output

    def wait(self, name: str, deadline: float = DEADLINE) -> dict:
        """The report ``name`` once complete, with every path in it checked."""
        end = time.monotonic() + deadline
        while (data := complete_report(self.folder / f"{name}.json")) is None:
            if self._alive is not None and not self._alive():
                said = self._output() if self._output else ""
                raise RuntimeError(f"the run ended before reporting {name}:\n{said}")
            if time.monotonic() > end:
                raise TimeoutError(f"no complete report {name} within {deadline}s")
            time.sleep(0.05)
        for value in data.get("paths", {}).values():
            checked(value, self.prefixes, self.tmp_path)
        for pid in data.get("pids", {}).values():
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
                raise UnsafePath(f"{pid!r} is not a process of the run's")
        return data


# --- removing what a test made ---------------------------------------------------------


def scrub(text, prefixes: Iterable[str], tmp_path: Path) -> None:
    """Remove a path a test made, once checked as checked() checks one, with only what
    holds it resolved: a link or a file is unlinked, a folder removed with every tmux
    server of the user's in it stopped, and no link followed. held_folders' own checks
    apply besides."""
    path = checked(text, prefixes, tmp_path, follow=False)
    roots = (hf.TMP, tmp_path)
    if stat.S_ISDIR(path.lstat().st_mode):
        hf.remove_tree(path, roots=roots)
    else:
        os.unlink(hf.removable(path, roots=roots))


def _remove_stem(folder: Path) -> None:
    beside = folder.name + "-"
    for name in sorted(os.listdir(folder.parent)):
        if name.startswith(beside):
            scrub(str(folder.parent / name), (beside,), folder)
    hf.remove(folder)


@contextlib.contextmanager
def stem() -> Iterator[str]:
    """A prefix for everything a test makes in /tmp: a held folder's name and ``-``.
    The folder is removed afterwards with everything whose name the prefix begins; a
    killed test's are cleared by the next stem."""
    with hf.held(STEM, remover=_remove_stem) as folder:
        yield folder.name + "-"


# --- a process whose first thread has ended ---------------------------------------------

# Run with ``python -I``: a second thread waits on the read end the first argument names, and
# the first thread then ends, as a multi-threaded process's first thread can while the others
# are still exiting.
_FIRST_THREAD_ENDS = """\
import ctypes, os, sys, threading
threading.Thread(target=os.read, args=(int(sys.argv[1]), 1)).start()
ctypes.CDLL(None).pthread_exit(None)
"""

# How a process reads (held_folders.state_and_threads) once its first thread has ended while
# another thread runs, and once all its threads have ended.
FIRST_THREAD_ENDED = ("Z", 2)
ENDED = ("Z", 1)


def wait_for_state(pid: int, wanted: tuple[str, int], deadline: float = DEADLINE) -> None:
    """Wait until ``pid`` reads ``wanted`` (held_folders.state_and_threads)."""
    end = time.monotonic() + deadline
    while (found := hf.state_and_threads(pid)) != wanted:
        if time.monotonic() > end:
            raise TimeoutError(f"process {pid} reads {found}, not {wanted}, after {deadline}s")
        time.sleep(0.02)


class FirstThreadEnded:
    """A process whose first thread has ended while its second waits: its stat reads
    FIRST_THREAD_ENDED, and it holds its open files, ``holding`` among them, until release()
    lets the second thread end too. ``argv`` runs it, with ``read_end`` given to it as that same
    descriptor; start() runs it in a session of its own, closes this process's ``holding``, and
    waits until its first thread has ended. Leaving the ``with`` releases it, and ends and reaps
    what start() ran."""

    def __init__(self, tmp_path: Path, holding: IO | None = None):
        script = tmp_path / "first_thread_ends.py"
        script.write_text(_FIRST_THREAD_ENDS, encoding="utf-8")
        self.read_end, self._write_end = os.pipe()
        self.argv = [sys.executable, "-I", str(script), str(self.read_end)]
        self._holding = holding
        self.proc: subprocess.Popen | None = None

    def __enter__(self) -> FirstThreadEnded:
        return self

    def __exit__(self, *exc) -> None:
        self.release()
        os.close(self.read_end)
        if self._holding is not None:
            self._holding.close()
        if self.proc is not None:
            try:
                self.proc.communicate(timeout=DEADLINE)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.communicate()

    def start(self) -> int:
        """Run it in a session of its own; its pid, once its first thread has ended. One that
        ends instead fails at once, with what it said."""
        kept = () if self._holding is None else (self._holding.fileno(),)
        self.proc = subprocess.Popen(
            self.argv,
            pass_fds=(self.read_end, *kept),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        if self._holding is not None:
            self._holding.close()  # the process holds it now
            self._holding = None
        end = time.monotonic() + DEADLINE
        while (found := hf.state_and_threads(self.proc.pid)) != FIRST_THREAD_ENDED:
            if found in (None, ENDED):
                said = self.proc.communicate()[1].decode(errors="replace")
                code = self.proc.returncode
                raise RuntimeError(f"the process ended instead, with {code}:\n{said}")
            if time.monotonic() > end:
                raise TimeoutError(f"process {self.proc.pid} reads {found} after {DEADLINE}s")
            time.sleep(0.02)
        return self.proc.pid

    def release(self) -> None:
        """Let its second thread end, by closing the pipe's write end."""
        if self._write_end is not None:
            os.close(self._write_end)
            self._write_end = None


# --- the run -----------------------------------------------------------------------------


# Whether a process has not ended (held_folders.running).
running = hf.running


def _in_session(sid: int) -> list[int]:
    """The processes of session ``sid`` that have not ended (held_folders.runs)."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            fields = Path(f"/proc/{entry}/stat").read_text().rsplit(")", 1)[1].split()
        except OSError:
            continue
        if int(fields[3]) == sid and hf.runs(*hf.state_and_threads_of(fields)):
            found.append(int(entry))
    return found


class KilledRun:
    """``python -m pytest -n 2`` over ``tests`` ({file name: source}), started at once in
    a session of its own under ``tmp_path``; paths its reports name must be under
    ``tmp_path`` or begin with one of ``prefixes`` in /tmp."""

    def __init__(
        self,
        tmp_path: Path,
        tests: dict[str, str],
        prefixes: Iterable[str],
        env: dict[str, str] | None = None,
    ):
        self.base = tmp_path / "killed-run"
        self.inner = self.base / "inner"
        self.tmpdir = self.base / "tmp"
        reports = self.base / "reports"
        self.release = self.base / "release"
        for folder in (self.inner, self.tmpdir, reports):
            folder.mkdir(parents=True)
        for name, source in tests.items():
            (self.inner / name).write_text(source, encoding="utf-8")
        ini = self.base / "pytest.ini"
        ini.write_text("[pytest]\n", encoding="utf-8")
        self.output = self.base / "output.txt"
        self.deadline = time.time() + DEADLINE
        run_env = child_env(
            {
                "PYTHONPATH": str(REPO),
                "TMPDIR": str(self.tmpdir),
                ENV_REPORTS: str(reports),
                ENV_RELEASE: str(self.release),
                ENV_DEADLINE: str(self.deadline),
                **(env or {}),
            }
        )
        argv = [
            "timeout", "-s", "KILL", str(int(DEADLINE) + 30),
            sys.executable, "-m", "pytest", "-n", "2", "-p", "no:cacheprovider", "-q",
            "-c", str(ini), "--rootdir", str(self.base),
            "--basetemp", str(self.base / "basetemp"), str(self.inner),
        ]  # fmt: skip
        with open(self.output, "w", encoding="utf-8") as out:
            self.proc = subprocess.Popen(
                argv,
                cwd=self.base,
                env=run_env,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self.reports = Reports(reports, prefixes, tmp_path, alive=self.alive, output=self.text)

    def __enter__(self) -> KilledRun:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def alive(self) -> bool:
        return self.proc.poll() is None

    def _gone(self, until: float) -> None:
        """Wait for the run and every process of its session to end."""
        try:
            self.proc.wait(timeout=max(0.0, until - time.monotonic()))
        except subprocess.TimeoutExpired as error:
            raise TimeoutError(f"the run is still running:\n{self.text()}") from error
        while left := _in_session(self.proc.pid):
            if time.monotonic() > until:
                raise TimeoutError(f"the run's processes {left} are still running")
            time.sleep(0.05)

    def kill(self, sig: int) -> None:
        """Send ``sig`` to the run's process group, and to anything else in its session,
        as a harness or a closed terminal would; wait until all of it has gone."""
        with contextlib.suppress(ProcessLookupError):
            os.killpg(self.proc.pid, sig)
        for pid in _in_session(self.proc.pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, sig)
        self._gone(time.monotonic() + DEADLINE)

    def finish(self) -> tuple[int, str]:
        """Release the run and wait for it to end; its exit status and output."""
        self.release.touch()
        self._gone(time.monotonic() + DEADLINE)
        return self.proc.returncode, self.text()

    def text(self) -> str:
        return self.output.read_text(encoding="utf-8", errors="replace")

    def close(self) -> None:
        """End the run if it still runs."""
        if self.alive() or _in_session(self.proc.pid):
            self.kill(signal.SIGKILL)
