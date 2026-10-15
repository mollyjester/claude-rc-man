"""The git routes (backend/rcman/routers/git.py): each takes a workspace by its id and runs
git in it, as the signed-in person's own; clone is not there yet."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from rcman import routes
from rcman import runtime as rt
from rcman.runtime import container_name

from helpers import rules
from tests.conftest import ALICE, BOB, CALLS, as_user, changing, made_workspace
from tests.git_fixtures import fixture_git, make_repo, push_branch_to_origin, write
from tests.test_audit import rows


def area_routes(app, prefix: str) -> list[tuple[str, str]]:
    found = sorted(
        (method, c.path)
        for c in routes.api_routes(app)
        if c.path == prefix or c.path.startswith(prefix + "/")
        for method in c.methods
    )
    assert found  # non-vacuity
    return found


def call(client, method, path, who=ALICE, **kwargs):
    kwargs = kwargs or CALLS[(method, path)]
    return client.request(method, path, headers=changing(who), **kwargs)


def get(client, path, workspace, who=ALICE):
    return client.get(path, params={"workspace": workspace}, headers=as_user(who))


def post(client, path, body, who=ALICE):
    return client.post(path, json=body, headers=changing(who))


@pytest.fixture
def work(client, app, runtime, tmp_path):
    """Alice's ready workspace, with her commit author set, and a repository at the root of
    its work folder with an origin outside it."""
    workspace = made_workspace(client, ALICE, "hers")
    r = client.patch(
        "/api/me/settings", json={"author_name": "Ann Author"}, headers=changing(ALICE)
    )
    assert r.status_code == 200, r.text
    folder = Path(runtime.layout(container_name(workspace["id"])).work)
    make_repo(folder, origin=tmp_path / "origins" / "repo.git")
    workspace["folder"] = folder
    return workspace


def codes(r) -> tuple[int, str | None]:
    return r.status_code, r.json().get("code")


# --- the routes --------------------------------------------------------------------


def test_every_git_route_takes_a_workspace_but_clone(app):
    from tests.test_problems import request_fields

    found = area_routes(app, "/api/git")
    assert len(found) >= 10  # status, branches and the changes
    taking = {(f.method, f.path) for f in request_fields(app) if f.name == "workspace"}
    assert set(found) - taking == {("POST", "/api/git/clone")}
    assert not [f for f in request_fields(app) if f.name == "path" and "git" in f.path]


def test_status_answers_the_branch_the_changes_and_the_author(client, work):
    write(work["folder"] / "new.txt")
    r = get(client, "/api/git/status", work["id"])
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["branch"]["name"] == "main" and out["branch"]["upstream"] == "origin/main"
    assert [f["path"] for f in out["not_staged"]["files"]] == ["new.txt"]
    assert out["author"] == {"name": "Ann Author", "email": ALICE}
    assert "path" not in out and "workspace" not in out
    assert {"merging", "staged", "push_has_remote", "push_to_github", "push_ahead"} <= set(out)


def test_status_has_no_author_until_a_name_is_set(client, app, runtime):
    workspace = made_workspace(client, ALICE, "hers")
    make_repo(Path(runtime.layout(container_name(workspace["id"])).work))
    assert get(client, "/api/git/status", workspace["id"]).json()["author"] is None


def test_stage_unstage_and_commit_as_the_persons_author(client, work):
    folder = work["folder"]
    write(folder / "a.txt")
    assert (
        post(client, "/api/git/stage", {"workspace": work["id"], "files": ["a.txt"]}).json() == {}
    )
    staged = get(client, "/api/git/status", work["id"]).json()["staged"]["files"]
    assert [f["path"] for f in staged] == ["a.txt"]
    post(client, "/api/git/unstage", {"workspace": work["id"], "files": ["a.txt"]})
    assert get(client, "/api/git/status", work["id"]).json()["staged"]["files"] == []
    post(client, "/api/git/stage", {"workspace": work["id"], "files": ["a.txt"]})
    r = post(client, "/api/git/commit", {"workspace": work["id"], "message": "Add a.txt\n"})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "commit": fixture_git(folder, "rev-parse", "HEAD").strip(),
        "branch": "main",
    }
    shown = fixture_git(folder, "log", "-1", "--format=%an <%ae> %cn <%ce>")
    assert shown == f"Ann Author <{ALICE}> Ann Author <{ALICE}>\n"


def test_a_commit_needs_an_author_name_and_commits_nothing_without_one(client, app, runtime):
    workspace = made_workspace(client, ALICE, "hers")
    folder = Path(runtime.layout(container_name(workspace["id"])).work)
    make_repo(folder)
    write(folder / "a.txt")
    post(client, "/api/git/stage", {"workspace": workspace["id"], "files": ["a.txt"]})
    r = post(client, "/api/git/commit", {"workspace": workspace["id"], "message": "m"})
    assert codes(r) == (409, "author_not_set")
    assert fixture_git(folder, "log", "--format=%s") == "First commit\n"


def test_branches_switch_and_new_branch(client, work):
    out = get(client, "/api/git/branches", work["id"]).json()
    assert [b["name"] for b in out["local"]["branches"]] == ["main"]
    assert [b["name"] for b in out["remote"]["branches"]] == ["origin/feature", "origin/main"]
    r = post(
        client,
        "/api/git/switch",
        {"workspace": work["id"], "branch": "origin/feature", "remote": True},
    )
    assert (r.status_code, r.json()) == (200, {"branch": "feature"})
    r = post(client, "/api/git/branch", {"workspace": work["id"], "name": "topic"})
    assert (r.status_code, r.json()) == (200, {"branch": "topic"})
    r = post(client, "/api/git/switch", {"workspace": work["id"], "branch": "nope"})
    assert codes(r) == (404, "branch_not_found")
    r = post(client, "/api/git/branch", {"workspace": work["id"], "name": "a..b"})
    assert codes(r) == (400, "branch_invalid")


def test_fetch_and_push(client, work):
    folder = work["folder"]
    assert post(client, "/api/git/fetch", {"workspace": work["id"]}).json() == {}
    write(folder / "more.txt")
    fixture_git(folder, "add", "more.txt")
    fixture_git(folder, "commit", "-q", "-m", "more")
    r = post(client, "/api/git/push", {"workspace": work["id"]})
    assert (r.status_code, r.json()) == (
        200,
        {"branch": "main", "upstream": "origin/main", "upstream_set": False},
    )
    origin = Path(fixture_git(folder, "remote", "get-url", "origin").strip())
    assert fixture_git(origin, "rev-parse", "main") == fixture_git(folder, "rev-parse", "HEAD")


def test_a_push_to_github_without_a_token_is_refused_and_with_one_gets_past_the_check(client, work):
    fixture_git(work["folder"], "remote", "set-url", "origin", "https://github.com/o/n.git")
    r = post(client, "/api/git/push", {"workspace": work["id"]})
    assert codes(r) == (409, "github_token_not_set")
    assert get(client, "/api/git/status", work["id"]).json()["github_token"] is False
    assert post(client, "/api/github/token", {"token": "t-123"}).status_code == 200
    status = get(client, "/api/git/status", work["id"]).json()
    assert (status["push_to_github"], status["github_token"]) == (True, True)


def test_commit_and_push_answers_the_commit_and_the_push(client, work, settings):
    write(work["folder"] / "a.txt")
    post(client, "/api/git/stage", {"workspace": work["id"], "files": ["a.txt"]})
    r = post(client, "/api/git/commit-push", {"workspace": work["id"], "message": "m"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["push_problem"] is None and out["push"]["upstream"] == "origin/main"
    assert out["commit"] == fixture_git(work["folder"], "rev-parse", "HEAD").strip()
    assert rows(settings)[-1]["result"] == "ok"


def test_a_commit_made_and_not_pushed_is_audited_as_such(client, work, settings):
    folder = work["folder"]
    origin = Path(fixture_git(folder, "remote", "get-url", "origin").strip())
    push_branch_to_origin(folder, origin, "elsewhere")
    fixture_git(
        origin, "update-ref", "refs/heads/main", "refs/heads/elsewhere"
    )  # the push is rejected
    write(folder / "a.txt")
    post(client, "/api/git/stage", {"workspace": work["id"], "files": ["a.txt"]})
    r = post(client, "/api/git/commit-push", {"workspace": work["id"], "message": "m"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["push"] is None and out["push_problem"]["code"] == "git_failed"
    assert out["commit"] == fixture_git(folder, "rev-parse", "HEAD").strip()
    assert rows(settings)[-1]["result"] == "committed_not_pushed"


def test_a_push_the_runtime_fails_after_the_commit_is_still_a_commit_not_pushed(
    client, app, work, settings
):
    write(work["folder"] / "a.txt")
    post(client, "/api/git/stage", {"workspace": work["id"], "files": ["a.txt"]})
    runtime = app.state.workspaces.runtime
    real = runtime.exec

    def exec_(name, argv, stdin=None, timeout=rt.PODMAN_TIMEOUT):
        if "push" in argv:
            raise rt.RuntimeFailed("the container went away")
        return real(name, argv, stdin, timeout)

    runtime.exec = exec_
    r = post(client, "/api/git/commit-push", {"workspace": work["id"], "message": "m"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["commit"] == fixture_git(work["folder"], "rev-parse", "HEAD").strip()
    assert out["push"] is None and out["push_problem"]["code"] == "workspace_failed"
    assert rows(settings)[-1]["result"] == "committed_not_pushed"


def test_a_problem_git_has_comes_with_what_git_said(client, work):
    (work["folder"] / ".git" / "index.lock").write_text("")
    write(work["folder"] / "a.txt")
    r = post(client, "/api/git/stage", {"workspace": work["id"], "files": ["a.txt"]})
    assert codes(r) == (409, "git_failed")
    assert any("index.lock" in line for line in r.json()["git"]) and r.json()["git_more"] == 0


# --- whose workspace, and in what state ---------------------------------------------


def test_another_persons_workspace_is_not_there_for_every_route(client, work):
    for method, path in area_routes(client.app, "/api/git"):
        if path == "/api/git/clone":
            continue
        kwargs = {k: {**v, "workspace": work["id"]} for k, v in CALLS[(method, path)].items()}
        r = call(client, method, path, who=BOB, **kwargs)
        assert codes(r) == (404, "workspace_not_found"), (method, path)
    # positive control: the same requests are answered for alice
    assert get(client, "/api/git/status", work["id"]).status_code == 200


def test_a_workspace_that_is_not_ready_is_not_asked(client, app):
    runtime = app.state.workspaces.runtime

    def broken(spec):
        raise rt.RuntimeFailed("the image is not there")

    runtime.create = broken
    r = client.post("/api/workspaces", json={"name": "broken"}, headers=changing(ALICE))
    failed = made_workspace_failed(client, r.json()["workspace"]["id"])
    assert failed["state"] == "failed"
    assert codes(get(client, "/api/git/status", failed["id"])) == (409, "workspace_not_ready")
    r = post(client, "/api/git/fetch", {"workspace": failed["id"]})
    assert codes(r) == (409, "workspace_not_ready")


def made_workspace_failed(client, workspace_id: int) -> dict:
    import time

    deadline = time.monotonic() + 30
    while True:
        listed = client.get("/api/workspaces", headers=as_user(ALICE)).json()["workspaces"]
        found = next(w for w in listed if w["id"] == workspace_id)
        if found["state"] != "creating" or time.monotonic() > deadline:
            return found
        time.sleep(0.05)


def test_a_stopped_container_is_the_workspace_failing(client, app, work):
    app.state.workspaces.runtime.stop(container_name(work["id"]))
    app.state.workspaces.close_channels()
    assert codes(get(client, "/api/git/status", work["id"])) == (502, "workspace_failed")
    assert codes(post(client, "/api/git/fetch", {"workspace": work["id"]})) == (
        502,
        "workspace_failed",
    )
    app.state.workspaces.runtime.start(container_name(work["id"]))
    assert get(client, "/api/git/status", work["id"]).status_code == 200  # positive control


def test_an_unreachable_runtime_is_unavailable(client, app, work):
    runtime = app.state.workspaces.runtime
    app.state.workspaces.close_channels()
    moved = runtime.root.with_name("moved-workspaces")
    runtime.root.rename(moved)
    try:
        assert codes(get(client, "/api/git/status", work["id"])) == (503, "runtime_unavailable")
    finally:
        moved.rename(runtime.root)
    assert get(client, "/api/git/status", work["id"]).status_code == 200


def test_git_needs_no_server_and_starts_none_and_takes_no_workspace_lock(client, app, work):
    service = app.state.workspaces
    assert service.get(work["id"]).server is False
    lock = service.lock(work["id"])
    answered = []
    lock.acquire()  # held, as a start or a deletion of the workspace holds it
    try:
        worker = threading.Thread(
            target=lambda: answered.append(get(client, "/api/git/status", work["id"])),
            daemon=True,
        )
        worker.start()
        worker.join(15)
    finally:
        lock.release()
    assert answered and answered[0].status_code == 200
    now = service.get(work["id"])
    assert (now.server, now.wanted) == (False, False)  # nothing was started


def test_a_workspace_with_no_repository_says_so(client, app, runtime):
    workspace = made_workspace(client, ALICE, "empty")
    r = get(client, "/api/git/status", workspace["id"])
    assert codes(r) == (409, "not_a_repository")


# --- audit ---------------------------------------------------------------------------


def test_every_changing_git_route_records_the_workspace_by_its_name(client, work, settings):
    changing_routes = [
        (m, p) for m, p in area_routes(client.app, "/api/git") if m == "POST" and "clone" not in p
    ]
    assert len(changing_routes) >= 8
    for method, path in changing_routes:
        kwargs = {k: {**v, "workspace": work["id"]} for k, v in CALLS[(method, path)].items()}
        call(client, method, path, **kwargs)
        last = rows(settings)[-1]
        assert last["target"] == "hers" and last["action"].startswith("git."), (path, last)
    assert get(client, "/api/git/status", work["id"]).status_code == 200


def test_a_commit_message_is_still_checked_by_its_rule(client):
    r = call(client, "POST", "/api/git/commit", json={"workspace": 1, "message": "  "})
    assert r.status_code == 422
    assert r.json()["errors"] == [{"field": "message", "message": rules.COMMIT_MESSAGE.sentence}]


def test_a_workspace_is_a_whole_number_from_one(client):
    r = call(client, "POST", "/api/git/fetch", json={"workspace": 0})
    assert r.status_code == 422
    assert r.json()["errors"] == [{"field": "workspace", "message": "This must be at least 1."}]
    r = client.get("/api/git/status", headers=as_user(ALICE))
    assert r.status_code == 422
    assert r.json()["errors"] == [{"field": "workspace", "message": "This is required."}]


# --- clone: the address's rule (the workspace it makes is tests/test_clone.py's) ----------


@pytest.mark.parametrize("url", ["https://user:pass@github.com/a/b", "http://github.com/a/b"])
def test_a_clone_address_is_checked_by_its_rule(client, url):
    r = call(client, "POST", "/api/git/clone", json={"url": url})
    assert r.status_code == 422
    assert r.json()["errors"] == [{"field": "url", "message": rules.GITHUB_URL.sentence}]


def test_a_read_needs_a_signed_in_person(client):
    assert client.get("/api/git/status", params={"workspace": 1}).status_code == 401
    r = client.get("/api/git/status", params={"workspace": 1}, headers=as_user(ALICE))
    assert codes(r) == (404, "workspace_not_found")  # there is none yet
