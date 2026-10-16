"""The fake claude as a Claude Code release, laid out as the installer downloads one.

Not a test module. ``release`` writes ``<version>/manifest.json`` and
``<version>/<platform>/claude`` under a folder, and the folder's ``latest``, which the
container suite serves over HTTP and gives the installer, and rcman, as the download
address: they download the fake and check it against the manifest as they do the real
one. A release is ``good`` (the fake, reporting the version it is released as),
``broken`` (a program that reports another version and nothing else Claude does) or
``failing`` (the fake, but the manifest names another checksum).

The binary is one file, as Claude Code's is: a shell script holding the fake and
what it reads, which unpacks itself under ``$TMPDIR`` (or the system's temporary
folder) the first time it runs, and runs it with the image's python3.
"""

from __future__ import annotations

import base64
import contextlib
import functools
import hashlib
import http.server
import io
import json
import tarfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
# What the fake reads: itself, the recorder it replays from, and the recordings.
PARTS = (
    "devtools/__init__.py",
    "devtools/fake_claude.py",
    "devtools/record_claude.py",
    "devtools/tmuxctl.py",
    "devtools/held_folders.py",
    "helpers/__init__.py",
    "helpers/claude_checks.py",
    "helpers/screens.py",
    "tests/fakes/claude",
    "tests/fakes/fixtures",
    "tests/fakes/observed",
)
MARK = "__BUNDLE__"
KINDS = ("good", "broken", "failing")
# What a ``broken`` release reports for its version: not the one it is released as.
BROKEN_VERSION = "0.0.1"
# The file a candidate is in a folder the canary is given (runtime.CANDIDATE).
CANDIDATE = "claude"


def _archive() -> bytes:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        for part in PARTS:
            path = REPO / part
            files = sorted(path.rglob("*")) if path.is_dir() else [path]
            for file in files:
                if file.is_file() and "__pycache__" not in file.parts:
                    info = tar.gettarinfo(str(file), arcname=str(file.relative_to(REPO)))
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = 0
                    with file.open("rb") as handle:
                        tar.addfile(info, handle)
    return data.getvalue()


def bundle(version: str | None = None) -> bytes:
    """The fake claude as one self-unpacking program, reporting ``version`` if one is given
    (``FAKE_CLAUDE_VERSION``, baked into the program) and the recorded one otherwise."""
    archive = _archive()
    digest = hashlib.sha256(archive).hexdigest()[:16]
    label = f"export FAKE_CLAUDE_VERSION={version}\n" if version else ""
    script = f"""#!/bin/sh
# The fake claude of rcman's tests, unpacked on its first run.
set -e
{label}d="${{TMPDIR:-/tmp}}/.rcman-fake-claude-{digest}"
if [ ! -f "$d/.unpacked" ]; then
  t="$(mktemp -d "${{TMPDIR:-/tmp}}/.rcman-fake-claude.XXXXXX")"
  sed '1,/^{MARK}$/d' "$0" | base64 -d | tar -xzf - -C "$t"
  touch "$t/.unpacked"
  mv -T "$t" "$d" 2>/dev/null || rm -rf "$t"
fi
exec python3 "$d/tests/fakes/claude" "$@"
{MARK}
"""
    return script.encode() + base64.encodebytes(archive)


def broken(version: str = BROKEN_VERSION) -> bytes:
    """A program that is not Claude Code: it reports ``version`` for everything it is asked."""
    return f"#!/bin/sh\necho '{version} (Claude Code)'\n".encode()


def binary(version: str, kind: str = "good") -> bytes:
    """The program a release of ``version`` of this ``kind`` holds."""
    if kind not in KINDS:
        raise ValueError(f"no such kind of release: {kind}")
    return broken() if kind == "broken" else bundle(version)


def release(
    folder: Path,
    version: str,
    kind: str = "good",
    platform: str | None = None,
    latest: bool = True,
) -> None:
    """The fake as Claude Code ``version``'s release, of ``kind``, under ``folder``; with
    ``latest``, the folder's ``latest`` names it."""
    from helpers import claude_release

    platform = platform or claude_release.platform_key()
    program = binary(version, kind)
    # a failing release names a checksum the program does not have
    checksum = hashlib.sha256(program + (b"changed" if kind == "failing" else b"")).hexdigest()
    (folder / version / platform).mkdir(parents=True, exist_ok=True)
    (folder / version / platform / "claude").write_bytes(program)
    manifest = {
        "version": version,
        "platforms": {platform: {"binary": "claude", "checksum": checksum}},
    }
    (folder / version / "manifest.json").write_text(json.dumps(manifest))
    if latest:
        (folder / "latest").write_text(version)


def candidate(folder: Path, version: str, kind: str = "good") -> Path:
    """A candidate for the canary: the program of a release of ``version`` as the only file of
    ``folder``, which it and its folder are readable and runnable by anyone, as a uid range
    that is not the owner's needs."""
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o755)
    path = folder / CANDIDATE
    path.write_bytes(binary(version, kind))
    path.chmod(0o755)
    return path


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


@contextlib.contextmanager
def served(folder: Path):
    """``folder`` served over HTTP on a free port of this machine: its address."""
    handler = functools.partial(_Quiet, directory=str(folder))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
