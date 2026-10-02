"""Folders under the signed-in user's own home."""

import pytest

from helpers import rc_helper
from tests.conftest import ALICE, BOB, as_user, changing
from tests.test_rc_helper import FOLDER_NAMES, format_characters, hangul_fillers


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


# --- new folders ------------------------------------------------------------------


def create(client, who, body):
    return client.post("/api/folders", json=body, headers=changing(who))


def test_a_new_folder_is_made_in_ones_own_home(client, helper_homes):
    r = create(client, ALICE, {"parent": "projects", "name": "new"})
    assert (r.status_code, r.json()) == (200, {"created": "projects/new"})
    assert (helper_homes.homes["alice"] / "projects" / "new").is_dir()
    assert not (helper_homes.homes["bob"] / "projects" / "new").exists()


def test_a_new_folder_without_a_parent_is_made_in_the_home_folder(client, helper_homes):
    r = create(client, ALICE, {"name": "top"})
    assert (r.status_code, r.json()) == (200, {"created": "top"})
    assert (helper_homes.homes["alice"] / "top").is_dir()


@pytest.mark.parametrize(("name", "accepted"), FOLDER_NAMES)
def test_the_server_accepts_exactly_the_names_the_helpers_rule_does(client, name, accepted):
    assert bool(rc_helper.FOLDER_NAME.fullmatch(name)) is accepted  # the helper's own verdict
    r = create(client, ALICE, {"parent": "projects", "name": name})
    if accepted:
        assert (r.status_code, r.json()) == (200, {"created": f"projects/{name}"})
    else:
        assert r.status_code == 422, name
        assert [e["field"] for e in r.json()["errors"]] == ["name"]


def test_the_server_refuses_exactly_the_characters_the_helpers_rule_does(client):
    spaces = [chr(c) for c in range(0x3001) if chr(c).isspace()]
    found = format_characters() + hangul_fillers() + spaces
    assert chr(0x200B) in found and " " in found and chr(0x3000) in found  # non-vacuity
    disagree = []
    for c in found:
        for name in (f"a{c}b", f"{c}a", f"a{c}"):
            status = create(client, ALICE, {"name": name}).status_code
            if (status == 200) is not bool(rc_helper.FOLDER_NAME.fullmatch(name)):
                disagree.append((f"U+{ord(c):04X}", name, status))
    assert disagree == []


def test_the_folder_name_rule_is_the_helpers_rule():
    from rcman.routers.folders import NewFolder

    properties = NewFolder.model_json_schema()["properties"]
    assert properties["name"]["pattern"] == f"^{rc_helper.FOLDER_NAME.pattern}$"


def test_a_name_the_file_system_cannot_take_is_the_helpers_refusal(client, helper_homes):
    import os

    long = "п" * (os.pathconf(helper_homes.homes["alice"], "PC_NAME_MAX") // 2 + 1)
    r = create(client, ALICE, {"name": long})
    assert (r.status_code, r.json()["code"]) == (400, "name_invalid")


def test_a_new_folder_where_one_is_already_is_a_conflict(client):
    r = create(client, ALICE, {"parent": "projects", "name": "alice-project"})
    assert (r.status_code, r.json()["code"]) == (409, "folder_exists")


def test_a_new_folder_is_never_made_in_another_persons_home(client, helper_homes):
    r = create(client, BOB, {"parent": "../alice/projects", "name": "x"})
    assert (r.status_code, r.json()["code"]) == (400, "path_outside_home")
    r = create(client, BOB, {"parent": "projects/alice-project", "name": "x"})
    assert (r.status_code, r.json()["code"]) == (404, "path_not_found")
    assert list((helper_homes.homes["alice"] / "projects" / "alice-project").iterdir()) == []


# --- deleting folders ---------------------------------------------------------------


def delete(client, who, path):
    return client.delete("/api/folders", params={"path": path}, headers=changing(who))


def test_deleting_a_folder_stops_and_forgets_its_server(client, helper_homes):
    project = {"path": "projects/alice-project"}
    assert client.post("/api/servers", json=project, headers=changing(ALICE)).status_code == 200
    r = delete(client, ALICE, "projects/alice-project")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "deleted": "projects/alice-project",
        "link": False,
        "stopped": ["projects/alice-project"],
        "forgotten": ["projects/alice-project"],
    }
    assert client.get("/api/servers", headers=as_user(ALICE)).json()["servers"] == []
    assert not (helper_homes.homes["alice"] / "projects" / "alice-project").exists()


@pytest.mark.parametrize("path", [".", "projects/.."])
def test_the_home_folder_cannot_be_deleted(client, helper_homes, path):
    r = delete(client, ALICE, path)
    assert (r.status_code, r.json()["code"]) == (400, "path_is_home")
    assert (helper_homes.homes["alice"] / "projects" / "alice-project").is_dir()


def test_an_empty_path_is_refused_before_the_helper(client):
    r = delete(client, ALICE, "")
    assert r.status_code == 422 and [e["field"] for e in r.json()["errors"]] == ["path"]


def test_one_person_cannot_delete_anothers_folders(client, helper_homes):
    alices = helper_homes.homes["alice"] / "projects" / "alice-project"
    r = delete(client, BOB, "../alice/projects/alice-project")
    assert (r.status_code, r.json()["code"]) == (400, "path_outside_home")
    r = delete(client, BOB, "projects/alice-project")
    assert (r.status_code, r.json()["code"]) == (404, "path_not_found")
    assert alices.is_dir()
    assert delete(client, ALICE, "projects/alice-project").status_code == 200  # positive control
    assert not alices.exists()


def test_a_path_beginning_with_a_hyphen_is_a_path(client, helper_homes):
    assert create(client, ALICE, {"name": "-x"}).status_code == 200
    r = create(client, ALICE, {"parent": "-x", "name": "-y"})
    assert (r.status_code, r.json()) == (200, {"created": "-x/-y"})
    assert delete(client, ALICE, "-x").status_code == 200
    assert not (helper_homes.homes["alice"] / "-x").exists()


def test_deleting_has_its_own_timeout(settings, admin_runner):
    from fastapi.testclient import TestClient
    from rcman import helpers_client
    from rcman.main import create_app

    from helpers import rc_helper

    seen = {}

    def runner(linux, args, stdin=None, timeout=None):
        seen[args[0]] = timeout
        return 0, {}

    with TestClient(create_app(settings, helper=runner, admin=admin_runner)) as client:
        client.get("/api/folders", headers=as_user(ALICE))
        delete(client, ALICE, "projects/alice-project")
    assert seen["tree"] == helpers_client.HELPER_TIMEOUT  # positive control: the usual one
    assert seen["delete"] == helpers_client.FOLDER_DELETE_TIMEOUT_SECONDS
    assert helpers_client.FOLDER_DELETE_TIMEOUT_SECONDS != helpers_client.HELPER_TIMEOUT
    # long enough for the helper to give up on a server that does not stop, and more
    assert helpers_client.FOLDER_DELETE_TIMEOUT_SECONDS > rc_helper.STOP_TIMEOUT
