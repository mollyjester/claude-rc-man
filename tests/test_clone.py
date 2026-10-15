"""A workspace made by cloning a repository (backend/rcman/workspaces.py, POST /api/git/clone):
a phase of ``creating``, on a host runtime that runs real git against the tests' stand-in for
github.com, and can answer a clone with what git would say, or hold it as a running git."""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import pytest
from rcman import db, people, problems
from rcman import runtime as rt
from rcman import workspaces as ws
from rcman.problems import Problem

from helpers import rules
from tests.conftest import ALICE, BOB, MADE_WITHIN, as_user, changing, host_runtime
from tests.git_fixtures import fixture_git, make_repo

URL = "https://github.com/octo/widget"
TOKEN = "ghp_a-token-for-the-clone-tests"

PROMPT = (
    "Cloning into '.'...\nfatal: could not read Username for 'https://github.com': "
    "terminal prompts disabled\n"
)
REFUSED = (
    "Cloning into '.'...\nremote: Invalid username or password.\n"
    "fatal: Authentication failed for 'https://github.com/octo/widget/'\n"
)
NOT_FOUND = (
    "Cloning into '.'...\nremote: Repository not found.\n"
    "fatal: repository 'https://github.com/octo/widget/' not found\n"
)
NO_NETWORK = (
    "Cloning into '.'...\nfatal: unable to access 'https://github.com/octo/widget/': "
    "Could not resolve host: github.com\n"
)
ODD = "Cloning into '.'...\nfatal: something git has not said before\n"


class Recording(rt.HostRuntime):
    """The host runtime, noting what it is asked to run and remove. A clone is answered
    with ``answer`` when one is set, and waits for ``gate`` when one is set: released by the
    test, or by the workspace's removal, which ends a running git as Podman's would."""

    def __init__(self, *args):
        super().__init__(*args)
        self.execs: list[tuple[list[str], bytes | None]] = []
        self.removed: list[str] = []
        self.answer: rt.Result | None = None
        self.gate: threading.Event | None = None
        self.cloning = threading.Event()

    def exec(self, name, argv, stdin=None, timeout=rt.PODMAN_TIMEOUT, cwd=None):
        self.execs.append((argv, stdin))
        if "git" in argv and "clone" in argv:
            self.cloning.set()
            if self.gate is not None:
                self.gate.wait(30)
                if name in self.removed:
                    return rt.Result(137, b"", b"Killed\n")
            if self.answer is not None:
                return self.answer
        return super().exec(name, argv, stdin, timeout, cwd)

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
    deadline = time.monotonic() + MADE_WITHIN
    while True:
        found = entry(client, workspace_id, who)
        if found["state"] != "creating":
            return found
        assert time.monotonic() < deadline, found
        time.sleep(0.05)


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
    deadline = time.monotonic() + MADE_WITHIN
    while listed(client) or runtime.labelled():
        assert time.monotonic() < deadline, (listed(client), runtime.labelled())
        time.sleep(0.05)


# --- making ------------------------------------------------------------------------------


def test_a_clone_is_a_phase_of_creating_and_ends_ready_with_the_repository(
    client, runtime, widget, settings
):
    runtime.gate = threading.Event()
    r = clone(client)
    assert r.status_code == 202, r.text
    made = r.json()["workspace"]
    assert (made["name"], made["state"], made["source"]) == ("widget", "creating", URL)
    assert runtime.cloning.wait(MADE_WITHIN)
    during = entry(client, made["id"])
    assert (during["state"], during["source"], during["error"]) == ("creating", URL, None)
    assert during["log"].splitlines() == ["Making the workspace", "Checking git", f"Cloning {URL}"]
    runtime.gate.set()
    done = finished(client, made["id"])
    assert (done["state"], done["error"], done["source"]) == ("ready", None, URL)
    assert done["log"] is None  # the steps are only shown while the workspace is not ready
    assert row(settings, made["id"])["log"] is None
    folder = Path(runtime.layout(rt.container_name(made["id"])).work)
    assert (folder / "README.md").is_file()
    branches = fixture_git(folder, "branch", "-r")
    assert "origin/feature" in branches and "origin/main" in branches  # all branches
    out = client.get("/api/git/status", params={"workspace": made["id"]}, headers=as_user(ALICE))
    assert out.status_code == 200, out.text
    assert out.json()["branch"]["name"] == "main"


def test_a_workspace_made_without_an_address_has_no_source_and_no_log_once_ready(client, settings):
    r = client.post("/api/workspaces", json={"name": "blank"}, headers=changing(ALICE))
    made = r.json()["workspace"]
    assert made["source"] is None
    blank = finished(client, made["id"])
    assert (blank["state"], blank["source"], blank["log"]) == (
        "ready",
        None,
        None,
    )
    assert row(settings, made["id"])["source"] is None


def test_a_failed_workspace_keeps_its_source_and_its_steps_until_it_is_deleted(
    client, runtime, settings
):
    runtime.answer = said(ODD)
    failed = cloned(client)
    assert failed["state"] == "failed" and failed["source"] == URL
    assert failed["log"].splitlines() == [
        "Making the workspace",
        "Checking git",
        f"Cloning {URL}",
        "The clone failed",
    ]
    assert row(settings, failed["id"])["state"] == "failed"
    r = client.delete(f"/api/workspaces/{failed['id']}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    cleared(runtime, client)


def test_a_clone_that_succeeds_logs_that_it_is_done_before_the_log_is_cleared(
    client, runtime, widget, settings, monkeypatch
):
    seen = []
    real = ws.Workspaces._set

    def set_(self, workspace_id, **columns):
        if columns.get("state") == ws.READY:
            seen.append(row(settings, workspace_id)["log"])
        return real(self, workspace_id, **columns)

    monkeypatch.setattr(ws.Workspaces, "_set", set_)
    assert cloned(client)["state"] == "ready"
    assert seen and "Cloned" in seen[0].splitlines()
    assert seen[0].splitlines()[-1] == "There is no devcontainer.json"  # then it looked


# --- failing, loudly ---------------------------------------------------------------------


CAUSES = [
    (PROMPT, False, "private_no_token"),
    (REFUSED, False, "private_no_token"),
    (REFUSED, True, "token_refused"),
    (NOT_FOUND, True, "not_found"),
    (NO_NETWORK, False, "network"),
    (ODD, False, None),
]


@pytest.mark.parametrize(("stderr", "token", "cause"), CAUSES)
def test_a_clone_git_refuses_fails_the_workspace_with_the_exit_code_what_git_said_and_why(
    client, runtime, stderr, token, cause
):
    if token:
        save_token(client)
    runtime.answer = said(stderr)
    failed = cloned(client)
    assert failed["state"] == "failed"
    assert "128" in failed["error"]
    assert stderr.strip().splitlines()[-1] in failed["error"]  # git's own words
    plain = [m for k, m in problems.CLONE_MESSAGES.items() if k != "restarted"]
    if cause is None:
        assert not [m for m in plain if m in failed["error"]]
    else:
        assert problems.CLONE_MESSAGES[cause] in failed["error"]
        assert [m for m in plain if m in failed["error"]] == [problems.CLONE_MESSAGES[cause]]


def test_a_private_repository_without_a_token_says_where_to_add_one():
    assert "GitHub token in Settings" in problems.CLONE_MESSAGES["private_no_token"]


def test_a_clone_the_timeout_tool_ended_fails_the_workspace_and_says_it_was_stopped(
    client, runtime
):
    runtime.answer = rt.Result(124, b"", b"")
    failed = cloned(client)
    assert failed["state"] == "failed"
    assert problems.CLONE_MESSAGES["timeout"] in failed["error"]


def test_a_repository_that_is_not_there_fails_with_gits_own_words_from_real_git(
    client, runtime, github_stand_in
):
    failed = cloned(client, "https://github.com/octo/missing")
    assert failed["state"] == "failed"
    assert "128" in failed["error"] and "does not appear to be a git repository" in failed["error"]
    assert not Path(runtime.layout(rt.container_name(failed["id"])).work, ".git").exists()


@pytest.mark.parametrize(
    ("said", "cause"),
    [
        # as git 2.43 words them against the tests' GitHub in Podman
        # (tests/container/test_github_in_podman.py): a repository GitHub does not have
        ("fatal: repository 'https://github.com/octo/not-there/' not found\n", "not_found"),
        ("remote: Repository not found.\nfatal: repository 'https://x/' not found\n", "not_found"),
        # a sign-in asked for with no token, and a token that was sent and not accepted
        (
            "fatal: could not read Username for 'https://github.com': terminal prompts disabled\n",
            "private_no_token",
        ),
        (
            "remote: Invalid username or password.\nfatal: Authentication failed for 'x'\n",
            "private_no_token",
        ),
        # nothing listening
        (
            "fatal: unable to access 'https://github.com/o/r/': Failed to connect to "
            "github.com port 443 after 3 ms: Couldn't connect to server\n",
            "network",
        ),
        ("fatal: something else entirely\n", None),
    ],
)
def test_the_cause_is_recognised_from_gits_words(said, cause):
    assert ws.Workspaces._cause(said, token=False) == cause


def test_a_sign_in_git_asks_for_is_a_refused_token_when_one_was_sent():
    said = "fatal: Authentication failed for 'https://github.com/o/r/'\n"
    assert ws.Workspaces._cause(said, token=True) == "token_refused"
    assert ws.Workspaces._cause(said, token=False) == "private_no_token"


def test_the_tail_of_what_git_said_is_kept_within_the_error_limit(client, runtime):
    noise = "".join(f"first-lines {i} " + "x" * 80 + "\n" for i in range(200))
    runtime.answer = said(noise + "fatal: the last thing git said\n")
    failed = cloned(client)
    assert len(failed["error"]) <= ws.MAX_ERROR_CHARS
    assert failed["error"].rstrip().endswith("fatal: the last thing git said")
    assert "first-lines 0 " not in failed["error"] and "128" in failed["error"]


def test_a_failed_clone_leaves_what_it_made_for_the_deletion_to_remove(client, runtime):
    runtime.answer = said(ODD)
    failed = cloned(client)
    name = rt.container_name(failed["id"])
    assert runtime.exists(name) and name in runtime.labelled()
    client.delete(f"/api/workspaces/{failed['id']}", headers=changing(ALICE))
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


def test_a_token_git_echoes_is_redacted_from_the_error_the_log_and_every_answer(
    client, runtime, settings
):
    save_token(client)
    runtime.answer = said(
        f"Cloning into '.'...\nfatal: Authentication failed for "
        f"'https://x-access-token:{TOKEN}@github.com/octo/widget/'\n"
    )
    failed = cloned(client)
    assert failed["state"] == "failed" and "fatal: Authentication failed" in failed["error"]
    assert "***" in failed["error"]
    stored = json.dumps([dict(row(settings, failed["id"]))])
    assert TOKEN not in json.dumps(listed(client)) + stored + json.dumps(audit_rows(settings))


def test_the_log_keeps_only_the_last_characters(service, settings, monkeypatch):
    monkeypatch.setattr(ws, "LOG_MAX_CHARS", 120)
    con = db.connect(settings.database)
    try:
        cursor = con.execute(
            "insert into workspaces (person, name, state) values (1, 'logged', 'creating')"
        )
        con.commit()
    finally:
        con.close()
    for i in range(60):
        service._log(cursor.lastrowid, f"step number {i}")
    kept = row(settings, cursor.lastrowid)["log"]
    assert 90 <= len(kept) <= 120
    assert kept.endswith("step number 59") and "step number 0\n" not in kept
    assert kept.splitlines()[-2] == "step number 58"


@pytest.mark.parametrize("limit", [100, 101, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111])
def test_the_log_is_cut_to_whole_lines(service, settings, monkeypatch, limit):
    """Whatever the limit falls on, no line of the log starts or ends part-way."""
    monkeypatch.setattr(ws, "LOG_MAX_CHARS", limit)
    con = db.connect(settings.database)
    try:
        cursor = con.execute(
            "insert into workspaces (person, name, state) values (1, 'logged', 'creating')"
        )
        con.commit()
    finally:
        con.close()
    for i in range(60):
        service._log(cursor.lastrowid, f"step number {i}")
        kept = row(settings, cursor.lastrowid)["log"]
        assert len(kept) <= limit
        assert all(re.fullmatch(r"step number \d+", line) for line in kept.splitlines()), kept
        assert kept.splitlines()[-1] == f"step number {i}"
    assert len(kept.splitlines()) >= 5  # the cut dropped one line, not all of them


def test_a_single_line_longer_than_the_limit_is_kept_by_its_end(service, settings, monkeypatch):
    monkeypatch.setattr(ws, "LOG_MAX_CHARS", 30)
    con = db.connect(settings.database)
    try:
        cursor = con.execute(
            "insert into workspaces (person, name, state) values (1, 'logged', 'creating')"
        )
        con.commit()
    finally:
        con.close()
    service._log(cursor.lastrowid, "x" * 50 + "END")
    assert row(settings, cursor.lastrowid)["log"] == "x" * 27 + "END"


# --- name -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("repository", "name"),
    [
        ("widget", "widget"),
        ("widget.git", "widget"),
        ("widget/", "widget"),
        ("widget.git/", "widget"),
        ("my_repo.v2", "my-repo-v2"),
        ("My-Repo", "My-Repo"),
        (".github", "github"),
        ("__init__", "init--"),
        ("_", "workspace"),
        ("-", "workspace"),
        ("a" * 40, "a" * 32),
        ("a" * 31 + "_" + "b" * 8, "a" * 31 + "-"),
    ],
)
def test_a_name_is_made_from_the_repository(repository, name):
    found = ws.name_from(f"https://github.com/octo/{repository}")
    assert found == name
    assert rules.WORKSPACE_NAME.regex.fullmatch(found)


def test_cloning_the_same_repository_again_numbers_the_names(client, runtime):
    runtime.answer = said(ODD)
    names = [cloned(client)["name"] for _ in range(3)]
    assert names == ["widget", "widget-2", "widget-3"]
    assert cloned(client, who=BOB)["name"] == "widget"  # names are each person's own


def test_a_long_name_leaves_room_for_the_number_within_the_limit(client, runtime):
    runtime.answer = said(ODD)
    url = "https://github.com/octo/" + "a" * 40
    names = [cloned(client, url)["name"] for _ in range(3)]
    assert names == ["a" * 32, "a" * 30 + "-2", "a" * 30 + "-3"]
    assert all(rules.WORKSPACE_NAME.regex.fullmatch(n) for n in names)


def test_a_name_given_is_the_name_and_a_name_taken_is_refused(client, runtime):
    runtime.answer = said(ODD)
    assert cloned(client, name="mine")["name"] == "mine"
    r = clone(client, name="mine")
    assert r.status_code == 409 and r.json()["code"] == "workspace_name_taken"
    r = client.post("/api/workspaces", json={"name": "widget"}, headers=changing(ALICE))
    assert r.status_code == 202
    r = clone(client, name="widget")  # the derived name is not special
    assert r.status_code == 409 and r.json()["code"] == "workspace_name_taken"
    assert [w["name"] for w in listed(client)] == ["mine", "widget"]


# --- the route ---------------------------------------------------------------------------


def test_the_route_answers_202_with_the_workspace_and_audits_the_address(
    client, runtime, widget, settings
):
    r = clone(client, name="mine")
    assert r.status_code == 202, r.text
    made = r.json()["workspace"]
    assert set(made) >= {"id", "name", "state", "error", "source", "log", "server"}
    assert (made["name"], made["source"]) == ("mine", URL)
    finished(client, made["id"])
    last = audit_rows(settings)[-1]
    assert (last["action"], last["target"], last["result"], last["email"]) == (
        "git.clone",
        URL,
        "ok",
        ALICE,
    )
    assert "mine" not in json.dumps(audit_rows(settings))


@pytest.mark.parametrize(
    ("body", "field", "sentence"),
    [
        ({"url": "https://user:pass@github.com/a/b"}, "url", rules.GITHUB_URL.sentence),
        ({"url": "http://github.com/a/b"}, "url", rules.GITHUB_URL.sentence),
        ({"url": URL, "name": "not a name!"}, "name", rules.WORKSPACE_NAME.sentence),
        ({"url": URL, "name": ""}, "name", rules.WORKSPACE_NAME.sentence),
    ],
)
def test_an_address_or_a_name_the_rules_refuse_is_422_and_nothing_is_made(
    client, runtime, body, field, sentence
):
    r = client.post("/api/git/clone", json=body, headers=changing(ALICE))
    assert r.status_code == 422
    assert r.json()["errors"] == [{"field": field, "message": sentence}]
    assert listed(client) == [] and runtime.labelled() == {}


def test_no_workspace_is_cloned_for_a_person_being_deleted(service, settings):
    con = db.connect(settings.database)
    try:
        bob = people.by_email(con, BOB)
        people.mark_deleting(con, bob)
    finally:
        con.close()
    with pytest.raises(Problem) as refused:
        service.create(bob, None, URL)
    assert refused.value.code == "not_allowed"
    assert service.of_person(bob.id) == []


def test_another_person_cannot_see_reach_or_delete_a_clone(client, runtime, widget):
    made = cloned(client)
    assert made["state"] == "ready"
    assert listed(client, BOB) == []
    r = client.delete(f"/api/workspaces/{made['id']}", headers=changing(BOB))
    assert (r.status_code, r.json()["code"]) == (404, "workspace_not_found")
    r = client.get("/api/git/status", params={"workspace": made["id"]}, headers=as_user(BOB))
    assert (r.status_code, r.json()["code"]) == (404, "workspace_not_found")
    assert [w["id"] for w in listed(client)] == [made["id"]]  # positive control


# --- cancelling: deleting a workspace that is being cloned -----------------------------


def test_deleting_a_workspace_being_cloned_stops_git_by_removing_its_container(client, runtime):
    runtime.gate = threading.Event()
    r = clone(client)
    made = r.json()["workspace"]
    name = rt.container_name(made["id"])
    assert runtime.cloning.wait(MADE_WITHIN)
    assert runtime.removed == []  # positive control: nothing has removed it yet
    began = time.monotonic()
    r = client.delete(f"/api/workspaces/{made['id']}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert name in runtime.removed  # the delete itself did it, git running
    assert time.monotonic() - began < 15
    cleared(runtime, client)
    assert not runtime.exists(name)


def test_a_clone_holds_neither_the_person_s_lock_nor_the_workspace_s(client, app, runtime):
    runtime.gate = threading.Event()
    made = clone(client).json()["workspace"]
    assert runtime.cloning.wait(MADE_WITHIN)
    service = app.state.workspaces
    got = []

    def try_locks():
        for lock in (service.person_lock(1), service.lock(made["id"])):
            got.append(lock.acquire(blocking=False))
            if got[-1]:
                lock.release()

    thread = threading.Thread(target=try_locks)
    thread.start()
    thread.join(10)
    assert got == [True, True]  # sign-out, which takes them, is never held up by a clone
    runtime.gate.set()
    finished(client, made["id"])


def test_deleting_a_person_during_a_clone_finishes_promptly(client, runtime, settings):
    runtime.gate = threading.Event()
    made = clone(client, who=BOB).json()["workspace"]
    assert runtime.cloning.wait(MADE_WITHIN)
    everyone = client.get("/api/people", headers=as_user(ALICE)).json()["people"]
    bob = next(p for p in everyone if p["email"] == BOB)
    began = time.monotonic()
    r = client.delete(f"/api/people/{bob['id']}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert time.monotonic() - began < 30
    assert rt.container_name(made["id"]) in runtime.removed
    assert runtime.labelled() == {}
    assert row(settings, made["id"]) is None


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


def test_a_restart_remakes_a_creating_workspace_that_has_no_source(service, settings, runtime):
    blank = insert(settings, "blank", "creating", None)
    service.restore()
    assert row(settings, blank)["state"] == "ready"
    assert runtime.running(rt.container_name(blank))
    assert not [a for a, _ in runtime.execs if "clone" in a]
