"""Collection rules shared by the whole suite."""

import os

# tests/container/ runs as root inside the test container, started by
# tests/test_container_suite.py; elsewhere it is not collected at all.
collect_ignore = [] if os.environ.get("RCMAN_IN_CONTAINER") == "1" else ["container"]


# --- backend fixtures ------------------------------------------------------------

import json  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ORIGIN = "https://rcman.example.com"
ALICE, BOB, CAROL = "alice@example.com", "bob@example.com", "carol@example.com"


class RecordingAdmin:
    """Stands in for `sudo rc-admin`; records argv and answers from a script."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.replies: list[tuple[int, dict]] = []

    def __call__(self, args, timeout=None):
        self.calls.append(list(args))
        return self.replies.pop(0) if self.replies else (0, {"users": []})


# The token the tests' GitHub takes for a push.
GITHUB_TOKEN = "github-token-for-the-tests"


@pytest.fixture(scope="session")
def github(tmp_path_factory):
    """The tests' GitHub (tests/github_server.py), which takes GITHUB_TOKEN for a push."""
    from tests.github_server import GitHubServer

    server = GitHubServer()
    server.start(tmp_path_factory.mktemp("github"), GITHUB_TOKEN)
    yield server
    server.stop()


@pytest.fixture(scope="session", autouse=True)
def github_hook_works(github, tmp_path_factory):
    """Before any test: git as rc-helper runs it sends GitHub to the address the hook
    gives, the tests' GitHub or UNREACHABLE, so a broken hook stops the session before
    any test could reach the real GitHub."""
    from tests.github_server import UNREACHABLE
    from tests.helper_env import assert_github_hooked

    scratch = tmp_path_factory.mktemp("hook-check")
    assert_github_hooked(scratch, github.url, github)
    assert_github_hooked(scratch, UNREACHABLE)


@pytest.fixture
def helper_homes(tmp_path, github):
    """A real rc-helper per mapped Linux user, each with its own home and tmux socket,
    and https://github.com/ pointed at the tests' GitHub."""
    from devtools import fake_claude as fake
    from devtools import record_claude as rec
    from tests.helper_env import helper_env

    homes, sockets = {}, []
    wrapper = tmp_path / "claude"
    wrapper.write_text(f'#!/bin/sh\nFAKE_CLAUDE_READY_SECONDS=0.3 exec {rec.FAKE_CLAUDE} "$@"\n')
    wrapper.chmod(0o755)
    for linux in ("alice", "bob", "carol"):
        home = tmp_path / "homes" / linux
        (home / "projects" / f"{linux}-project").mkdir(parents=True)
        fake.write_state(home / ".claude-fake", logged_in=True)
        homes[linux] = home
    sock_root = Path("/tmp") / f"rcb-{tmp_path.name[-10:]}"
    sock_root.mkdir(exist_ok=True)

    def runner(linux, args, stdin=None, timeout=60):
        sock = sock_root / f"{linux}.sock"
        sockets.append(sock)
        env = helper_env(homes[linux], wrapper, sock, github.url)
        proc = subprocess.run(
            [str(REPO / "helpers" / "rc-helper"), *args],
            env=env, input=stdin, capture_output=True, text=True, timeout=timeout,
        )  # fmt: skip
        return proc.returncode, json.loads(proc.stdout)

    runner.homes = homes
    yield runner
    for sock in set(sockets):
        subprocess.run(["tmux", "-S", str(sock), "kill-server"], capture_output=True)


@pytest.fixture
def settings(tmp_path):
    from rcman.settings import Settings

    from helpers import users_file as uf

    users = tmp_path / "users.toml"
    uf.save(
        users,
        [uf.User(ALICE, "alice", True), uf.User(BOB, "bob", False), uf.User(CAROL, "carol", False)],
    )
    return Settings(
        public_origin=ORIGIN,
        users_file=users,
        database=tmp_path / "rcman.db",
        helpers_dir=REPO / "helpers",
        docs_dir=REPO / "docs",
    )


@pytest.fixture
def admin_runner():
    return RecordingAdmin()


@pytest.fixture
def app(settings, helper_homes, admin_runner):
    from rcman.main import create_app

    return create_app(settings, helper=helper_homes, admin=admin_runner)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c


# A valid request for each route that takes one: (method, path) -> keyword
# arguments for the test client. Tests that drive every route of a kind, found
# in the route table, take their requests from here.
CALLS = {
    ("POST", "/api/servers"): {"json": {"path": "projects/alice-project"}},
    ("POST", "/api/servers/resume"): {"json": {"path": "projects/alice-project"}},
    ("POST", "/api/servers/stop"): {"json": {"path": "projects/alice-project"}},
    ("POST", "/api/servers/forget"): {"json": {"path": "projects/alice-project"}},
    ("POST", "/api/servers/confirm"): {"json": {"path": "projects/alice-project", "answer": "y"}},
    ("POST", "/api/folders"): {"json": {"parent": "projects", "name": "new"}},
    ("DELETE", "/api/folders"): {"params": {"path": "projects/alice-project"}},
    ("PATCH", "/api/me/settings"): {"json": {"prefix": "alice"}},
    ("POST", "/api/git/stage"): {
        "json": {"path": "projects/alice-project", "files": ["README.md"]}
    },
    ("POST", "/api/git/unstage"): {
        "json": {"path": "projects/alice-project", "files": ["README.md"]}
    },
    ("POST", "/api/git/commit"): {
        "json": {"path": "projects/alice-project", "message": "A message"}
    },
    ("POST", "/api/git/switch"): {"json": {"path": "projects/alice-project", "branch": "main"}},
    ("POST", "/api/git/branch"): {"json": {"path": "projects/alice-project", "name": "new-branch"}},
    ("POST", "/api/git/fetch"): {"json": {"path": "projects/alice-project"}},
    ("POST", "/api/git/push"): {"json": {"path": "projects/alice-project"}},
    ("POST", "/api/git/commit-push"): {
        "json": {"path": "projects/alice-project", "message": "A message"}
    },
    ("GET", "/api/git/status"): {"params": {"path": "projects/alice-project"}},
    ("GET", "/api/git/branches"): {"params": {"path": "projects/alice-project"}},
    ("GET", "/api/github"): {},
    ("POST", "/api/github/token"): {"json": {"token": "a-github-token"}},
    ("DELETE", "/api/github/token"): {},
    ("POST", "/api/users"): {"json": {"linux": "dave", "email": "dave@example.com"}},
    ("DELETE", "/api/users/{linux}"): {"params": {"home": "keep"}},
    ("POST", "/api/claude/login"): {"json": {}},
    ("POST", "/api/claude/code"): {"json": {"code": "x"}},
    ("POST", "/api/claude/upgrade"): {"json": {"version": "2.2.0"}},
    ("GET", "/api/folders"): {"params": {"path": "projects"}},
    ("GET", "/api/servers/output"): {"params": {"path": "projects/alice-project"}},
    ("GET", "/api/docs/{slug}"): {},
    ("GET", "/api/audit"): {},
    ("GET", "/api/users"): {},
    ("GET", "/api/claude/versions"): {},
    ("GET", "/api/servers/everyone"): {},
    ("GET", "/api/attention"): {},
}


def concrete(path):
    """A route's path with its parameters filled in."""
    return path.replace("{linux}", "bob").replace("{slug}", "servers")


def as_user(email):
    return {"X-Forwarded-Email": email}


def changing(email):
    return {"X-Forwarded-Email": email, "Origin": ORIGIN}
