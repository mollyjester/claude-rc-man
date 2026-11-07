"""Collection rules, the hang guard's hooks and the fixtures shared by the whole suite."""

import os
import signal
import sys
from pathlib import Path

# The guards over how the suite is split run small pytest projects of their own; the test
# doubles for time (tests/time_doubles.py) are held to their hang bound.
pytest_plugins = ["pytester", "tests.time_doubles"]

import pytest  # noqa: E402
from rcman.forwarding_settings import DEFAULTS as forwarding_defaults  # noqa: E402
from rcman.monitor import DEFAULTS as monitor_defaults  # noqa: E402

from tests import container_marker, hang_guard  # noqa: E402


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    """Before -m selects: the derived markers (tests/container_marker.py), and the container
    tests' hang guard, which follows the container marker (tests/hang_guard.py)."""
    container_marker.mark(items)
    if config.pluginmanager.has_plugin("timeout"):  # its marker is not known without it
        hang_guard.mark(items, float(config.getini(hang_guard.CONTAINER_TIMEOUT)))


def _ignore_sighup(signum, frame) -> None:
    """SIGHUP caught and dropped: what an ignore the suite was started with does for the
    suite, which a program it starts does not inherit."""


def pytest_configure(config):
    """A suite started with SIGHUP ignored, as under nohup, catches it instead: an ignore
    survives exec into the programs a test starts, a terminal's shell among them, which a
    hangup then does not end; a caught signal is back at its default in each of them."""
    if signal.getsignal(signal.SIGHUP) is signal.SIG_IGN:
        signal.signal(signal.SIGHUP, _ignore_sighup)


# --- the hang guard (tests/hang_guard.py; project-knowledge/tests.md, "The hang guard") ---


def pytest_addoption(parser):
    parser.addini(hang_guard.CONTAINER_TIMEOUT, "The hang guard of the container tests, seconds")


_INTERACTING = pytest.StashKey[bool]()


@pytest.hookimpl(wrapper=True)
def pytest_exception_interact(node):
    """Marks the cancel pytest-timeout asks for when a setup or call fails, so that
    pytest_timeout_cancel_timer can tell it from the one at the test's end."""
    node.stash[_INTERACTING] = True
    try:
        return (yield)
    finally:
        node.stash[_INTERACTING] = False


@pytest.hookimpl(tryfirst=True, optionalhook=True)  # none without the plugin
def pytest_timeout_cancel_timer(item):
    """pytest-timeout cancels the guard when a setup or call fails, which would leave the
    teardown, a session fixture's among it, unguarded: that cancel is taken (the hook's first
    result ends it) unless the run is to enter the debugger. The cancel at the test's end goes
    through."""
    if item.stash.get(_INTERACTING, False) and not item.config.getvalue("usepdb"):
        return True
    return None


@pytest.hookimpl(tryfirst=True, optionalhook=True)  # none without the plugin
def pytest_timeout_set_timer(item, settings):
    """The thread method's timer. On expiry it ends the test as pytest-timeout's own does,
    with what the test printed and the stacks of every thread, and kills every process below
    this one: stopped before the stacks are taken, so that a test waiting on one is shown
    where it waits, and killed after, before this process ends. Each step is guarded: a failure
    to stop or kill them, or to report, is said on stderr, the processes stopped are killed, and
    the test is ended all the same."""
    import contextlib
    import threading
    import traceback

    import pytest_timeout

    if settings.method != "thread":
        return None

    def said():
        """What failed, on stderr, unless that fails too."""
        with contextlib.suppress(Exception):
            traceback.print_exc()

    # Mirrors pytest-timeout's own timeout_timer, with the stop and the kill around its report.
    def expire():
        if not settings.disable_debugger_detection and pytest_timeout.is_debugging():
            return
        held: dict[int, int] = {}
        terminal = None
        try:
            try:
                held = hang_guard.stop_descendants()
            except Exception:
                said()
            stdout = stderr = None
            try:  # first, so that what is said from here on reaches the real stderr
                capman = item.config.pluginmanager.getplugin("capturemanager")
                if capman:
                    capman.suspend_global_capture(item)
                    stdout, stderr = capman.read_global_capture()
            except Exception:
                said()
            try:
                terminal = item.config.get_terminal_writer()
                terminal.sep("+", title="Timeout")
                for title, text in (("Captured stdout", stdout), ("Captured stderr", stderr)):
                    if text:
                        terminal.sep("~", title=title)
                        terminal.write(text)
                pytest_timeout.dump_stacks(terminal)
                terminal.sep("+", title="Timeout")
            except Exception:
                said()
        finally:
            try:
                try:
                    hang_guard.kill(held)
                except Exception:
                    said()
                for stream in (terminal, sys.stdout, sys.stderr):
                    with contextlib.suppress(Exception):
                        if stream is not None:
                            stream.flush()
            finally:
                os._exit(1)

    timer = threading.Timer(settings.timeout, expire)
    timer.name = f"{pytest_timeout.__name__} {item.nodeid}"

    def cancel():
        timer.cancel()
        timer.join()

    item.cancel_timeout = cancel
    timer.start()
    return True


@pytest.fixture(scope="session", autouse=True)
def collections_shared_by_the_workers(tmp_path_factory):
    """Where the workers of a run share what pytest collects for a command line
    (tests/collected.py), so that each collection is worked out once, not by each worker."""
    from tests import collected

    base = tmp_path_factory.getbasetemp()
    collected.shared = (
        base.parent if os.environ.get("PYTEST_XDIST_WORKER") else base
    ) / "collected"
    yield
    collected.shared = None


@pytest.fixture(scope="session", autouse=True)
def databases_that_do_not_wait_for_the_disk():
    """Every database the backend opens takes ``synchronous = off`` in the tests: a commit
    otherwise waits for the disk, which costs many times the work it commits and is slower
    still with every worker's commits waiting for the same disk. No test is about a
    database surviving a crash."""
    from rcman import db

    real = db.connect

    def connect(path):
        con = real(path)
        con.execute("pragma synchronous = off")
        return con

    db.connect = connect
    yield
    db.connect = real


# --- backend fixtures ------------------------------------------------------------

import contextlib  # noqa: E402

from tests.until import until  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ORIGIN = "https://rcman.example.com"
ALICE, BOB, CAROL = "alice@example.com", "bob@example.com", "carol@example.com"
# Who the app fixtures start with: (email, administrator).
PEOPLE = ((ALICE, True), (BOB, False), (CAROL, False))
TMUX_CONF = REPO / "backend" / "rcman" / "tmux.conf"


@pytest.fixture(scope="session")
def podman_service():
    """The tests' container engine (tests/podman_service.py): the user's rootless Podman on a
    socket and a storage of its own, for the session. Where it cannot run the test skips, unless
    RCMAN_REQUIRE_CONTAINER=1, as make test-container and make benchmark set, in which case it
    fails."""
    from tests import podman_service as engine

    # A session's fixture is in no test: it names its tier's guard, the container tests'.
    why = engine.usable(hang_guard.CONTAINER_HANG_GUARD)
    if why is not None:
        if os.environ.get("RCMAN_REQUIRE_CONTAINER") == "1":
            pytest.fail(f"the user's rootless Podman cannot run: {why}")
        pytest.skip(f"the user's rootless Podman cannot run: {why}")
    with engine.started(hang_guard.CONTAINER_HANG_GUARD) as started:
        yield started


@pytest.fixture(scope="session")
def labelled(podman_service) -> list[str]:
    """podman run's arguments that label a container the suite starts itself on the engine as
    this run's, held while the run lasts; containers a killed run left are removed first, and
    this run's afterwards (tests/containers.py)."""
    from tests import containers

    with containers.holder(podman_service.argv()) as folder:
        yield containers.labels(folder)


@pytest.fixture(scope="session")
def source(tmp_path_factory) -> Path:
    """The checkout's files as they are on disk (tests/snapshot.py), which a container the
    suite starts sees read-only at /src: made once for the session and removed when it ends."""
    from devtools import held_folders
    from tests.snapshot import snapshot

    folder = tmp_path_factory.mktemp("src")
    try:
        yield snapshot(REPO, folder)
    finally:
        held_folders.remove_tree(folder, roots=[tmp_path_factory.getbasetemp()])


@pytest.fixture(scope="session")
def toolchain() -> Path:
    """The installer's toolchain (installer/install.py ensure_toolchain: Node.js, uv,
    oauth2-proxy and tmux's source, each checked against its pinned sha256), filled with the
    installer's own download code in the user's cache, where it stays between runs."""
    from installer import install as ins

    folder = Path.home() / ".cache" / "rcman-test" / "toolchain"
    ins.ensure_toolchain(folder)
    return folder


@pytest.fixture(scope="session")
def system_image(podman_service) -> str:
    """The system image on the engine (tests/container/system.py), held for the session: a
    service deleting a workspace collects the images nothing refers to."""
    from tests import podman_service as engine
    from tests.container import system

    return engine.hold(podman_service, system.build(podman_service))


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


@pytest.fixture(autouse=True)
def github_stand_in(tmp_path_factory):
    """The folder that stands in for https://github.com/ in this test: a repository at
    ``<folder>/<owner>/<name>.git`` is what the address ``https://github.com/<owner>/<name>``
    clones. Every git command the host runtime runs that may reach a remote is given it
    (tests/git_fixtures.py: NETWORK_SUBCOMMANDS), and one that was not fails the test, so
    that nothing a test drives, a route that clones, fetches or pushes among them, reaches
    the real github.com. It patches by a context of its own, not the
    ``monkeypatch`` fixture, which would then outlive the fixtures a test sets up after it,
    and be undone only after their teardown. Where rcman's backend cannot be imported, as in
    the container suites of tests/container that need none, there is no host runtime to
    guard."""
    try:
        from rcman.runtime import HostRuntime
    except ImportError:
        yield None
        return
    from tests import git_fixtures

    # not in the test's tmp_path: tests list that folder's content
    base = tmp_path_factory.mktemp("stand-in")
    real = HostRuntime.exec

    def exec_(self, name, argv, *args, **kwargs):
        # looked up now, so that a test can take the rewrite away and see the guard fire
        argv = git_fixtures.through_stand_in(argv, base)
        git_fixtures.assert_through_stand_in(argv, base)
        return real(self, name, argv, *args, **kwargs)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(HostRuntime, "exec", exec_)
        yield base


from devtools.fake_store import FAKE_VERSION, fake_claude  # noqa: E402,F401 - for the tests


@contextlib.contextmanager
def host_runtime(tmp_path: Path, kind=None, **more):
    """The host runtime (backend/rcman/runtime.py) for a test, or ``kind``, a subclass of
    it: workspaces under ``tmp_path`` with the fake `claude`, their tmux sockets in a
    tmux_sockets folder (devtools/held_folders.py), every workspace removed afterwards."""
    from rcman.runtime import HostRuntime

    from devtools.held_folders import tmux_sockets

    root = tmp_path / "workspaces"
    root.mkdir()
    claude = tmp_path / "claude"  # the store, where the settings fixture's claude_store is
    fake_claude(claude)
    with tmux_sockets("rcb-") as sockets:
        runtime = (kind or HostRuntime)(root, sockets, claude, TMUX_CONF, **more)
        try:
            yield runtime
        finally:
            for name in runtime.labelled():
                runtime.remove(name)


@pytest.fixture
def runtime(tmp_path):
    with host_runtime(tmp_path) as made:
        yield made


@pytest.fixture(scope="session")
def no_releases(tmp_path_factory):
    """The address of a release site with no release and no latest, so that nothing a test
    drives reaches the real one."""
    from tests import claude_download

    with claude_download.served(tmp_path_factory.mktemp("no-releases")) as url:
        yield url


@pytest.fixture
def settings(tmp_path, no_releases):
    from rcman import db, people
    from rcman.settings import Settings

    made = Settings(
        public_origin=ORIGIN,
        database=tmp_path / "rcman.db",
        allowed_emails=tmp_path / "allowed-emails",
        docs_dir=REPO / "docs",
        claude_store=tmp_path / "claude",  # the host runtime's (host_runtime)
        claude_download=no_releases,
    )
    db.migrate(made.database)
    con = db.connect(made.database)
    try:
        for email, admin in PEOPLE:
            people.add(con, email, admin)
    finally:
        con.close()
    return made


def into_the_first_service(monkeypatch, name: str, double) -> None:
    """``double`` put in as ``name`` of the first workspaces' service made from here on, the
    app's where the test has one, before any thread of it starts; a service made after it, as
    rcman-admin's in a test of two processes, keeps its own."""
    from rcman.workspaces import Workspaces

    assert name in ("_changed", "_closing"), name
    real, given = Workspaces.__init__, []

    def init(self, *args, **kwargs):
        real(self, *args, **kwargs)
        if not given:
            given.append(self)
            setattr(self, name, double)

    monkeypatch.setattr(Workspaces, "__init__", init)


@pytest.fixture
def changed(monkeypatch):
    """The workspaces' condition (``Workspaces._changed``) a virtual WatchedCondition
    (tests/time_doubles.py), in the first service made (``into_the_first_service``): ``app``
    asks for it first where the test does."""
    from tests.time_doubles import WatchedCondition

    made = WatchedCondition(virtual=True)
    into_the_first_service(monkeypatch, "_changed", made)
    return made


@pytest.fixture
def periodic(monkeypatch):
    """The event the service's periodic checks wait on between rounds (``Workspaces._closing``)
    a ControlledEvent (tests/time_doubles.py), in the first service made
    (``into_the_first_service``): the test ends a wait itself (``periodic_round``), and close
    sets it as ever."""
    from tests.time_doubles import ControlledEvent

    made = ControlledEvent()
    into_the_first_service(monkeypatch, "_closing", made)
    return made


@pytest.fixture
def periodic_round(client, periodic):
    """One round of the service's periodic checks, done once they wait again: their wait of
    IDLE_CHECK_SECONDS is ended as if it had passed, and a new one is waited for."""
    from rcman import workspaces

    periodic.wait_parked(1, match=workspaces.IDLE_CHECK_SECONDS)

    def round_() -> None:
        periodic.expire(workspaces.IDLE_CHECK_SECONDS)
        periodic.wait_parked(1, match=workspaces.IDLE_CHECK_SECONDS)

    return round_


@pytest.fixture
def person_locks(monkeypatch):
    """Every person's lock (``Workspaces.person_lock``) as a RecordingLock, one for each lock,
    put in before ``client`` starts the service, so that a test sees a take of it blocked rather
    than waiting for one (``recorded_person_lock``)."""
    import threading

    from rcman import workspaces as ws

    from tests.time_doubles import RecordingLock

    real, made, guard = ws.Workspaces.person_lock, {}, threading.Lock()

    def person_lock(self, person_id):
        lock = real(self, person_id)
        with guard:
            if id(lock) not in made:
                made[id(lock)] = (lock, RecordingLock(lock))  # the lock kept: its id stays its
            return made[id(lock)][1]

    monkeypatch.setattr(ws.Workspaces, "person_lock", person_lock)


def recorded_person_lock(app, person_id: int):
    """The person's lock as ``person_locks`` put it in, which the test lists before ``client``."""
    from tests.time_doubles import RecordingLock

    lock = app.state.workspaces.person_lock(person_id)
    assert isinstance(lock, RecordingLock), "the test lists person_locks before client"
    return lock


# What a test puts into the app's parts, each before ``app`` makes them, where the test asks.
SEAMS = ("changed", "periodic")


@pytest.fixture
def app(request, settings, runtime):
    from rcman.main import create_app

    for seam in SEAMS:
        if seam in request.fixturenames:
            request.getfixturevalue(seam)
    return create_app(settings, runtime=runtime)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        assert app.state.workspaces.restored.wait(hang_guard.tier_guard())
        yield c


def made_workspace(client, who: str, name: str = "work") -> dict:
    """A workspace of ``who``'s called ``name``, once it is ready."""
    r = client.post("/api/workspaces", json={"name": name}, headers=changing(who))
    assert r.status_code == 202, r.text
    workspace = r.json()["workspace"]

    def made() -> dict | None:
        listed = client.get("/api/workspaces", headers=as_user(who)).json()["workspaces"]
        found = next(w for w in listed if w["id"] == workspace["id"])
        return None if found["state"] == "creating" else found

    found = until(made)
    assert found["state"] == "ready", found
    return found


@pytest.fixture
def quick_watch(monkeypatch):
    """The watcher of work a deletion stops (workspaces.py ``_watch``) looks at the row often."""
    from rcman import workspaces

    hang_guard.pace(monkeypatch, workspaces, "WORKING_POLL_SECONDS", 0.02)


def ended(service, workspace_id: int, within: float | None = None) -> None:
    """Until no thread of ``service``'s that makes or rebuilds the workspace is left: told by
    the service's condition, within ``within`` seconds, HANG_GUARD unless given (a container
    test gives CONTAINER_HANG_GUARD), the tier's unless given."""
    within = hang_guard.tier_guard() if within is None else within
    with service._changed:
        assert service._changed.wait_for(
            lambda: not any(h.workspace_id == workspace_id for h in service._unended), within
        ), f"a thread making workspace {workspace_id} did not end within {within}s"


def signed_in(app, workspace: dict, trusted: bool = False) -> None:
    """The fake `claude` of ``workspace`` signed in, with its folder trusted if ``trusted``."""
    from rcman.runtime import container_name

    from devtools import fake_claude as fake

    workspaces = app.state.workspaces
    layout = workspaces.runtime.layout(container_name(workspace["id"]))
    folders = [layout.work] if trusted else []
    fake.write_state(Path(layout.config), logged_in=True, trusted=folders)
    from devtools.world import record_sign_in

    record_sign_in(workspaces.settings.database, workspaces.get(workspace["id"]).person)


# A valid request for each route that takes one: (method, path) -> keyword
# arguments for the test client. Tests that drive every route of a kind, found
# in the route table, take their requests from here. A workspace's id is 1, the
# first one a test makes.
CALLS = {
    ("POST", "/api/servers"): {"json": {"workspace": 1}},
    ("POST", "/api/servers/resume"): {"json": {"workspace": 1}},
    ("POST", "/api/servers/stop"): {"json": {"workspace": 1}},
    ("POST", "/api/servers/forget"): {"json": {"workspace": 1}},
    ("POST", "/api/servers/confirm"): {"json": {"workspace": 1, "answer": "y"}},
    ("GET", "/api/servers/output"): {"params": {"workspace": 1}},
    ("POST", "/api/workspaces"): {"json": {"name": "another"}},
    ("DELETE", "/api/workspaces/{workspace}"): {},
    ("POST", "/api/workspaces/{workspace}/rebuild"): {"json": {"confirm": True}},
    ("GET", "/api/workspaces/{workspace}/devcontainer"): {},
    ("PATCH", "/api/me/settings"): {"json": {"prefix": "alice"}},
    ("POST", "/api/me/sign-out"): {"json": {"keep_sessions": True}},
    ("POST", "/api/git/stage"): {"json": {"workspace": 1, "files": ["README.md"]}},
    ("POST", "/api/git/unstage"): {"json": {"workspace": 1, "files": ["README.md"]}},
    ("POST", "/api/git/commit"): {"json": {"workspace": 1, "message": "A message"}},
    ("POST", "/api/git/switch"): {"json": {"workspace": 1, "branch": "main"}},
    ("POST", "/api/git/branch"): {"json": {"workspace": 1, "name": "new-branch"}},
    ("POST", "/api/git/fetch"): {"json": {"workspace": 1}},
    ("POST", "/api/git/push"): {"json": {"workspace": 1}},
    ("POST", "/api/git/sync"): {"json": {"workspace": 1}},
    ("POST", "/api/git/commit-push"): {"json": {"workspace": 1, "message": "A message"}},
    ("POST", "/api/git/clone"): {"json": {"url": "https://github.com/alice/a-clone"}},
    ("GET", "/api/git/status"): {"params": {"workspace": 1}},
    ("GET", "/api/git/branches"): {"params": {"workspace": 1}},
    ("GET", "/api/github"): {},
    ("POST", "/api/github/token"): {"json": {"token": "a-github-token"}},
    ("DELETE", "/api/github/token"): {},
    ("POST", "/api/people"): {"json": {"email": "dave@example.com"}},
    ("DELETE", "/api/people/{person}"): {},
    ("GET", "/api/people/{person}/limits"): {},
    ("PUT", "/api/people/{person}/limits"): {"json": {"memory": "1G", "cpu": "1", "pids": "1000"}},
    ("PUT", "/api/people/{person}/workspaces/{workspace}/limits"): {
        "json": {"memory": "1G", "cpu": "1", "pids": "1000"}
    },
    ("GET", "/api/claude"): {},
    ("POST", "/api/claude/login"): {"json": {}},
    ("POST", "/api/claude/code"): {"json": {"code": "x"}},
    ("POST", "/api/claude/logout"): {"json": {"keep_sessions": True}},
    ("POST", "/api/claude/upgrade"): {"json": {"version": "2.2.0"}},
    ("PUT", "/api/claude/failures"): {"json": {"version": "2.2.0", "reason": "It hangs."}},
    ("DELETE", "/api/claude/failures/{version}"): {},
    ("GET", "/api/rcman/upgrade"): {},
    ("POST", "/api/rcman/upgrade"): {},
    ("GET", "/api/docs/{slug}"): {},
    ("GET", "/api/audit"): {},
    ("GET", "/api/people"): {},
    ("GET", "/api/claude/versions"): {},
    ("GET", "/api/claude/latest"): {},
    ("GET", "/api/servers/everyone"): {},
    ("GET", "/api/attention"): {},
    ("GET", "/api/monitor"): {},
    ("GET", "/api/monitor/settings"): {},
    # the monitor's defaults, from its settings model
    ("PUT", "/api/monitor/settings"): {"json": monitor_defaults.model_dump()},
    ("GET", "/api/forwarding/settings"): {},
    # the forwarding defaults, from its settings model
    ("PUT", "/api/forwarding/settings"): {"json": forwarding_defaults.model_dump()},
    ("GET", "/api/forward/hand-off"): {"params": {"host": "6080-1.ws.rcman.example.com"}},
    ("POST", "/api/client-errors"): {
        "json": {
            "kind": "unreachable",
            "method": "GET",
            "path": "/api/workspaces",
            "status": None,
            "name": "TypeError",
            "message": "Failed to fetch",
            "content_type": None,
            "page": "/",
            "visibility": "hidden",
            "online": False,
            "elapsed_ms": 12,
            "count": 3,
            "first_at": 1700000000000,
            "last_at": 1700000005000,
        }
    },
}


def concrete(path):
    """A route's path with its parameters filled in: bob, the second person, the first
    workspace, and a Claude Code version that is not the approved one."""
    return (
        path.replace("{person}", "2")
        .replace("{workspace}", "1")
        .replace("{slug}", "workspaces")
        .replace("{version}", "2.2.0")
    )


def as_user(email):
    return {"X-Forwarded-Email": email}


def changing(email):
    return {"X-Forwarded-Email": email, "Origin": ORIGIN}
