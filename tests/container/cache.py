"""What the container tests would otherwise fetch from the network every session, kept between
runs in the user's cache, beside the toolchain (the ``toolchain`` fixture): the images the
session's fixtures build (an archive ``podman save`` writes, named by the image's own
content-hashed name), the tools the installer builds (named by the build's argument list and its
source's sha256), and the install test's offline npm and uv caches (named by the lockfiles and
what fills them). The first run, and the first after any of those changes, fills an entry with
the network; every later one loads it.

Not a test module. Every entry is filled or loaded under one lock, ``cache.lock``. An entry is
written beside its place as ``<name>.part`` and renamed into it, so a killed run leaves at most a
``.part``, which the next fill of its kind removes; filling an entry removes the other entries of
its kind, which a changed key left; and an archive Podman cannot load, a tool whose bytes are
not those it was stored with, or a folder without the mark its fill ends with, is removed and
filled again.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path

from tests.hang_guard import CONTAINER_HANG_GUARD

ROOT = Path.home() / ".cache" / "rcman-test"
IMAGES, TOOLS, OFFLINE = ROOT / "images", ROOT / "tools", ROOT / "offline"
PART = ".part"
# What a folder entry holds once it is whole.
MARK = ".filled"


@contextlib.contextmanager
def locked() -> Iterator[None]:
    """The cache's one lock, held while an entry is filled or loaded."""
    ROOT.mkdir(parents=True, exist_ok=True)
    with open(ROOT / "cache.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def key(*parts: bytes | str) -> str:
    """A short digest of ``parts``, each kept apart from the next by its length."""
    digest = hashlib.sha256()
    for part in parts:
        data = part.encode() if isinstance(part, str) else part
        digest.update(len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()[:16]


def _replaced(folder: Path, kind: str, keep: Path) -> None:
    """``keep``, written as its ``.part``, put in place; then every other entry of ``kind`` (a
    name starting so) in ``folder`` removed, with every ``.part`` left there."""
    os.replace(keep.with_name(keep.name + PART), keep)
    for other in folder.iterdir():
        if other != keep and (other.name.startswith(kind) or other.name.endswith(PART)):
            _remove(other)


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _clear_parts(folder: Path) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    for left in folder.glob("*" + PART):
        _remove(left)


def image(engine, name: str, build: Callable[[], None]) -> str:
    """The image ``name`` on ``engine``: loaded from its archive, or built by ``build`` and its
    archive saved; ``name``, which is tagged after its build files' content, names the archive,
    and an archive of the same repository but another tag is removed."""
    repository, _, tag = name.rpartition(":")
    kind = repository.replace("/", "_") + "@"
    archive = IMAGES / f"{kind}{tag}.tar"
    with locked():
        if archive.exists():
            loaded = engine.cli("load", "-i", str(archive), timeout=CONTAINER_HANG_GUARD)
            if loaded.returncode == 0 and engine.cli("image", "exists", name).returncode == 0:
                return name
            archive.unlink()  # not an archive Podman loads: filled again
        _clear_parts(IMAGES)
        build()
        part = archive.with_name(archive.name + PART)
        saved = engine.cli("save", "-o", str(part), name, timeout=CONTAINER_HANG_GUARD)
        assert saved.returncode == 0, saved.stderr
        _replaced(IMAGES, kind, archive)
    return name


def tool(name: str, argv: list[str], source: Path, build: Callable[[], bytes]) -> bytes:
    """The program ``name`` built with ``argv`` from ``source``: the stored one, or what
    ``build`` gives, stored. Its entry is named by ``argv`` and ``source``'s sha256, and by the
    program's own sha256, which a stored one must still have."""
    kind = f"{name}-{key(*argv, hashlib.sha256(source.read_bytes()).hexdigest())}-"
    with locked():
        for found in TOOLS.glob(kind + "*") if TOOLS.exists() else ():
            if found.name.endswith(PART):
                continue
            program = found.read_bytes()
            if hashlib.sha256(program).hexdigest() == found.name.removeprefix(kind):
                return program
            found.unlink()  # damaged: built again
        _clear_parts(TOOLS)
        program = build()
        entry = TOOLS / (kind + hashlib.sha256(program).hexdigest())
        entry.with_name(entry.name + PART).write_bytes(program)
        _replaced(TOOLS, f"{name}-", entry)
    return program


def folder(kind: str, entry_key: str, fill: Callable[[Path], None], into: Path) -> None:
    """The folder of ``kind`` named by ``entry_key`` copied into ``into``, under the lock: the
    stored one, or a new one ``fill`` fills, stored. One without its mark, which is written once
    ``fill`` is done, is removed and filled again."""
    entry = OFFLINE / f"{kind}-{entry_key}"
    with locked():
        if not (entry / MARK).is_file():
            _remove(entry)
            _clear_parts(OFFLINE)
            part = entry.with_name(entry.name + PART)
            part.mkdir(mode=0o755)
            fill(part)
            (part / MARK).touch()
            _replaced(OFFLINE, f"{kind}-", entry)
        shutil.copytree(
            entry, into, symlinks=True, dirs_exist_ok=True,
            ignore=lambda where, _: [MARK] if where == str(entry) else [],  # the top's mark alone
        )  # fmt: skip
