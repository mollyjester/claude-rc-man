"""A workspace made by cloning a repository (backend/rcman/workspaces.py, POST /api/git/clone):
a phase of ``creating``, on a host runtime that runs real git against the tests' stand-in for
github.com, and can answer a clone with what git would say, or hold it as a running git."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from rcman import db, problems
from rcman import runtime as rt
from rcman import workspaces as ws

from tests.conftest import ALICE, BOB, as_user, changing, host_runtime
from tests.git_fixtures import fixture_git, make_repo
from tests.hang_guard import HANG_GUARD
from tests.until import until

URL = "https://github.com/octo/widget"
TOKEN = "ghp_a-token-for-the-clone-tests"

ODD = "Cloning into '.'...\nfatal: something git has not said before\n"


class Recording(rt.HostRuntime):
    """The host runtime, noting what it is asked to run and remove. A clone is answered
    with ``answer`` when one is set, and waits for ``gate`` when one is set: released by the
    test, or by the workspace's removal, which ends a running git as Podman's would; else only
    once the test is over, so that what waits for git rather than removing it fails the test."""

    def __init__(self, *args):
        super().__init__(*args)
        self.execs: list[tuple[list[str], bytes | None]] = []
        self.removed: list[str] = []
        self.answer: rt.Result | None = None
        self.gate: threading.Event | None = None
        self.cloning = threading.Event()

    def exec(self, name, argv, stdin=None, timeout=HANG_GUARD, cwd=None, keep=None, again=False):
        self.execs.append((argv, stdin))
        if "git" in argv and "clone" in argv:
            self.cloning.set()
            if self.gate is not None:
                self.gate.wait(2 * HANG_GUARD)
                if name in self.removed:
                    return rt.Result(137, b"", b"Killed\n")
            if self.answer is not None:
                return self.answer
        return super().exec(name, argv, stdin, timeout, cwd, keep, again)

    def remove(self, name):
        self.removed.append(name)
        if self.gate is not None:
            self.gate.set()
        super().remove(name)


@pytest.fixture
def runtime(tmp_path):
    with host_runtime(tmp_path, Recording) as made:
        yield made


@pytest.fixture
def service(settings, runtime):
    made = ws.Workspaces(settings, runtime)
    yield made
    made.close()


@pytest.fixture
def widget(github_stand_in, tmp_path) -> Path:
    """A repository at ``URL`` on the stand-in for github.com, holding main and feature."""
    return make_repo(tmp_path / "src", origin=github_stand_in / "octo" / "widget.git")


def said(text: str, code: int = 128) -> rt.Result:
    return rt.Result(code, b"", text.encode())


def clone(client, url=URL, who=ALICE, **body):
    return client.post("/api/git/clone", json={"url": url, **body}, headers=changing(who))


def listed(client, who=ALICE) -> list[dict]:
    return client.get("/api/workspaces", headers=as_user(who)).json()["workspaces"]


def entry(client, workspace_id: int, who=ALICE) -> dict:
    return next(w for w in listed(client, who) if w["id"] == workspace_id)


def finished(client, workspace_id: int, who=ALICE) -> dict:
    def made():
        found = entry(client, workspace_id, who)
        return None if found["state"] == "creating" else found

    return until(made)


def cloned(client, url=URL, who=ALICE, **body) -> dict:
    r = clone(client, url, who, **body)
    assert r.status_code == 202, r.text
    return finished(client, r.json()["workspace"]["id"], who)


def row(settings, workspace_id: int):
    con = db.connect(settings.database)
    try:
        return con.execute("select * from workspaces where id = ?", (workspace_id,)).fetchone()
    finally:
        con.close()


def audit_rows(settings) -> list[dict]:
    con = db.connect(settings.database)
    try:
        return [dict(r) for r in con.execute("select * from audit order by id")]
    finally:
        con.close()


def save_token(client, who=ALICE):
    r = client.post("/api/github/token", json={"token": TOKEN}, headers=changing(who))
    assert r.status_code == 200, r.text


def cleared(runtime, client) -> None:
    until(lambda: not listed(client) and not runtime.labelled())


def ended_in_a_thread(call):
    """What ``call`` gave, run in a thread of its own that has ended within the hang guard:
    work the test's runtime holds at a gate, which only a removal, a cancel or the test's end
    opens, does not keep it from ending."""
    out: list = []
    worker = threading.Thread(target=lambda: out.append(call()), daemon=True)
    worker.start()
    worker.join(HANG_GUARD)
    assert not worker.is_alive(), "it waited for the work the runtime holds"
    [given] = out
    return given


# --- making ------------------------------------------------------------------------------


def test_a_clone_is_a_phase_of_creating_and_ends_ready_with_the_repository(
    client, runtime, widget, settings
):
    runtime.gate = threading.Event()
    r = clone(client)
    assert r.status_code == 202, r.text
    made = r.json()["workspace"]
    assert (made["name"], made["state"], made["source"]) == ("widget", "creating", URL)
    assert made["source_page"] == URL
    assert runtime.cloning.wait(HANG_GUARD)
    during = entry(client, made["id"])
    assert (during["state"], during["source"], during["error"]) == ("creating", URL, None)
    assert during["source_page"] == URL
    assert during["log"].splitlines() == ["Making the workspace", "Checking git", f"Cloning {URL}"]
    runtime.gate.set()
    done = finished(client, made["id"])
    assert (done["state"], done["error"], done["source"]) == ("ready", None, URL)
    assert done["source_page"] == URL
    assert done["log"] is None  # the steps are only shown while the workspace is not ready
    assert row(settings, made["id"])["log"] is None
    folder = Path(runtime.layout(rt.container_name(made["id"])).work)
    assert (folder / "README.md").is_file()
    branches = fixture_git(folder, "branch", "-r")
    assert "origin/feature" in branches and "origin/main" in branches  # all branches
    out = client.get("/api/git/status", params={"workspace": made["id"]}, headers=as_user(ALICE))
    assert out.status_code == 200, out.text
    assert out.json()["branch"]["name"] == "main"
    last = [a for a in audit_rows(settings) if a["action"] == "git.clone"][-1]
    assert (last["action"], last["target"], last["result"], last["email"]) == (
        "git.clone",
        URL,
        "ok",
        ALICE,
    )


# --- failing, loudly ---------------------------------------------------------------------


def test_a_clone_git_refuses_fails_the_workspace_saying_why_and_deleting_it_removes_it(
    client, runtime
):
    """The workspace fails saying why in plain words, and deleting it removes what it made."""
    save_token(client)
    runtime.answer = said(
        f"Cloning into '.'...\nfatal: Authentication failed for "
        f"'https://x-access-token:{TOKEN}@github.com/octo/widget/'\n"
    )
    failed = cloned(client)
    assert failed["state"] == "failed" and failed["source"] == URL
    assert problems.CLONE_MESSAGES["token_refused"] in failed["error"]
    name = rt.container_name(failed["id"])
    assert runtime.exists(name) and name in runtime.labelled()
    r = client.delete(f"/api/workspaces/{failed['id']}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    cleared(runtime, client)
    assert not runtime.exists(name)


# --- the token ---------------------------------------------------------------------------


def test_the_token_is_nowhere_a_clone_leaves_a_trace(client, runtime, widget, settings):
    save_token(client)
    con = db.connect(settings.database)
    try:
        assert con.execute("select token from github_tokens").fetchall()[0][0] == TOKEN
    finally:
        con.close()  # positive control: the token is stored where the search can find it
    made = cloned(client)
    assert made["state"] == "ready"
    clones = [e for e in runtime.execs if "clone" in e[0]]
    assert clones  # positive control: the clone was run and watched
    assert not [1 for argv, stdin in runtime.execs if TOKEN in " ".join(argv)]
    assert not [1 for argv, stdin in runtime.execs if stdin and TOKEN.encode() in stdin]
    assert TOKEN not in json.dumps(listed(client)) + json.dumps(audit_rows(settings))
    assert TOKEN not in json.dumps([dict(row(settings, made["id"]))])


# --- name -----------------------------------------------------------------------------


def test_cloning_the_same_repository_again_numbers_the_names(client, runtime):
    runtime.answer = said(ODD)
    names = [cloned(client)["name"] for _ in range(3)]
    assert names == ["widget", "widget-2", "widget-3"]
    assert cloned(client, who=BOB)["name"] == "widget"  # names are each person's own


# --- cancelling: deleting a workspace that is being cloned -----------------------------


def test_deleting_a_workspace_being_cloned_stops_git_by_removing_its_container(client, runtime):
    runtime.gate = threading.Event()
    r = clone(client)
    made = r.json()["workspace"]
    name = rt.container_name(made["id"])
    assert runtime.cloning.wait(HANG_GUARD)
    assert runtime.removed == []  # positive control: nothing has removed it yet
    r = ended_in_a_thread(
        lambda: client.delete(f"/api/workspaces/{made['id']}", headers=changing(ALICE))
    )
    assert r.status_code == 200, r.text
    assert name in runtime.removed  # the delete itself did it, git running
    cleared(runtime, client)
    assert not runtime.exists(name)


# --- restarting ----------------------------------------------------------------------


def insert(settings, name: str, state: str, source: str | None, log: str | None = None) -> int:
    con = db.connect(settings.database)
    try:
        cursor = con.execute(
            "insert into workspaces (person, name, state, source, log) values (1, ?, ?, ?, ?)",
            (name, state, source, log),
        )
        con.commit()
        return cursor.lastrowid
    finally:
        con.close()


def test_a_restart_that_interrupted_a_clone_fails_the_workspace_and_deleting_it_works(
    service, settings, runtime
):
    cloning = insert(settings, "cloning", "creating", URL, "Making the workspace")
    name = rt.container_name(cloning)
    runtime.create(rt.Spec(name, slot=1, workspace=cloning, person=1))
    service.restore()
    now = row(settings, cloning)
    assert now["state"] == "failed"
    assert now["error"] == problems.CLONE_MESSAGES["restarted"]
    assert "rcman restarted while this workspace was being made" in now["error"]
    assert now["source"] == URL
    assert runtime.exists(name)  # left, for the deletion to remove with its volume
    service.delete(service.get(cloning))
    assert row(settings, cloning) is None and not runtime.exists(name)
