"""Reading what git writes, and what it says when it refuses.

Mechanics only: git is run elsewhere, in a workspace, through its runner
(backend/rcman/repository.py); this module reads the records it writes and
builds a refusal from a command's result. What a caller makes of a refusal is
its own: this module raises its own exceptions.

Python standard library only.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

# How much of what git says when it refuses is passed on; the rest is counted.
MAX_GIT_MESSAGE_LINES = 40
MAX_GIT_LINE_LENGTH = 500
# The oldest git that has everything run through this module: rev-parse --path-format.
GIT_MIN_VERSION = (2, 31)

# What a file's change is, by the letter git's porcelain status gives it.
LETTER_CHANGES = {
    "M": "modified",
    "T": "type_changed",
    "A": "added",
    "D": "deleted",
    "R": "renamed",
    "C": "copied",
}
# Every change a file is reported with.
CHANGES = (*LETTER_CHANGES.values(), "unmerged", "untracked")
# for-each-ref's format for parse_branches: name, symbolic target, upstream, current.
BRANCH_FORMAT = "%(refname)%00%(symref)%00%(upstream)%00%(HEAD)"


class GitFailed(Exception):
    """git refused: what it said, at most MAX_GIT_MESSAGE_LINES lines, and how many more."""

    def __init__(self, lines: list[str], more: int, exit_code: int):
        super().__init__(f"git exited with {exit_code}")
        self.lines, self.more, self.exit_code = lines, more, exit_code


class GitUnrecognised(Exception):
    """git wrote something this module does not know how to read."""

    def __init__(self, record: bytes | str):
        text = record.decode("utf-8", "replace") if isinstance(record, bytes) else record
        super().__init__(text[:MAX_GIT_LINE_LENGTH])
        self.lines = [text[:MAX_GIT_LINE_LENGTH]]


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


def failure(code: int, out: bytes, err: bytes) -> GitFailed:
    """The refusal of a git that ended with ``code``: what it said on standard error and
    then on standard output, at most MAX_GIT_MESSAGE_LINES lines in all, and how many more."""
    said, written = _Message(), _Message()
    said.feed(err)
    said.close()
    written.feed(out)
    written.close()
    lines = said.lines + written.lines
    shown = lines[:MAX_GIT_MESSAGE_LINES]
    return GitFailed(shown, said.more + written.more + len(lines) - len(shown), code)


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
