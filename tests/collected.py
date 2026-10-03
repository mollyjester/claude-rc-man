"""What pytest collects for a command line, as the guards over the suite's split read it.

Not a test module. collect() runs pytest's collection in a child process from the
repository root, with the environment the tests themselves were started with less
anything that steers a run (RCMAN_*, PYTEST_* and COV_*), plus what the caller gives,
and returns each selected test's markers and fixtures by node ID.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

# Runs in the child: pytest's own collection, with a plugin that writes down what it
# selected once deselection is done.
_RECORDER = """
import json, sys
import pytest

out, args = sys.argv[1], sys.argv[2:]

class Recorder:
    def pytest_collection_finish(self, session):
        found = {
            item.nodeid: {
                "markers": sorted({m.name for m in item.iter_markers()}),
                "fixtures": sorted(item.fixturenames),
            }
            for item in session.items
        }
        with open(out, "w", encoding="utf-8") as f:
            json.dump(found, f)

code = pytest.main(["--collect-only", "-q", "-p", "no:cacheprovider", *args], plugins=[Recorder()])
sys.exit(0 if code in (0, 5) else code)
"""

# Options that choose how tests run, not which: collection runs in this one process.
_RUN_ONLY = {"-n": 1, "--numprocesses": 1, "--dist": 1, "--maxprocesses": 1}


def _selection_args(args: tuple[str, ...]) -> list[str]:
    kept, skip = [], 0
    for arg in args:
        if skip:
            skip -= 1
            continue
        name = arg.split("=", 1)[0]
        if arg.startswith("-n") and not arg.startswith("--") and len(arg) > 2:
            continue  # -n4, -nauto
        if name in _RUN_ONLY:
            skip = 0 if "=" in arg else _RUN_ONLY[name]
            continue
        kept.append(arg)
    return kept


def child_env(env: dict[str, str] | None = None) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("RCMAN_", "PYTEST_", "COV_"))}
    return {**base, **(env or {})}


@functools.cache
def _collect(args: tuple[str, ...], env: tuple[tuple[str, str], ...]) -> dict[str, dict]:
    with tempfile.TemporaryDirectory() as scratch:
        out = Path(scratch) / "collected.json"
        proc = subprocess.run(
            [sys.executable, "-c", _RECORDER, str(out), *_selection_args(args)],
            cwd=REPO,
            env=child_env(dict(env)),
            capture_output=True,
            text=True,
            timeout=600,
        )
        if proc.returncode != 0 or not out.exists():
            raise RuntimeError(
                f"collection failed ({proc.returncode}):\n{proc.stdout}{proc.stderr}"
            )
        return json.loads(out.read_text(encoding="utf-8"))


def collect(args=(), env: dict[str, str] | None = None) -> dict[str, dict]:
    """{node ID: {"markers": [...], "fixtures": [...]}} for every test pytest would run
    for ``args``, run from the repository root with ``env`` added."""
    return _collect(tuple(args), tuple(sorted((env or {}).items())))
