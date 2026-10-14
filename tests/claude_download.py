"""The fake claude as a Claude Code release, laid out as the installer downloads one.

Not a test module. ``release`` writes ``<version>/manifest.json`` and
``<version>/<platform>/claude`` under a folder, which the container suite serves
over HTTP and gives the installer as its download address: the installer
downloads the fake and checks it against the manifest as it does the real one.

The binary is one file, as Claude Code's is: a shell script holding the fake and
what it reads, which unpacks itself under ``$TMPDIR`` (or the system's temporary
folder) the first time it runs, and runs it with the image's python3.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
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


def bundle() -> bytes:
    """The fake claude as one self-unpacking program."""
    archive = _archive()
    digest = hashlib.sha256(archive).hexdigest()[:16]
    script = f"""#!/bin/sh
# The fake claude of rcman's tests, unpacked on its first run.
set -e
d="${{TMPDIR:-/tmp}}/.rcman-fake-claude-{digest}"
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


def release(folder: Path, version: str, platform: str | None = None) -> None:
    """The fake as Claude Code ``version``'s release, under ``folder``."""
    from installer import install as ins

    platform = platform or ins.claude_platform()
    binary = bundle()
    (folder / version / platform).mkdir(parents=True, exist_ok=True)
    (folder / version / platform / "claude").write_bytes(binary)
    manifest = {
        "version": version,
        "platforms": {
            platform: {"binary": "claude", "checksum": hashlib.sha256(binary).hexdigest()}
        },
    }
    (folder / version / "manifest.json").write_text(json.dumps(manifest))
