"""Folders under the signed-in user's own home."""

from tests.conftest import ALICE, BOB, as_user


def test_each_user_sees_their_own_home(client):
    alice = client.get("/api/folders", params={"path": "projects"}, headers=as_user(ALICE)).json()
    bob = client.get("/api/folders", params={"path": "projects"}, headers=as_user(BOB)).json()
    assert [e["path"] for e in alice["entries"]] == ["projects/alice-project"]
    assert [e["path"] for e in bob["entries"]] == ["projects/bob-project"]


def test_home_listing_by_default(client):
    entries = client.get("/api/folders", headers=as_user(ALICE)).json()["entries"]
    assert [e["path"] for e in entries] == ["projects"]


def test_helper_refusals_become_problems(client):
    r = client.get("/api/folders", params={"path": "../bob"}, headers=as_user(ALICE))
    assert (r.status_code, r.json()["code"]) == (400, "path_outside_home")
    r = client.get("/api/folders", params={"path": "missing"}, headers=as_user(ALICE))
    assert (r.status_code, r.json()["code"]) == (404, "path_not_found")
    assert r.json()["title"]


def test_depth_and_hidden_folders_are_passed_through(client, helper_homes):
    (helper_homes.homes["alice"] / ".dot").mkdir()
    entries = client.get("/api/folders", params={"all": "true"}, headers=as_user(ALICE)).json()[
        "entries"
    ]
    assert ".dot" in [e["path"] for e in entries]
    r = client.get("/api/folders", params={"depth": 99}, headers=as_user(ALICE))
    assert r.status_code == 422
