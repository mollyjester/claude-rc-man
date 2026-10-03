#!/usr/bin/env python3
"""PostToolUse hook: run a file's paired test when the file changes.

The pairing is a convention, not a table, so nothing has to be kept in step:

- ``<dir>/<name>.py`` is tested by ``tests/test_<name>.py``;
- a test file is its own pair, and a package ``__init__.py`` has none;
- a Markdown file is checked by ``tests/test_docs_guard.py``;
- ``frontend/src/<name>.ts`` or ``.tsx`` is tested by ``<name>.test.ts(x)`` beside
  it, run with Vitest.

A Python file outside ``tests/`` with no paired test is reported rather than
skipped; a frontend file without one is not, because components are covered
through the screens that use them. Silent on success. On failure the message
reaches the operator through ``systemMessage`` and the agent through
``additionalContext``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS_GUARD_TEST = Path("tests") / "test_docs_guard.py"
OUTPUT_TAIL_LINES = 60


FRONTEND = Path("frontend")
FRONTEND_SOURCE = FRONTEND / "src"


def _frontend_pair(root: Path, rel: Path) -> Path | None:
    if rel.suffix not in (".ts", ".tsx") or FRONTEND_SOURCE not in rel.parents:
        return None
    if rel.stem.endswith(".test"):
        return root / rel
    for suffix in (".test.tsx", ".test.ts"):
        candidate = root / rel.with_name(rel.stem + suffix)
        if candidate.exists():
            return candidate
    return None


def paired_test(root: Path, path: Path) -> Path | None:
    try:
        rel = path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    if rel.suffix in (".ts", ".tsx"):
        return _frontend_pair(root, rel)
    if rel.suffix == ".md":
        return root / DOCS_GUARD_TEST
    if rel.suffix != ".py" or rel.name == "__init__.py":
        return None
    if rel.parts[0] == "tests":
        return root / rel if rel.name.startswith("test_") else None
    return root / "tests" / f"test_{rel.stem}.py"


def _report(summary: str, detail: str) -> dict:
    return {
        "systemMessage": summary,
        "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": detail},
    }


def repository_root(path: Path, fallback: Path) -> Path:
    """The checkout the edited file is in, asked of git where the file is.

    The hook is started from the main checkout, and a worktree lies inside it,
    under ``.claude/worktrees/``; only git, asked from the file's own folder,
    tells the two apart. Outside any repository the given root stands."""
    folder = path.parent
    while not folder.is_dir() and folder != folder.parent:
        folder = folder.parent
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], cwd=folder, capture_output=True, text=True
    )
    top = proc.stdout.strip()
    return Path(top) if proc.returncode == 0 and top else fallback


def import_path(root: Path) -> str:
    """PYTHONPATH that puts the checkout's own packages first.

    The hook's Python may have another checkout's packages installed; the
    folders that hold the packages ``pyproject.toml`` builds come before them."""
    try:
        with open(root / "pyproject.toml", "rb") as f:
            wheel = tomllib.load(f)["tool"]["hatch"]["build"]["targets"]["wheel"]
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        return os.environ.get("PYTHONPATH", "")
    folders = dict.fromkeys(str(root / Path(package).parent) for package in wheel["packages"])
    return os.pathsep.join([*folders, os.environ.get("PYTHONPATH", "")]).rstrip(os.pathsep)


def handle(payload: dict, root: Path) -> dict | None:
    file_path = (payload.get("tool_input") or {}).get("file_path")
    if not file_path:
        return None
    root = repository_root(Path(file_path), root)
    test = paired_test(root, Path(file_path))
    if test is None:
        return None
    test_rel = test.relative_to(root).as_posix()
    if not test.is_file():
        message = f"{file_path} has no paired test: expected {test_rel}"
        return _report(message, message)
    if FRONTEND in test.relative_to(root).parents:
        argv = ["npx", "vitest", "run", test.relative_to(root / FRONTEND).as_posix()]
        proc = subprocess.run(argv, cwd=root / FRONTEND, capture_output=True, text=True)
    else:
        proc = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", test_rel],
            cwd=root,
            env={**os.environ, "PYTHONPATH": import_path(root)},
            capture_output=True,
            text=True,
        )
    if proc.returncode == 0:
        return None
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-OUTPUT_TAIL_LINES:])
    return _report(f"Paired test failed: {test_rel}", f"{test_rel} failed:\n{tail}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root")
    args = parser.parse_args()
    out = handle(json.load(sys.stdin), args.root)
    if out is not None:
        print(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
