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


def test_the_wrapper_imports_the_standard_library_only():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, node.lineno  # nothing relative: it runs alone
            imported.add(node.module.split(".")[0])
    assert {"os", "json"} <= imported  # non-vacuity
    assert imported - {"__future__"} <= set(sys.stdlib_module_names), imported


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


def test_the_status_is_root_s_under_the_installation():
    assert up.STATUS == up.STATUS_DIR / "status.json"
    assert up.STATUS_DIR == up.ROOT / "upgrade" and up.ROOT == Path("/opt/rcman")


# --- the owner --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("UID_MIN 1000\n", 1000),
        ("# UID_MIN 5\nUID_MIN\t\t 2000 \n", 2000),
        ("UID_MAX 60000\n", up.UID_MIN_FALLBACK),
        ("UID_MIN lots\n", up.UID_MIN_FALLBACK),
        ("", up.UID_MIN_FALLBACK),
    ],
)
def test_uid_min_is_read_from_login_defs_with_a_fallback(tmp_path, monkeypatch, text, expected):
    path = tmp_path / "login.defs"
    path.write_text(text)
    monkeypatch.setattr(up, "LOGIN_DEFS", path)
    assert up.uid_min() == expected


def test_a_missing_login_defs_gives_the_fallback(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "LOGIN_DEFS", tmp_path / "absent")
    assert up.uid_min() == up.UID_MIN_FALLBACK


def test_the_test_s_own_account_is_a_fine_owner(login_defs):
    up.check_owner(ME, refused=("rcman", "rcman-proxy", "caddy"))  # positive control


def test_root_is_refused_as_the_owner(login_defs, monkeypatch):
    monkeypatch.setattr(up, "LOGIN_DEFS", login_defs.with_name("absent"))
    with pytest.raises(up.Refused, match="root"):
        up.check_owner("root", refused=())


def test_an_account_below_uid_min_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "login.defs"
    path.write_text(f"UID_MIN {os.getuid() + 1}\n")
    monkeypatch.setattr(up, "LOGIN_DEFS", path)
    with pytest.raises(up.Refused, match="UID_MIN") as refused:
        up.check_owner(ME, refused=())
    assert str(os.getuid() + 1) in str(refused.value)


@pytest.mark.parametrize("name", ["rcman", "rcman-proxy", "caddy"])
def test_rcmans_accounts_and_caddys_are_refused_by_name(login_defs, name):
    with pytest.raises(up.Refused, match=name):
        up.check_owner(name, refused=("rcman", "rcman-proxy", "caddy"))


def test_an_owner_that_is_refused_by_name_is_refused_whatever_its_uid(login_defs):
    """The test's own account, fine by its uid, refused once it is named."""
    with pytest.raises(up.Refused, match=ME):
        up.check_owner(ME, refused=(ME,))


def test_the_owner_is_the_clone_directory_s(tmp_path):
    folder = tmp_path / "c"
    folder.mkdir()
    assert up.clone_owner(folder, None) == ME
    assert up.clone_owner(folder, ME) == ME


def test_an_owner_other_than_the_account_that_ran_install_sh_is_refused(tmp_path):
    folder = tmp_path / "c"
    folder.mkdir()
    with pytest.raises(up.Refused, match="someone-else") as refused:
        up.clone_owner(folder, "someone-else")
    assert ME in str(refused.value)


def test_a_clone_of_root_s_is_refused(tmp_path, monkeypatch):
    folder = tmp_path / "c"
    folder.mkdir()
    real = os.lstat

    def lstat(path, *a, **k):
        found = real(path, *a, **k)
        if Path(path) != folder:
            return found
        fields = list(found)
        fields[stat.ST_UID] = 0
        return os.stat_result(fields)

    monkeypatch.setattr(up.os, "lstat", lstat)
    with pytest.raises(up.Refused, match="root"):
        up.clone_owner(folder, None)


# --- who can write the clone ------------------------------------------------------------------


def test_the_ancestors_of_a_path_are_every_folder_above_it():
    assert up.ancestors(Path("/a/b/c")) == [Path("/a/b"), Path("/a"), Path("/")]


def test_the_paths_checked_are_the_whole_clone_and_every_ancestor(clone):
    checked = set(up.checked_paths(clone))
    for name in CLONE_FILES:
        if "node_modules" not in name and "/dist/" not in name:
            assert clone / name in checked, name
    for folder in (".git", ".git/objects/ab", "installer/templates", "helpers", "frontend"):
        assert clone / folder in checked, folder
    assert clone in checked and clone.parent in checked
    for owner_only in up.OWNER_ONLY:  # walked past, as only the owner uses them
        assert not any(p == clone / owner_only or clone / owner_only in p.parents
                       for p in checked), owner_only  # fmt: skip


def test_what_only_the_owner_uses_may_be_written_by_anyone(clone):
    for name in ("frontend/node_modules/x/index.js", "frontend/dist/index.html"):
        (clone / name).chmod(0o666)
    (clone / "frontend" / "node_modules").chmod(0o777)
    assert up.check_clone(clone, None, refused=()) == ME


def test_a_clone_only_its_owner_can_write_passes(clone):
    assert up.check_clone(clone, None, refused=("rcman",)) == ME  # positive control


@pytest.mark.parametrize(
    "name",
    ["", ".git", "install.sh", "installer", "helpers/rules.py", "..", "installer/install.py",
     "helpers", ".git/objects/ab/cdef0123", ".git/hooks", "installer/templates/rcman.service",
     "uv.lock", "frontend/package.json"],
)  # fmt: skip
def test_a_path_anyone_can_write_is_refused(clone, name):
    path = (clone / name) if name != ".." else clone.parent
    path.chmod(stat.S_IMODE(path.stat().st_mode) | 0o002)
    assert path.stat().st_mode & 0o002  # the plant took effect
    with pytest.raises(up.Refused, match="write") as refused:
        up.check_clone(clone, None, refused=())
    assert str(path) in str(refused.value)


def test_a_world_writable_folder_with_the_sticky_bit_is_refused_too(clone):
    clone.parent.chmod(0o1777)
    with pytest.raises(up.Refused):
        up.check_clone(clone, None, refused=())


def test_a_path_its_group_can_write_passes_only_when_the_group_is_the_owner_s_alone(
    clone, monkeypatch
):
    target = clone / "installer"
    target.chmod(0o775)
    monkeypatch.setattr(up, "group_members", lambda gid: {ME})
    up.check_clone(clone, None, refused=())  # the owner's own group: passes
    monkeypatch.setattr(up, "group_members", lambda gid: {ME, "someone-else"})
    with pytest.raises(up.Refused, match="someone-else"):
        up.check_clone(clone, None, refused=())


def test_a_path_of_another_account_is_refused():
    found = os.stat_result((0o40755, 0, 0, 0, 4321, 0, 0, 0, 0, 0))
    assert up.writers(found, owner_uid=1500) == {"uid 4321"}
    mine = os.stat_result((0o40755, 0, 0, 0, 1500, 0, 0, 0, 0, 0))
    assert up.writers(mine, owner_uid=1500) == set()
    roots = os.stat_result((0o40755, 0, 0, 0, 0, 0, 0, 0, 0, 0))
    assert up.writers(roots, owner_uid=1500) == set()


@pytest.mark.parametrize(
    "name", [".git/hooks/post-merge", ".git/objects/ab", "installer/planted", "helpers/planted"]
)
def test_a_link_under_git_installer_or_helpers_is_refused(clone, tmp_path, name):
    """What root reads by its path: git's own files, and the installer and helpers it runs from
    the clone, tracked or not."""
    (tmp_path / "elsewhere").mkdir()
    path = clone / name
    if path.is_dir():
        path.rename(tmp_path / "moved")
    path.symlink_to(tmp_path / "elsewhere")
    assert path.is_symlink()  # the plant took effect
    with pytest.raises(up.Refused, match="link") as refused:
        up.check_clone(clone, None, refused=())
    assert str(path) in str(refused.value)


@pytest.mark.parametrize("name", ["install.sh", "pyproject.toml", "frontend/package.json"])
def test_a_tracked_path_that_is_a_link_is_refused(clone, tmp_path, name):
    (clone / name).unlink()
    (clone / name).symlink_to(tmp_path)
    with pytest.raises(up.Refused, match="link"):
        up.check_clone(clone, None, refused=())


@pytest.mark.parametrize("plant", ["writable config", "writable index", "hook link"])
def test_git_s_folder_is_checked_before_git_is_asked_what_it_tracks(
    clone, tmp_path, monkeypatch, plant
):
    """git, run as the owner, reads .git: someone else able to change it is refused first."""
    asked = []
    monkeypatch.setattr(up, "tracked", lambda clone, owner: asked.append(owner) or set(TRACKED))
    up.check_clone(clone, None, refused=())
    assert asked == [ME]  # positive control: a clean clone is asked
    asked.clear()
    if plant == "writable config":
        (clone / ".git" / "config").chmod(0o666)
    elif plant == "writable index":
        (clone / ".git" / "index").write_text("x")
        (clone / ".git" / "index").chmod(0o646)
    else:
        (clone / ".git" / "hooks" / "post-checkout").symlink_to(tmp_path)
    with pytest.raises(up.Refused):
        up.check_clone(clone, None, refused=())
    assert asked == []


def test_a_required_path_that_is_a_link_is_refused_whatever_git_tracks(
    clone, tmp_path, monkeypatch
):
    monkeypatch.setattr(up, "tracked", lambda clone, owner: set())  # git lists nothing
    (clone / "install.sh").unlink()
    (clone / "install.sh").symlink_to(tmp_path / "elsewhere.sh")
    with pytest.raises(up.Refused, match="link"):
        up.check_clone(clone, None, refused=())


def test_a_tracked_folder_that_is_a_link_is_refused(clone, tmp_path, monkeypatch):
    monkeypatch.setattr(up, "tracked", lambda clone, owner: {*TRACKED, "docs/page.txt"})
    (tmp_path / "docs").mkdir()
    (clone / "docs").symlink_to(tmp_path / "docs")  # docs/page.txt is tracked under the link
    with pytest.raises(up.Refused, match="link"):
        up.check_clone(clone, None, refused=())


def test_links_a_development_setup_makes_pass(clone):
    """What make setup and the owner's tools leave in the clone, untracked and ignored, which
    root never reads: the virtualenv's interpreter, a worktree's packages' programs, a test's
    output."""
    venv = clone / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "lib").mkdir()
    (venv / "bin" / "python").symlink_to("/usr/bin/python3")
    (venv / "bin" / "python3").symlink_to("python")
    (venv / "lib64").symlink_to("lib")
    bin_dir = clone / ".claude" / "worktrees" / "w" / "frontend" / "node_modules" / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "vite").symlink_to("../vite/bin/vite.js")
    (clone / "frontend" / "test-results").mkdir()
    (clone / "frontend" / "test-results" / "trace").symlink_to("/nonexistent")
    for path in (venv, venv / "bin", venv / "lib", *bin_dir.parents, bin_dir):
        if clone in path.parents:
            path.chmod(0o755)
    assert (venv / "lib64").is_symlink()  # the plant took effect
    assert up.check_clone(clone, None, refused=()) == ME
    (venv / "lib").chmod(0o777)  # the writers' check still holds there
    with pytest.raises(up.Refused, match="anyone"):
        up.check_clone(clone, None, refused=())


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


def test_a_clone_git_cannot_list_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(
        up.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 128, b"", b"fatal: not a repo\n"),
    )  # fmt: skip
    with pytest.raises(up.Refused, match="not a repo"):
        up.tracked(tmp_path, ME)


def acl_access(uid: int) -> bytes:
    """A POSIX ACL as the kernel stores it in system.posix_acl_access: the owner, the group
    and others with their mode, and another account ``uid`` that may write."""
    import struct

    entries = [(0x01, 6, 0xFFFFFFFF), (0x02, 6, uid), (0x04, 4, 0xFFFFFFFF),
               (0x10, 6, 0xFFFFFFFF), (0x20, 4, 0xFFFFFFFF)]  # fmt: skip
    return struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *e) for e in entries)


def test_a_path_with_an_acl_is_refused(clone, monkeypatch):
    """Whatever the filesystem: an ACL on a path is seen by its name."""
    target = clone / "installer" / "install.py"
    real = os.listxattr

    def listxattr(path, *a, **k):
        assert k.get("follow_symlinks") is False  # the path itself, never where a link leads
        return ["system.posix_acl_access"] if Path(path) == target else real(path, *a, **k)

    monkeypatch.setattr(up.os, "listxattr", listxattr)
    with pytest.raises(up.Refused, match="ACL") as refused:
        up.check_clone(clone, None, refused=())
    assert str(target) in str(refused.value)


def test_a_real_acl_letting_another_account_write_is_refused(clone):
    """Planted twin, on the test's own filesystem where it keeps ACLs: skipped, after trying,
    where it does not."""
    target = clone / "installer" / "install.py"
    try:
        os.setxattr(target, "system.posix_acl_access", acl_access(os.getuid() + 1))
    except OSError as error:
        pytest.skip(f"the filesystem keeps no ACL: {error}")
    assert "system.posix_acl_access" in os.listxattr(target)  # the plant took effect
    with pytest.raises(up.Refused, match="ACL"):
        up.check_clone(clone, None, refused=())


def test_a_filesystem_without_extended_attributes_has_no_acl(clone, monkeypatch):
    import errno

    def unsupported(path, *a, **k):
        raise OSError(errno.ENOTSUP, "Operation not supported")

    monkeypatch.setattr(up.os, "listxattr", unsupported)
    assert up.check_clone(clone, None, refused=()) == ME


def test_a_link_where_install_sh_is_is_refused(clone, tmp_path):
    (clone / "install.sh").unlink()
    elsewhere = tmp_path / "elsewhere.sh"
    elsewhere.write_text("#!/bin/bash\n")
    (clone / "install.sh").symlink_to(elsewhere)
    with pytest.raises(up.Refused, match="link"):
        up.check_clone(clone, None, refused=())


def test_a_clone_missing_what_install_sh_runs_is_refused(clone):
    (clone / "helpers" / "rules.py").unlink()
    with pytest.raises(up.Refused, match="rules.py"):
        up.check_clone(clone, None, refused=())


def test_a_git_folder_that_is_a_file_is_refused(clone):
    """A worktree's .git names a folder elsewhere, which nothing here checks."""
    import shutil

    shutil.rmtree(clone / ".git")
    (clone / ".git").write_text("gitdir: /elsewhere\n")
    with pytest.raises(up.Refused, match=".git"):
        up.check_clone(clone, None, refused=())


# --- the status -------------------------------------------------------------------------------


def status(**fields) -> dict:
    found = dict.fromkeys(up.FIELDS)
    found.update(state="running", boot_id="boot", started="2026-01-01T00:00:00+00:00")
    found.update(fields)
    return found


def test_the_status_is_written_whole_with_its_mode_and_nothing_left_beside_it(tmp_path):
    path = tmp_path / "upgrade" / "status.json"
    path.parent.mkdir()
    up.write_status(status(), path)
    assert json.loads(path.read_text()) == status()
    assert stat.S_IMODE(path.stat().st_mode) == up.STATUS_MODE == 0o640
    assert [p.name for p in path.parent.iterdir()] == ["status.json"]


def test_the_status_holds_exactly_its_fields():
    assert up.FIELDS == ("state", "boot_id", "invocation", "started", "finished", "from", "to",
                         "error")  # fmt: skip


def test_a_status_with_another_field_or_without_one_is_refused(tmp_path):
    path = tmp_path / "status.json"
    with pytest.raises(ValueError):
        up.write_status({**status(), "log": "everything"}, path)
    without = status()
    del without["error"]
    with pytest.raises(ValueError):
        up.write_status(without, path)
    assert not path.exists()


def test_a_status_folder_that_is_not_there_is_not_made(tmp_path):
    path = tmp_path / "upgrade" / "status.json"
    with pytest.raises(OSError):
        up.write_status(status(), path)
    assert not path.parent.exists()


def test_the_status_is_replaced_by_a_rename_and_a_link_in_its_place_is_not_followed(tmp_path):
    """Planted twin: a link where the status goes would have root write through it."""
    path = tmp_path / "status.json"
    target = tmp_path / "elsewhere"
    target.write_text("kept")
    path.symlink_to(target)
    assert path.is_symlink()  # the plant took effect
    up.write_status(status(), path)
    assert target.read_text() == "kept"
    assert not path.is_symlink() and json.loads(path.read_text())["state"] == "running"


def test_a_status_that_is_not_json_or_not_a_status_reads_as_none(tmp_path):
    path = tmp_path / "status.json"
    assert up.read_status(path) is None
    for text in ("{", "[]", '{"state": "running"}', json.dumps(status(state="lost"))):
        path.write_text(text)
        assert up.read_status(path) is None, text
    path.write_text(json.dumps(status()))
    assert up.read_status(path) == status()  # positive control


def test_a_status_with_a_leftover_notices_key_reads_as_the_same_status_without_it(tmp_path):
    """The old wrapper, still in memory when the first upgrade onto this release finishes,
    writes the key."""
    path = tmp_path / "status.json"
    path.write_text(json.dumps({**status(state="succeeded"), "notices": ["Restart Caddy."]}))
    assert up.read_status(path) == status(state="succeeded")


def test_the_release_is_the_commit_its_complete_mark_names(tmp_path, monkeypatch):
    monkeypatch.setattr(up, "ROOT", tmp_path)
    assert up.release() is None
    (tmp_path / "releases" / "abc").mkdir(parents=True)
    (tmp_path / "releases" / "abc" / ".complete").write_text("abc123\n")
    (tmp_path / "current").symlink_to("releases/abc")
    assert up.release() == "abc123"


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
NPM_FAILS = """echo "install.sh: npm ci --no-audit --no-fund failed:" >&2
echo "npm warn deprecated inflight@1.0.6: This module is not supported" >&2
echo "npm error code E404" >&2
echo "npm error 404 Not Found - GET https://registry.npmjs.org/x" >&2
echo "" >&2
exit 1"""
NO_PREFIX_FAILS = """echo "first" >&2
echo "Traceback: the last line" >&2
exit 1"""
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
    assert up.upgrade(clone, "caddy") == 0
    found = json.loads(up.STATUS.read_text())
    assert found["state"] == "succeeded" and found["boot_id"] == "this-boot"
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


def test_the_error_is_the_last_line_install_sh_or_the_installer_said_it_on(clone, installation):
    with_install_sh(clone, NPM_FAILS)
    assert up.upgrade(clone, "caddy") == 1
    found = json.loads(up.STATUS.read_text())
    assert found["error"] == "install.sh: npm ci --no-audit --no-fund failed:"
    assert "npm error" not in up.STATUS.read_text()


def test_without_such_a_line_the_error_is_the_last_line(clone, installation):
    with_install_sh(clone, NO_PREFIX_FAILS)
    up.upgrade(clone, "caddy")
    assert json.loads(up.STATUS.read_text())["error"] == "Traceback: the last line"


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


def test_the_fixed_environment_never_lets_git_prompt():
    env = up.ENVIRONMENT
    assert env["HOME"] == "/root" and env["LANG"] == "C.UTF-8"
    assert env["PATH"] == up.SECURE_PATH
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GCM_INTERACTIVE"] == "never"
    assert "-oBatchMode=yes" in env["GIT_SSH_COMMAND"].split()
    assert int(env["GIT_HTTP_LOW_SPEED_TIME"]) > 0 and int(env["GIT_HTTP_LOW_SPEED_LIMIT"]) > 0
    assert "SUDO_USER" not in env  # the owner is the clone's
    assert env[up.SERVICE_MARK] == "1"  # what tells the installer it runs in the service


def test_a_refused_clone_is_a_failed_status_and_install_sh_does_not_run(clone, installation):
    with_install_sh(clone, SUCCEEDS)
    (clone / "installer").chmod(0o777)
    assert up.upgrade(clone, "caddy") == 1
    found = json.loads(up.STATUS.read_text())
    assert found["state"] == "failed" and "installer" in found["error"]
    assert not (clone / "env.seen").exists()


def test_the_status_says_running_while_install_sh_runs(clone, installation):
    with_install_sh(clone, f'cat "{up.STATUS}" > "$(dirname "$0")/status.seen"\nexit 0')
    up.upgrade(clone, "caddy")
    seen = json.loads((clone / "status.seen").read_text())
    assert seen["state"] == "running" and seen["from"] == "old-commit" and seen["to"] is None


# --- --finish ---------------------------------------------------------------------------------


def test_a_run_records_the_invocation_of_its_unit(clone, installation):
    with_install_sh(clone, SUCCEEDS)
    up.upgrade(clone, "caddy", invocation="abc123")
    assert json.loads(up.STATUS.read_text())["invocation"] == "abc123"


def test_finish_changes_only_the_status_of_its_own_invocation(installation):
    """Planted twin: a running status of another invocation is another run's, left alone."""
    up.write_status(status(invocation="other"), up.STATUS)
    up.finish({"SERVICE_RESULT": "timeout", "EXIT_STATUS": "TERM", "INVOCATION_ID": "mine"})
    assert json.loads(up.STATUS.read_text())["state"] == "running"
    up.write_status(status(invocation="mine"), up.STATUS)  # positive control
    up.finish({"SERVICE_RESULT": "timeout", "EXIT_STATUS": "TERM", "INVOCATION_ID": "mine"})
    assert json.loads(up.STATUS.read_text())["state"] == "failed"


def test_a_run_ended_by_its_unit_is_finished_as_failed_with_the_result(installation):
    up.write_status(status(**{"from": "old-commit"}), up.STATUS)
    assert up.finish({"SERVICE_RESULT": "timeout", "EXIT_STATUS": "TERM"}) == 0
    found = json.loads(up.STATUS.read_text())
    assert found["state"] == "failed" and found["finished"]
    assert "timeout" in found["error"] and "TERM" in found["error"]
    assert found["from"] == "old-commit" and found["started"] == status()["started"]


@pytest.mark.parametrize("state", ["succeeded", "failed"])
def test_a_run_that_ended_by_itself_is_left_as_it_wrote_it(installation, state):
    written = status(state=state, finished="2026-01-01T00:10:00+00:00", error="kept")
    up.write_status(written, up.STATUS)
    result = "success" if state == "succeeded" else "exit-code"
    up.finish({"SERVICE_RESULT": result, "EXIT_STATUS": "0" if state == "succeeded" else "1"})
    assert json.loads(up.STATUS.read_text()) == written


def test_a_success_the_unit_did_not_see_as_one_is_failed(installation):
    up.write_status(status(state="succeeded", finished="2026-01-01T00:10:00+00:00"), up.STATUS)
    up.finish({"SERVICE_RESULT": "signal", "EXIT_STATUS": "KILL"})
    found = json.loads(up.STATUS.read_text())
    assert found["state"] == "failed" and "signal" in found["error"]


def test_a_status_that_is_missing_or_not_one_is_finished_as_failed(installation):
    """Planted twin: whatever is there in place of a status, the end is still written."""
    for planted in (None, "not json", json.dumps(status(state="lost"))):
        up.STATUS.unlink(missing_ok=True)
        if planted is not None:
            up.STATUS.write_text(planted)
        up.finish({"SERVICE_RESULT": "exit-code", "EXIT_STATUS": "1", "INVOCATION_ID": "i"})
        found = json.loads(up.STATUS.read_text())
        assert found["state"] == "failed" and found["boot_id"] == "this-boot", planted
        assert "exit-code" in found["error"] and found["invocation"] == "i"


def test_finish_reads_only_the_result_and_the_status(installation, monkeypatch):
    up.write_status(status(), up.STATUS)

    def refuse(*a, **k):
        raise AssertionError("--finish ran something")

    monkeypatch.setattr(up.subprocess, "Popen", refuse)
    monkeypatch.setattr(up.subprocess, "run", refuse)
    monkeypatch.setattr(up, "release", lambda: pytest.fail("--finish read the release"))
    environ = {"SERVICE_RESULT": "timeout", "EXIT_STATUS": "TERM", "ERROR": "planted"}
    up.finish(environ)
    assert "planted" not in up.STATUS.read_text()


def test_main_finishes_from_the_unit_s_environment(installation, monkeypatch):
    up.write_status(status(), up.STATUS)
    monkeypatch.setenv("SERVICE_RESULT", "timeout")
    monkeypatch.setenv("EXIT_STATUS", "TERM")
    monkeypatch.setattr(up, "status_owner", lambda: None)
    assert up.main(["--finish"]) == 0
    assert json.loads(up.STATUS.read_text())["state"] == "failed"
