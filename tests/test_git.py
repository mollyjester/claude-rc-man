"""The git routes (backend/rcman/routers/git.py): each takes a workspace by its id and runs
git in it, as the signed-in person's own, and records the workspace by its name. Each verb is
tests/test_repository.py's."""

from __future__ import annotations

from pathlib import Path

import pytest
from rcman import routes
from rcman.runtime import container_name

from tests.conftest import ALICE, BOB, CALLS, as_user, changing, made_workspace
from tests.git_fixtures import fixture_git, make_repo, write
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


def call(client, method, path, who, **kwargs):
    return client.request(method, path, headers=changing(who), **kwargs)


def get(client, path, workspace):
    return client.get(path, params={"workspace": workspace}, headers=as_user(ALICE))


def post(client, path, body):
    return client.post(path, json=body, headers=changing(ALICE))


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


def test_a_change_is_seen_staged_and_committed_as_the_persons_author(client, work, settings):
    folder = work["folder"]
    write(folder / "a.txt")
    out = get(client, "/api/git/status", work["id"]).json()
    assert [f["path"] for f in out["not_staged"]["files"]] == ["a.txt"]
    assert out["author"] == {"name": "Ann Author", "email": ALICE}
    assert (
        post(client, "/api/git/stage", {"workspace": work["id"], "files": ["a.txt"]}).json() == {}
    )
    r = post(client, "/api/git/commit", {"workspace": work["id"], "message": "Add a.txt\n"})
    assert r.status_code == 200, r.text
    assert r.json() == {
        "commit": fixture_git(folder, "rev-parse", "HEAD").strip(),
        "branch": "main",
    }
    shown = fixture_git(folder, "log", "-1", "--format=%an <%ae> %cn <%ce>")
    assert shown == f"Ann Author <{ALICE}> Ann Author <{ALICE}>\n"
    last = rows(settings)[-1]
    assert (last["action"], last["target"]) == ("git.commit", "hers")  # the workspace by its name


# --- whose workspace -------------------------------------------------------------


def test_another_persons_workspace_is_not_there_for_every_route(client, work):
    for method, path in area_routes(client.app, "/api/git"):
        if path == "/api/git/clone":
            continue
        kwargs = {k: {**v, "workspace": work["id"]} for k, v in CALLS[(method, path)].items()}
        r = call(client, method, path, who=BOB, **kwargs)
        assert codes(r) == (404, "workspace_not_found"), (method, path)
    # positive control: the same requests are answered for alice
    assert get(client, "/api/git/status", work["id"]).status_code == 200
