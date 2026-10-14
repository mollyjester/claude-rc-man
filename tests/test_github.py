"""The GitHub token's routes: not there yet in workspaces, each answers
not_available_yet, its body checked as it will be."""

from helpers import rules
from tests.conftest import ALICE, CALLS, changing
from tests.test_git import area_routes, call


def test_every_github_route_answers_not_available_yet(client, app):
    for method, path in area_routes(app, "/api/github"):
        r = call(client, method, path)
        assert (r.status_code, r.json()["code"]) == (501, "not_available_yet"), (method, path)


def test_a_token_is_still_checked_by_its_rule_and_never_echoed(client):
    r = client.post("/api/github/token", json={"token": "has space"}, headers=changing(ALICE))
    assert r.status_code == 422
    assert r.json()["errors"] == [{"field": "token", "message": rules.GITHUB_TOKEN.sentence}]
    assert "has space" not in r.text
    assert CALLS[("POST", "/api/github/token")]
