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
from tests.hang_guard import HANG_GUARD
from tests.time_doubles import ControlledEvent
from tests.until import until

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


def test_a_push_is_taken_only_with_the_token_under_any_username(server, remote, local, tmp_path):
    bare, url = remote
    before = len(server.attempts)
    refused = git_as(local, tmp_path / "home", "push", url, "main")
    assert refused.returncode != 0 and commits(bare) == []
    proc = git_as(
        local, tmp_path / "home", "push", url, "main", config=FROM_ENV, U="anyone", P=TOKEN
    )
    assert proc.returncode == 0, proc.stderr
    assert commits(bare) == ["First commit"]
    assert ("anyone", True) in server.attempts[before:]
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


def test_requests_wait_while_the_gate_is_closed_and_go_on_once_it_is_opened(
    server, remote, tmp_path, monkeypatch
):
    """A test that needs a slow GitHub closes the gate: a request is logged, then waits."""
    _, url = remote
    monkeypatch.setattr(server, "gate", ControlledEvent())  # closed, and its waits recorded
    try:
        before = len(server.requests)
        waiting = subprocess.Popen(
            ["git", "ls-remote", url],
            cwd=tmp_path,
            env={
                **fixture_env(tmp_path / "home"),
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "0",
            },
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        until(lambda: len(server.requests) > before)  # positive control: it reached the server
        server.gate.wait_waiting(1)  # and waits at the gate
        assert waiting.poll() is None  # so it has no answer yet
    finally:
        server.gate.set()
    assert waiting.wait(timeout=HANG_GUARD) == 0, waiting.stderr.read()
