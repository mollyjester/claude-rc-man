"""Rebuilding a workspace on its volume (backend/rcman/workspaces.py: ``Workspaces.rebuild``,
POST /api/workspaces/{id}/rebuild): telling that its devcontainer.json changed (GET
/api/workspaces/{id}/devcontainer), preparing the new image beside the running container,
replacing the container, putting the old one back when the new one fails, the refusal while
rcman is upgraded, a Start that makes a lost container again, what a restart of rcman finds in
the switch, and the rebuild of a failed workspace that kept its volume. On the host runtime of
tests/test_devcontainer_flow.py, whose images a test plants; the repository is cloned by real
git from the tests' stand-in for github.com, and a change to the file is written in the
workspace's work folder, as a person makes it there."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import shutil
import threading

import pytest
from rcman import db, problems
from rcman import devcontainer as dc
from rcman import runtime as rt
from rcman import workspaces as ws

from tests.conftest import as_user, changing, ended, host_runtime, signed_in
from tests.git_fixtures import fixture_git, make_repo
from tests.test_clone import ALICE, cloned, entry, row
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
    work_of,
)
from tests.until import until

OTHER = "registry.example.com/team/other:latest"
OLD_ID, NEW_ID = "a" * 64, "b" * 64
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
    ended(client.app.state.workspaces, workspace_id)
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


# --- telling that the file changed ----------------------------------------------------------


def test_the_digest_stored_at_the_making_tells_an_unchanged_file_from_a_changed_one(
    client, runtime, publish, settings
):
    with_file = made(client, publish)
    digest = row(settings, with_file["id"])["devcontainer_digest"]
    assert digest and digest != dc.NO_FILE_DIGEST
    text = (work_of(runtime, with_file) / FILE).read_text()
    assert digest == dc.digest(dc.ConfigFile(text, FILE))
    assert changed(client, with_file["id"]) is False
    rewrite(runtime, with_file, to())
    assert changed(client, with_file["id"]) is True


# --- asking for a rebuild --------------------------------------------------------------------


def test_no_rebuild_is_asked_for_while_rcman_is_upgraded(client, runtime, publish):
    found = made(client, publish)
    client.app.state.workspaces.upgrading = lambda: True
    assert entry(client, found["id"])["rebuildable"] is False
    r = ask(client, found["id"])
    assert (r.status_code, r.json()["code"]) == (409, "rcman_upgrading")


def test_whether_a_container_is_there_is_asked_without_the_lock_workspaces_are_recorded_under(
    client, runtime, publish, monkeypatch
):
    """A request for a rebuild asks the runtime before it takes the images' lock, which every
    workspace being made and the admission of an upgrade wait for."""
    held = images_lock_held_when_the_request_asks(client, runtime, publish, monkeypatch)
    assert held and not any(held)  # held: the control, that the request's call was seen


def test_a_rebuild_that_asks_whether_its_container_is_there_under_the_images_lock_is_caught(
    client, runtime, publish, monkeypatch
):
    """The planted twin of the test above: the request asks under the images' lock, and that
    is seen."""
    held = images_lock_held_when_the_request_asks(
        client, runtime, publish, monkeypatch, under_the_lock=True
    )
    assert held and all(held)


def images_lock_held_when_the_request_asks(
    client, runtime, publish, monkeypatch, *, under_the_lock=False
) -> list[bool]:
    """Whether the images' lock was held each time the request for a rebuild asked the runtime
    whether a container is there, the rebuild having ended. The request's thread is told by
    being inside ``rebuild``, and no lock is taken to tell it: the runtime is asked under the
    service's own locks too. ``under_the_lock`` has the request ask under the images' lock."""
    found = made(client, publish)
    service = client.app.state.workspaces
    real_rebuild, real_exists = service.rebuild, runtime.exists
    requests: set[int] = set()
    held: list[bool] = []

    def rebuild(*args):
        requests.add(threading.get_ident())
        try:
            with service._images_lock if under_the_lock else contextlib.nullcontext():
                return real_rebuild(*args)
        finally:
            requests.discard(threading.get_ident())

    def exists(name):
        if threading.get_ident() in requests:
            free: list = []

            def look():  # from another thread, since the images' lock is reentrant
                free.append(service._images_lock.acquire(False))
                if free[0]:
                    service._images_lock.release()

            looking = threading.Thread(target=look)
            looking.start()
            looking.join()
            held.append(not free[0])
        return real_exists(name)

    monkeypatch.setattr(service, "rebuild", rebuild)
    monkeypatch.setattr(runtime, "exists", exists)
    assert ask(client, found["id"]).status_code == 202
    settled(client, found["id"])
    return held


# --- a rebuild that succeeds -----------------------------------------------------------------


def test_a_rebuild_makes_the_container_again_from_the_new_image_on_the_same_volume(
    client, runtime, publish, settings
):
    """Once confirmed: the container made again from the new image on the same volume, every
    create command run again and then postStart, the new plan and digest stored, the server
    that was running started again, the old image collected, and the rebuild audited as the
    person's with the workspace's name."""
    found = made(client, publish)
    keep_work(runtime, found)
    signed_in(client.app, found, trusted=True)
    r = client.post("/api/servers", json={"workspace": found["id"]}, headers=changing(ALICE))
    assert r.status_code == 200, r.text
    runtime.collectable |= {IMAGE, OTHER}
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
    commands = len(ran(runtime))
    done = rebuilt(client, found["id"])
    assert done["state"] == "ready" and done["rebuild"] == {
        "state": None,
        "log": None,
        "error": None,
    }
    assert spec_of(runtime, found).image == OTHER and spec_of(runtime, found).image_id == NEW_ID
    assert work_kept(runtime, found)
    assert [c for c in ran(runtime)[commands:] if c.startswith("echo ")] == [
        "echo on-create",
        "echo update",
        "echo post-create",
        "echo post-start",
    ]
    plan = plan_of(settings, found["id"])
    assert (plan.image, plan.image_id) == (OTHER, NEW_ID)
    assert changed(client, found["id"]) is False  # the new digest is stored
    assert runtime.exists(name_of(found))
    assert IMAGE not in runtime.images and OTHER in runtime.images
    assert row(settings, found["id"])["wanted"] == 1
    server = until(lambda: entry(client, found["id"])["server"])
    assert server["exit_code"] is None
    con = db.connect(settings.database)
    try:
        last = con.execute(
            "select * from audit where action = 'workspace.rebuild' order by id desc limit 1"
        ).fetchone()
    finally:
        con.close()
    assert (last["action"], last["target"], last["result"]) == (
        "workspace.rebuild",
        found["name"],
        "ok",
    )


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


# --- what fails ------------------------------------------------------------------------------


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
    assert entry(client, found["id"])["rebuildable"] is True  # it may be asked for again


# --- starting a stopped workspace ----------------------------------------------------------


def power(client, workspace_id: int, verb: str):
    return client.post(f"/api/workspaces/{workspace_id}/{verb}", headers=changing(ALICE))


def powered(client, workspace_id: int) -> dict:
    """The workspace once no Stop or Start of it is under way."""
    return until(
        lambda: (
            (found := entry(client, workspace_id))
            and not (found["starting"] or found["stopping"])
            and found
        )
    )


def test_a_start_makes_a_lost_container_again_on_its_volume(client, runtime, publish, settings):
    """A stopped workspace whose container was lost is made again at its Start from the image it
    was made from, on its volume."""
    found = made(client, publish)
    name = name_of(found)
    keep_work(runtime, found)
    assert power(client, found["id"], "stop").status_code == 200
    runtime.discard(name)
    assert power(client, found["id"], "start").status_code == 202
    assert powered(client, found["id"])["state"] == "ready"
    assert runtime.running(name)
    assert (spec_of(runtime, found).image, spec_of(runtime, found).image_id) == (IMAGE, OLD_ID)
    assert work_kept(runtime, found)


# --- a restart of rcman in the switch --------------------------------------------------------


def restarted(settings, runtime) -> None:
    again = ws.Workspaces(settings, runtime)
    try:
        again.restore()
    finally:
        again.close()


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


# --- a failed workspace rebuilt --------------------------------------------------------------


def failed_by_its_command(client, publish) -> dict:
    publish(config(postCreateCommand="exit 3"))
    found = cloned(client)
    assert found["state"] == "failed", found
    return found


def test_a_workspace_failed_by_its_command_is_rebuilt_and_ready(client, runtime, publish, settings):
    failed = failed_by_its_command(client, publish)
    assert row(settings, failed["id"])["failure"] == "hook_failed"  # the kind is recorded
    keep_work(runtime, failed)
    assert entry(client, failed["id"])["rebuildable"] is True
    rewrite(runtime, failed, to(postCreateCommand="echo now-it-works"))
    done = rebuilt(client, failed["id"])
    assert (done["state"], done["error"]) == ("ready", None)
    now = row(settings, failed["id"])
    assert now["failure"] is None and now["log"] is None and now["rebuild_state"] is None
    assert spec_of(runtime, failed).image == OTHER and work_kept(runtime, failed)
    assert "echo now-it-works" in ran(runtime)
