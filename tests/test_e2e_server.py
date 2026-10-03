"""The browser tests: their backend, which leaves nothing running behind it, and the
Playwright configs that run them."""

import json
import os
import shutil
import signal
import socket
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests import e2e_server as e2e

REPO = Path(__file__).resolve().parent.parent
FRONTEND = REPO / "frontend"
CONFIGS = ("playwright.config.ts", "playwright.stack.config.ts")
TOKEN = "the-worlds-github-token"


@pytest.fixture
def world(monkeypatch):
    # Under /tmp, not tmp_path: a unix socket's path has to stay short.
    root = Path(tempfile.mkdtemp(prefix="rce2e-test-", dir="/tmp"))
    monkeypatch.setenv("RCMAN_E2E_DIR", str(root))
    monkeypatch.setenv("RCMAN_PUBLIC_ORIGIN", "http://127.0.0.1:8765")
    monkeypatch.setenv("RCMAN_E2E_GITHUB_TOKEN", TOKEN)
    started = []
    yield root, started
    for pid in started:
        if _running(pid):
            os.killpg(pid, signal.SIGKILL)
    shutil.rmtree(root, ignore_errors=True)


def _start_tmux(sock: Path, started: list[int]) -> int:
    sock.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["tmux", "-S", str(sock), "-f", "/dev/null", "new-session", "-d", "sleep", "600"],
        check=True,
    )
    pid = int(
        subprocess.run(
            ["tmux", "-S", str(sock), "display-message", "-p", "#{pid}"],
            check=True, capture_output=True, text=True,
        ).stdout
    )  # fmt: skip
    started.append(pid)
    assert _running(pid)
    return pid


def _running(pid: int) -> bool:
    # By process, not by socket: deleting the socket leaves the server running.
    return Path(f"/proc/{pid}").exists()


def test_sockets_live_inside_the_world(world):
    root, _ = world
    assert e2e.sockets(root).parent == root


def test_building_the_world_stops_a_tmux_server_an_earlier_run_left(world):
    root, started = world
    pid = _start_tmux(e2e.sockets(root) / "alice.sock", started)
    e2e.create_app()
    assert not _running(pid)


def test_shutting_down_stops_the_tmux_servers_it_started(world):
    root, started = world
    app = e2e.create_app()
    with TestClient(app):
        pid = _start_tmux(e2e.sockets(root) / "alice.sock", started)
    assert not _running(pid)


def test_playwright_stops_the_backend_so_its_shutdown_runs():
    # By default Playwright SIGKILLs webServer, and no shutdown code runs.
    config = (REPO / "frontend" / "playwright.config.ts").read_text(encoding="utf-8")
    assert 'gracefulShutdown: { signal: "SIGTERM"' in config


def test_alice_has_a_repository_with_an_origin(world):
    from tests.git_fixtures import fixture_git

    root, _ = world
    e2e.create_app()
    repo = root / "homes" / "alice" / "projects" / "alice-repo"
    origin = root / "origins" / "alice" / "alice-repo.git"
    url = "https://github.com/alice/alice-repo.git"
    assert fixture_git(repo, "remote", "get-url", "origin").strip() == url
    on_origin = fixture_git(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    assert on_origin.split() == ["feature", "later", "main"]
    known = fixture_git(repo, "for-each-ref", "--format=%(refname:short)", "refs/remotes")
    assert known.split() == ["origin/feature", "origin/main"]  # later needs a fetch
    assert fixture_git(repo, "branch", "--show-current").strip() == "main"
    assert not (root / "homes" / "bob" / "projects" / "alice-repo").exists()


def test_shutting_down_removes_the_fixtures_git_home(world):
    """uvicorn ends itself with the signal it was stopped by, so no exit handler runs:
    the backend's own shutdown removes what the world's git fixtures left."""
    from tests.git_fixtures import FIXTURE_HOME

    app = e2e.create_app()
    assert FIXTURE_HOME.exists()  # positive control: building the world used it
    with TestClient(app):
        pass
    assert not FIXTURE_HOME.exists()


def test_the_worlds_github_serves_alices_origin_and_takes_a_push_with_its_token_only(world):
    import subprocess

    from tests.conftest import ALICE, as_user
    from tests.git_fixtures import fixture_env, fixture_git, write

    root, _ = world
    app = e2e.create_app()
    repo = root / "homes" / "alice" / "projects" / "alice-repo"
    origin = root / "origins" / "alice" / "alice-repo.git"
    with TestClient(app) as client:
        github = app.state.github
        assert github.token == TOKEN
        status = client.get(
            "/api/git/status", params={"path": "projects/alice-repo"}, headers=as_user(ALICE)
        )
        assert status.json()["push_to_github"] is True  # the helper has the hook
        fetched = client.post(
            "/api/git/fetch",
            json={"path": "projects/alice-repo"},
            headers={**as_user(ALICE), "Origin": "http://127.0.0.1:8765"},
        )
        assert fetched.status_code == 200, fetched.text  # read from the world's GitHub
        write(repo / "pushed.txt")
        fixture_git(repo, "add", "pushed.txt")
        fixture_git(repo, "commit", "-q", "-m", "Pushed")
        env = {
            **fixture_env(root / "pusher"),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": f"url.{github.url}.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://github.com/",
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": '!f() { echo username=alice; echo "password=$P"; }; f',
        }
        args = ["git", "-C", str(repo), "push", "origin", "HEAD:refs/heads/pushed"]
        refused = subprocess.run(args, env={**env, "P": "wrong"}, capture_output=True, text=True)
        assert refused.returncode != 0
        accepted = subprocess.run(args, env={**env, "P": TOKEN}, capture_output=True, text=True)
        assert accepted.returncode == 0, accepted.stderr
    head = fixture_git(repo, "rev-parse", "HEAD").strip()
    assert fixture_git(origin, "rev-parse", "refs/heads/pushed").strip() == head


def test_the_worlds_github_has_a_repository_to_clone_that_no_home_holds(world):
    from tests.git_fixtures import fixture_git

    root, _ = world
    e2e.create_app()
    origin = root / "origins" / "alice" / "demo.git"
    on_origin = fixture_git(origin, "for-each-ref", "--format=%(refname:short)", "refs/heads")
    assert on_origin.split() == ["feature", "main"]
    assert fixture_git(origin, "show", "main:README.md")  # a file to find in the clone
    for home in (root / "homes").iterdir():
        assert not list(home.rglob("demo")), home


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def listed(config: str, env: dict[str, str]) -> dict:
    """What Playwright makes of a config, read with --list, which starts no web server."""
    proc = subprocess.run(
        ["npx", "playwright", "test", "--config", config, "--list", "--reporter=json"],
        cwd=FRONTEND, env=env, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_the_stack_browser_config_refuses_to_run_without_the_stack_port():
    """No port is assumed: only tests/test_container_suite.py's run knows the port its
    container published."""
    env = {k: v for k, v in os.environ.items() if k != "RCMAN_STACK_PORT"}
    proc = subprocess.run(
        ["npx", "playwright", "test", "--config", "playwright.stack.config.ts", "--list"],
        cwd=FRONTEND, env=env, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert proc.returncode != 0, proc.stdout
    assert "RCMAN_STACK_PORT" in proc.stdout + proc.stderr
    assert "tests/test_container_suite.py" in proc.stdout + proc.stderr
    env["RCMAN_STACK_PORT"] = str(_free_port())
    listing = subprocess.run(
        ["npx", "playwright", "test", "--config", "playwright.stack.config.ts", "--list"],
        cwd=FRONTEND, env=env, capture_output=True, text=True, timeout=120,
    )  # fmt: skip
    assert listing.returncode == 0, listing.stderr  # positive control: given one, it loads


def test_the_two_browser_configs_write_to_different_output_folders():
    # Why each has its own: project-knowledge/frontend.md.
    env = {**os.environ, "RCMAN_STACK_PORT": str(_free_port())}
    folders = {}
    for config in CONFIGS:
        listing = listed(config, env)
        assert listing["suites"], config  # positive control: the config's specs are read
        (project,) = listing["config"]["projects"]
        folders[config] = Path(project["outputDir"])
    (one, other) = folders.values()
    assert not one.is_relative_to(other) and not other.is_relative_to(one), folders
    for folder in folders.values():
        ignored = subprocess.run(["git", "check-ignore", "-q", str(folder)], cwd=REPO)
        assert ignored.returncode == 0, folder
