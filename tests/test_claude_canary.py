"""The canary's program (helpers/claude_canary.py): what a throwaway container runs against
a candidate Claude Code version, which prints the checks the candidate fails as JSON."""

import json
import os
import subprocess
import sys
from pathlib import Path

from helpers import claude_checks as cc
from tests import claude_download

REPO = Path(__file__).resolve().parent.parent


def canary(candidate: Path, version: str, tmp_path: Path, **env) -> subprocess.CompletedProcess:
    """The canary as the runtime runs it: ``python -m helpers.claude_canary``, in a
    temporary folder of its own, with only the environment it is given."""
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return subprocess.run(
        [sys.executable, "-m", "helpers.claude_canary", str(candidate), version],
        cwd=REPO,
        env={"PATH": os.environ["PATH"], "TMPDIR": str(scratch), **env},
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_a_good_candidate_fails_no_check(tmp_path):
    candidate = claude_download.candidate(tmp_path / "good", "7.6.5")
    ran = canary(candidate, "7.6.5", tmp_path)
    assert ran.returncode == 0, ran.stderr
    assert json.loads(ran.stdout) == {}


def test_a_broken_candidate_names_the_checks_it_fails(tmp_path):
    candidate = claude_download.candidate(tmp_path / "broken", "7.6.5", kind="broken")
    ran = canary(candidate, "7.6.5", tmp_path)
    assert ran.returncode == 0, ran.stderr
    failed = json.loads(ran.stdout)
    assert {name for name, _ in cc.CHECKS} == set(failed) != set()
    for entry in failed.values():  # each with its report, as claude_checks makes it
        assert set(entry) == {"lines", "more"}
        assert entry["lines"]


def test_a_candidate_that_reports_another_version_fails_the_version_check(tmp_path):
    candidate = claude_download.candidate(tmp_path / "good", "7.6.5")
    ran = canary(candidate, "7.6.6", tmp_path)  # asked for a version it is not
    assert "version" in json.loads(ran.stdout)


def test_a_candidate_that_is_not_there_fails_every_check(tmp_path):
    ran = canary(tmp_path / "nothing" / "claude", "7.6.5", tmp_path)
    assert ran.returncode == 0, ran.stderr
    assert set(json.loads(ran.stdout)) == {name for name, _ in cc.CHECKS}


def test_the_canary_leaves_nothing_in_its_temporary_folder(tmp_path):
    candidate = claude_download.candidate(tmp_path / "good", "7.6.5")
    assert canary(candidate, "7.6.5", tmp_path).returncode == 0
    assert list((tmp_path / "scratch").iterdir()) == []


def test_the_candidate_runs_with_no_autoupdate_and_a_home_of_its_own(tmp_path):
    marker = tmp_path / "environment"
    candidate = tmp_path / "candidate" / "claude"
    candidate.parent.mkdir()
    candidate.write_text(f"#!/bin/sh\nenv > {marker}\necho 7.6.5\n")
    candidate.chmod(0o755)
    canary(candidate, "7.6.5", tmp_path, SECRET_OF_THE_SERVICE="hidden", HOME="/nonexistent")
    environment = dict(
        line.split("=", 1) for line in marker.read_text().splitlines() if "=" in line
    )
    assert environment["DISABLE_AUTOUPDATER"] == "1"
    assert environment["HOME"].startswith(str(tmp_path / "scratch"))
    assert "SECRET_OF_THE_SERVICE" not in environment
