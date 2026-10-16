"""The canary: the checks a candidate Claude Code version is held to (claude_checks.py), run
against a candidate program and reported as JSON.

``python -m helpers.claude_canary <candidate> <version>`` is what the canary container runs,
and what the host runtime runs for the tests. It prints one JSON object on standard output,
the checks the candidate fails with a report of each (already redacted), empty when it
passes them all, and exits 0 whatever the candidate does: an exit code of anything else, or
output that is not that object, means the checks themselves could not be run.

The candidate runs with a home, a configuration folder and a temporary folder of this run's
own, removed afterwards, with no account and no environment but what is named here.

Python standard library and the helpers only.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from helpers import claude_checks

# What the candidate is given of the environment that runs this, and what its path is when
# there is none.
KEPT = ("PATH", "LANG")
DEFAULT_PATH = "/usr/local/bin:/usr/bin:/bin"
TERM = "xterm-256color"
CONFIG = "config"


def environment(home: Path) -> dict[str, str]:
    """The only environment the candidate runs with."""
    env = {key: os.environ[key] for key in KEPT if key in os.environ}
    env.setdefault("PATH", DEFAULT_PATH)
    env.setdefault("LANG", "C.UTF-8")
    return {**env, "HOME": str(home), "TMPDIR": str(home), "TERM": TERM}


def failures(candidate: str, version: str) -> dict[str, dict]:
    """The checks ``candidate`` fails as ``version``, each with its report."""
    home = Path(tempfile.mkdtemp(prefix="canary-"))
    try:
        run = claude_checks.local_runner(candidate, environment(home), home, home / CONFIG)
        return claude_checks.candidate_failures(run, version)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m helpers.claude_canary <candidate> <version>", file=sys.stderr)
        return 2
    print(json.dumps(failures(argv[0], argv[1])))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
