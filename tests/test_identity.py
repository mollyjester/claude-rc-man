"""Who is asking: the proxy's email header, the users file, the origin."""

from helpers import users_file as uf
from tests.conftest import ALICE, BOB, ORIGIN, as_user


def test_no_identity_is_401(client):
    assert client.get("/api/me").status_code == 401


def test_unknown_email_is_403(client):
    r = client.get("/api/me", headers=as_user("stranger@example.com"))
    assert (r.status_code, r.json()["code"]) == (403, "not_allowed")


def test_email_matching_is_case_insensitive(client):
    assert client.get("/api/me", headers=as_user("ALICE@Example.com")).json()["linux"] == "alice"


def test_removed_user_is_refused_at_once(client, settings):
    assert client.get("/api/me", headers=as_user(BOB)).status_code == 200
    uf.save(settings.users_file, [u for u in uf.load(settings.users_file) if u.email != BOB])
    assert client.get("/api/me", headers=as_user(BOB)).status_code == 403


def test_admin_routes_refuse_non_admins(client):
    for path in ("/api/audit", "/api/users"):
        r = client.get(path, headers=as_user(BOB))
        assert (r.status_code, r.json()["code"]) == (403, "admin_only"), path
    assert client.get("/api/audit", headers=as_user(ALICE)).status_code == 200


def test_changes_need_the_public_origin(client):
    body = {"prefix": "abc"}
    headers = as_user(ALICE)
    r = client.put("/api/me/settings", json=body, headers=headers)  # no Origin
    assert (r.status_code, r.json()["code"]) == (403, "origin_refused")
    r = client.put(
        "/api/me/settings", json=body, headers={**headers, "Origin": "https://evil.example.com"}
    )
    assert r.status_code == 403
    assert (
        client.put("/api/me/settings", json=body, headers={**headers, "Origin": ORIGIN}).status_code
        == 200
    )


def test_reads_do_not_need_an_origin(client):
    assert client.get("/api/me", headers=as_user(ALICE)).status_code == 200


def test_invalid_users_file_is_a_500_problem(client, settings):
    settings.users_file.write_text("not toml [")
    r = client.get("/api/me", headers=as_user(ALICE))
    assert (r.status_code, r.json()["code"]) == (500, "users_file_invalid")


def test_health_needs_no_identity(client):
    assert client.get("/api/health").json() == {"status": "ok"}
