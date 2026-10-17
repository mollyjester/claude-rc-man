"""tests/git_fixtures.py: fixture git leaves nothing behind, however its run ends, and a
commit made on an origin is made there alone."""

import os
import signal
import subprocess
from pathlib import Path

import pytest

from tests import killed_runs as kr
from tests.git_fixtures import (
    FIXTURE_HOME,
    commit_on_origin,
    fixture_env,
    fixture_git,
    make_repo,
)

# Each worker makes a repository with the fixtures, and a file of its own in TMPDIR, the
# positive control that shows the run's temporary files go where they are looked for.
USES_THE_FIXTURES = """
import os, tempfile
from tests import killed_runs as kr
from tests.git_fixtures import make_repo

def made(tmp_path, name):
    repo = make_repo(tmp_path / "repo")
    fd, control = tempfile.mkstemp(prefix="control-")
    os.close(fd)
    kr.report(name, {"paths": {"repo": str(repo), "control": control}})
    kr.hold()

def test_one(tmp_path):
    made(tmp_path, "one")

def test_two(tmp_path):
    made(tmp_path, "two")
"""


def _left_in_tmpdir(run: kr.KilledRun, controls: set[str]) -> list[str]:
    assert {Path(c).parent for c in controls} == {run.tmpdir}  # positive control
    assert all(Path(c).is_file() for c in controls)
    return sorted(p.name for p in run.tmpdir.iterdir() if str(p) not in controls)


def _made(run: kr.KilledRun) -> set[str]:
    controls = set()
    for name in ("one", "two"):
        reported = run.reports.wait(name)
        assert (Path(reported["paths"]["repo"]) / ".git").is_dir()  # the fixtures ran
        controls.add(reported["paths"]["control"])
    return controls


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["SIGTERM", "SIGKILL"])
def test_a_killed_run_of_the_git_fixtures_leaves_nothing(tmp_path, sig):
    with kr.KilledRun(tmp_path, {"test_fixtures.py": USES_THE_FIXTURES}, ()) as run:
        controls = _made(run)
        run.kill(sig)
        assert not run.alive()
        assert _left_in_tmpdir(run, controls) == []


def test_a_normal_run_of_the_git_fixtures_leaves_nothing(tmp_path):
    with kr.KilledRun(tmp_path, {"test_fixtures.py": USES_THE_FIXTURES}, ()) as run:
        controls = _made(run)
        code, output = run.finish()
        assert code == 0 and "2 passed" in output, output
        assert _left_in_tmpdir(run, controls) == []


def test_fixture_git_has_a_home_that_does_not_exist(tmp_path):
    assert fixture_env(FIXTURE_HOME)["HOME"] == str(FIXTURE_HOME)
    repo = make_repo(tmp_path / "repo")
    assert fixture_git(repo, "log", "--format=%s").strip() == "First commit"  # git ran
    assert not os.path.lexists(FIXTURE_HOME)
    probe = subprocess.run(
        ["sh", "-c", 'test -e "$HOME"'], env=fixture_env(FIXTURE_HOME), capture_output=True
    )
    assert probe.returncode != 0


def test_fixture_env_makes_no_home(tmp_path):
    home = tmp_path / "home"
    assert fixture_env(home)["HOME"] == str(home)
    assert not home.exists()


def test_a_commit_on_origin_moves_that_branch_of_origin_alone(tmp_path):
    origin = tmp_path / "origins" / "repo.git"
    repo = make_repo(tmp_path / "repo", origin=origin)
    before = fixture_git(origin, "rev-parse", "main").strip()
    feature = fixture_git(origin, "rev-parse", "feature").strip()
    beside = sorted(p.name for p in origin.parent.iterdir())
    made = commit_on_origin(origin, "main", "theirs.txt", "their text\n")
    assert fixture_git(origin, "rev-parse", "main").strip() == made != before
    assert fixture_git(origin, "rev-parse", f"{made}^").strip() == before  # on top of main
    assert fixture_git(origin, "show", f"{made}:theirs.txt") == "their text\n"
    assert fixture_git(origin, "rev-parse", "feature").strip() == feature
    assert fixture_git(repo, "rev-parse", "main").strip() == before  # the clone is untouched
    assert fixture_git(repo, "rev-parse", "origin/main").strip() == before
    assert sorted(p.name for p in origin.parent.iterdir()) == beside  # no scratch clone left
    again = commit_on_origin(origin, "main", "more.txt")  # a second one has a folder of its own
    assert fixture_git(origin, "rev-parse", f"{again}^").strip() == made
