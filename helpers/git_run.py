"""Running git for rc-helper: finding it, starting and ending it, and reading what it writes.

Mechanics only. git is looked for on the system path, never on the person's
own. Every call has a deadline; a call past it, or one whose output is left
unread, ends git and everything it started, hooks and remote helpers among
them, since each git runs in a process group of its own. What rc-helper
makes of a refusal is rc-helper's: this module raises its own exceptions.

Python standard library only: it runs under the system interpreter.
"""

from __future__ import annotations

import contextlib
import os
import re
import selectors
import shutil
import signal
import subprocess
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

GIT_SEARCH_PATH = "/usr/local/bin:/usr/bin:/bin"
# How much of what git says when it refuses is passed on; the rest is counted.
MAX_GIT_MESSAGE_LINES = 40
MAX_GIT_LINE_LENGTH = 500
# The oldest git that has everything rc-helper runs: rev-parse --path-format.
GIT_MIN_VERSION = (2, 31)
_READ_SIZE = 65536

# What a file's change is, by the letter git's porcelain status gives it.
LETTER_CHANGES = {
    "M": "modified",
    "T": "type_changed",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
}
# Every change rc-helper reports a file with.
CHANGES = (*LETTER_CHANGES.values(), "unmerged", "untracked")
# for-each-ref's format for parse_branches: name, symbolic target, upstream, current.
BRANCH_FORMAT = "%(refname)%00%(symref)%00%(upstream)%00%(HEAD)"


class GitMissing(Exception):
    """There is no git on the system path."""


class GitFailed(Exception):
    """git refused: what it said, at most MAX_GIT_MESSAGE_LINES lines, and how many more."""

    def __init__(self, lines: list[str], more: int, exit_code: int):
        super().__init__(f"git exited with {exit_code}")
        self.lines, self.more, self.exit_code = lines, more, exit_code


class GitTimedOut(Exception):
    """git was still running at the deadline, and was ended."""


class GitUnrecognised(Exception):
    """git wrote something this module does not know how to read."""

    def __init__(self, record: bytes | str):
        text = record.decode("utf-8", "replace") if isinstance(record, bytes) else record
        super().__init__(text[:MAX_GIT_LINE_LENGTH])
        self.lines = [text[:MAX_GIT_LINE_LENGTH]]


def find_git() -> str:
    found = shutil.which("git", path=GIT_SEARCH_PATH)
    if found is None:
        raise GitMissing()
    return found


def version_of(text: str) -> tuple[int, int]:
    """The major and minor version in ``git --version``'s answer."""
    found = re.match(r"git version (\d+)\.(\d+)", text)
    if not found:
        raise GitUnrecognised(text)
    return int(found.group(1)), int(found.group(2))


class _Message:
    """What git says, kept up to MAX_GIT_MESSAGE_LINES lines and counted beyond."""

    def __init__(self):
        self.lines: list[str] = []
        self.more = 0
        self._partial = b""

    def feed(self, data: bytes) -> None:
        data = self._partial + data
        *complete, self._partial = data.split(b"\n")
        for line in complete:
            self._add(line)
        if len(self._partial) > MAX_GIT_LINE_LENGTH * 4:  # a line too long to keep whole
            self._partial = self._partial[: MAX_GIT_LINE_LENGTH * 4]

    def _add(self, line: bytes) -> None:
        if len(self.lines) < MAX_GIT_MESSAGE_LINES:
            text = line.decode("utf-8", "replace").rstrip("\r")
            self.lines.append(text[:MAX_GIT_LINE_LENGTH])
        else:
            self.more += 1

    def close(self) -> None:
        if self._partial:
            self._add(self._partial)
            self._partial = b""


class Git:
    """git, started with ``env`` and nothing else in its environment."""

    def __init__(self, env: dict[str, str], popen=subprocess.Popen):
        self.env = env
        self.popen = popen

    def stream(
        self,
        cwd: Path,
        args: list[str],
        *,
        sep: bytes,
        deadline: float,
        input: bytes | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> Iterator[bytes]:
        """What git writes, as records ending in ``sep``, while it runs.

        Closing the iterator before the end ends git. A refusal is raised once
        git has ended, after the records it wrote.
        """
        pending = b""
        for chunk in self._process(cwd, args, deadline, input, extra_env, False):
            pending += chunk
            *records, pending = pending.split(sep)
            yield from records
        if pending:
            yield pending

    def run(
        self,
        cwd: Path,
        args: list[str],
        *,
        deadline: float,
        input: bytes | None = None,
        extra_env: dict[str, str] | None = None,
        secret_output: bool = False,
    ) -> str:
        """What git writes on standard output, once it has ended without refusing.

        With ``secret_output``, what git writes on standard output, which may
        hold a secret, is left out of a refusal: it carries git's standard
        error only.
        """
        out = b"".join(self._process(cwd, args, deadline, input, extra_env, secret_output))
        return out.decode("utf-8", "surrogateescape")

    def _process(self, cwd, args, deadline, input, extra_env, secret_output) -> Iterator[bytes]:
        """The one place git is started: its output as it comes, then its refusal, if any.

        stdin, stdout and stderr are served by one loop against the deadline.
        git runs in a session of its own; at the deadline, or when the caller
        stops reading, or on any exception (SIGTERM among them, which rc-helper
        turns into one), the whole process group is killed.
        """
        argv = [find_git(), *args]
        env = {**self.env, **(extra_env or {})}
        proc = self.popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        message = _Message()
        kept = _Message()  # standard output, which is part of a refusal too
        try:
            with selectors.DefaultSelector() as selector:
                to_write = memoryview(input or b"")
                if to_write:
                    os.set_blocking(proc.stdin.fileno(), False)
                    selector.register(proc.stdin, selectors.EVENT_WRITE)
                else:
                    proc.stdin.close()
                selector.register(proc.stdout, selectors.EVENT_READ)
                selector.register(proc.stderr, selectors.EVENT_READ)
                while len(selector.get_map()) > 0:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise GitTimedOut()
                    for key, _ in selector.select(left):
                        if key.fileobj is proc.stdin:
                            try:
                                written = os.write(proc.stdin.fileno(), to_write[:_READ_SIZE])
                            except BrokenPipeError:
                                written = len(to_write)
                            to_write = to_write[written:]
                            if not to_write:
                                selector.unregister(proc.stdin)
                                proc.stdin.close()
                            continue
                        data = os.read(key.fileobj.fileno(), _READ_SIZE)
                        if not data:
                            selector.unregister(key.fileobj)
                        elif key.fileobj is proc.stderr:
                            message.feed(data)
                        else:
                            if not secret_output:
                                kept.feed(data)
                            yield data
            left = deadline - time.monotonic()
            try:
                code = proc.wait(timeout=max(left, 0))
            except subprocess.TimeoutExpired:
                raise GitTimedOut() from None
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            for pipe in (proc.stdin, proc.stdout, proc.stderr):
                with contextlib.suppress(OSError):
                    pipe.close()
        if code != 0:
            message.close()
            kept.close()
            lines = message.lines + kept.lines
            more = message.more + kept.more
            shown = lines[:MAX_GIT_MESSAGE_LINES]
            raise GitFailed(shown, more + len(lines) - len(shown), code)


# --- what git writes ------------------------------------------------------------


@dataclass
class Branch:
    """The branch a status is on, from its header records."""

    name: str | None = None  # None while HEAD is detached
    commit: str | None = None  # None before the first commit
    upstream: str | None = None
    ahead: int | None = None
    behind: int | None = None


@dataclass(frozen=True)
class Change:
    """One file in one of the two lists: staged, or not staged."""

    path: bytes
    change: str
    staged: bool
    origin: bytes | None = None  # where a renamed or copied file came from
    submodule: bool = False


def _letter(letter: str, record: bytes) -> str:
    if letter not in LETTER_CHANGES:
        raise GitUnrecognised(record)
    return LETTER_CHANGES[letter]


def _header(record: bytes, branch: Branch) -> None:
    key, _, value = record[2:].decode("utf-8", "replace").partition(" ")
    if key == "branch.oid":
        branch.commit = None if value == "(initial)" else value
    elif key == "branch.head":
        branch.name = None if value == "(detached)" else value
    elif key == "branch.upstream":
        branch.upstream = value
    elif key == "branch.ab":
        found = re.fullmatch(r"\+(\d+) -(\d+)", value)
        if not found:
            raise GitUnrecognised(record)
        branch.ahead, branch.behind = int(found.group(1)), int(found.group(2))
    # Other headers, such as a stash's, are git's to add; they are not read.


def parse_status(records: Iterable[bytes], branch: Branch) -> Iterator[Change]:
    """The changes in ``git status --porcelain=v2 -z --branch``'s records.

    The header records fill in ``branch``. A file changed both in the index
    and in the folder is a change in each list.
    """
    records = iter(records)
    for record in records:
        kind = record[:2]
        if record.startswith(b"# "):
            _header(record, branch)
            continue
        if kind == b"? ":
            yield Change(record[2:], "untracked", staged=False)
            continue
        fields_by_kind = {b"1 ": 8, b"2 ": 9, b"u ": 10}
        if kind not in fields_by_kind:
            raise GitUnrecognised(record)
        fields = record.split(b" ", fields_by_kind[kind])
        if len(fields) != fields_by_kind[kind] + 1 or len(fields[1]) != 2:
            raise GitUnrecognised(record)
        path, submodule = fields[-1], fields[2].startswith(b"S")
        x, y = fields[1].decode("ascii", "replace")
        if kind == b"u ":
            yield Change(path, "unmerged", staged=False, submodule=submodule)
            continue
        origin = None
        if kind == b"2 ":
            origin = next(records, None)
            if origin is None or x not in "RC":
                raise GitUnrecognised(record)
        if x != ".":
            yield Change(path, _letter(x, record), True, origin, submodule)
        if y != ".":
            yield Change(path, _letter(y, record), False, None, submodule)


@dataclass(frozen=True)
class Ref:
    """A branch: local, or remote-tracking (``remote``), by its short name."""

    remote: bool
    name: str
    current: bool
    upstream: str | None  # the full name of the branch it tracks


def parse_branches(records: Iterable[bytes]) -> Iterator[Ref]:
    """The branches in ``for-each-ref --format=BRANCH_FORMAT refs/heads refs/remotes``'s
    lines; symbolic refs, such as ``origin/HEAD``, are left out."""
    for record in records:
        fields = record.decode("utf-8", "replace").split("\0")
        if len(fields) != 4:
            raise GitUnrecognised(record)
        refname, symref, upstream, head = fields
        if symref:
            continue
        for prefix, remote in (("refs/heads/", False), ("refs/remotes/", True)):
            if refname.startswith(prefix):
                yield Ref(remote, refname[len(prefix) :], head == "*", upstream or None)
                break
        else:
            raise GitUnrecognised(record)
