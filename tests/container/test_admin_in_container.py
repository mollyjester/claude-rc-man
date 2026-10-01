"""User management on a real system, as root, inside the test container.

``tests/test_container_suite.py`` runs this module in the container built from
``tests/container/Dockerfile``; anywhere else it skips. Everything goes through
the real command lines — ``helpers/rc-admin``, ``helpers/rcman-user`` and,
through sudo, ``helpers/rc-helper`` — against real accounts.
"""

import grp
import json
import os
import pty
import pwd
import select
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RCMAN_IN_CONTAINER") != "1" or os.geteuid() != 0,
    reason="runs only as root inside the test container",
)

SRC = Path("/src")
RC_ADMIN = SRC / "helpers" / "rc-admin"
RCMAN_USER = SRC / "helpers" / "rcman-user"
RC_HELPER = SRC / "helpers" / "rc-helper"
INSTALLER = SRC / "tests" / "container" / "fake-installer"


def approved_version():
    return (SRC / "helpers" / "claude-version").read_text().strip()


@pytest.fixture(scope="module", autouse=True)
def accounts():
    """What the installer creates: the rcusers group and the rcman application user."""
    if subprocess.run(["getent", "group", "rcusers"], capture_output=True).returncode != 0:
        subprocess.run(["groupadd", "rcusers"], check=True)
    if subprocess.run(["getent", "passwd", "rcman"], capture_output=True).returncode != 0:
        subprocess.run(["useradd", "--system", "--user-group", "rcman"], check=True)


@pytest.fixture
def etc(tmp_path):
    return tmp_path / "etc"


@pytest.fixture
def env(etc):
    return {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "RC_ADMIN_ETC": str(etc),
        "RC_ADMIN_INSTALLER": str(INSTALLER),
    }


@pytest.fixture(autouse=True)
def cleanup():
    before = {p.pw_name for p in pwd.getpwall()}
    yield
    for name in {p.pw_name for p in pwd.getpwall()} - before:
        subprocess.run(["pkill", "--signal", "KILL", "--uid", name], capture_output=True)
        time.sleep(0.2)
        subprocess.run(["userdel", "--remove", name], capture_output=True)
    for path in Path("/etc/sudoers.d").glob("rcman-test-*"):
        path.unlink()


def admin(env, *args):
    proc = subprocess.run(
        [str(RC_ADMIN), *args], env=env, capture_output=True, text=True, timeout=120
    )
    return proc.returncode, json.loads(proc.stdout)


def admin_ok(env, *args):
    code, out = admin(env, *args)
    assert code == 0, out
    return out


def admin_error(env, *args):
    code, out = admin(env, *args)
    assert code == 1, out
    return out["error"]["code"]


def account(name):
    try:
        return pwd.getpwnam(name)
    except KeyError:
        return None


def as_user(name, *argv):
    return subprocess.run(
        ["runuser", "-u", name, "--", *argv], capture_output=True, text=True, timeout=60
    )


def uid_processes(uid):
    out = subprocess.run(["pgrep", "--uid", str(uid)], capture_output=True, text=True).stdout
    return out.split()


# --- add ------------------------------------------------------------------------------


def test_add_creates_a_working_account(env, etc):
    out = admin_ok(env, "add", "alice", "Alice@Example.com", "--admin")
    assert out["created"] is True

    user = account("alice")
    home = Path(user.pw_dir)
    assert "alice" in grp.getgrnam("rcusers").gr_mem
    assert oct(home.stat().st_mode & 0o777) == "0o750"
    assert home.stat().st_uid == user.pw_uid
    assert (home / ".local" / "bin" / "claude").exists()
    assert (home / ".fake-installs").read_text().split() == [approved_version()]
    settings = home / ".claude" / "settings.json"
    assert json.loads(settings.read_text())["env"]["DISABLE_AUTOUPDATER"] == "1"
    assert settings.stat().st_uid == user.pw_uid
    assert (home / "projects").stat().st_uid == user.pw_uid

    users_file, allowlist = etc / "users.toml", etc / "allowed-emails"
    assert 'email = "alice@example.com"' in users_file.read_text()
    assert allowlist.read_text() == "alice@example.com\n"
    for path in (users_file, allowlist):
        assert oct(path.stat().st_mode & 0o777) == "0o640"
        assert path.stat().st_gid == grp.getgrnam("rcman").gr_gid
        assert path.stat().st_uid == 0


def test_add_again_repairs_without_reinstalling(env):
    admin_ok(env, "add", "alice", "alice@example.com")
    out = admin_ok(env, "add", "alice", "alice@example.com")
    assert out["created"] is False
    home = Path(account("alice").pw_dir)
    assert (home / ".fake-installs").read_text().split() == [approved_version()]  # installed once


def test_existing_account_needs_adopt(env):
    subprocess.run(["useradd", "--create-home", "carol"], check=True)
    assert admin_error(env, "add", "carol", "carol@example.com") == "user_exists"
    assert "carol" not in grp.getgrnam("rcusers").gr_mem
    admin_ok(env, "add", "carol", "carol@example.com", "--adopt")
    assert "carol" in grp.getgrnam("rcusers").gr_mem


def test_system_accounts_are_refused(env):
    assert admin_error(env, "add", "daemon", "d@example.com", "--adopt") == "system_user"


# --- delete ---------------------------------------------------------------------------


@pytest.fixture
def two(env):
    admin_ok(env, "add", "alice", "alice@example.com", "--admin")
    admin_ok(env, "add", "bob", "bob@example.com")


def test_delete_keeping_home_stops_everything(env, etc, two):
    bob = account("bob")
    home = Path(bob.pw_dir)
    fake_state = home / ".claude-fake" / "fake-state.json"
    as_user(
        "bob",
        "/bin/sh",
        "-c",
        f"mkdir -p {fake_state.parent} && echo '{{\"logged_in\": true}}' > {fake_state}",
    )
    as_user("bob", "tmux", "-L", "rcman", "new-session", "-d", "sleep", "600")
    subprocess.Popen(["runuser", "-u", "bob", "--", "sleep", "600"])
    time.sleep(0.5)
    assert uid_processes(bob.pw_uid)  # positive control: bob has processes

    out = admin_ok(env, "del", "bob", "--keep-home", "--requester", "alice@example.com")

    assert out == {"deleted": "bob", "home_deleted": False}
    assert account("bob") is None
    assert uid_processes(bob.pw_uid) == []
    assert home.is_dir()
    assert json.loads(fake_state.read_text())["logged_in"] is False  # logged out of Claude
    assert "bob" not in (etc / "users.toml").read_text()
    assert (etc / "allowed-emails").read_text() == "alice@example.com\n"


def test_delete_with_home(env, two):
    home = Path(account("bob").pw_dir)
    admin_ok(env, "del", "bob", "--delete", "--requester", "alice@example.com")
    assert not home.exists()


def test_rc_admin_never_asks(env, two):
    assert admin_error(env, "del", "bob") == "home_decision_required"
    assert account("bob") is not None


def test_cannot_delete_yourself_or_the_last_admin(env, two):
    assert (
        admin_error(env, "del", "alice", "--keep-home", "--requester", "alice@example.com")
        == "cannot_delete_self"
    )
    assert (
        admin_error(env, "del", "alice", "--keep-home", "--requester", "bob@example.com")
        == "last_admin"
    )


def test_unmanaged_accounts_are_refused(env, two):
    subprocess.run(["useradd", "--create-home", "dave"], check=True)
    assert admin_error(env, "del", "dave", "--keep-home") == "not_managed"
    assert account("dave") is not None


# --- rcman-user -------------------------------------------------------------------


def rcman_user_tty(env, args, answer):
    """Run rcman-user on a real terminal, answer its question, return its output."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execve(str(RCMAN_USER), [str(RCMAN_USER), *args], {**env, "SUDO_USER": "alice"})
    output = b""
    answered = False
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.5)
        if ready:
            try:
                chunk = os.read(fd, 1024)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
            if not answered and b"[y/N]" in output:
                os.write(fd, answer)
                answered = True
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), output.decode(errors="replace")


def test_rcman_user_asks_and_defaults_to_keeping_the_home(env, two):
    home = Path(account("bob").pw_dir)
    code, output = rcman_user_tty(env, ["del", "bob"], b"\n")
    assert code == 0, output
    assert "Delete home directory" in output and "[y/N]" in output
    assert account("bob") is None and home.is_dir()


def test_rcman_user_deletes_the_home_when_told_yes(env, two):
    home = Path(account("bob").pw_dir)
    code, output = rcman_user_tty(env, ["del", "bob"], b"y\n")
    assert code == 0, output
    assert not home.exists()


def test_rcman_user_without_a_terminal_refuses(env, two):
    proc = subprocess.run(
        [str(RCMAN_USER), "del", "bob"],
        env={**env, "SUDO_USER": "alice"},
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert account("bob") is not None


# --- sudo, as installed -------------------------------------------------------------


@pytest.fixture
def sudoers():
    rules = []
    for template, placeholder, path in [
        ("sudoers.rc-helper", "@HELPER@", RC_HELPER),
        ("sudoers.rc-admin", "@ADMIN@", RC_ADMIN),
    ]:
        text = (SRC / "helpers" / template).read_text()
        rules.append(text.replace(placeholder, str(path)).replace("@APP_USER@", "rcman"))
    target = Path("/etc/sudoers.d/rcman-test-rules")
    target.write_text("\n".join(rules))
    target.chmod(0o440)
    check = subprocess.run(["visudo", "-c"], capture_output=True, text=True)
    assert check.returncode == 0, check.stdout + check.stderr


def as_app(*argv, extra_env=None):
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", **(extra_env or {})}
    return subprocess.run(
        ["runuser", "-u", "rcman", "--", *argv], env=env, capture_output=True, text=True, timeout=60
    )


def test_app_user_runs_rc_helper_as_a_managed_user(env, two, sudoers):
    proc = as_app("sudo", "-n", "-u", "bob", str(RC_HELPER), "tree")
    assert proc.returncode == 0, proc.stderr
    assert [e["path"] for e in json.loads(proc.stdout)["entries"]] == ["projects"]


def test_app_user_cannot_run_rc_helper_as_root_or_unmanaged_users(env, two, sudoers):
    subprocess.run(["useradd", "--create-home", "eve"], check=True)
    for target in ("root", "eve", "daemon"):
        proc = as_app("sudo", "-n", "-u", target, str(RC_HELPER), "list")
        assert proc.returncode != 0, target


def test_app_user_cannot_run_anything_else(env, two, sudoers):
    for argv in (["-u", "bob", "/bin/sh", "-c", "id"], ["-u", "root", "/bin/sh", "-c", "id"]):
        assert as_app("sudo", "-n", *argv).returncode != 0, argv


def test_test_hooks_do_not_pass_through_sudo(env, two, sudoers, tmp_path):
    decoy = tmp_path / "decoy"
    (decoy / "trap").mkdir(parents=True)
    os.chmod(tmp_path, 0o755)
    proc = as_app(
        "sudo", "-n", "-u", "bob", str(RC_HELPER), "tree", extra_env={"RC_HELPER_HOME": str(decoy)}
    )
    paths = [e["path"] for e in json.loads(proc.stdout)["entries"]]
    assert paths == ["projects"]  # bob's real home, not the decoy


def test_app_user_runs_rc_admin_as_root(env, two, sudoers):
    proc = as_app("sudo", "-n", str(RC_ADMIN), "list")
    # Through sudo the environment is reset: the real /etc/rcman, which has no users file here.
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(proc.stdout) == {"users": []}


def test_fake_installer_is_executable_by_users():
    assert os.access(INSTALLER, os.X_OK)
    assert shutil.which("runuser")


# --- Claude Code versions ------------------------------------------------------------


def installed(name):
    out = as_user(
        name, str(Path(account(name).pw_dir) / ".local" / "bin" / "claude"), "--version"
    ).stdout
    return out.split()[0] if out else None


def test_upgrade_checks_in_the_canary_then_rolls_out(env, etc, two):
    assert installed("alice") == approved_version()
    out = admin_ok(env, "claude-upgrade", "2.1.999")
    assert out["approved"] == "2.1.999"
    assert out["users"] == {"alice": "installed", "bob": "installed"}
    assert (etc / "claude-version").read_text().strip() == "2.1.999"
    assert installed("alice") == installed("bob") == "2.1.999"
    canary = Path("/var/lib/rcman/canary")
    assert canary.stat().st_uid == account("rcman").pw_uid
    status = admin_ok(env, "claude-status")
    assert status["approved"] == "2.1.999" and status["tested"] == approved_version()


def test_a_version_failing_the_checks_changes_nothing(env, etc, two):
    code, out = admin(env, "claude-upgrade", "0.0.1")
    assert code == 1 and out["error"]["code"] == "claude_checks_failed"
    assert set(out["error"]["failed"]) >= {"version", "auth_status", "sign_in_screen"}
    assert not (etc / "claude-version").exists()
    assert installed("alice") == installed("bob") == approved_version()


def test_people_added_after_an_upgrade_get_the_approved_version(env, two):
    admin_ok(env, "claude-upgrade", "2.1.999")
    admin_ok(env, "add", "carol", "carol@example.com")
    assert installed("carol") == "2.1.999"
