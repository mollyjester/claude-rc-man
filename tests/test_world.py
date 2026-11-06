"""The world make screenshots shows (devtools/world.py): the backend makes it in a held folder
of its own and removes it when it shuts down, whether it stops, fails to build or cannot take
its port, stopping the tmux servers it started; alice's Claude is signed in and bob's is not."""

import os
import signal
import socket
import subprocess
from pathlib import Path

import pytest
import uvicorn
from fastapi.testclient import TestClient

from devtools import world
from tests.conftest import ORIGIN, as_user, changing
from tests.hang_guard import HANG_GUARD
from tests.until import until

# The length of a sleep that outlasts the test (rule (g) of "Time in tests").
OUTLASTING = f"{3 * HANG_GUARD:g}"


@pytest.fixture
def origin(monkeypatch):
    """The origin the world's rcman is told it is served at; nothing listens there."""
    monkeypatch.setenv("RCMAN_PUBLIC_ORIGIN", ORIGIN)
    return ORIGIN


@pytest.fixture
def started():
    """The tmux servers a test starts, by pid, killed afterwards if they still run."""
    pids: list[int] = []
    yield pids
    for pid in pids:
        if _running(pid):
            os.kill(pid, signal.SIGKILL)


def _start_tmux(sock: Path, started: list[int]) -> int:
    sock.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["tmux", "-S", str(sock), "-f", "/dev/null", "new-session", "-d", "sleep", OUTLASTING],
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


def test_the_backend_makes_its_world_and_removes_it_when_it_shuts_down(origin):
    app = world.create_app()
    root = app.state.world
    assert root.is_dir() and (root / "rcman.db").is_file()
    assert root.stat().st_mode & 0o777 == 0o700
    with TestClient(app):
        assert root.is_dir()
    assert not root.exists()


def test_a_world_that_fails_to_build_is_removed(origin, monkeypatch):
    made = []

    def broken(workspaces, *args, **kwargs):
        made.append(workspaces.parent)
        assert workspaces.parent.is_dir()  # positive control: the world was made before this
        raise RuntimeError("no runtime today")

    monkeypatch.setattr(world, "HostRuntime", broken)
    with pytest.raises(RuntimeError, match="no runtime today"):
        world.create_app()
    assert len(made) == 1 and not made[0].exists()


def test_shutting_down_stops_the_tmux_servers_it_started(origin, started):
    app = world.create_app()
    with TestClient(app):
        pid = _start_tmux(world.sockets(app.state.world) / "alice.sock", started)
    assert not _running(pid)


def test_alices_workspaces_are_signed_in_and_bobs_are_not(origin):
    with TestClient(world.create_app()) as client:
        for who, code in (("alice@example.com", 200), ("bob@example.com", 409)):
            made = _made(client, who)
            r = client.post("/api/servers", json={"workspace": made["id"]}, headers=changing(who))
            assert r.status_code == code, (who, r.text)
            if code == 409:
                assert r.json()["code"] == "claude_not_logged_in"


def _made(client, who):
    r = client.post("/api/workspaces", json={"name": "work"}, headers=changing(who))
    assert r.status_code == 202, r.text

    def made():
        [found] = client.get("/api/workspaces", headers=as_user(who)).json()["workspaces"]
        return None if found["state"] == "creating" else found

    found = until(made)
    assert found["state"] == "ready", found
    return found


def test_a_backend_that_cannot_take_its_port_removes_its_world(origin):
    """uvicorn starts the app before it binds; when the bind fails it shuts the app down and
    exits, and the world goes with it."""
    app = world.create_app()
    root = app.state.world
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None))
        with pytest.raises(SystemExit):
            server.run()
    assert server.lifespan.startup_event.is_set()  # positive control: the app had started
    assert not root.exists()
