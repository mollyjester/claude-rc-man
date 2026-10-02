"""The tests' GitHub: real git http-backend, reading for anyone, pushing with the token.

Everything here runs the system's git against tests/github_server.py, so a
server that let a push through without the token, or with a wrong one, would
make every test that relies on it meaningless.
"""

import subprocess
from pathlib import Path

import pytest

from tests.git_fixtures import fixture_env, fixture_git, make_repo, write
from tests.github_server import GitHubServer

TOKEN = "test-token-of-the-server"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    github = GitHubServer()
    github.start(tmp_path_factory.mktemp("github"), TOKEN)
    yield github
    github.stop()


_names = iter(range(10_000))


@pytest.fixture
def remote(server):
    """A fresh empty repository on the server: its bare repository and its URL there."""
    name = f"repo-{next(_names)}"
    return server.bare("owner", name), f"{server.url}owner/{name}.git"


def git_as(cwd: Path, home: Path, *args: str, config: dict[str, str] | None = None, **env):
    """git as a person with ``home``, never prompting; configuration from ``config``
    through GIT_CONFIG_*, and anything else from ``env``."""
    full = {**fixture_env(home), "GIT_TERMINAL_PROMPT": "0", **env}
    for i, (key, value) in enumerate((config or {}).items()):
        full[f"GIT_CONFIG_KEY_{i}"], full[f"GIT_CONFIG_VALUE_{i}"] = key, value
    full["GIT_CONFIG_COUNT"] = str(len(config or {}))
    return subprocess.run(["git", *args], cwd=cwd, env=full, capture_output=True, text=True)


# A credential helper that answers with the username and password in the
# environment, so neither is in any argv.
FROM_ENV = {"credential.helper": '!f() { echo "username=$U"; echo "password=$P"; }; f'}


def commits(bare: Path) -> list[str]:
    out = subprocess.run(
        ["git", "-C", str(bare), "log", "--format=%s", "main"], capture_output=True, text=True
    )
    return out.stdout.split("\n")[:-1] if out.returncode == 0 else []


@pytest.fixture
def local(tmp_path):
    repo = make_repo(tmp_path / "local")
    return repo


def test_a_push_without_a_token_is_refused(server, remote, local, tmp_path):
    bare, url = remote
    before = len(server.attempts)
    proc = git_as(local, tmp_path / "home", "push", url, "main")
    assert proc.returncode != 0
    assert "terminal prompts disabled" in proc.stderr, proc.stderr
    assert commits(bare) == []
    assert server.attempts[before:] == []  # nothing was offered


def test_a_push_with_a_wrong_token_is_refused(server, remote, local, tmp_path):
    bare, url = remote
    before = len(server.attempts)
    proc = git_as(local, tmp_path / "home", "push", url, "main", config=FROM_ENV, U="x", P="wrong")
    assert proc.returncode != 0
    assert "Authentication failed" in proc.stderr, proc.stderr
    assert commits(bare) == []
    assert server.attempts[before:] and all(a == ("x", False) for a in server.attempts[before:])


@pytest.mark.parametrize("username", ["x-access-token", "anyone"])
def test_a_push_with_the_token_and_any_username_is_accepted(
    server, remote, local, tmp_path, username
):
    bare, url = remote
    before = len(server.attempts)
    proc = git_as(
        local, tmp_path / "home", "push", url, "main", config=FROM_ENV, U=username, P=TOKEN
    )
    assert proc.returncode == 0, proc.stderr
    assert commits(bare) == ["First commit"]
    assert (username, True) in server.attempts[before:]
    assert all(TOKEN not in str(a) for a in server.attempts)  # the password is not kept


def test_reading_needs_no_token(server, remote, local, tmp_path):
    bare, url = remote
    fixture_git(local, "push", "-q", str(bare), "main")
    clone = tmp_path / "clone"
    proc = git_as(tmp_path, tmp_path / "home", "clone", "-q", url, str(clone))
    assert proc.returncode == 0, proc.stderr
    assert (clone / "README.md").exists()
    write(local / "b.txt")
    fixture_git(local, "add", "b.txt")
    fixture_git(local, "commit", "-q", "-m", "Second")
    fixture_git(local, "push", "-q", str(bare), "main")
    proc = git_as(clone, tmp_path / "home", "fetch", "-q")
    assert proc.returncode == 0, proc.stderr
    assert fixture_git(clone, "log", "-1", "--format=%s", "origin/main").strip() == "Second"


def test_a_repository_is_reached_with_and_without_dot_git(server, remote, local, tmp_path):
    bare, url = remote
    fixture_git(local, "push", "-q", str(bare), "main")
    without = url.removesuffix(".git")
    proc = git_as(tmp_path, tmp_path / "home", "ls-remote", without, "main")
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split()[0] == fixture_git(local, "rev-parse", "main").strip()


def test_github_url_is_the_real_one():
    assert GitHubServer.github_url("alice", "repo") == "https://github.com/alice/repo.git"


def stored(home: Path, url: str, token: str) -> None:
    """A credential for ``url``'s protocol and host in ``home``'s ~/.git-credentials."""
    protocol, _, host = url.partition("://")
    host = host.split("/", 1)[0]
    data = f"protocol={protocol}\nhost={host}\nusername=x-access-token\npassword={token}\n\n"
    proc = subprocess.run(
        ["git", "credential-store", "store"],
        env=fixture_env(home),
        input=data,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr


def test_a_credential_for_github_com_is_not_offered_to_the_rewritten_url(
    server, remote, local, tmp_path
):
    """The load-bearing assumption: insteadOf rewrites the URL before git looks up a
    credential, so rc-helper must store the token for the address it rewrites to."""
    bare, _ = remote
    github_url = f"https://github.com/owner/{bare.name}"
    hooked = {f"url.{server.url}.insteadOf": "https://github.com/"}
    home = tmp_path / "home"
    stored(home, "https://github.com", TOKEN)
    for_github = {**hooked, "credential.https://github.com.helper": "store"}
    proc = git_as(local, home, "push", github_url, "main", config=for_github)
    assert proc.returncode != 0 and commits(bare) == []
    base = server.url.removesuffix("/")
    stored(home, base, TOKEN)
    for_server = {**hooked, f"credential.{base}.helper": "store"}
    proc = git_as(local, home, "push", github_url, "main", config=for_server)
    assert proc.returncode == 0, proc.stderr  # positive control: stored for the rewritten URL
    assert commits(bare) == ["First commit"]
