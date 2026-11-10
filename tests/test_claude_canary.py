"""The canary's program (helpers/claude_canary.py): what a throwaway container runs against
a candidate Claude Code version, which prints the checks the candidate fails as JSON."""

import json
import os
import subprocess
import sys
from pathlib import Path

from tests import claude_download
from tests.hang_guard import HANG_GUARD

REPO = Path(__file__).resolve().parent.parent


def canary(candidate: Path, version: str, tmp_path: Path) -> subprocess.CompletedProcess:
    """The canary as the runtime runs it: ``python -m helpers.claude_canary``, in a
    temporary folder of its own, with only a PATH and that folder."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, "-m", "helpers.claude_canary", str(candidate), version],
        cwd=REPO,
        env={"PATH": os.environ["PATH"], "TMPDIR": str(scratch)},
        capture_output=True,
        text=True,
        timeout=HANG_GUARD,
    )


def test_a_good_candidate_fails_no_check_and_leaves_nothing_behind(tmp_path):
    candidate = claude_download.candidate(tmp_path / "good", "7.6.5")
    ran = canary(candidate, "7.6.5", tmp_path)
    assert ran.returncode == 0, ran.stderr
    assert json.loads(ran.stdout) == {}
    assert list((tmp_path / "scratch").iterdir()) == []
