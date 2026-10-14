"""The git routes: not there yet in workspaces, each answers not_available_yet, in the
shape it will have, its body checked as it will be."""

import pytest
from rcman import routes

from helpers import rules
from tests.conftest import ALICE, CALLS, as_user, changing


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


def test_every_git_route_answers_not_available_yet(client, app):
    found = area_routes(app, "/api/git")
    assert len(found) >= 10  # status, branches and the changes
    for method, path in found:
        r = call(client, method, path)
        assert (r.status_code, r.json()["code"]) == (501, "not_available_yet"), (method, path)


def test_a_commit_message_is_still_checked_by_its_rule(client):
    r = call(client, "POST", "/api/git/commit", json={"path": "repo", "message": "  "})
    assert r.status_code == 422
    assert r.json()["errors"] == [{"field": "message", "message": rules.COMMIT_MESSAGE.sentence}]


@pytest.mark.parametrize("url", ["https://user:pass@github.com/a/b", "http://github.com/a/b"])
def test_a_clone_address_is_still_checked_by_its_rule(client, url):
    r = call(client, "POST", "/api/git/clone", json={"url": url})
    assert r.status_code == 422
    assert r.json()["errors"] == [{"field": "url", "message": rules.GITHUB_URL.sentence}]


def test_a_read_needs_a_signed_in_person(client):
    assert client.get("/api/git/status", params={"path": "repo"}).status_code == 401
    r = client.get("/api/git/status", params={"path": "repo"}, headers=as_user(ALICE))
    assert r.status_code == 501
