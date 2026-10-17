"""Collection rules shared by the whole suite."""

import os

# tests/container/ runs as root inside the test container, started by
# tests/test_container_suite.py; elsewhere it is not collected at all.
collect_ignore = [] if os.environ.get("RCMAN_IN_CONTAINER") == "1" else ["container"]

# The guards over how the suite is split run small pytest projects of their own.
pytest_plugins = ["pytester"]

import pytest  # noqa: E402

from tests import container_marker  # noqa: E402


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items):
    """Before -m selects: the derived markers (tests/container_marker.py)."""
    container_marker.mark(items)


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
import time  # noqa: E402
from pathlib import Path  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ORIGIN = "https://rcman.example.com"
ALICE, BOB, CAROL = "alice@example.com", "bob@example.com", "carol@example.com"
# Who the app fixtures start with: (email, administrator).
PEOPLE = ((ALICE, True), (BOB, False), (CAROL, False))
TMUX_CONF = REPO / "backend" / "rcman" / "tmux.conf"
# How long the tests wait for a workspace to be made.
MADE_WITHIN = 30.0


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


from tests.fake_store import FAKE_VERSION, fake_claude  # noqa: E402,F401 - the tests' own


@contextlib.contextmanager
def host_runtime(tmp_path: Path, kind=None):
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
        runtime = (kind or HostRuntime)(root, sockets, claude, TMUX_CONF)
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


@pytest.fixture
def app(settings, runtime):
    from rcman.main import create_app

    return create_app(settings, runtime=runtime)


@pytest.fixture
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        assert app.state.workspaces.restored.wait(30)
        yield c


def made_workspace(client, who: str, name: str = "work") -> dict:
    """A workspace of ``who``'s called ``name``, once it is ready."""
    r = client.post("/api/workspaces", json={"name": name}, headers=changing(who))
    assert r.status_code == 202, r.text
    workspace = r.json()["workspace"]
    deadline = time.monotonic() + MADE_WITHIN
    while True:
        listed = client.get("/api/workspaces", headers=as_user(who)).json()["workspaces"]
        found = next(w for w in listed if w["id"] == workspace["id"])
        if found["state"] != "creating":
            assert found["state"] == "ready", found
            return found
        assert time.monotonic() < deadline, found
        time.sleep(0.05)


def signed_in(app, workspace: dict, trusted: bool = False) -> None:
    """The fake `claude` of ``workspace`` signed in, with its folder trusted if ``trusted``."""
    from rcman.runtime import container_name

    from devtools import fake_claude as fake

    workspaces = app.state.workspaces
    layout = workspaces.runtime.layout(container_name(workspace["id"]))
    folders = [layout.work] if trusted else []
    fake.write_state(Path(layout.config), logged_in=True, trusted=folders)
    from tests.e2e_server import record_sign_in

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
    ("GET", "/api/docs/{slug}"): {},
    ("GET", "/api/audit"): {},
    ("GET", "/api/people"): {},
    ("GET", "/api/claude/versions"): {},
    ("GET", "/api/claude/latest"): {},
    ("GET", "/api/servers/everyone"): {},
    ("GET", "/api/attention"): {},
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
    """A route's path with its parameters filled in: bob, the second person, and the
    first workspace."""
    return path.replace("{person}", "2").replace("{workspace}", "1").replace("{slug}", "workspaces")


def as_user(email):
    return {"X-Forwarded-Email": email}


def changing(email):
    return {"X-Forwarded-Email": email, "Origin": ORIGIN}
