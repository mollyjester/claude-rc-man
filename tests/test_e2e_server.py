"""The browser tests' backend leaves no tmux server running behind it."""

import os
import shutil
import signal
import subprocess
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests import e2e_server as e2e

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def world(monkeypatch):
    # Under /tmp, not tmp_path: a unix socket's path has to stay short.
    root = Path(tempfile.mkdtemp(prefix="rce2e-test-", dir="/tmp"))
    monkeypatch.setenv("RCMAN_E2E_DIR", str(root))
    monkeypatch.setenv("RCMAN_PUBLIC_ORIGIN", "http://127.0.0.1:8765")
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
    origin = root / "origins" / "alice-repo.git"
    assert fixture_git(repo, "remote", "get-url", "origin").strip() == str(origin)
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
