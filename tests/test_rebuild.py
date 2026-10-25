"""Rebuilding a workspace on its volume (backend/rcman/workspaces.py: ``Workspaces.rebuild``,
POST /api/workspaces/{id}/rebuild): telling that its devcontainer.json changed (GET
/api/workspaces/{id}/devcontainer), preparing the new image beside the running container,
replacing the container, putting the old one back when the new one fails, what a restart of
rcman finds in each window, deletions in each phase, and the rebuild of a failed workspace that
kept its volume. On the host runtime of tests/test_devcontainer_flow.py, whose images a test
plants; the repository is cloned by real git from the tests' stand-in for github.com, and a
change to the file is written in the workspace's work folder, as a person makes it there."""

from __future__ import annotations

import dataclasses
import json
import logging
import shutil
import threading
import time

import pytest
from rcman import db, people, problems
from rcman import devcontainer as dc
from rcman import runtime as rt
from rcman import workspaces as ws

from tests.conftest import BOB, MADE_WITHIN, as_user, changing, host_runtime, signed_in
from tests.git_fixtures import fixture_git, make_repo
from tests.test_clone import ALICE, cloned, entry, listed, row
from tests.test_devcontainer_flow import (
    FILE,
    GROUP,
    IMAGE,
    PASSWD,
    Flow,
    Link,
    building,
    config,
    name_of,
    no_trace,
    work_of,
)

OTHER = "registry.example.com/team/other:latest"
OLD_ID, NEW_ID, DEFAULT_ID, NEWER_DEFAULT_ID = "a" * 64, "b" * 64, "d" * 64, "e" * 64
BUILT_ID, REBUILT_ID = "1" * 64, "2" * 64
OLD_COMMAND = "echo old-made"


@pytest.fixture
def runtime(tmp_path):
    with host_runtime(tmp_path, Flow) as made:
        yield made


@pytest.fixture
def publish(github_stand_in, tmp_path):
    """``publish(files)``: the repository at the address the tests clone, with ``files`` (name
    -> text, or a ``Link`` to a path) committed in it, as tests/test_devcontainer_flow.py's."""
    counter = iter(range(1000))

    def publish(files: dict):
        origin = github_stand_in / "octo" / "widget.git"
        if origin.exists():  # a test that publishes again publishes anew
            shutil.rmtree(origin)
        repo = make_repo(
            tmp_path / f"src{next(counter)}", origin=github_stand_in / "octo" / "widget.git"
        )
        for name, content in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, Link):
                path.symlink_to(content.target)
            else:
                path.write_text(content)
        fixture_git(repo, "add", "-A")
        fixture_git(repo, "commit", "-q", "-m", "A devcontainer")
        fixture_git(repo, "push", "-q", "origin", "main")
        return repo

    return publish


@pytest.fixture(autouse=True)
def images(runtime):
    """The image the workspaces are made from, and another one, each with an id of its own."""
    runtime.images[IMAGE] = dataclasses.replace(runtime.images[IMAGE], id=OLD_ID)
    runtime.images[OTHER] = rt.ImageFacts(
        passwd=PASSWD, group=GROUP, env={"PATH": "/other/bin:/bin"}, id=NEW_ID
    )
    runtime.build_facts = dataclasses.replace(runtime.build_facts, id=BUILT_ID)


def made(client, publish, files: dict | None = None) -> dict:
    publish(config(postCreateCommand=OLD_COMMAND) if files is None else files)
    found = cloned(client)
    assert found["state"] == "ready", found
    return found


def rewrite(runtime, entry_: dict, files: dict) -> None:
    """Change the files of the workspace's repository in its work folder; None removes one."""
    for name, text in files.items():
        path = work_of(runtime, entry_) / name
        if text is None:
            path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)


def to(image: str = OTHER, **keys) -> dict:
    return {FILE: json.dumps({"image": image, **keys})}


def ask(client, workspace_id: int, confirm=True, who=ALICE):
    return client.post(
        f"/api/workspaces/{workspace_id}/rebuild", json={"confirm": confirm}, headers=changing(who)
    )


def settled(client, workspace_id: int, who=ALICE) -> dict:
    """The workspace once nothing works on it any more: its rebuild ended."""
    service = client.app.state.workspaces
    with service._lock:
        thread = service._making.get(workspace_id)
    if thread is not None:
        thread.join(MADE_WITHIN)
        assert not thread.is_alive()
    return entry(client, workspace_id, who)


def rebuilt(client, workspace_id: int) -> dict:
    r = ask(client, workspace_id)
    assert r.status_code == 202, r.text
    return settled(client, workspace_id)


def changed(client, workspace_id: int, who=ALICE):
    r = client.get(f"/api/workspaces/{workspace_id}/devcontainer", headers=as_user(who))
    assert r.status_code == 200, r.text
    return r.json()["changed"]


def plan_of(settings, workspace_id: int) -> dc.Plan | None:
    text = row(settings, workspace_id)["environment"]
    return None if text is None else dc.Plan.from_json(text)


def spec_of(runtime, entry_: dict) -> rt.Spec:
    return runtime.specs[name_of(entry_)]


def keep_work(runtime, entry_: dict) -> None:
    (work_of(runtime, entry_) / "kept.txt").write_text("my work")


def work_kept(runtime, entry_: dict) -> bool:
    return (work_of(runtime, entry_) / "kept.txt").read_text() == "my work"


def ran(runtime) -> list[str]:
    """The lifecycle commands run so far, each by what it runs."""
    return [argv[-1] for argv in runtime.hooks_run()]


def set_row(settings, workspace_id: int, **columns) -> None:
    con = db.connect(settings.database)
    try:
        con.execute(
            f"update workspaces set {', '.join(f'{k} = ?' for k in columns)} where id = ?",
            (*columns.values(), workspace_id),
        )
        con.commit()
    finally:
        con.close()


def alice(settings) -> people.Person:
    con = db.connect(settings.database)
    try:
        return people.by_email(con, ALICE)
    finally:
        con.close()


def wait_for(check, within: float = MADE_WITHIN):
    deadline = time.monotonic() + within
    while not (found := check()):
        assert time.monotonic() < deadline
        time.sleep(0.02)
    return found


# --- telling that the file changed ----------------------------------------------------------


def test_the_digest_of_the_file_is_stored_when_the_workspace_is_made(
    client, runtime, publish, settings
):
    with_file = made(client, publish)
    digest = row(settings, with_file["id"])["devcontainer_digest"]
    assert digest and digest != dc.NO_FILE_DIGEST
    text = (work_of(runtime, with_file) / FILE).read_text()
    assert digest == dc.digest(dc.ConfigFile(text, FILE))


def test_the_digest_of_no_file_is_stored_for_a_repository_without_one(client, publish, settings):
    without = made(client, publish, {"README.md": "no devcontainer here\n"})
    assert row(settings, without["id"])["devcontainer_digest"] == dc.NO_FILE_DIGEST


def test_the_digest_covers_the_dockerfile_a_file_builds_from(client, runtime, publish, settings):
    found = made(client, publish, building())
    text = (work_of(runtime, found) / FILE).read_text()
    assert row(settings, found["id"])["devcontainer_digest"] == dc.digest(
        dc.ConfigFile(text, FILE), b"FROM ubuntu:24.04\n"
    )


def test_an_unchanged_file_is_not_changed_and_a_changed_one_is(client, runtime, publish):
    found = made(client, publish)
    assert changed(client, found["id"]) is False
    rewrite(runtime, found, to())
    assert changed(client, found["id"]) is True


def test_a_file_removed_or_added_is_a_change(client, runtime, publish):
    with_file = made(client, publish)
    rewrite(runtime, with_file, {FILE: None})
    assert changed(client, with_file["id"]) is True
    client.delete(f"/api/workspaces/{with_file['id']}", headers=changing(ALICE))
    without = made(client, publish, {"README.md": "none\n"})
    assert changed(client, without["id"]) is False
    rewrite(runtime, without, to())
    assert changed(client, without["id"]) is True


def test_a_changed_dockerfile_is_a_change(client, runtime, publish):
    found = made(client, publish, building())
    assert changed(client, found["id"]) is False
    rewrite(runtime, found, {".devcontainer/Dockerfile": "FROM debian:12\n"})
    assert changed(client, found["id"]) is True


def test_a_workspace_with_no_digest_stored_is_not_known_to_have_changed(
    client, runtime, publish, settings
):
    found = made(client, publish)
    assert changed(client, found["id"]) is False  # the positive
    set_row(settings, found["id"], devcontainer_digest=None)
    assert changed(client, found["id"]) is None


def test_a_link_in_place_of_the_file_is_not_known_and_nothing_is_read(
    client, runtime, publish, tmp_path
):
    found = made(client, publish)
    secret = tmp_path / "secret.json"
    secret.write_text('{"image": "the-secret"}')
    path = work_of(runtime, found) / FILE
    path.unlink()
    path.symlink_to(secret)
    before = len(runtime.execs)
    assert changed(client, found["id"]) is None
    assert not [a for _, a, _ in runtime.execs[before:] if a and a[0].endswith("cat")]


def test_a_dockerfile_that_is_a_link_is_not_known(client, runtime, publish, tmp_path):
    found = made(client, publish, building())
    elsewhere = tmp_path / "Dockerfile"
    elsewhere.write_text("FROM the-secret\n")
    path = work_of(runtime, found) / ".devcontainer" / "Dockerfile"
    path.unlink()
    path.symlink_to(elsewhere)
    assert changed(client, found["id"]) is None


def test_whether_it_changed_is_not_asked_of_a_workspace_that_is_not_ready_or_is_rebuilding(
    client, runtime, publish, settings
):
    found = made(client, publish)
    assert changed(client, found["id"]) is False  # the positive: a ready one is asked
    service = client.app.state.workspaces
    for columns in (
        {"rebuild_state": ws.REBUILD_PREPARING},
        {"rebuild_state": ws.REBUILD_SWITCHING},
        {"state": ws.FAILED},
    ):
        set_row(settings, found["id"], **columns)
        service.close_channels()
        before = len(runtime.execs)
        assert changed(client, found["id"]) is None, columns
        assert len(runtime.execs) == before and not service.open_channels(), columns
        set_row(settings, found["id"], state=ws.READY, rebuild_state=None)


def test_whether_it_changed_is_not_asked_of_a_workspace_without_its_container(
    client, runtime, publish
):
    found = made(client, publish)
    runtime.discard(name_of(found))
    assert changed(client, found["id"]) is None


def test_another_persons_workspace_is_not_found_and_not_rebuilt(client, runtime, publish):
    found = made(client, publish)
    r = client.get(f"/api/workspaces/{found['id']}/devcontainer", headers=as_user(BOB))
    assert (r.status_code, r.json()["code"]) == (404, "workspace_not_found")
    r = ask(client, found["id"], who=BOB)
    assert (r.status_code, r.json()["code"]) == (404, "workspace_not_found")
    assert entry(client, found["id"])["rebuild"]["state"] is None


# --- asking for a rebuild --------------------------------------------------------------------


def test_a_rebuild_must_be_confirmed(client, runtime, publish):
    found = made(client, publish)
    before = len(runtime.events)
    for body in ({"confirm": False}, {}):
        r = client.post(
            f"/api/workspaces/{found['id']}/rebuild", json=body, headers=changing(ALICE)
        )
        assert (r.status_code, r.json()["code"]) == (422, "rebuild_confirm_required"), body
    assert runtime.events[before:] == []


def test_an_empty_workspace_is_not_rebuilt(client, runtime):
    r = client.post("/api/workspaces", json={"name": "empty"}, headers=changing(ALICE))
    empty = settled(client, r.json()["workspace"]["id"])
    assert empty["state"] == "ready" and empty["rebuildable"] is False
    r = ask(client, empty["id"])
    assert (r.status_code, r.json()["code"]) == (409, "rebuild_empty")


def test_a_workspace_without_its_container_is_not_rebuilt(client, runtime, publish):
    found = made(client, publish)
    runtime.discard(name_of(found))
    assert entry(client, found["id"])["rebuildable"] is False
    r = ask(client, found["id"])
    assert (r.status_code, r.json()["code"]) == (409, "rebuild_no_container")


def test_a_second_rebuild_while_one_runs_is_refused(client, runtime, publish):
    found = made(client, publish)
    rewrite(runtime, found, to())
    runtime.pull_gate = threading.Event()
    runtime.pulling.clear()
    assert ask(client, found["id"]).status_code == 202
    assert runtime.pulling.wait(MADE_WITHIN)
    shown = entry(client, found["id"])
    assert shown["rebuild"]["state"] == ws.REBUILD_PREPARING and shown["rebuildable"] is False
    r = ask(client, found["id"])
    assert (r.status_code, r.json()["code"]) == (409, "workspace_rebuilding")
    runtime.pull_gate.set()
    assert settled(client, found["id"])["rebuild"]["state"] is None


def test_no_rebuild_is_asked_for_while_rcman_brings_the_workspaces_back(client, runtime, publish):
    found = made(client, publish)
    client.app.state.workspaces.restored.clear()
    try:
        assert entry(client, found["id"])["rebuildable"] is False
        r = ask(client, found["id"])
        assert (r.status_code, r.json()["code"]) == (409, "rcman_starting")
    finally:
        client.app.state.workspaces.restored.set()
    assert entry(client, found["id"])["rebuildable"] is True


def test_no_rebuild_is_asked_for_while_rcman_is_upgraded(client, runtime, publish):
    found = made(client, publish)
    client.app.state.workspaces.upgrading = lambda: True
    assert entry(client, found["id"])["rebuildable"] is False
    r = ask(client, found["id"])
    assert (r.status_code, r.json()["code"]) == (409, "rcman_upgrading")


def test_a_rebuild_is_recorded_under_the_lock_an_upgrades_admission_holds(
    client, runtime, publish, settings
):
    """The admission of an upgrade holds the images' lock from its checks to its record; a
    rebuild asked for meanwhile waits for it, then finds the upgrade under way."""
    found = made(client, publish)
    service = client.app.state.workspaces
    answers: list = []
    with service.recording_held(5) as held:
        assert held
        asking = threading.Thread(
            target=lambda: answers.append(
                _refusal(service.rebuild, alice(settings), found["id"], True)
            )
        )
        asking.start()
        asking.join(0.3)
        assert asking.is_alive()  # it waits for the lock
        service.upgrading = lambda: True
    asking.join(10)
    assert answers == ["rcman_upgrading"]
    assert row(settings, found["id"])["rebuild_state"] is None


def test_whether_a_container_is_there_is_asked_without_the_lock_workspaces_are_recorded_under(
    client, runtime, publish, settings
):
    """A request for a rebuild asks the runtime before it takes the images' lock, which every
    workspace being made and the admission of an upgrade wait for."""
    found = made(client, publish)
    service = client.app.state.workspaces
    real = runtime.exists
    held: list = []

    def exists(name):
        with service._lock:
            rebuilding = threading.current_thread() in service._making.values()
        if rebuilding:  # the rebuild's own thread: not the request's
            return real(name)
        free: list = []

        def look():
            free.append(service._images_lock.acquire(False))
            if free[0]:
                service._images_lock.release()

        looking = threading.Thread(target=look)
        looking.start()
        looking.join()
        held.append(not free[0])
        return real(name)

    runtime.exists = exists
    assert ask(client, found["id"]).status_code == 202
    runtime.exists = real
    settled(client, found["id"])
    assert held and not any(held)


def test_the_listing_asks_the_runtime_once_for_the_containers_of_every_workspace(
    client, runtime, publish, settings
):
    first, second = made(client, publish), made(client, publish)
    lost = made(client, publish)
    runtime.discard(name_of(lost))
    asked: list = []
    real_exists, real_labelled = runtime.exists, runtime.labelled
    runtime.exists = lambda name: asked.append(("exists", name)) or real_exists(name)
    runtime.labelled = lambda: asked.append(("labelled",)) or real_labelled()
    shown = {w["id"]: w["rebuildable"] for w in listed(client)}
    assert shown == {first["id"]: True, second["id"]: True, lost["id"]: False}
    assert asked == [("labelled",)]


def _refusal(call, *args):
    try:
        call(*args)
    except problems.Problem as refused:
        return refused.code
    return None


def test_a_rebuild_is_recorded_as_the_persons_with_the_workspaces_name(
    client, runtime, publish, settings
):
    found = made(client, publish)
    rebuilt(client, found["id"])
    con = db.connect(settings.database)
    try:
        last = con.execute("select * from audit order by id desc limit 1").fetchone()
    finally:
        con.close()
    assert (last["action"], last["target"], last["result"]) == (
        "workspace.rebuild",
        found["name"],
        "ok",
    )


# --- a rebuild that succeeds -----------------------------------------------------------------


def test_a_rebuild_makes_the_container_again_from_the_new_image_on_the_same_volume(
    client, runtime, publish, settings
):
    found = made(client, publish)
    keep_work(runtime, found)
    rewrite(runtime, found, to(postCreateCommand="echo new-made"))
    done = rebuilt(client, found["id"])
    assert done["state"] == "ready" and done["rebuild"] == {
        "state": None,
        "log": None,
        "error": None,
    }
    assert spec_of(runtime, found).image == OTHER and spec_of(runtime, found).image_id == NEW_ID
    assert work_kept(runtime, found)
    plan = plan_of(settings, found["id"])
    assert (plan.image, plan.image_id) == (OTHER, NEW_ID)
    assert changed(client, found["id"]) is False  # the new digest is stored
    assert runtime.exists(name_of(found))


def test_every_create_command_runs_again_and_then_post_start(client, runtime, publish):
    found = made(client, publish)
    rewrite(
        runtime,
        found,
        to(
            onCreateCommand="echo on-create",
            updateContentCommand="echo update",
            postCreateCommand="echo post-create",
            postStartCommand="echo post-start",
        ),
    )
    before = len(ran(runtime))
    rebuilt(client, found["id"])
    assert ran(runtime)[before:] == [
        "echo on-create",
        "echo update",
        "echo post-create",
        "echo post-start",
    ]


def test_the_rebuild_log_says_what_is_done_and_is_kept_out_of_the_making_log(
    client, runtime, publish, settings
):
    found = made(client, publish)
    rewrite(runtime, found, to(postCreateCommand="echo GATE"))
    runtime.gate = threading.Event()
    assert ask(client, found["id"]).status_code == 202
    assert runtime.hooking.wait(MADE_WITHIN)
    shown = entry(client, found["id"])
    assert f"Pulling {OTHER}" in shown["rebuild"]["log"]
    assert "Running postCreateCommand" in shown["rebuild"]["log"]
    assert row(settings, found["id"])["log"] is None and shown["log"] is None
    runtime.release_ok = True
    runtime.gate.set()
    settled(client, found["id"])


def test_the_new_plan_is_stored_only_once_the_new_container_is_good(
    client, runtime, publish, settings
):
    found = made(client, publish)
    rewrite(runtime, found, to(postCreateCommand="echo GATE"))
    runtime.gate = threading.Event()
    assert ask(client, found["id"]).status_code == 202
    assert runtime.hooking.wait(MADE_WITHIN)
    assert spec_of(runtime, found).image == OTHER  # the new container is there
    assert plan_of(settings, found["id"]).image == IMAGE  # and the old plan is still stored
    assert row(settings, found["id"])["rebuild_state"] == ws.REBUILD_SWITCHING
    runtime.release_ok = True
    runtime.gate.set()
    settled(client, found["id"])
    assert plan_of(settings, found["id"]).image == OTHER


def test_a_file_removed_makes_the_workspace_from_the_default_image_again(
    client, runtime, publish, settings
):
    runtime.default_id = DEFAULT_ID
    found = made(client, publish)
    runtime.default_id = NEWER_DEFAULT_ID
    rewrite(runtime, found, {FILE: None})
    rebuilt(client, found["id"])
    assert row(settings, found["id"])["environment"] is None
    assert row(settings, found["id"])["image_id"] == NEWER_DEFAULT_ID
    assert spec_of(runtime, found).image is None
    assert row(settings, found["id"])["devcontainer_digest"] == dc.NO_FILE_DIGEST


def test_a_workspace_of_the_default_image_is_rebuilt_from_the_current_default(
    client, runtime, publish, settings
):
    runtime.default_id = DEFAULT_ID
    found = made(client, publish, {"README.md": "none\n"})
    assert row(settings, found["id"])["image_id"] == DEFAULT_ID
    runtime.default_id = NEWER_DEFAULT_ID
    keep_work(runtime, found)
    rebuilt(client, found["id"])
    assert row(settings, found["id"])["image_id"] == NEWER_DEFAULT_ID
    assert spec_of(runtime, found).image_id == NEWER_DEFAULT_ID
    assert work_kept(runtime, found)


def test_a_rebuild_of_a_file_with_no_image_uses_the_current_default_image(
    client, runtime, publish, settings
):
    default = settings.workspace_image
    runtime.images[default] = rt.ImageFacts(passwd=PASSWD, group=GROUP, id=DEFAULT_ID)
    no_image = {FILE: json.dumps({"postCreateCommand": OLD_COMMAND})}
    found = made(client, publish, no_image)
    assert (plan_of(settings, found["id"]).image_id, spec_of(runtime, found).image_id) == (
        DEFAULT_ID,
        DEFAULT_ID,
    )
    # The default image's name moves on to a newer image, as an upgrade of rcman makes one.
    runtime.extra_ids.add(DEFAULT_ID)
    runtime.images[default] = rt.ImageFacts(passwd=PASSWD, group=GROUP, id=NEWER_DEFAULT_ID)
    keep_work(runtime, found)
    rewrite(runtime, found, {FILE: json.dumps({"postCreateCommand": "echo new-made"})})
    done = rebuilt(client, found["id"])
    assert done["state"] == "ready" and done["rebuild"]["error"] is None, done
    plan = plan_of(settings, found["id"])
    assert (plan.image, plan.image_id) == (default, NEWER_DEFAULT_ID)
    assert (spec_of(runtime, found).image, spec_of(runtime, found).image_id) == (
        default,
        NEWER_DEFAULT_ID,
    )
    assert "echo new-made" in ran(runtime)
    assert work_kept(runtime, found)


def test_a_file_added_makes_the_workspace_from_its_image(client, runtime, publish, settings):
    found = made(client, publish, {"README.md": "none\n"})
    rewrite(runtime, found, to(postCreateCommand="echo added"))
    rebuilt(client, found["id"])
    assert plan_of(settings, found["id"]).image == OTHER
    assert "echo added" in ran(runtime)


def test_the_old_image_is_kept_until_the_switch_and_collected_after(
    client, runtime, publish, settings
):
    found = made(client, publish)
    runtime.collectable |= {IMAGE, OTHER}
    rewrite(runtime, found, to())
    runtime.pull_gate = threading.Event()
    runtime.pulling.clear()
    assert ask(client, found["id"]).status_code == 202
    assert runtime.pulling.wait(MADE_WITHIN)
    client.app.state.workspaces._collect_images()
    assert IMAGE in runtime.images  # the container is still made from it
    runtime.pull_gate.set()
    settled(client, found["id"])
    assert IMAGE not in runtime.images and OTHER in runtime.images


def test_a_built_image_is_built_as_next_and_moved_to_latest_at_the_commit(
    client, runtime, publish, settings
):
    found = made(client, publish, building())
    assert runtime.built_images() == {found["id"]: rt.built_name(found["id"])}
    runtime.build_facts = dataclasses.replace(runtime.build_facts, id=REBUILT_ID)
    rewrite(runtime, found, {".devcontainer/Dockerfile": "FROM debian:12\n"})
    rebuilt(client, found["id"])
    assert [b.tag for b in runtime.builds] == [rt.BUILT_TAG, rt.NEXT_TAG]
    assert runtime.next_images() == {}
    assert runtime.built_images() == {found["id"]: rt.built_name(found["id"])}
    plan = plan_of(settings, found["id"])
    assert (plan.image, plan.image_id) == (rt.built_name(found["id"]), REBUILT_ID)


def test_a_next_image_is_not_collected(client, runtime, publish, settings):
    found = made(client, publish, building())
    upcoming = rt.built_name(found["id"], rt.NEXT_TAG)
    runtime.images[upcoming] = dataclasses.replace(runtime.build_facts, id=REBUILT_ID)
    runtime.next_built[found["id"]] = upcoming
    runtime.collectable.add(upcoming)
    client.app.state.workspaces._collect_images()
    assert upcoming in runtime.images


def test_a_rebuild_of_one_workspace_leaves_the_built_image_of_another_alone(
    client, runtime, publish, settings
):
    first = made(client, publish, building())
    client.post("/api/workspaces", json={"name": "x"}, headers=changing(ALICE))  # ids move on
    second = made(client, publish, building())
    rewrite(runtime, first, {".devcontainer/Dockerfile": "FROM debian:12\n"})
    rebuilt(client, first["id"])
    assert set(runtime.built_images()) >= {first["id"], second["id"]}
    assert spec_of(runtime, second).image == rt.built_name(second["id"])


# --- what is refused, and what fails ---------------------------------------------------------


def unchanged(runtime, settings, found: dict, events_before: int) -> None:
    """The workspace as it was: its container from the old image, running, on its volume, and
    its plan the old one."""
    assert runtime.exists(name_of(found)) and runtime.running(name_of(found))
    assert spec_of(runtime, found).image == IMAGE
    assert plan_of(settings, found["id"]).image == IMAGE
    assert work_kept(runtime, found)
    assert not [e for e in runtime.events[events_before:] if e[0] in ("discard", "remove")]


@pytest.mark.parametrize(
    ("files", "named"),
    [
        (to(capAdd=["SYS_ADMIN"]), "capAdd"),
        ({FILE: "{not json"}, "line 1"),
        (to(image="registry.example.com/team/gone:latest"), "gone"),
    ],
)
def test_a_refused_file_or_image_changes_nothing_and_says_to_rebuild(
    client, runtime, publish, settings, files, named
):
    found = made(client, publish)
    keep_work(runtime, found)
    digest = row(settings, found["id"])["devcontainer_digest"]
    rewrite(runtime, found, files)
    before = len(runtime.events)
    done = rebuilt(client, found["id"])
    assert done["state"] == "ready" and done["rebuild"]["state"] is None
    error = done["rebuild"]["error"]
    assert named in error and "make the workspace again" not in error
    unchanged(runtime, settings, found, before)
    assert row(settings, found["id"])["devcontainer_digest"] == digest  # nothing was rebuilt


def test_a_rebuild_with_the_instructions_missing_leaves_the_old_container_running_and_says_why(
    client, runtime, publish, settings
):
    """Found before the switch, so that nothing is discarded that cannot be made again."""
    found = made(client, publish)
    keep_work(runtime, found)
    rewrite(runtime, found, to())
    before = len(runtime.events)
    runtime.instructions_ready = lambda: rt.INSTRUCTIONS_MISSING
    done = rebuilt(client, found["id"])
    assert done["state"] == "ready" and done["rebuild"]["state"] is None
    assert done["rebuild"]["error"] == rt.INSTRUCTIONS_MISSING
    unchanged(runtime, settings, found, before)
    assert runtime.next_images() == {}
    assert ("pull", OTHER) in runtime.events[before:]  # the positive: it was prepared
    del runtime.instructions_ready  # the control: with them there, it is rebuilt
    assert rebuilt(client, found["id"])["rebuild"]["error"] is None
    assert spec_of(runtime, found).image == OTHER


def test_a_refused_file_says_what_to_do_in_the_words_of_a_rebuild(client, runtime, publish):
    found = made(client, publish)
    rewrite(runtime, found, to(capAdd=["SYS_ADMIN"]))
    error = rebuilt(client, found["id"])["rebuild"]["error"]
    assert problems.REBUILD_ADVICE in error


def test_a_build_that_fails_leaves_the_image_the_container_is_made_from(
    client, runtime, publish, settings
):
    found = made(client, publish, building())
    old = runtime.images[rt.built_name(found["id"])]
    rewrite(runtime, found, {".devcontainer/Dockerfile": "FROM debian:12\nRUN false\n"})
    runtime.build_error = rt.BuildFailed(tail="Error: building at STEP")
    done = rebuilt(client, found["id"])
    assert done["rebuild"]["error"]
    assert runtime.images[rt.built_name(found["id"])] is old
    assert runtime.next_images() == {}
    assert spec_of(runtime, found).image == rt.built_name(found["id"])


def test_a_dockerfile_too_big_is_refused_and_nothing_changes(client, runtime, publish, settings):
    found = made(client, publish, building())
    rewrite(runtime, found, {".devcontainer/Dockerfile": "#" * (dc.MAX_BYTES + 1)})
    before = len(runtime.builds)
    done = rebuilt(client, found["id"])
    assert "too big" in done["rebuild"]["error"]
    assert len(runtime.builds) == before  # nothing was built


def test_a_dockerfile_that_is_a_link_is_refused_when_the_workspace_is_made(
    client, runtime, publish, tmp_path
):
    secret = tmp_path / "secret"
    secret.write_text("FROM the-secret\n")
    publish({**building(), ".devcontainer/Dockerfile": Link(str(secret))})
    failed = cloned(client)
    assert failed["state"] == "failed"
    assert failed["error"] == problems.DEVCONTAINER_MESSAGES["dockerfile_symlink"]
    assert runtime.builds == []


def failing_create(runtime, image: str):
    """Make every container from ``image`` fail to run from now on."""
    real = runtime.create

    def create(spec):
        if spec.image == image:
            runtime.events.append(("create", spec.image, spec.user))
            raise rt.RuntimeFailed("OCI runtime error: cannot run the image")
        real(spec)

    runtime.create = create


def rolled_back(runtime, settings, found: dict, done: dict) -> None:
    """Put back: ready, its container made again from the old image on the same volume, its old
    create commands run again, and why in its rebuild error."""
    assert done["state"] == "ready" and done["rebuild"]["state"] is None
    head = problems.REBUILD_MESSAGES["rolled_back"].split("{cause}")[0]
    assert done["rebuild"]["error"].startswith(head)
    assert spec_of(runtime, found).image == IMAGE and runtime.running(name_of(found))
    assert plan_of(settings, found["id"]).image == IMAGE
    assert work_kept(runtime, found)
    assert ran(runtime)[-1] == OLD_COMMAND
    assert runtime.next_images() == {}


def test_an_image_that_cannot_be_run_is_rolled_back(client, runtime, publish, settings):
    found = made(client, publish)
    keep_work(runtime, found)
    rewrite(runtime, found, to())
    failing_create(runtime, OTHER)
    done = rebuilt(client, found["id"])
    rolled_back(runtime, settings, found, done)
    assert "could not run a container" in done["rebuild"]["error"]


def test_an_image_on_musl_is_rolled_back(client, runtime, publish, settings):
    found = made(client, publish)
    keep_work(runtime, found)
    rewrite(runtime, found, to())
    runtime.libc = b"musl\n"
    done = rebuilt(client, found["id"])
    runtime.libc = b"ok\n"
    rolled_back(runtime, settings, found, done)
    assert "musl" in done["rebuild"]["error"]


def test_an_image_without_a_usable_git_is_rolled_back(client, runtime, publish, settings):
    found = made(client, publish)
    keep_work(runtime, found)
    rewrite(runtime, found, to())
    real = runtime.exec

    def exec_(name, argv, *args, **more):
        if argv[-1:] == ["--version"] and "git" in argv and runtime.specs[name].image == OTHER:
            return rt.Result(127, b"", b"git: not found\n")
        return real(name, argv, *args, **more)

    runtime.exec = exec_
    done = rebuilt(client, found["id"])
    rolled_back(runtime, settings, found, done)
    assert "git" in done["rebuild"]["error"]


def test_a_new_create_command_that_fails_is_rolled_back_with_its_output(
    client, runtime, publish, settings
):
    found = made(client, publish)
    keep_work(runtime, found)
    rewrite(runtime, found, to(postCreateCommand="echo the-new-output; exit 3"))
    done = rebuilt(client, found["id"])
    rolled_back(runtime, settings, found, done)
    assert "exit code 3" in done["rebuild"]["error"]
    assert "the-new-output" in done["rebuild"]["error"]
    assert ran(runtime)[-2:] == ["echo the-new-output; exit 3", OLD_COMMAND]


def left_to_be_put_back(client, runtime, settings, found: dict, cause: str) -> None:
    """A workspace a rebuild could not put back: still in the switch, so that its server, git
    and terminal are refused and no rebuild is offered, without its container, its old plan
    stored, and the steps the person sees saying that it is put back as soon as rcman can."""
    head = problems.REBUILD_MESSAGES["rollback_failed"].split("{cause}")[0]
    shown = entry(client, found["id"])
    assert shown["state"] == "ready" and shown["rebuild"]["state"] == ws.REBUILD_SWITCHING
    assert shown["rebuild"]["error"].startswith(head) and cause in shown["rebuild"]["error"]
    assert head.splitlines()[0] in shown["rebuild"]["log"]
    assert shown["rebuildable"] is False
    assert shown["server_error"]["code"] == "workspace_rebuilding"
    for r in (
        client.get("/api/git/status", params={"workspace": found["id"]}, headers=as_user(ALICE)),
        client.post("/api/servers", json={"workspace": found["id"]}, headers=changing(ALICE)),
    ):
        assert (r.status_code, r.json()["code"]) == (409, "workspace_rebuilding")
    assert plan_of(settings, found["id"]).image == IMAGE  # the old plan is still stored


def put_back_at_the_start(runtime, settings, found: dict, cause: str) -> None:
    """rcman started again: the workspace is made again from its old plan, its create commands
    run again, and it says why the rebuild failed and that it was put back."""
    before = len(ran(runtime))
    restarted(settings, runtime)
    assert runtime.exists(name_of(found)) and spec_of(runtime, found).image == IMAGE
    assert work_kept(runtime, found)
    assert ran(runtime)[before:] == [OLD_COMMAND]
    now = row(settings, found["id"])
    assert (now["state"], now["rebuild_state"]) == (ws.READY, None)
    head = problems.REBUILD_MESSAGES["put_back_later"].split("{cause}")[0]
    assert now["rebuild_error"].startswith(head) and cause in now["rebuild_error"]


def test_a_rollback_that_fails_leaves_the_workspace_to_be_put_back_at_the_next_start(
    client, runtime, publish, settings
):
    found = made(client, publish)
    keep_work(runtime, found)
    rewrite(runtime, found, to(postCreateCommand="exit 3"))
    real = runtime.create
    failing_create(runtime, IMAGE)  # the old image no longer runs either
    rebuilt(client, found["id"])
    left_to_be_put_back(client, runtime, settings, found, "exit code 3")
    assert not runtime.exists(name_of(found))
    runtime.create = real
    put_back_at_the_start(runtime, settings, found, "exit code 3")


def tries_to_make(runtime, image: str) -> int:
    """How often a container was asked to be made from ``image`` so far."""
    return len([e for e in runtime.events if e[0] == "create" and e[1] == image])


def test_a_switch_left_to_put_back_is_put_back_by_the_service_without_a_restart(
    quick_periodic_checks, client, app, runtime, publish, settings, caplog
):
    """The service's periodic look tries again what a restart would do, until it is done, and
    starts the server again that was running. The journal says the first failure and the
    success, not every look."""
    caplog.set_level(logging.INFO, logger="rcman.workspaces")
    found = made(client, publish)
    keep_work(runtime, found)
    signed_in(app, found, trusted=True)
    r = client.post("/api/servers", json={"workspace": found["id"]}, headers=changing(ALICE))
    assert r.status_code == 200, r.text
    rewrite(runtime, found, to(postCreateCommand="exit 3"))
    real = runtime.create
    failing_create(runtime, IMAGE)  # the old image does not run either, for now
    rebuilt(client, found["id"])
    left_to_be_put_back(client, runtime, settings, found, "exit code 3")
    tried = tries_to_make(runtime, IMAGE)
    wait_for(lambda: tries_to_make(runtime, IMAGE) >= tried + 3)  # it tried again, and again
    assert row(settings, found["id"])["rebuild_state"] == ws.REBUILD_SWITCHING
    before = len(ran(runtime))
    runtime.create = real
    wait_for(lambda: row(settings, found["id"])["rebuild_state"] is None)
    assert runtime.exists(name_of(found)) and spec_of(runtime, found).image == IMAGE
    assert work_kept(runtime, found)
    assert ran(runtime)[before:][:1] == [OLD_COMMAND]
    head = problems.REBUILD_MESSAGES["put_back_later"].split("{cause}")[0]
    assert row(settings, found["id"])["rebuild_error"].startswith(head)
    server = wait_for(lambda: entry(client, found["id"])["server"])
    assert server["exit_code"] is None
    said = [r.getMessage() for r in caplog.records if r.name == "rcman.workspaces"]
    me = f"workspace {found['id']} "
    assert len([m for m in said if m.startswith(me + "was not put back yet: ")]) == 1
    assert said.count(me + "was put back after its rebuild did not finish") == 1


def test_closing_waits_for_a_put_back_under_way(
    quick_periodic_checks, client, runtime, publish, settings
):
    found = made(client, publish)
    rewrite(runtime, found, to(postCreateCommand="exit 3"))
    real = runtime.create
    failing_create(runtime, IMAGE)
    rebuilt(client, found["id"])
    service = client.app.state.workspaces
    runtime.create_gate, runtime.creating = threading.Event(), threading.Event()
    runtime.create = real  # held at the gate from now on, as the put-back makes the container
    assert runtime.creating.wait(MADE_WITHIN)
    with service._lock:
        putting = service._putting_back[found["id"]]
    closing = threading.Thread(target=service.close)
    closing.start()
    closing.join(0.5)
    assert closing.is_alive()  # it waits for the put-back
    runtime.create_gate.set()
    closing.join(MADE_WITHIN)
    assert not closing.is_alive() and not putting.is_alive()


def test_a_put_back_is_not_begun_once_rcman_is_closing(client, runtime, publish, settings):
    found = made(client, publish)
    runtime.discard(name_of(found))
    set_row(settings, found["id"], rebuild_state=ws.REBUILD_SWITCHING)
    service = client.app.state.workspaces
    service._closing.set()
    before = tries_to_make(runtime, IMAGE)
    service._restore_task(service.get(found["id"]), only_switching=True)
    assert tries_to_make(runtime, IMAGE) == before
    assert row(settings, found["id"])["rebuild_state"] == ws.REBUILD_SWITCHING


def test_a_switch_left_to_put_back_does_not_hold_up_an_upgrade(client, runtime, publish, settings):
    """A rebuild under way does; one whose workspace could not be put back does not, as the
    upgrade's restart puts it back as the service would."""
    found, other = made(client, publish), made(client, publish)
    service = client.app.state.workspaces
    rewrite(runtime, found, to(postCreateCommand="exit 3"))
    real = runtime.create
    failing_create(runtime, IMAGE)
    rebuilt(client, found["id"])
    left_to_be_put_back(client, runtime, settings, found, "exit code 3")
    assert not service.any_rebuilding()
    runtime.create = real
    held_at_the_new_command(client, runtime, other)
    assert service.any_rebuilding()  # the positive: a switch under way
    let_go(client, runtime, other)


def test_a_put_back_that_fails_at_a_restart_stays_in_the_switch(client, runtime, publish, settings):
    found = made(client, publish)
    keep_work(runtime, found)
    runtime.discard(name_of(found))
    set_row(settings, found["id"], rebuild_state=ws.REBUILD_SWITCHING)
    real = runtime.create
    failing_create(runtime, IMAGE)
    restarted(settings, runtime)
    now = row(settings, found["id"])
    assert (now["state"], now["rebuild_state"]) == (ws.READY, ws.REBUILD_SWITCHING)
    assert not runtime.exists(name_of(found))
    runtime.create = real
    restarted(settings, runtime)
    assert row(settings, found["id"])["rebuild_state"] is None
    assert spec_of(runtime, found).image == IMAGE and work_kept(runtime, found)


def test_a_put_back_whose_image_is_gone_fails_the_workspace_and_says_so(
    client, runtime, publish, settings
):
    found = made(client, publish)
    runtime.discard(name_of(found))
    del runtime.images[IMAGE]
    set_row(settings, found["id"], rebuild_state=ws.REBUILD_SWITCHING)
    restarted(settings, runtime)
    now = row(settings, found["id"])
    assert (now["state"], now["failure"], now["rebuild_state"]) == (
        ws.FAILED,
        "image_gone",
        None,
    )
    assert IMAGE in now["error"]


def test_a_runtime_that_fails_as_the_old_container_is_discarded_puts_the_workspace_back(
    client, app, runtime, publish, settings
):
    """Podman not answering once the server was stopped is a failure of the switch as any
    other: the old container is put back and its server started again."""
    found = made(client, publish)
    keep_work(runtime, found)
    signed_in(app, found, trusted=True)
    r = client.post("/api/servers", json={"workspace": found["id"]}, headers=changing(ALICE))
    assert r.status_code == 200, r.text
    rewrite(runtime, found, to())
    real = runtime.discard
    failed: list = []

    def discard(name):
        if not failed:
            failed.append(name)
            raise rt.RuntimeUnavailable("podman did not answer")
        real(name)

    runtime.discard = discard
    done = rebuilt(client, found["id"])
    assert failed == [name_of(found)]  # the plant took effect
    rolled_back(runtime, settings, found, done)
    server = wait_for(lambda: entry(client, found["id"])["server"])
    assert server["exit_code"] is None
    assert row(settings, found["id"])["wanted"] == 1


def test_an_unexpected_failure_after_the_switch_leaves_the_workspace_to_be_put_back(
    client, runtime, publish, settings
):
    """What the container is then is not known: the switch is left for rcman's next start,
    which makes it again from the old plan."""
    found = made(client, publish)
    keep_work(runtime, found)
    rewrite(runtime, found, to())
    service = client.app.state.workspaces

    def commit(*args, **more):
        raise RuntimeError("something unforeseen")

    service._commit = commit
    rebuilt(client, found["id"])
    cause = problems.MESSAGES["workspace_failed"]
    left_to_be_put_back(client, runtime, settings, found, cause)
    put_back_at_the_start(runtime, settings, found, cause)


def test_an_unexpected_failure_keeps_what_a_failed_workspace_says_and_its_kind(
    client, runtime, publish, settings
):
    failed = failed_by_its_command(client, publish)
    before = row(settings, failed["id"])
    rewrite(runtime, failed, to())
    service = client.app.state.workspaces

    def commit(*args, **more):
        raise RuntimeError("something unforeseen")

    service._commit = commit
    done = rebuilt(client, failed["id"])
    now = row(settings, failed["id"])
    assert (now["state"], now["error"], now["failure"]) == (
        ws.FAILED,
        before["error"],
        "hook_failed",
    )
    assert now["rebuild_state"] is None and done["rebuildable"] is True
    assert problems.MESSAGES["workspace_failed"] in done["rebuild"]["log"]


def test_a_rebuild_that_fails_and_is_rolled_back_can_be_asked_for_again(client, runtime, publish):
    found = made(client, publish)
    rewrite(runtime, found, to(postCreateCommand="exit 3"))
    rebuilt(client, found["id"])
    assert entry(client, found["id"])["rebuildable"] is True
    rewrite(runtime, found, to())
    done = rebuilt(client, found["id"])
    assert done["rebuild"]["error"] is None and spec_of(runtime, found).image == OTHER


# --- what is refused meanwhile ---------------------------------------------------------------


def held_at_the_new_command(client, runtime, found: dict) -> None:
    rewrite(runtime, found, to(postCreateCommand="echo GATE"))
    runtime.gate = threading.Event()
    assert ask(client, found["id"]).status_code == 202
    assert runtime.hooking.wait(MADE_WITHIN)


def let_go(client, runtime, found: dict) -> dict:
    runtime.release_ok = True
    runtime.gate.set()
    return settled(client, found["id"])


def test_git_changes_and_servers_are_refused_while_the_container_is_replaced(
    client, runtime, publish
):
    found = made(client, publish)
    assert (
        client.get(
            "/api/git/status", params={"workspace": found["id"]}, headers=as_user(ALICE)
        ).status_code
        == 200
    )  # the positive
    held_at_the_new_command(client, runtime, found)
    for r in (
        client.get("/api/git/status", params={"workspace": found["id"]}, headers=as_user(ALICE)),
        client.post("/api/servers", json={"workspace": found["id"]}, headers=changing(ALICE)),
        client.get(
            "/api/servers/output", params={"workspace": found["id"]}, headers=as_user(ALICE)
        ),
    ):
        assert (r.status_code, r.json()["code"]) == (409, "workspace_rebuilding")
    shown = entry(client, found["id"])
    assert shown["server"] is None and shown["server_error"]["code"] == "workspace_rebuilding"
    let_go(client, runtime, found)


def test_the_terminal_is_ended_and_the_channel_closed_at_the_switch(client, runtime, publish):
    found = made(client, publish)
    service = client.app.state.workspaces
    ended: list = []
    service.on_rebuilding = lambda workspace: ended.append(
        (workspace.id, row_state(service, workspace.id))
    )
    service.run_in(service.get(found["id"]))  # opens its channel
    assert name_of(found) in service.open_channels()
    held_at_the_new_command(client, runtime, found)
    assert ended == [(found["id"], ws.REBUILD_SWITCHING)]
    let_go(client, runtime, found)
    assert name_of(found) not in service.open_channels()


def row_state(service, workspace_id: int) -> str | None:
    return service.get(workspace_id).rebuild_state


def test_a_server_started_meanwhile_is_stopped_at_the_switch_and_started_again(
    client, app, runtime, publish, settings
):
    found = made(client, publish)
    signed_in(app, found, trusted=True)
    rewrite(runtime, found, to())
    runtime.pull_gate = threading.Event()
    runtime.pulling.clear()
    assert ask(client, found["id"]).status_code == 202
    assert runtime.pulling.wait(MADE_WITHIN)
    r = client.post("/api/servers", json={"workspace": found["id"]}, headers=changing(ALICE))
    assert r.status_code == 200, r.text  # allowed while the image is prepared
    started: list = []
    real = app.state.workspaces.start_server
    app.state.workspaces.start_server = lambda w, prefix, resume=False: (
        started.append((w.id, prefix, resume)) or real(w, prefix, resume)
    )
    stops: list = []
    app.state.workspaces.stop_server = lambda w: stops.append(w.id)  # never called
    runtime.pull_gate.set()
    settled(client, found["id"])
    assert stops == []
    assert started == [(found["id"], found["name"], False)]
    assert row(settings, found["id"])["wanted"] == 1
    server = wait_for(lambda: entry(client, found["id"])["server"])
    assert server["exit_code"] is None


def test_deleting_between_the_commit_and_the_start_of_the_server_leaves_nothing(
    client, app, runtime, publish, settings
):
    """The rebuild has stored its plan and gives its server back: a deletion then is finished
    by the rebuild's thread, and no server is left in a workspace that is gone."""
    found = made(client, publish)
    signed_in(app, found, trusted=True)
    r = client.post("/api/servers", json={"workspace": found["id"]}, headers=changing(ALICE))
    assert r.status_code == 200, r.text
    rewrite(runtime, found, to())
    service = app.state.workspaces
    real = service.start_server
    deleted: list = []

    def start_server(workspace, prefix, resume=False):
        assert plan_of(settings, found["id"]).image == OTHER  # committed
        r = client.delete(f"/api/workspaces/{found['id']}", headers=changing(ALICE))
        deleted.append(r.status_code)
        try:
            return real(workspace, prefix, resume)
        except problems.Problem as refused:
            deleted.append(refused.code)
            raise

    service.start_server = start_server
    assert ask(client, found["id"]).status_code == 202
    settled_gone(client, found)
    assert deleted == [200, "workspace_not_ready"]  # no server is started in it
    no_trace(runtime, found)
    assert row(settings, found["id"]) is None


SERVER_REQUESTS = {
    "start": ("", "start_server"),
    "resume": ("/resume", "start_server"),
    "stop": ("/stop", "stop_server"),
    "forget": ("/forget", "forget_server"),
    "confirm": ("/confirm", "confirm"),
}


@pytest.mark.parametrize("verb", list(SERVER_REQUESTS))
def test_a_server_request_let_in_before_the_switch_is_refused_once_it_has_begun(
    client, app, runtime, publish, settings, verb
):
    """The request found the workspace ready while its image was prepared, and reaches the
    workspace's lock only once the switch has begun: it is refused then, and nothing is started
    or stopped in a container that is being replaced."""
    found = made(client, publish)
    signed_in(app, found, trusted=True)
    service = app.state.workspaces
    path, method = SERVER_REQUESTS[verb]
    rewrite(runtime, found, to(postCreateCommand="echo GATE"))
    runtime.gate = threading.Event()
    runtime.pull_gate = threading.Event()
    runtime.pulling.clear()
    switched, entered = threading.Event(), threading.Event()
    service.on_rebuilding = lambda workspace: switched.set()
    real = getattr(service, method)

    def held(*args, **more):
        entered.set()
        assert switched.wait(MADE_WITHIN)
        return real(*args, **more)

    setattr(service, method, held)
    assert ask(client, found["id"]).status_code == 202
    assert runtime.pulling.wait(MADE_WITHIN)
    body = {"workspace": found["id"], **({"answer": "y"} if verb == "confirm" else {})}
    answers: list = []
    request = threading.Thread(
        target=lambda: answers.append(
            client.post(f"/api/servers{path}", json=body, headers=changing(ALICE))
        )
    )
    request.start()
    assert entered.wait(MADE_WITHIN)  # it was let in while the image was prepared
    runtime.pull_gate.set()
    request.join(MADE_WITHIN)
    assert (answers[0].status_code, answers[0].json()["code"]) == (409, "workspace_rebuilding")
    now = row(settings, found["id"])
    assert (now["wanted"], now["server"], now["claude_version"]) == (0, 0, None)
    let_go(client, runtime, found)
    assert entry(client, found["id"])["server"] is None  # none was started in the new one


def test_a_server_that_was_not_running_is_not_started(client, app, runtime, publish, settings):
    found = made(client, publish)
    started: list = []
    app.state.workspaces.start_server = lambda *a, **k: started.append(a)
    rewrite(runtime, found, to())
    rebuilt(client, found["id"])
    assert started == []


# --- a restart of rcman in each window -------------------------------------------------------


def restarted(settings, runtime) -> None:
    again = ws.Workspaces(settings, runtime)
    try:
        again.restore()
    finally:
        again.close()


def test_a_restart_while_preparing_leaves_the_old_container_and_says_so(
    client, runtime, publish, settings
):
    found = made(client, publish)
    upcoming = rt.built_name(found["id"], rt.NEXT_TAG)
    runtime.images[upcoming] = runtime.build_facts
    runtime.next_built[found["id"]] = upcoming
    set_row(settings, found["id"], rebuild_state=ws.REBUILD_PREPARING)
    before = len(runtime.events)
    restarted(settings, runtime)
    now = row(settings, found["id"])
    assert now["rebuild_state"] is None
    assert now["rebuild_error"] == problems.REBUILD_MESSAGES["restarted"]
    assert not [e for e in runtime.events[before:] if e[0] in ("discard", "remove", "create")]
    assert runtime.next_images() == {}


def test_a_restart_after_the_old_container_was_discarded_makes_it_again_as_it_was(
    client, runtime, publish, settings
):
    found = made(client, publish)
    keep_work(runtime, found)
    runtime.discard(name_of(found))
    set_row(settings, found["id"], rebuild_state=ws.REBUILD_SWITCHING)
    before = len(ran(runtime))
    restarted(settings, runtime)
    now = row(settings, found["id"])
    assert now["rebuild_state"] is None and now["state"] == ws.READY
    assert now["rebuild_error"] == problems.REBUILD_MESSAGES["restarted_switching"]
    assert spec_of(runtime, found).image == IMAGE and work_kept(runtime, found)
    assert ran(runtime)[before:] == [OLD_COMMAND]  # its create commands, run again


def test_a_restart_after_the_new_container_was_made_puts_the_old_one_back(
    client, runtime, publish, settings
):
    found = made(client, publish)
    keep_work(runtime, found)
    runtime.discard(name_of(found))
    service = ws.Workspaces(settings, runtime)
    plan = dataclasses.replace(plan_of(settings, found["id"]), image=OTHER, image_id=NEW_ID)
    runtime.create(service._spec(service.get(found["id"]), 1, plan))
    assert spec_of(runtime, found).image_id == NEW_ID  # the plant took effect
    set_row(settings, found["id"], rebuild_state=ws.REBUILD_SWITCHING)
    restarted(settings, runtime)
    assert spec_of(runtime, found).image_id == OLD_ID
    assert spec_of(runtime, found).image == IMAGE and work_kept(runtime, found)
    assert row(settings, found["id"])["rebuild_state"] is None


def test_a_restart_during_the_switch_makes_even_a_container_of_the_old_image_again(
    client, runtime, publish, settings
):
    """A container of the image the stored plan names may be the new one all the same: a
    rebuild that keeps the image and changes the user, the environment or the commands. So it
    is made again from the stored plan, with its create commands run again."""
    found = made(client, publish)
    keep_work(runtime, found)
    old = spec_of(runtime, found)
    runtime.discard(name_of(found))
    service = ws.Workspaces(settings, runtime)
    plan = dataclasses.replace(
        plan_of(settings, found["id"]), user_name="dev", run_as="dev", uid=1234, gid=1235
    )
    runtime.create(service._spec(service.get(found["id"]), 1, plan))
    planted = spec_of(runtime, found)
    assert planted.image_id == OLD_ID  # the plant took effect: the same image
    assert (planted.user, planted.uid) != (old.user, old.uid)  # and another user
    set_row(settings, found["id"], rebuild_state=ws.REBUILD_SWITCHING)
    before, commands = len(runtime.events), len(ran(runtime))
    restarted(settings, runtime)
    assert ("discard", name_of(found)) in runtime.events[before:]
    now = spec_of(runtime, found)
    assert (now.image, now.user, now.uid) == (old.image, old.user, old.uid)
    assert ran(runtime)[commands:] == [OLD_COMMAND]
    assert work_kept(runtime, found)
    done = row(settings, found["id"])
    assert done["rebuild_state"] is None
    assert done["rebuild_error"] == problems.REBUILD_MESSAGES["restarted_switching"]


def test_a_restart_between_the_commit_and_the_new_name_finishes_the_promotion(
    client, runtime, publish, settings
):
    found = made(client, publish, building())
    upcoming = rt.built_name(found["id"], rt.NEXT_TAG)
    runtime.images[upcoming] = dataclasses.replace(runtime.build_facts, id=REBUILT_ID)
    runtime.next_built[found["id"]] = upcoming
    plan = dataclasses.replace(plan_of(settings, found["id"]), image_id=REBUILT_ID)
    set_row(settings, found["id"], environment=plan.to_json(), rebuild_state=ws.REBUILD_PROMOTING)
    restarted(settings, runtime)
    assert runtime.next_images() == {}
    assert runtime.images[rt.built_name(found["id"])].id == REBUILT_ID
    assert row(settings, found["id"])["rebuild_state"] is None


def test_a_next_image_no_rebuild_uses_is_removed_at_a_restart(client, runtime, publish, settings):
    found = made(client, publish, building())
    upcoming = rt.built_name(found["id"], rt.NEXT_TAG)
    runtime.images[upcoming] = runtime.build_facts
    runtime.next_built[found["id"]] = upcoming
    restarted(settings, runtime)
    assert runtime.next_images() == {}
    assert found["id"] in runtime.built_images()  # the one it is made from stays


# --- deleting in each phase ------------------------------------------------------------------


def test_deleting_while_the_image_is_prepared_leaves_nothing(client, runtime, publish, settings):
    found = made(client, publish)
    rewrite(runtime, found, to())
    runtime.pull_gate = threading.Event()
    runtime.pulling.clear()
    assert ask(client, found["id"]).status_code == 202
    assert runtime.pulling.wait(MADE_WITHIN)
    assert (
        client.delete(f"/api/workspaces/{found['id']}", headers=changing(ALICE)).status_code == 200
    )
    settled_gone(client, found)
    no_trace(runtime, found)
    assert row(settings, found["id"]) is None


def settled_gone(client, found: dict) -> None:
    service = client.app.state.workspaces
    with service._lock:
        thread = service._making.get(found["id"])
    if thread is not None:
        thread.join(MADE_WITHIN)
    wait_for(lambda: service.get(found["id"]) is None)


def test_deleting_while_the_new_container_runs_its_commands_leaves_nothing(
    client, runtime, publish, settings
):
    found = made(client, publish)
    held_at_the_new_command(client, runtime, found)
    assert (
        client.delete(f"/api/workspaces/{found['id']}", headers=changing(ALICE)).status_code == 200
    )
    runtime.gate.set()
    settled_gone(client, found)
    no_trace(runtime, found)


def test_deleting_while_a_build_runs_removes_the_builder_and_leaves_nothing(
    client, runtime, publish, settings
):
    found = made(client, publish, building())
    rewrite(runtime, found, {".devcontainer/Dockerfile": "FROM debian:12\n"})
    runtime.build_gate = threading.Event()
    runtime.building.clear()
    assert ask(client, found["id"]).status_code == 202
    assert runtime.building.wait(MADE_WITHIN)
    client.delete(f"/api/workspaces/{found['id']}", headers=changing(ALICE))
    settled_gone(client, found)
    no_trace(runtime, found)
    assert runtime.builders() == set() and runtime.next_images() == {}
    assert found["id"] not in runtime.built_images()


def held_at_the_promotion(client, runtime, found: dict) -> threading.Event:
    """A rebuild of a workspace that builds its image, held as the built image's name is moved,
    once the new plan is stored; the event this gives lets it go."""
    rewrite(runtime, found, {".devcontainer/Dockerfile": "FROM debian:12\n"})
    gate, promoting = threading.Event(), threading.Event()
    real = runtime.promote_build

    def promote_build(workspace_id):
        promoting.set()
        assert gate.wait(MADE_WITHIN)
        return real(workspace_id)

    runtime.promote_build = promote_build
    assert ask(client, found["id"]).status_code == 202
    assert promoting.wait(MADE_WITHIN)
    return gate


@pytest.mark.parametrize("phase", ["preparing", "switching", "promoting"])
def test_a_deletion_by_rcman_admin_stops_the_rebuild_and_leaves_nothing(
    client, runtime, publish, settings, phase
):
    found = made(client, publish, building() if phase == "promoting" else None)
    gate = None
    if phase == "preparing":
        rewrite(runtime, found, to())
        runtime.pull_gate = threading.Event()
        runtime.pulling.clear()
        assert ask(client, found["id"]).status_code == 202
        assert runtime.pulling.wait(MADE_WITHIN)
    elif phase == "switching":
        held_at_the_new_command(client, runtime, found)
    else:
        gate = held_at_the_promotion(client, runtime, found)
        assert row(settings, found["id"])["rebuild_state"] == ws.REBUILD_PROMOTING
    admin = ws.Workspaces(settings, runtime, prunes=False)  # rcman-admin's, a process of its own
    try:
        admin.delete(admin.get(found["id"]))
        assert admin.get(found["id"]).state == ws.DELETING  # left for the service to finish
        if gate is not None:
            gate.set()
        settled_gone(client, found)
    finally:
        admin.close()
    no_trace(runtime, found)


def test_deleting_as_the_rebuild_ends_leaves_nothing(client, runtime, publish, settings):
    """A deletion while the rebuild's thread, its phase cleared, still removes the images
    nothing refers to: the thread finishes the deletion all the same."""
    found = made(client, publish)
    rewrite(runtime, found, to())
    service = client.app.state.workspaces
    real = service._collect_images_quietly
    deleted: list = []

    def collect():
        with service._lock:
            rebuilding = service._making.get(found["id"]) is threading.current_thread()
        if rebuilding and not deleted:
            deleted.append(None)
            assert row(settings, found["id"])["rebuild_state"] is None  # the rebuild ended
            r = client.delete(f"/api/workspaces/{found['id']}", headers=changing(ALICE))
            deleted[0] = r.status_code
        real()

    service._collect_images_quietly = collect
    assert ask(client, found["id"]).status_code == 202
    settled_gone(client, found)
    assert deleted == [200]  # the plant took effect
    no_trace(runtime, found)


@pytest.fixture
def quick_periodic_checks(monkeypatch):
    """rcman's periodic checks every fraction of a second. The service reads the interval
    when it starts, so a test takes this before ``client``, which starts it."""
    monkeypatch.setattr(ws, "IDLE_CHECK_SECONDS", 0.2)


def promotion_left(client, runtime, publish, settings) -> dict:
    """A workspace whose rebuild stored its new plan and could not move the built image's name,
    so that its last phase is left, with no thread working on it."""
    found = made(client, publish, building())
    rewrite(runtime, found, {".devcontainer/Dockerfile": "FROM debian:12\n"})
    runtime.build_facts = dataclasses.replace(runtime.build_facts, id=REBUILT_ID)
    real = runtime.promote_build

    def promote_build(workspace_id):
        raise rt.RuntimeFailed("the image could not be named")

    runtime.promote_build = promote_build
    rebuilt(client, found["id"])
    assert row(settings, found["id"])["rebuild_state"] == ws.REBUILD_PROMOTING
    runtime.promote_build = real
    return found


def test_a_promotion_left_over_is_finished_by_the_service_without_a_restart(
    quick_periodic_checks, client, runtime, publish, settings
):
    found = promotion_left(client, runtime, publish, settings)
    wait_for(lambda: row(settings, found["id"])["rebuild_state"] is None)
    assert runtime.next_images() == {}
    assert runtime.images[rt.built_name(found["id"])].id == REBUILT_ID
    assert entry(client, found["id"])["rebuildable"] is True


def test_a_workspace_with_a_promotion_left_over_is_deleted_by_the_service(
    client, runtime, publish, settings
):
    found = promotion_left(client, runtime, publish, settings)
    r = client.delete(f"/api/workspaces/{found['id']}", headers=changing(ALICE))
    assert r.status_code == 200
    assert row(settings, found["id"]) is None
    no_trace(runtime, found)


def test_a_workspace_with_a_promotion_left_over_is_deleted_through_rcman_admin(
    quick_periodic_checks, client, runtime, publish, settings
):
    """No thread of the service works on it: the service finishes the deletion rcman-admin
    began, without a restart, and the person's deletion is done."""
    found = promotion_left(client, runtime, publish, settings)

    def promote_build(workspace_id):  # it stays left over: only the deletion finishes it
        raise rt.RuntimeFailed("the image could not be named")

    runtime.promote_build = promote_build
    admin = ws.Workspaces(settings, runtime, prunes=False)
    try:
        admin.delete_person(alice(settings))
    finally:
        admin.close()
    assert alice(settings) is None
    assert row(settings, found["id"]) is None
    no_trace(runtime, found)


def test_deleting_a_person_through_rcman_admin_during_a_build_leaves_nothing_of_theirs(
    client, runtime, publish, settings
):
    found = made(client, publish, building())
    rewrite(runtime, found, {".devcontainer/Dockerfile": "FROM debian:12\n"})
    runtime.build_gate = threading.Event()
    runtime.building.clear()
    assert ask(client, found["id"]).status_code == 202
    assert runtime.building.wait(MADE_WITHIN)
    admin = ws.Workspaces(settings, runtime, prunes=False)
    try:
        admin.delete_person(alice(settings))
    finally:
        admin.close()
    assert alice(settings) is None
    no_trace(runtime, found)
    person = str(found["id"])
    assert not [
        labels for labels in runtime.labelled().values() if labels.get(rt.LABEL_WORKSPACE) == person
    ]


# --- a failed workspace rebuilt --------------------------------------------------------------


def failed_by_its_command(client, publish, command: str = "exit 3") -> dict:
    publish(config(postCreateCommand=command))
    found = cloned(client)
    assert found["state"] == "failed", found
    return found


def test_the_kind_of_a_failure_is_recorded(client, runtime, publish, settings):
    failed = failed_by_its_command(client, publish)
    assert row(settings, failed["id"])["failure"] == "hook_failed"
    publish(config(capAdd=["SYS_ADMIN"]))
    refused_file = cloned(client)
    assert refused_file["state"] == "failed"
    assert row(settings, refused_file["id"])["failure"] is None


def test_a_command_that_takes_too_long_is_recorded_as_such(client, runtime, publish, settings):
    failed = failed_by_its_command(client, publish, "exit 124")
    assert row(settings, failed["id"])["failure"] == "hook_timeout"


@pytest.mark.parametrize(
    ("losing", "kind"),
    [("pulled", "image_gone"), ("built", "built_image_gone"), ("default", "default_image_gone")],
)
def test_the_kind_of_an_image_that_is_gone_is_recorded(
    client, runtime, publish, settings, losing, kind
):
    if losing == "default":
        runtime.default_id = DEFAULT_ID
        found = made(client, publish, {"README.md": "none\n"})
        runtime.lost_ids.add(DEFAULT_ID)
    elif losing == "built":
        found = made(client, publish, building())
        runtime.remove_build(found["id"], image=True)
    else:
        found = made(client, publish)
        del runtime.images[IMAGE]
    runtime.discard(name_of(found))
    restarted(settings, runtime)
    now = row(settings, found["id"])
    assert (now["state"], now["failure"]) == (ws.FAILED, kind)


def test_a_workspace_failed_by_its_command_is_rebuilt_and_ready(client, runtime, publish, settings):
    failed = failed_by_its_command(client, publish)
    keep_work(runtime, failed)
    assert entry(client, failed["id"])["rebuildable"] is True
    rewrite(runtime, failed, to(postCreateCommand="echo now-it-works"))
    done = rebuilt(client, failed["id"])
    assert (done["state"], done["error"]) == ("ready", None)
    now = row(settings, failed["id"])
    assert now["failure"] is None and now["log"] is None and now["rebuild_state"] is None
    assert spec_of(runtime, failed).image == OTHER and work_kept(runtime, failed)
    assert "echo now-it-works" in ran(runtime)


def test_a_workspace_whose_image_is_gone_is_rebuilt_through_a_container_of_the_default_image(
    client, runtime, publish, settings
):
    runtime.default_id = DEFAULT_ID
    found = made(client, publish)
    keep_work(runtime, found)
    del runtime.images[IMAGE]
    runtime.discard(name_of(found))
    restarted(settings, runtime)
    assert row(settings, found["id"])["failure"] == "image_gone"
    rewrite(runtime, found, to())
    before = len(runtime.events)
    done = rebuilt(client, found["id"])
    assert done["state"] == "ready", done
    creates = [e for e in runtime.events[before:] if e[0] == "create"]
    assert creates[0][1] is None and creates[-1][1] == OTHER  # read through the default's
    assert work_kept(runtime, found)
    assert plan_of(settings, found["id"]).image_id == NEW_ID


def test_a_workspace_whose_built_image_is_gone_has_it_built_again(
    client, runtime, publish, settings
):
    runtime.default_id = DEFAULT_ID
    found = made(client, publish, building())
    runtime.remove_build(found["id"], image=True)
    runtime.discard(name_of(found))
    restarted(settings, runtime)
    assert row(settings, found["id"])["failure"] == "built_image_gone"
    runtime.build_facts = dataclasses.replace(runtime.build_facts, id=REBUILT_ID)
    done = rebuilt(client, found["id"])
    assert done["state"] == "ready", done
    assert runtime.built_images() == {found["id"]: rt.built_name(found["id"])}
    assert plan_of(settings, found["id"]).image_id == REBUILT_ID


def test_a_failed_workspace_with_no_default_image_to_read_it_through_says_so(
    client, runtime, publish, settings
):
    found = made(client, publish)
    del runtime.images[IMAGE]
    runtime.discard(name_of(found))
    restarted(settings, runtime)
    assert row(settings, found["id"])["failure"] == "image_gone"
    runtime.default_id = ""
    done = rebuilt(client, found["id"])
    assert done["state"] == "failed"
    assert done["error"] == problems.DEVCONTAINER_MESSAGES["default_image_gone"]
    assert runtime.has_volume(found["id"])


def test_a_rebuild_of_a_failed_workspace_that_fails_keeps_it_failed_and_its_volume(
    client, runtime, publish, settings
):
    failed = failed_by_its_command(client, publish)
    keep_work(runtime, failed)
    rewrite(runtime, failed, to(postCreateCommand="echo again; exit 4"))
    done = rebuilt(client, failed["id"])
    assert done["state"] == "failed" and "exit code 4" in done["error"]
    assert row(settings, failed["id"])["failure"] == "hook_failed"
    assert runtime.has_volume(failed["id"]) and work_kept(runtime, failed)
    assert runtime.exists(name_of(failed))  # kept for a look, as at its making
    assert entry(client, failed["id"])["rebuildable"] is True  # and it may be tried again


def test_a_rebuild_of_a_failed_workspace_with_the_instructions_missing_keeps_its_container(
    client, runtime, publish, settings
):
    """Found before the switch, as for a ready workspace: the container kept for a look is not
    discarded for one that could not be made."""
    failed = failed_by_its_command(client, publish)
    keep_work(runtime, failed)
    error, failure = failed["error"], row(settings, failed["id"])["failure"]
    rewrite(runtime, failed, to(postCreateCommand="echo now-it-works"))
    before = len(runtime.events)
    runtime.instructions_ready = lambda: rt.INSTRUCTIONS_MISSING
    done = rebuilt(client, failed["id"])
    assert done["state"] == "failed" and done["error"] == rt.INSTRUCTIONS_MISSING
    assert row(settings, failed["id"])["failure"] == failure  # the kind it failed of before
    assert ("pull", OTHER) in runtime.events[before:]  # the positive: it was prepared
    assert not [e for e in runtime.events[before:] if e[0] in ("discard", "remove")]
    assert runtime.exists(name_of(failed)) and work_kept(runtime, failed)
    assert entry(client, failed["id"])["rebuildable"] is True
    assert error  # it said why it failed before, and now why it was not rebuilt
    del runtime.instructions_ready  # the control: with them there, it is rebuilt
    assert rebuilt(client, failed["id"])["state"] == "ready"


def test_instructions_missing_when_the_new_container_is_made_are_not_blamed_on_the_image(
    client, runtime, publish, settings
):
    """The file is there at the check and gone at create: the old container is put back, and
    the error is the installation's, not image_unrunnable's."""
    found = made(client, publish)
    rewrite(runtime, found, to())
    making = runtime.create

    def create(spec):
        if spec.image == OTHER:
            raise rt.InstructionsMissing()
        making(spec)

    runtime.create = create
    done = rebuilt(client, found["id"])
    assert done["state"] == "ready"
    assert rt.INSTRUCTIONS_MISSING in done["rebuild"]["error"]
    unrunnable = problems.DEVCONTAINER_MESSAGES["image_unrunnable"].split("{")[0]
    assert unrunnable not in done["rebuild"]["error"]
    assert spec_of(runtime, found).image == IMAGE  # put back


def test_a_refused_file_in_a_failed_workspace_keeps_its_volume(client, runtime, publish, settings):
    failed = failed_by_its_command(client, publish)
    keep_work(runtime, failed)
    rewrite(runtime, failed, to(capAdd=["SYS_ADMIN"]))
    done = rebuilt(client, failed["id"])
    assert done["state"] == "failed" and "capAdd" in done["error"]
    assert runtime.has_volume(failed["id"]) and work_kept(runtime, failed)
    assert entry(client, failed["id"])["rebuildable"] is True


def test_a_restart_during_the_rebuild_of_a_failed_workspace_leaves_it_failed_and_rebuildable(
    client, runtime, publish, settings
):
    failed = failed_by_its_command(client, publish)
    for phase in (ws.REBUILD_PREPARING, ws.REBUILD_SWITCHING):
        set_row(settings, failed["id"], rebuild_state=phase)
        restarted(settings, runtime)
        now = row(settings, failed["id"])
        assert (now["state"], now["rebuild_state"], now["failure"]) == (
            ws.FAILED,
            None,
            "hook_failed",
        ), phase
        assert now["error"] == problems.REBUILD_MESSAGES["restarted"], phase
        assert runtime.has_volume(failed["id"])
    assert entry(client, failed["id"])["rebuildable"] is True


def test_deleting_a_failed_workspace_while_it_is_rebuilt_leaves_nothing(
    client, runtime, publish, settings
):
    failed = failed_by_its_command(client, publish)
    rewrite(runtime, failed, to())
    runtime.pull_gate = threading.Event()
    runtime.pulling.clear()
    assert ask(client, failed["id"]).status_code == 202
    assert runtime.pulling.wait(MADE_WITHIN)
    client.delete(f"/api/workspaces/{failed['id']}", headers=changing(ALICE))
    settled_gone(client, failed)
    no_trace(runtime, failed)


def test_only_a_failure_that_kept_the_volume_is_offered_a_rebuild(
    client, runtime, publish, settings
):
    failed = failed_by_its_command(client, publish)
    assert entry(client, failed["id"])["rebuildable"] is True  # the positive
    for failure in (None, "clone_failed", "refused"):
        set_row(settings, failed["id"], failure=failure)
        assert entry(client, failed["id"])["rebuildable"] is False, failure
        r = ask(client, failed["id"])
        assert (r.status_code, r.json()["code"]) == (409, "workspace_not_ready"), failure
    set_row(settings, failed["id"], failure="hook_failed")
    runtime.remove(name_of(failed))  # its volume with it
    assert entry(client, failed["id"])["rebuildable"] is False
    assert ask(client, failed["id"]).status_code == 409


def test_a_clone_that_failed_or_rcman_restarting_is_no_failure_to_rebuild(
    client, runtime, publish, settings
):
    r = client.post(
        "/api/git/clone", json={"url": "https://github.com/octo/nothing"}, headers=changing(ALICE)
    )
    failed = settled(client, r.json()["workspace"]["id"])
    assert failed["state"] == "failed" and row(settings, failed["id"])["failure"] is None
    assert failed["rebuildable"] is False


def test_the_listing_offers_a_rebuild_exactly_where_one_is_taken(
    client, runtime, publish, settings
):
    """Driven from the route: each workspace's ``rebuildable`` is whether the route takes it."""
    ready = made(client, publish)
    failed = failed_by_its_command(client, publish)
    publish(config(capAdd=["SYS_ADMIN"]))
    refused = cloned(client)
    r = client.post("/api/workspaces", json={"name": "empty"}, headers=changing(ALICE))
    empty = settled(client, r.json()["workspace"]["id"])
    lost = made(client, publish)
    runtime.discard(name_of(lost))
    shown = {w["id"]: w["rebuildable"] for w in listed(client)}
    assert set(shown.values()) == {True, False}  # both are there
    for workspace in (ready, failed, refused, empty, lost):
        r = ask(client, workspace["id"])
        assert (r.status_code == 202) is shown[workspace["id"]], (workspace["name"], r.text)
        settled(client, workspace["id"])
