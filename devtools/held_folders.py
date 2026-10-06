"""Folders a run makes under /tmp and holds while it uses them, and the clearing of those
a run killed by a signal left behind.

Shared by the tests and by devtools/record_claude.py, so it lives here: devtools never
imports the tests. The rules it keeps — how a folder is held, what a later run clears
and what it leaves alone — are in project-knowledge/tests.md, "What a killed run
leaves". Every removal here refuses anything but a path strictly inside /tmp, or
inside a root its caller names, and never the repository, the home folder, the
working folder or what holds them.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# Where held folders are made, and the only place a removal reaches by default.
TMP = Path("/tmp")
LOCK = ".lock"
LOCKING = ".locking"
# Seconds a folder may be without its lock file: one is, only in the instants between
# making it and taking its lock, and between emptying it and removing it.
UNLOCKED_FOR = 60
# Seconds to wait for a stopped tmux server to go.
TMUX_GONE_WITHIN = 10
# What mkdtemp adds to a prefix: eight of these.
_MKDTEMP_TAIL = r"[a-z0-9_]{8}"


class UnsafeRemoval(ValueError):
    """A removal was asked for something no cleanup may remove."""


def _guarded() -> list[tuple[Path, bool]]:
    """What no removal may reach, nor anything holding it, each with whether what is
    inside it is out of reach too."""
    guarded = [(Path(os.path.realpath(p)), True) for p in (Path.home(), REPO)]
    with contextlib.suppress(OSError):
        guarded.append((Path(os.path.realpath(os.getcwd())), False))
    return guarded


def removable(path: Path | str, roots: Iterable[Path] | None = None) -> Path:
    """``path``, if a cleanup may remove it; otherwise UnsafeRemoval.

    It must be absolute, without ``.`` or ``..``, and, with what holds it resolved,
    strictly inside one of ``roots`` (by default /tmp), and neither the repository,
    the home folder, the working folder nor ``/``, nor inside or holding any of them.
    The path itself is not resolved: a link is removed, never followed."""
    text = os.fspath(path)
    if not text or not os.path.isabs(text):
        raise UnsafeRemoval(f"refusing to remove {text!r}: not an absolute path")
    if any(part in (".", "..") for part in text.split("/")):
        raise UnsafeRemoval(f"refusing to remove {text!r}: it holds . or ..")
    given = Path(text)
    if given.name == "":
        raise UnsafeRemoval(f"refusing to remove {text!r}: it is /")
    real = Path(os.path.realpath(given.parent)) / given.name
    for guarded, with_inside in _guarded():
        if real == guarded or real in guarded.parents or (with_inside and guarded in real.parents):
            raise UnsafeRemoval(f"refusing to remove {text!r}: it is, holds or is in {guarded}")
    allowed = [Path(os.path.realpath(root)) for root in (roots or (TMP,))]
    if not any(root in real.parents for root in allowed):
        raise UnsafeRemoval(f"refusing to remove {text!r}: not inside {allowed}")
    return given


def own(path: Path, kind: Callable[[int], bool], uid: int | None = None) -> bool:
    """Whether the path itself, not what a link there points at, is of the kind (a
    stat.S_IS* test) and belongs to ``uid``, by default the current user."""
    try:
        status = path.lstat()
    except OSError:
        return False
    return kind(status.st_mode) and status.st_uid == (os.getuid() if uid is None else uid)


def _stop_server(sock: Path) -> None:
    tmux = ["tmux", "-S", str(sock)]
    pid = subprocess.run(
        [*tmux, "display-message", "-p", "#{pid}"], capture_output=True, text=True
    ).stdout.strip()
    subprocess.run([*tmux, "kill-server"], capture_output=True)
    # kill-server returns before the server has gone.
    deadline = time.monotonic() + TMUX_GONE_WITHIN
    while pid and Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
        time.sleep(0.05)


def stop_tmux(folder: Path) -> None:
    """Stop the tmux server on each socket in ``folder`` itself. tmux detaches from
    whatever started it, so a run that ends leaves it running. Only a socket of the
    current user's, in a folder of theirs: a link could name any tmux server."""
    if not own(folder, stat.S_ISDIR):
        return
    for sock in sorted(folder.glob("*.sock")):
        if own(sock, stat.S_ISSOCK):
            _stop_server(sock)


def lock(folder: Path):
    """The folder's lock, held while the returned file is open. It is taken under
    another name and then renamed, so a lock file is held from the moment it appears."""
    taking = folder / LOCKING
    held_file = open(taking, "x")  # noqa: SIM115 -- held for as long as the folder is used
    fcntl.flock(held_file, fcntl.LOCK_EX)
    taking.rename(folder / LOCK)
    return held_file


def remove(
    folder: Path,
    stop: Callable[[Path], None] | None = None,
    roots: Iterable[Path] | None = None,
) -> None:
    """Stop the folder's tmux servers with ``stop`` (by default stop_tmux) and remove
    it, its lock file last, so a run killed while removing it leaves a folder with a
    lock file nothing holds. A link is refused, never followed."""
    folder = removable(folder, roots)
    if not stat.S_ISDIR(folder.lstat().st_mode):
        raise UnsafeRemoval(f"refusing to remove {folder}: not a folder")
    (stop if stop is not None else stop_tmux)(folder)
    for entry in folder.iterdir():
        if entry.name == LOCK:
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    (folder / LOCK).unlink(missing_ok=True)
    folder.rmdir()


def remove_tree(folder: Path, roots: Iterable[Path] | None = None) -> None:
    """remove(), for a folder tests may have filled with anything: every tmux server on
    a socket of the current user's anywhere in it is stopped, and every folder of
    theirs in it made theirs to remove. No link is followed."""
    folder = removable(folder, roots)
    if own(folder, stat.S_ISDIR):
        os.chmod(folder, 0o700)
    for top, dirs, files in os.walk(folder):  # top down: opened before it is listed
        for name in dirs:
            if own(Path(top, name), stat.S_ISDIR):
                os.chmod(Path(top, name), 0o700)
        for name in files:
            if own(Path(top, name), stat.S_ISSOCK):
                _stop_server(Path(top, name))
    remove(folder, stop=lambda _: None, roots=roots)


def made_with(prefix: str) -> Callable[[str], bool]:
    """Whether a name is one mkdtemp made with ``prefix``: the prefix and exactly the
    eight characters mkdtemp adds, so that a longer prefix beginning with this one, or
    a name made some other way, is never taken for it."""
    pattern = re.compile(re.escape(prefix) + _MKDTEMP_TAIL)
    return lambda name: pattern.fullmatch(name) is not None


def unheld(folder: Path, uid: int | None = None) -> bool:
    """Whether nothing holds ``folder``: it is gone, its lock file is one no one holds,
    or it has been without one longer than UNLOCKED_FOR. A folder, or a lock file, that
    is not the user's own is taken as held."""
    uid = os.getuid() if uid is None else uid
    try:
        folder.lstat()
    except FileNotFoundError:
        return True
    if not own(folder, stat.S_ISDIR, uid):
        return False
    try:  # neither following a link nor waiting on a FIFO
        fd = os.open(folder / LOCK, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return time.time() - folder.lstat().st_mtime > UNLOCKED_FOR
    except OSError:
        return False
    try:
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode) or status.st_uid != uid:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True
    finally:
        os.close(fd)


def clear_abandoned(
    parent: Path,
    chosen: Callable[[str], bool],
    *,
    uid: int | None = None,
    remover: Callable[[Path], None] | None = None,
    label: str,
) -> None:
    """Stop and remove every folder in ``parent`` whose name is ``chosen`` that a killed
    run of ``uid``, by default the current user, left, with ``remover`` (by default
    remove); leave everything else, and go on past what cannot be read or removed, or
    what a removal refuses, saying so after ``label``. A folder another clearing run
    removed first is passed over in silence."""
    uid = os.getuid() if uid is None else uid
    for name in sorted(os.listdir(parent)):
        if not chosen(name):
            continue
        folder = parent / name
        try:
            _clear(folder, uid, remover if remover is not None else remove)
        except FileNotFoundError:
            pass  # another run cleared it meanwhile
        except UnsafeRemoval as error:
            print(f"{label}: left {folder}: refused: {error}", file=sys.stderr)
        except OSError as error:
            print(f"{label}: left {folder}: {error}", file=sys.stderr)


def _clear(folder: Path, uid: int, remover: Callable[[Path], None]) -> None:
    if not own(folder, stat.S_ISDIR, uid):
        return  # a link, a file, or someone else's
    try:  # neither following a link nor waiting on a FIFO
        fd = os.open(folder / LOCK, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        if time.time() - folder.lstat().st_mtime > UNLOCKED_FOR:
            remover(folder)  # killed between making it and taking its lock
        return
    try:
        status = os.fstat(fd)
        if not stat.S_ISREG(status.st_mode) or status.st_uid != uid:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return  # a running run's
        try:  # another run may have cleared it, and a new one made, since the open
            if (folder / LOCK).lstat().st_ino != status.st_ino:
                return
        except FileNotFoundError:
            return
        remover(folder)  # under its lock, which no one else can take meanwhile
    finally:
        os.close(fd)


@contextlib.contextmanager
def held(prefix: str, remover: Callable[[Path], None] | None = None) -> Iterator[Path]:
    """A folder of its own under /tmp, named with ``prefix``, locked while in use and
    removed afterwards with ``remover`` (by default remove). Before it is given, the
    folders of the same prefix that a killed run left are cleared with ``remover``."""
    folder = Path(tempfile.mkdtemp(prefix=prefix, dir="/tmp"))
    held_file = None
    try:
        held_file = lock(folder)
        clear_abandoned(
            folder.parent, made_with(prefix), remover=remover, label=f"held_folders {prefix}"
        )
        yield folder
    finally:
        try:
            (remover if remover is not None else remove)(folder)
        finally:
            if held_file is not None:
                held_file.close()


@contextlib.contextmanager
def tmux_sockets(prefix: str) -> Iterator[Path]:
    """A held folder for a test's tmux sockets, named with ``prefix``.

    Under /tmp rather than under tmp_path because a unix socket's path has to stay
    short, and never named after tmp_path, whose name pytest-xdist's workers repeat.
    Afterwards every tmux server with a socket in it is stopped and the folder removed;
    one a killed run left is stopped and removed by the next run of the same prefix."""
    with held(prefix) as folder:
        yield folder
