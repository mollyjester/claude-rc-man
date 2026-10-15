"""The GitHub token's routes (backend/rcman/routers/github.py): what each answers, what is
audited, and that the token is never answered back. Where the token goes, and what a failed
write leaves, is tests/test_git_account.py's."""

from helpers import rules
from tests.conftest import ALICE, CALLS, as_user, changing
from tests.test_audit import rows
from tests.test_git import area_routes

TOKEN = "a-github-token-never-echoed"


def test_the_token_is_saved_reported_and_forgotten(client):
    assert client.get("/api/github", headers=as_user(ALICE)).json() == {"token": False}
    r = client.post("/api/github/token", json={"token": TOKEN}, headers=changing(ALICE))
    assert (r.status_code, r.json()) == (200, {"token": True})
    assert client.get("/api/github", headers=as_user(ALICE)).json() == {"token": True}
    r = client.delete("/api/github/token", headers=changing(ALICE))
    assert (r.status_code, r.json()) == (200, {"token": False})
    assert client.get("/api/github", headers=as_user(ALICE)).json() == {"token": False}


def test_every_github_route_is_driven_here(app):
    assert area_routes(app, "/api/github") == [
        ("DELETE", "/api/github/token"),
        ("GET", "/api/github"),
        ("POST", "/api/github/token"),
    ]
    assert all(key in CALLS for key in area_routes(app, "/api/github"))


def test_a_token_is_still_checked_by_its_rule_and_never_echoed(client):
    r = client.post("/api/github/token", json={"token": "has space"}, headers=changing(ALICE))
    assert r.status_code == 422
    assert r.json()["errors"] == [{"field": "token", "message": rules.GITHUB_TOKEN.sentence}]
    assert "has space" not in r.text
    assert CALLS[("POST", "/api/github/token")]


def test_neither_audit_row_has_a_target_and_the_token_is_nowhere_in_them(client, settings, caplog):
    caplog.set_level("DEBUG")
    r = client.post("/api/github/token", json={"token": TOKEN}, headers=changing(ALICE))
    assert TOKEN not in r.text
    client.delete("/api/github/token", headers=changing(ALICE))
    saved, forgotten = rows(settings)[-2:]
    assert (saved["action"], saved["target"], saved["result"]) == ("github.token", None, "ok")
    assert (forgotten["action"], forgotten["target"], forgotten["result"]) == (
        "github.forget",
        None,
        "ok",
    )
    assert caplog.records  # positive control: the audit events were logged
    assert TOKEN not in caplog.text
    assert all(TOKEN not in str(v) for row in rows(settings) for v in row.values())


def test_a_failed_save_is_audited_with_the_problem_and_still_no_token(client, app, settings):
    from rcman import runtime as rt

    def down(*args, **kwargs):
        raise rt.RuntimeUnavailable("down")

    app.state.workspaces.runtime.put_account = down
    r = client.post("/api/github/token", json={"token": TOKEN}, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (503, "runtime_unavailable")
    last = rows(settings)[-1]
    assert (last["action"], last["target"], last["result"]) == (
        "github.token",
        None,
        "runtime_unavailable",
    )


def test_a_read_needs_a_signed_in_person(client):
    assert client.get("/api/github").status_code == 401
