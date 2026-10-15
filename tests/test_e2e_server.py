"""The browser tests: their backend, which leaves nothing running behind it, and the
Playwright configs that run them."""

import fcntl
import hashlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import threading
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from devtools import held_folders
from devtools.held_folders import held, remove_tree
from tests import e2e_server as e2e
from tests import killed_runs as kr

REPO = Path(__file__).resolve().parent.parent
FRONTEND = REPO / "frontend"
CONFIGS = ("playwright.config.ts", "playwright.stack.config.ts")
ORIGIN = "http://rcman.test"  # what the browser would send; nothing listens there


def _named(tag: str, lead: str = "7") -> str:
    """A world's name, shaped as frontend/e2e/world.ts makes one, told apart by ``tag``;
    ``lead`` decides where it sorts."""
    tail = hashlib.sha256(tag.encode()).hexdigest()[:12]
    return f"rcman-e2e-{lead * 8}-0000-4000-8000-{tail}"


# What the folder each test's world is made in is named with.
WORLD_PARENTS = "rce2e-test-"


@pytest.fixture
def world(monkeypatch):
    """Where a run of the browser tests would name its world, not yet made, in a held
    folder of its own that stands for /tmp: worlds a killed run left are looked for
    beside it. Afterwards the folder is removed with every tmux server and changed
    permission in it, and one a killed test left is removed by the next."""
    # Under /tmp, not tmp_path: a unix socket's path has to stay short.
    with held(WORLD_PARENTS, remover=remove_tree) as parent:
        root = parent / _named("this-run")
        monkeypatch.setenv("RCMAN_E2E_DIR", str(root))
        monkeypatch.setenv("RCMAN_PUBLIC_ORIGIN", ORIGIN)
        started = []
        yield root, started
        for pid in started:
            if _running(pid):
                os.killpg(pid, signal.SIGKILL)


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


def test_the_backend_makes_the_world_and_removes_it_when_it_shuts_down(world):
    root, _ = world
    assert not root.exists()
    app = e2e.create_app()
    assert root.is_dir() and (root / "rcman.db").is_file()
    assert root.stat().st_mode & 0o777 == 0o700
    with TestClient(app):
        assert root.is_dir()
    assert not root.exists()


def test_a_world_that_already_exists_is_refused(world):
    """Whatever the folder holds is someone else's: it is neither used nor removed."""
    root, _ = world
    root.mkdir()
    planted = root / "planted"
    planted.write_text("not the backend's")
    with pytest.raises(FileExistsError):
        e2e.create_app()
    assert planted.read_text() == "not the backend's"


def test_a_world_that_fails_to_build_is_removed(world, monkeypatch):
    root, _ = world

    def broken(*args, **kwargs):
        assert root.is_dir()  # positive control: the world was made before this
        raise RuntimeError("no runtime today")

    monkeypatch.setattr(e2e, "HostRuntime", broken)
    held = _holding_locks(monkeypatch)
    with pytest.raises(RuntimeError, match="no runtime today"):
        e2e.create_app()
    assert not root.exists()
    assert len(held) == 1 and held[0].closed  # the lock is released, not left to the GC


def _holding_locks(monkeypatch) -> list:
    """Every world lock the backend takes, as it takes it."""
    held, take = [], e2e._lock
    monkeypatch.setattr(e2e, "_lock", lambda root: held.append(take(root)) or held[-1])
    return held


def test_shutting_down_releases_the_worlds_lock(world):
    app = e2e.create_app()
    assert not app.state.world_lock.closed
    with TestClient(app):
        pass
    assert app.state.world_lock.closed


def test_the_lock_is_released_when_removing_the_world_fails(world):
    root, _ = world
    app = e2e.create_app()

    def fails(root):
        raise OSError("cannot remove it today")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(e2e, "_remove", fails)
        with pytest.raises(OSError, match="cannot remove it today"), TestClient(app):
            pass
    assert app.state.world_lock.closed
    e2e._remove(root)


def test_a_backend_that_cannot_take_its_port_removes_its_world(world):
    """uvicorn builds the app and starts it before it binds; when the bind fails it shuts
    the app down, and the world goes with it."""
    root, _ = world
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        proc = subprocess.run(
            [sys.executable, "-m", "uvicorn", "--factory", "tests.e2e_server:create_app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=REPO, env=os.environ, capture_output=True, text=True, timeout=120,
        )  # fmt: skip
    assert proc.returncode != 0
    assert "address already in use" in proc.stderr  # positive control: the bind failed
    assert "Application startup complete" in proc.stderr  # after the world was built
    assert not root.exists()


def _abandoned(parent: Path, name: str, started: list[int]) -> tuple[Path, int]:
    """A world as a killed run leaves it: its lock file, which nothing holds, and a tmux
    server still running on a socket in it."""
    left = parent / name
    left.mkdir()
    (left / ".lock").touch()
    pid = _start_tmux(e2e.sockets(left) / "alice.sock", started)
    return left, pid


def test_building_a_world_stops_and_removes_one_a_killed_run_left(world):
    root, started = world
    left, pid = _abandoned(root.parent, _named("killed"), started)
    unlike, unlike_pid = _abandoned(root.parent, "not-a-world", started)
    assert e2e.is_world(left.name) and not e2e.is_world(unlike.name)
    with TestClient(e2e.create_app()):
        assert not _running(pid) and not left.exists()
        assert _running(unlike_pid) and unlike.exists()  # only what the pattern names


@pytest.mark.parametrize(
    "name",
    [
        "rcman-e2e-x",
        "rcman-e2e-worktree",
        "rcman-e2e-77777777-0000-4000-8000-00000000000",
        "rcman-e2e-77777777-0000-4000-8000-0000000000000",
        "rcman-e2e-77777777-0000-4000-8000-00000000000G",
        "xrcman-e2e-77777777-0000-4000-8000-000000000000",
    ],
)
def test_a_folder_named_unlike_a_world_is_left(world, name):
    """Only the shape frontend/e2e/world.ts names a world with is cleared, so a folder
    such as a checkout that happens to begin like one is never taken for one."""
    root, started = world
    left, pid = _abandoned(root.parent, _named("killed"), started)
    unlike, unlike_pid = _abandoned(root.parent, name, started)
    e2e.clear_abandoned(root.parent)
    assert not left.exists() and not _running(pid)  # positive control
    assert unlike.is_dir() and _running(unlike_pid)


def test_the_world_shape_is_the_one_world_ts_names():
    source = (REPO / "frontend" / "e2e" / "world.ts").read_text(encoding="utf-8")
    assert "`rcman-e2e-${randomUUID()}`" in source
    import uuid

    assert e2e.is_world(f"rcman-e2e-{uuid.uuid4()}")


def test_the_address_the_browser_tests_clone_is_the_one_the_world_has():
    source = (FRONTEND / "e2e" / "nav.ts").read_text(encoding="utf-8")
    assert f'GITHUB_ADDRESS = "{e2e.SEEDED_ADDRESS}"' in source


def test_a_world_is_removed_while_its_lock_is_held(world, monkeypatch):
    """So no other run clearing at the same time takes it for abandoned meanwhile."""
    root, started = world
    left, _ = _abandoned(root.parent, _named("killed"), started)
    seen, stop = [], e2e._stop_tmux

    def stopping(world):
        with open(world / ".lock") as other:
            try:
                fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
                seen.append("free")
            except BlockingIOError:
                seen.append("held")
        stop(world)

    monkeypatch.setattr(e2e, "_stop_tmux", stopping)
    e2e.clear_abandoned(root.parent)
    assert seen == ["held"] and not left.exists()


def test_a_world_cleared_and_made_again_meanwhile_is_left_alone(world):
    """Between opening a world's lock file and locking it, another run may clear the
    world and a new one make it again under the same name, holding a lock of its own."""
    root, started = world
    left, _ = _abandoned(root.parent, _named("reused"), started)
    lock, again = fcntl.flock, []

    def meanwhile(fd, operation):
        if not again:
            again.append(None)
            e2e._remove(left)
            left.mkdir(mode=0o700)
            again[0] = e2e._lock(left)  # the new run's, through its own flock
        return lock(fd, operation)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fcntl, "flock", meanwhile)
        e2e.clear_abandoned(root.parent)
    assert again and left.is_dir() and (left / ".lock").exists()
    again[0].close()


def test_a_world_is_not_cleared_while_its_lock_is_being_taken(world):
    """Its lock file appears only once its lock is held, so another run clearing at that
    moment finds a world still being made."""
    root, _ = world
    lock, cleared = fcntl.flock, []

    def clearing_first(fd, operation):
        if not cleared:
            cleared.append(True)
            e2e.clear_abandoned(root.parent)
        return lock(fd, operation)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(fcntl, "flock", clearing_first)
        app = e2e.create_app()
    assert cleared  # positive control: another run cleared while the lock was taken
    with TestClient(app):
        held = os.fstat(app.state.world_lock.fileno()).st_ino
        assert (root / ".lock").stat().st_ino == held  # the world it made, and locked


def test_removing_a_world_takes_its_lock_file_last(world):
    """A run killed while removing its world leaves the lock file for the next to find."""
    root, _ = world
    app = e2e.create_app()
    unlink, left = os.unlink, []

    def unlinking(path, *args, **kwargs):
        if os.path.basename(path) == ".lock":
            left.append(sorted(os.listdir(root)))
        return unlink(path, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(os, "unlink", unlinking)
        with TestClient(app):
            pass
    assert left == [[".lock"]] and not root.exists()


def test_a_world_long_without_a_lock_file_is_cleared(world):
    """One a run was killed in the instant between making it and taking its lock."""
    root, _ = world
    left = root.parent / _named("killed-while-making")
    left.mkdir()
    (left / ".locking").touch()
    old = left.stat().st_mtime - held_folders.UNLOCKED_FOR - 1
    os.utime(left, (old, old))
    with TestClient(e2e.create_app()):
        assert root.is_dir()  # positive control: the world was built
    assert not left.exists()


def _victim(parent: Path, started: list[int]) -> tuple[Path, int]:
    """A tmux server outside every world, which clearing must never stop."""
    sock = parent / "victim" / "v.sock"
    return sock, _start_tmux(sock, started)


def _later_world(parent: Path, started: list[int]) -> tuple[Path, int]:
    """An abandoned world that sorts after every odd one, so clearing it shows the odd
    ones did not stop the clearing."""
    return _abandoned(parent, _named("zz-killed", lead="f"), started)


def _locked_world(parent: Path, name: str) -> Path:
    """A world's folder with a lock file nothing holds, and nothing running in it."""
    (parent / name).mkdir()
    (parent / name / ".lock").touch()
    return parent / name


def _odd_symlinked_world(parent, name, started):
    elsewhere = _locked_world(parent, "elsewhere")
    (elsewhere / "keep").touch()
    (parent / name).symlink_to(elsewhere)
    return elsewhere / "keep", []


def _odd_lock_folder(parent, name, started):
    (parent / name / ".lock").mkdir(parents=True)
    return parent / name / ".lock", []


def _odd_lock_fifo(parent, name, started):
    (parent / name).mkdir()
    os.mkfifo(parent / name / ".lock")
    return parent / name / ".lock", []


def _odd_unreadable(parent, name, started):
    left = _locked_world(parent, name)
    left.chmod(0)
    return left, []


def _odd_partly_removable(parent, name, started):
    stuck = _locked_world(parent, name) / "stuck"
    stuck.mkdir()
    (stuck / "file").touch()
    stuck.chmod(0o500)
    return stuck / "file", []


def _odd_socket_link(parent, name, started):
    sockets = e2e.sockets(_locked_world(parent, name))
    sockets.mkdir()
    victim, pid = _victim(parent, started)
    (sockets / "x.sock").symlink_to(victim)
    return victim, [pid]


def _odd_sockets_folder_link(parent, name, started):
    victim, pid = _victim(parent, started)
    e2e.sockets(_locked_world(parent, name)).symlink_to(victim.parent)
    return victim, [pid]


@pytest.mark.parametrize(
    "odd",
    [
        _odd_symlinked_world,
        _odd_lock_folder,
        _odd_lock_fifo,
        _odd_unreadable,
        _odd_partly_removable,
        _odd_socket_link,
        _odd_sockets_folder_link,
    ],
)
def test_clearing_harms_nothing_but_abandoned_worlds_of_its_own(world, odd):
    """What a name like a world's may be besides: none of it stops the clearing, nothing
    outside a world is touched, and no tmux server outside one is stopped."""
    root, started = world
    kept, victims = odd(root.parent, _named("a-odd", lead="0"), started)
    later, later_pid = _later_world(root.parent, started)
    clearing = threading.Thread(target=e2e.clear_abandoned, args=(root.parent,), daemon=True)
    clearing.start()
    clearing.join(timeout=60)
    if clearing.is_alive():  # stuck opening a FIFO: give it a writer, then fail
        os.close(os.open(kept, os.O_WRONLY | os.O_NONBLOCK))
        pytest.fail("clearing hung")
    assert os.path.lexists(kept)
    assert all(_running(pid) for pid in victims)
    assert not later.exists() and not _running(later_pid)  # it went on to the next


def test_a_world_another_user_owns_is_left(world):
    """Only the clearing user's own worlds are cleared: here a world of the current user,
    cleared as though by another, since the tests have no second user to make one."""
    root, started = world
    left, pid = _abandoned(root.parent, _named("killed"), started)
    e2e.clear_abandoned(root.parent, uid=os.getuid() + 1)
    assert left.is_dir() and _running(pid)
    e2e.clear_abandoned(root.parent)
    assert not left.exists() and not _running(pid)  # positive control: its own user clears it


def test_a_world_whose_backend_still_runs_is_left_alone(world, monkeypatch):
    root, started = world
    running = e2e.create_app()  # holds its world's lock while it lives
    pid = _start_tmux(e2e.sockets(root) / "alice.sock", started)
    other = root.parent / _named("other-run")
    monkeypatch.setenv("RCMAN_E2E_DIR", str(other))
    with TestClient(e2e.create_app()):
        assert other.is_dir()  # positive control: the second world was built beside it
        assert root.is_dir() and _running(pid)
    with TestClient(running):
        pass
    assert not root.exists() and not _running(pid)


def test_a_world_still_being_made_is_left_alone(world):
    """A world gets its lock file only once its lock is held, so one without is being
    made by a run that has not taken it yet."""
    root, _ = world
    making = root.parent / _named("making")
    making.mkdir()
    (making / "rcman.db").write_text("")
    with TestClient(e2e.create_app()):
        assert root.is_dir()  # positive control: the world was built
    assert (making / "rcman.db").exists()


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


def test_alices_workspaces_are_signed_in_and_bobs_are_not(world):
    from tests.conftest import as_user

    with TestClient(e2e.create_app()) as client:
        for who, code in (("alice@example.com", 200), ("bob@example.com", 409)):
            made = _made(client, who)
            r = client.post(
                "/api/servers",
                json={"workspace": made["id"]},
                headers={**as_user(who), "Origin": ORIGIN},
            )
            assert r.status_code == code, (who, r.text)
            if code == 409:
                assert r.json()["code"] == "claude_not_logged_in"


def _made(client, who):
    import time

    from tests.conftest import as_user

    r = client.post(
        "/api/workspaces", json={"name": "work"}, headers={**as_user(who), "Origin": ORIGIN}
    )
    assert r.status_code == 202, r.text
    for _ in range(200):
        [found] = client.get("/api/workspaces", headers=as_user(who)).json()["workspaces"]
        if found["state"] == "ready":
            return found
        time.sleep(0.05)
    raise AssertionError(found)


def _cloned(client, who, url, name=None):
    """The workspace a clone of ``url`` made, once it is no longer being made."""
    import time

    from tests.conftest import as_user

    body = {"url": url, **({"name": name} if name else {})}
    r = client.post("/api/git/clone", json=body, headers={**as_user(who), "Origin": ORIGIN})
    assert r.status_code == 202, r.text
    made = r.json()["workspace"]
    for _ in range(400):
        [found] = [
            w
            for w in client.get("/api/workspaces", headers=as_user(who)).json()["workspaces"]
            if w["id"] == made["id"]
        ]
        if found["state"] != "creating":
            return found
        time.sleep(0.05)
    raise AssertionError(found)


def test_the_world_clones_an_address_on_github_from_a_repository_of_its_own(world):
    """The world has a public repository at the address the browser tests give, and a
    clone of it is what the page shows: ready, on main, with the files."""
    from tests.conftest import as_user

    root, _ = world
    with TestClient(e2e.create_app()) as client:
        found = _cloned(client, "alice@example.com", e2e.SEEDED_ADDRESS)
        assert found["state"] == "ready" and found["name"] == "widget", found
        assert found["source"] == e2e.SEEDED_ADDRESS and found["log"] is None
        status = client.get(
            "/api/git/status",
            params={"workspace": found["id"]},
            headers=as_user("alice@example.com"),
        )
        assert status.status_code == 200, status.text
        assert status.json()["branch"]["name"] == "main"
        assert (root / "workspaces").is_dir()
        assert any(root.glob("workspaces/**/README.md"))


def test_an_address_the_world_has_no_repository_for_fails_with_what_git_said_and_reaches_nothing(
    world,
):
    with TestClient(e2e.create_app()) as client:
        found = _cloned(client, "alice@example.com", "https://github.com/octo/not-there")
    assert found["state"] == "failed", found
    assert "git clone failed, exit code 128:" in found["error"], found["error"]
    assert "not-there" in found["error"]  # git looked in the world's own folder, nowhere else
    assert "github.com" not in found["error"].replace("https://github.com/octo/not-there", "")


def test_every_git_clone_of_the_world_is_taken_from_its_repositories_or_refused(world):
    """The guard that keeps the browser tests off the real github.com: a clone the world's
    runtime is asked for is told to take GitHub from the world's own folder, and the very
    wrapper refuses one that is not told so."""
    root, _ = world
    asked = []

    class Recording:
        def exec(self, name, argv, *args, **kwargs):
            asked.append(argv)

    runtime = Recording()
    github = root / "github"
    e2e.keep_off_github(runtime, github)
    runtime.exec("w", ["timeout", "git", "-C", "/w", "clone", "--", e2e.SEEDED_ADDRESS, "."])
    [argv] = asked
    assert f"url.{github}/.insteadOf=https://github.com/" in argv  # the positive
    assert argv[argv.index("clone") - 1] == "protocol.git.allow=never"
    runtime.exec("w", ["git", "status"])  # anything but a clone as it is
    assert asked[-1] == ["git", "status"]
    # the planted twin: a rewrite that does nothing is refused, not run
    from tests import git_fixtures

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(git_fixtures, "through_stand_in", lambda argv, base: argv)
        with pytest.raises(AssertionError, match="would reach github.com"):
            runtime.exec("w", ["git", "clone", "--", e2e.SEEDED_ADDRESS, "."])
    assert len(asked) == 2  # the refused one was never run


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


def _identity(env: dict[str, str]) -> tuple[str, str]:
    """The world and port a run of the browser tests names, and the checks that the
    backend, its address and the browser's origin all agree on them."""
    listing = listed("playwright.config.ts", env)
    assert "workspaces.spec.ts" in {s["file"] for s in listing["suites"]}  # positive control
    server = listing["config"]["webServer"]
    port = re.search(r" --port (\S+)", server["command"])[1]
    origin = server["env"]["RCMAN_PUBLIC_ORIGIN"]
    assert str(urlsplit(origin).port) == port
    assert server["url"] == origin + "/api/health"
    return server["env"]["RCMAN_E2E_DIR"], port


def _without_a_world() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("RCMAN_E2E_")}


def test_two_runs_of_the_browser_tests_name_different_worlds_and_ports():
    world, port = _identity(_without_a_world())
    # The first run's port is taken before the second chooses, as its backend would take
    # it, so a port chosen at run time differs and a fixed one is the same.
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", int(port)))
        other_world, other_port = _identity(_without_a_world())
    assert world != other_world and port != other_port
    for named in (world, other_world):
        assert Path(named).is_absolute() and not Path(named).exists()  # the backend makes it
        assert e2e.is_world(Path(named).name)  # so a killed run's is cleared


def test_a_run_keeps_the_world_and_port_it_is_given(tmp_path):
    given = (str(tmp_path / "given"), str(_free_port()))
    env = {**_without_a_world(), "RCMAN_E2E_DIR": given[0], "RCMAN_E2E_PORT": given[1]}
    assert _identity(env) == given


# --- what a killed run leaves of a test's world ---------------------------------------

USES_A_WORLD = """
import os
from pathlib import Path
from tests import killed_runs as kr
from tests import test_e2e_server as t
from tests.test_e2e_server import world  # noqa: F401 -- the fixture itself

t.WORLD_PARENTS = {prefix!r}

def test_one(world):
    root, started = world
    left = os.environ.get("LEFT")
    if left:  # the next run: its world's fixture has cleared what the killed one left
        kr.report("next", {{"cleared": not os.path.lexists(left)}})
        return
    pid = t._start_tmux(t.e2e.sockets(root) / "alice.sock", started)
    (root / "shut").mkdir()
    (root / "shut").chmod(0)
    kr.report("one", {{"paths": {{"parent": str(root.parent)}}, "pids": {{"tmux": pid}}}})
    kr.hold()
"""


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGKILL], ids=["SIGTERM", "SIGKILL"])
def test_the_next_world_clears_what_a_killed_runs_world_left(tmp_path, sig):
    """Its folder, the tmux servers in it and a folder the test shut are all cleared."""
    with kr.stem() as stem:
        source = {"test_world.py": USES_A_WORLD.format(prefix=stem + WORLD_PARENTS)}
        with kr.KilledRun(tmp_path / "killed", source, (stem,)) as run:
            reported = run.reports.wait("one")
            run.kill(sig)
        parent, pid = Path(reported["paths"]["parent"]), reported["pids"]["tmux"]
        assert parent.is_dir() and kr.running(pid)  # positive control: the kill left them
        with kr.KilledRun(tmp_path / "next", source, (stem,), env={"LEFT": str(parent)}) as run:
            assert run.reports.wait("next") == {"cleared": True}
            code, output = run.finish()
        assert code == 0, output
        assert not os.path.lexists(parent) and not kr.running(pid)
