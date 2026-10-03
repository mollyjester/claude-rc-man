"""The GitHub token, push and clone on a real system, through sudo, against https://github.com.

``tests/test_container_suite.py`` runs this module in a container with no
network (``--network none``), where github.com is 127.0.0.1 (``--add-host``),
which the github_com fixture checks before git is run, and a certificate authority made
here is trusted system-wide, so git reaches the tests' GitHub
(``tests/github_server.py``) at https://github.com itself: no RC_HELPER_GITHUB
hook, as in production. Anywhere else it skips.
"""

import json
import os
import socket
import ssl
import subprocess
from pathlib import Path

import pytest

from tests.container import test_admin_in_container as admin
from tests.github_server import UNREACHABLE, GitHubServer

pytestmark = pytest.mark.skipif(
    os.environ.get("RCMAN_IN_CONTAINER") != "1" or os.geteuid() != 0,
    reason="runs only as root inside the test container",
)

# The user-management suite's fixtures: its accounts, people and sudo rules.
accounts, cleanup, env, etc, sudoers, two = (
    admin.accounts,
    admin.cleanup,
    admin.env,
    admin.etc,
    admin.sudoers,
    admin.two,
)
account, as_app, RC_HELPER = admin.account, admin.as_app, admin.RC_HELPER

TOKEN = "github_pat_container-test-token"
CA_NAME = "rcman-test-github-ca"


def openssl(*args, cwd):
    proc = subprocess.run(["openssl", *args], cwd=cwd, capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.fixture(scope="module")
def github_com(tmp_path_factory):
    """The tests' GitHub on 127.0.0.1:443 with a certificate for github.com from a
    certificate authority the system trusts; removed again afterwards."""
    # Before any git is run: no network but loopback, and github.com is this container.
    assert sorted(os.listdir("/sys/class/net")) == ["lo"]
    assert socket.gethostbyname("github.com") == "127.0.0.1"
    pki = tmp_path_factory.mktemp("pki")
    openssl("req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj",
            f"/CN={CA_NAME}", "-keyout", "ca.key", "-out", "ca.crt", cwd=pki)  # fmt: skip
    openssl("req", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=github.com",
            "-keyout", "leaf.key", "-out", "leaf.csr", cwd=pki)  # fmt: skip
    (pki / "san.ext").write_text("subjectAltName=DNS:github.com\n")
    openssl("x509", "-req", "-in", "leaf.csr", "-CA", "ca.crt", "-CAkey", "ca.key",
            "-CAcreateserial", "-days", "2", "-extfile", "san.ext", "-out", "leaf.crt",
            cwd=pki)  # fmt: skip
    trusted = Path("/usr/local/share/ca-certificates") / f"{CA_NAME}.crt"
    trusted.write_bytes((pki / "ca.crt").read_bytes())
    subprocess.run(["update-ca-certificates"], check=True, capture_output=True)
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(pki / "leaf.crt", pki / "leaf.key")
    server = GitHubServer()
    url = server.start(tmp_path_factory.mktemp("github"), TOKEN, ssl_context=context, port=443)
    assert url == "https://127.0.0.1:443/"
    yield server
    server.stop()
    trusted.unlink()
    subprocess.run(["update-ca-certificates", "--fresh"], check=True, capture_output=True)


def helper_as_bob(*args, stdin=None, extra_env=None):
    proc = as_app("sudo", "-n", "-u", "bob", str(RC_HELPER), *args, stdin=stdin,
                  extra_env=extra_env)  # fmt: skip
    return proc.returncode, json.loads(proc.stdout), proc


def as_bob(*argv, cwd=None):
    """A command as bob, with only his home and the system's PATH: as a terminal would."""
    home = Path(account("bob").pw_dir)
    return subprocess.run(
        ["runuser", "-u", "bob", "--", "env", "-i", f"HOME={home}", "PATH=/usr/bin:/bin",
         "GIT_TERMINAL_PROMPT=0", *argv],
        cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
    )  # fmt: skip


@pytest.fixture
def bobs_repo(two, sudoers, github_com, tmp_path):
    """bob's projects/repo, one commit on main, whose origin is
    https://github.com/bob/<unique>.git, an empty repository on the tests' GitHub."""
    name = f"repo-{tmp_path.name}"
    bare = github_com.bare("bob", name)
    home = Path(account("bob").pw_dir)
    repo = home / "projects" / "repo"
    for argv in (
        ["git", "init", "-q", "-b", "main", str(repo)],
        ["git", "-C", str(repo), "-c", "user.name=Bob", "-c", "user.email=bob@example.com",
         "commit", "-q", "--allow-empty", "-m", "First"],
        ["git", "-C", str(repo), "remote", "add", "origin", GitHubServer.github_url("bob", name)],
    ):  # fmt: skip
        proc = as_bob(*argv)
        assert proc.returncode == 0, proc.stderr
    return repo, bare


def on_remote(bare: Path, ref: str) -> str:
    out = subprocess.run(["git", "-C", str(bare), "rev-parse", "--verify", "-q", ref],
                         capture_output=True, text=True)  # fmt: skip
    return out.stdout.strip()


def test_the_token_is_stored_for_github_com_in_the_persons_home(env, two, sudoers, github_com):
    code, out, proc = helper_as_bob("github-token", stdin=TOKEN + "\n")
    assert (code, out) == (0, {"token": True}), proc.stderr
    assert TOKEN not in proc.stdout + proc.stderr
    bob = account("bob")
    stored = Path(bob.pw_dir) / ".git-credentials"
    assert stored.read_text() == f"https://x-access-token:{TOKEN}@github.com\n"
    assert stored.stat().st_uid == bob.pw_uid and oct(stored.stat().st_mode & 0o777) == "0o600"
    helpers = as_bob(
        "git", "config", "--global", "--get-all", "credential.https://github.com.helper"
    )
    assert helpers.stdout == "store\n", helpers.stderr
    assert helper_as_bob("github-status")[1] == {"token": True}


def test_a_push_through_sudo_reaches_github_with_the_token(env, bobs_repo, github_com):
    repo, bare = bobs_repo
    code, out, _ = helper_as_bob("git-push", "--", "projects/repo")
    assert (code, out["error"]["code"]) == (1, "github_token_not_set"), out
    assert helper_as_bob("github-token", stdin=TOKEN + "\n")[0] == 0
    before = len(github_com.attempts)
    code, out, proc = helper_as_bob("git-push", "--", "projects/repo")
    assert code == 0, (out, proc.stderr)
    assert out["upstream"] == "origin/main" and out["upstream_set"] is True
    head = as_bob("git", "-C", str(repo), "rev-parse", "HEAD").stdout.strip()
    assert on_remote(bare, "refs/heads/main") == head
    assert set(github_com.attempts[before:]) == {("x-access-token", True)}


def test_a_plain_git_push_as_the_person_uses_the_token(env, bobs_repo, github_com):
    repo, bare = bobs_repo
    refused = as_bob("git", "-C", str(repo), "push", "origin", "HEAD:refs/heads/plain")
    assert refused.returncode != 0 and on_remote(bare, "refs/heads/plain") == ""
    assert helper_as_bob("github-token", stdin=TOKEN + "\n")[0] == 0
    accepted = as_bob("git", "-C", str(repo), "push", "origin", "HEAD:refs/heads/plain")
    assert accepted.returncode == 0, accepted.stderr
    assert on_remote(bare, "refs/heads/plain") != ""


def test_the_github_hook_does_not_pass_through_sudo(env, bobs_repo, github_com):
    """With RC_HELPER_GITHUB at an address nothing answers on in the caller's environment,
    a fetch through sudo still reaches github.com; under the hook it would fail, as
    tests/test_rc_helper.py shows."""
    repo, bare = bobs_repo
    assert helper_as_bob("github-token", stdin=TOKEN + "\n")[0] == 0
    assert helper_as_bob("git-push", "--", "projects/repo")[0] == 0
    subprocess.run(["git", "-C", str(bare), "update-ref", "refs/heads/later", "refs/heads/main"],
                   check=True)  # fmt: skip
    hooked = {"RC_HELPER_GITHUB": UNREACHABLE}
    seen = as_app("env", extra_env=hooked)  # positive control: the caller has the hook
    assert f"RC_HELPER_GITHUB={UNREACHABLE}" in seen.stdout.splitlines(), seen.stdout
    code, out, proc = helper_as_bob("git-fetch", "--", "projects/repo", extra_env=hooked)
    assert code == 0, (out, proc.stderr)
    fetched = as_bob("git", "-C", str(repo), "rev-parse", "--verify", "origin/later")
    assert fetched.returncode == 0, fetched.stderr  # it came from github.com


def on_github_com(github_com, tmp_path, private=False) -> tuple[str, str]:
    """A repository of bob's on the tests' GitHub with one commit on main: its address
    on github.com, without .git, and the folder a clone of it makes."""
    name = f"clone-{tmp_path.name}"
    bare = github_com.bare("bob", name, private=private)
    source = tmp_path / "source"
    for argv in (
        ["git", "init", "-q", "-b", "main", str(source)],
        ["git", "-C", str(source), "-c", "user.name=Bob", "-c", "user.email=bob@example.com",
         "commit", "-q", "--allow-empty", "-m", "First"],
        ["git", "-C", str(source), "push", "-q", str(bare), "main"],
    ):  # fmt: skip
        subprocess.run(argv, check=True, capture_output=True)
    return GitHubServer.github_url("bob", name).removesuffix(".git"), name


def test_a_clone_through_sudo_comes_from_github_and_is_the_persons(
    env, two, sudoers, github_com, tmp_path
):
    url, name = on_github_com(github_com, tmp_path)
    before = len(github_com.requests)
    code, out, proc = helper_as_bob("git-clone", f"--url={url}", "--", "projects")
    assert (code, out) == (0, {"created": f"projects/{name}"}), proc.stderr
    assert any(p.startswith(f"/bob/{name}.git/") for _, p in github_com.requests[before:])
    bob = account("bob")
    clone = Path(bob.pw_dir) / "projects" / name
    owned = [clone, *clone.rglob("*")]
    assert len(owned) > 1 and all(p.lstat().st_uid == bob.pw_uid for p in owned)
    origin = as_bob("git", "-C", str(clone), "config", "remote.origin.url")
    assert origin.stdout.strip() == url, origin.stderr


def test_a_private_clone_through_sudo_needs_the_stored_token(
    env, two, sudoers, github_com, tmp_path
):
    url, name = on_github_com(github_com, tmp_path, private=True)
    clone = Path(account("bob").pw_dir) / "projects" / name
    code, out, _ = helper_as_bob("git-clone", f"--url={url}", "--", "projects")
    assert (code, out["error"]["code"]) == (1, "git_failed"), out
    assert not clone.exists()
    assert helper_as_bob("github-token", stdin=TOKEN + "\n")[0] == 0
    code, out, proc = helper_as_bob("git-clone", f"--url={url}", "--", "projects")
    assert (code, out) == (0, {"created": f"projects/{name}"}), proc.stderr
    assert TOKEN not in (clone / ".git" / "config").read_text()
