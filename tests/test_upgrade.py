"""The upgrade wrapper, installer/upgrade.py: the checks of the clone and its owner, the run of
the clone's install.sh, and the status it writes, as root runs it from rcman-upgrade.service.

Every path it uses is a module constant, which these tests point at folders of their own; the
clone's ancestors above the test's folder, which are the machine's, are left out by standing in
for ``ancestors``.
"""

import ast
import json
import os
import pwd
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from installer import upgrade as up

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "installer" / "upgrade.py"
ME = pwd.getpwuid(os.getuid()).pw_name


@pytest.fixture
def login_defs(tmp_path, monkeypatch):
    """A login.defs whose UID_MIN lets the test's own account in."""
    path = tmp_path / "login.defs"
    path.write_text(f"# a comment\nUID_MIN\t\t{os.getuid()}\nUID_MAX 60000\n")
    monkeypatch.setattr(up, "LOGIN_DEFS", path)
    return path


# What a clone holds, as far as the checks care: what root runs or reads, and what only its
# owner uses (the frontend's packages and its build).
CLONE_FILES = (
    "install.sh", "pyproject.toml", "uv.lock", "installer/install.py",
    "installer/templates/rcman.service", "helpers/rules.py", "helpers/seccomp.py",
    ".git/config", ".git/hooks/pre-commit.sample", ".git/objects/ab/cdef0123",
    "frontend/package.json", "frontend/node_modules/x/index.js", "frontend/dist/index.html",
)  # fmt: skip


def make_clone(root: Path) -> Path:
    """A clone of the test's own account with what the checks look at."""
    clone = root / "clone"
    for name in CLONE_FILES:
        (clone / name).parent.mkdir(parents=True, exist_ok=True)
        (clone / name).write_text("x\n")
        (clone / name).chmod(0o644)
    (clone / "install.sh").chmod(0o755)
    for path in (root, *(p for p in clone.rglob("*") if p.is_dir()), clone):
        path.chmod(0o755)
    return clone


# What git tracks of that clone: all of it, but what only the owner makes.
TRACKED = {name for name in CLONE_FILES if "node_modules" not in name and "/dist/" not in name}


@pytest.fixture
def clone(tmp_path, monkeypatch, login_defs):
    """A clone that passes the checks: the machine's folders above the test's are not looked
    at, and what git tracks is TRACKED (the folder is not a repository)."""
    made = make_clone(tmp_path / "home")
    top = tmp_path / "home"
    monkeypatch.setattr(
        up, "ancestors", lambda path: [p for p in Path(path).parents if top in (p, *p.parents)]
    )
    monkeypatch.setattr(up, "tracked", lambda clone, owner: set(TRACKED))
    return made


# --- the wrapper runs alone -------------------------------------------------------------------


def outside_the_standard_library(source: str) -> set[str]:
    """The modules ``source`` imports that are not the standard library's, a relative one too."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            found.add("." * node.level + (node.module or "").split(".")[0])
    return found - set(sys.stdlib_module_names) - {"__future__"}


def test_the_wrapper_imports_the_standard_library_only():
    source = SOURCE.read_text(encoding="utf-8")
    assert "import json" in source  # non-vacuity: the wrapper's own imports are read
    assert outside_the_standard_library(source) == set()
    assert outside_the_standard_library("import os\nfrom helpers import rules\n") == {"helpers"}


def test_the_wrapper_runs_alone_in_isolated_mode_as_the_unit_runs_it(tmp_path):
    copy = tmp_path / "bin" / "rcman-upgrade"
    copy.parent.mkdir()
    copy.write_bytes(SOURCE.read_bytes())
    proc = subprocess.run(
        ["/usr/bin/python3", "-I", str(copy), "--help"],
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "--finish" in proc.stdout and "--clone" in proc.stdout


# --- the owner --------------------------------------------------------------------------------


def test_the_owner_is_the_clone_directory_s(tmp_path):
    folder = tmp_path / "c"
    folder.mkdir()
    assert up.clone_owner(folder, None) == ME
    assert up.clone_owner(folder, ME) == ME


# --- who can write the clone ------------------------------------------------------------------


def test_a_path_anyone_can_write_is_refused(clone):
    assert up.check_clone(clone, None, refused=("rcman",)) == ME  # the clone as it is passes
    path = clone / "installer" / "install.py"
    path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o002)
    assert path.stat().st_mode & 0o002  # the plant took effect
    with pytest.raises(up.Refused, match="write") as refused:
        up.check_clone(clone, None, refused=())
    assert str(path) in str(refused.value)


def test_what_git_tracks_is_asked_as_the_owner_with_no_prompt_and_no_terminal(
    tmp_path, monkeypatch
):
    """git ls-files, for real, as the test's own user: the runuser in front left out."""
    from tests import git_fixtures

    repo = git_fixtures.make_repo(tmp_path / "repo")
    (repo / "linked").symlink_to("README.md")
    git_fixtures.fixture_git(repo, "add", "linked")
    git_fixtures.fixture_git(repo, "commit", "-q", "-m", "a link")
    seen = []
    real = up.subprocess.run

    def as_me(argv, **kw):
        seen.append((argv, kw))
        assert argv[:4] == ["runuser", "-u", ME, "--"]
        return real(argv[4:], **kw)

    monkeypatch.setattr(up.subprocess, "run", as_me)
    assert up.tracked(repo, ME) == {"README.md", "linked"}
    [(argv, kw)] = seen
    assert argv[4:6] == ["/usr/bin/env", "-i"]
    for key, value in up.GIT_ENVIRONMENT.items():
        assert f"{key}={value}" in argv, key
    assert kw["stdin"] == subprocess.DEVNULL and kw["start_new_session"] is True
    assert "core.fsmonitor=false" in argv  # nothing of the owner's runs to answer it


def status(**fields) -> dict:
    found = dict.fromkeys(up.FIELDS)
    found.update(state="running", boot_id="boot", started="2026-01-01T00:00:00+00:00")
    found.update(fields)
    return found


# --- a run ------------------------------------------------------------------------------------

INSTALL_SH = """#!/bin/bash
env > "$(dirname "$0")/env.seen"
if [ -t 0 ]; then echo terminal; else echo none; fi > "$(dirname "$0")/stdin.seen"
echo "Building."
echo "a warning on the way" >&2
{done}
"""
SUCCEEDS = """echo '{"release": "abc", "changed": []}'
echo 'snippet {'
exit 0"""
FAILS = """echo "install.sh: first problem" >&2
echo "install.sh: the clone has uncommitted changes; commit or discard them first" >&2
echo "" >&2
exit 1"""


@pytest.fixture
def installation(tmp_path, monkeypatch):
    """The installation's root, its current release's mark, and where the status goes."""
    root = tmp_path / "opt"
    (root / "releases" / "old").mkdir(parents=True)
    (root / "releases" / "old" / ".complete").write_text("old-commit\n")
    (root / "current").symlink_to("releases/old")
    (root / "upgrade").mkdir()
    monkeypatch.setattr(up, "ROOT", root)
    monkeypatch.setattr(up, "STATUS", root / "upgrade" / "status.json")
    monkeypatch.setattr(up, "boot_id", lambda: "this-boot")
    return root


def with_install_sh(clone: Path, done: str) -> None:
    (clone / "install.sh").write_text(INSTALL_SH.format(done=done))


def test_a_run_that_succeeds_writes_the_status_with_the_releases(clone, installation):
    with_install_sh(clone, SUCCEEDS)
    assert up.upgrade(clone, "caddy", invocation="abc123") == 0
    found = json.loads(up.STATUS.read_text())
    assert found["state"] == "succeeded" and found["boot_id"] == "this-boot"
    assert found["invocation"] == "abc123"  # the unit's run, which --finish looks for
    assert found["from"] == "old-commit" and found["to"] == "old-commit"
    assert found["error"] is None
    assert found["started"] and found["finished"] >= found["started"]


def test_a_run_that_fails_keeps_the_last_error_line_only(clone, installation):
    with_install_sh(clone, FAILS)
    assert up.upgrade(clone, "caddy") == 1
    found = json.loads(up.STATUS.read_text())
    assert found["state"] == "failed" and found["finished"]
    assert found["error"] == (
        "install.sh: the clone has uncommitted changes; commit or discard them first"
    )
    assert "first problem" not in up.STATUS.read_text()
    assert "a warning" not in up.STATUS.read_text()  # what it said on the way stays out


def test_install_sh_runs_with_the_fixed_environment_and_no_terminal(clone, installation):
    with_install_sh(clone, SUCCEEDS)
    up.upgrade(clone, "caddy")
    seen = dict(
        line.split("=", 1) for line in (clone / "env.seen").read_text().splitlines() if "=" in line
    )
    for key, value in up.ENVIRONMENT.items():
        assert seen[key] == value, key
    added_by_bash = {"PWD", "SHLVL", "_", "OLDPWD"}
    assert set(seen) - set(up.ENVIRONMENT) <= added_by_bash, set(seen) - set(up.ENVIRONMENT)
    assert (clone / "stdin.seen").read_text().strip() == "none"


# --- --finish ---------------------------------------------------------------------------------


def test_a_run_ended_by_its_unit_is_finished_as_failed_with_the_result(installation, monkeypatch):
    up.write_status(status(**{"from": "old-commit"}), up.STATUS)
    monkeypatch.setenv("SERVICE_RESULT", "timeout")  # what systemd gives ExecStopPost
    monkeypatch.setenv("EXIT_STATUS", "TERM")
    monkeypatch.setattr(up, "status_owner", lambda: None)
    assert up.main(["--finish"]) == 0
    found = json.loads(up.STATUS.read_text())
    assert found["state"] == "failed" and found["finished"]
    assert "timeout" in found["error"] and "TERM" in found["error"]
    assert found["from"] == "old-commit" and found["started"] == status()["started"]


def test_a_run_that_ended_by_itself_is_left_as_it_wrote_it(installation):
    written = status(state="failed", finished="2026-01-01T00:10:00+00:00", error="kept")
    up.write_status(written, up.STATUS)
    up.finish({"SERVICE_RESULT": "exit-code", "EXIT_STATUS": "1"})
    assert json.loads(up.STATUS.read_text()) == written
