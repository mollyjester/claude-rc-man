"""User management for administrators, through rc-admin."""

from helpers import users_file as uf
from tests.conftest import ALICE, BOB, as_user, changing


def test_list_comes_from_rc_admin(client, admin_runner):
    admin_runner.replies = [(0, {"users": [{"linux": "alice"}]})]
    assert client.get("/api/users", headers=as_user(ALICE)).json() == {
        "users": [{"linux": "alice"}]
    }
    assert admin_runner.calls == [["list"]]


def test_add_passes_exactly_the_request(client, admin_runner):
    admin_runner.replies = [(0, {"user": {"linux": "dave"}, "created": True})]
    body = {"linux": "dave", "email": "dave@example.com", "admin": True, "adopt": True}
    r = client.post("/api/users", json=body, headers=changing(ALICE))
    assert r.status_code == 200
    assert admin_runner.calls == [["add", "--admin", "--adopt", "--", "dave", "dave@example.com"]]


def test_delete_needs_a_home_decision_and_names_the_requester(client, admin_runner):
    r = client.delete("/api/users/bob", headers=changing(ALICE))
    assert r.status_code == 422 and admin_runner.calls == []
    admin_runner.replies = [(0, {"deleted": "bob", "home_deleted": True})]
    r = client.delete("/api/users/bob", params={"home": "delete"}, headers=changing(ALICE))
    assert r.status_code == 200
    assert admin_runner.calls == [["del", "--delete", f"--requester={ALICE}", "--", "bob"]]


def test_admin_refusals_become_problems(client, admin_runner):
    admin_runner.replies = [
        (1, {"error": {"code": "last_admin", "message": "That is the last administrator."}})
    ]
    r = client.delete("/api/users/alice", params={"home": "keep"}, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (409, "last_admin")


def test_input_is_validated_before_rc_admin(client, admin_runner):
    r = client.post(
        "/api/users", json={"linux": "Bad!", "email": "x@example.com"}, headers=changing(ALICE)
    )
    assert r.status_code == 422 and admin_runner.calls == []
    assert r.json()["errors"] == [{"field": "linux", "message": uf.LINUX_NAME.sentence}]
    r = client.post("/api/users", json={"linux": "dave", "email": "nope"}, headers=changing(ALICE))
    assert r.status_code == 422 and admin_runner.calls == []
    assert r.json()["errors"] == [{"field": "email", "message": uf.EMAIL.sentence}]


def test_an_email_that_reads_as_an_option_is_refused_at_its_field(client, admin_runner):
    body = {"linux": "dave", "email": "-x@example.com"}
    r = client.post("/api/users", json=body, headers=changing(ALICE))
    assert r.status_code == 422 and admin_runner.calls == []
    assert r.json()["errors"] == [{"field": "email", "message": uf.EMAIL.sentence}]


def test_the_user_to_delete_is_validated_before_rc_admin(client, admin_runner):
    r = client.delete("/api/users/Bad!", params={"home": "keep"}, headers=changing(ALICE))
    assert r.status_code == 422 and admin_runner.calls == []
    assert r.json()["errors"] == [{"field": "linux", "message": uf.LINUX_NAME.sentence}]


def test_non_admins_cannot_manage_users(client, admin_runner):
    r = client.post(
        "/api/users", json={"linux": "dave", "email": "d@example.com"}, headers=changing(BOB)
    )
    assert r.status_code == 403 and admin_runner.calls == []
