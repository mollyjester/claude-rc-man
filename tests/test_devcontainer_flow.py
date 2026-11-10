"""A workspace made from a repository's devcontainer.json (backend/rcman/workspaces.py), on a host
runtime that has the images a test plants: reading and refusing the file, pulling the image,
making the container again from the plan on the same volume, the lifecycle commands, building an
image, and bringing it all back when rcman starts. The repository is cloned by real git from the
tests' stand-in for github.com."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest
from rcman import db, people, problems
from rcman import devcontainer as dc
from rcman import runtime as rt
from rcman import workspaces as ws

from tests.conftest import changing, host_runtime, made_workspace
from tests.git_fixtures import fixture_git, make_repo
from tests.hang_guard import HANG_GUARD
from tests.test_clone import (
    ALICE,
    TOKEN,
    URL,
    cleared,
    clone,
    cloned,
    entry,
    finished,
    insert,
    row,
    save_token,
)
from tests.until import until

IMAGE = "registry.example.com/team/dev:latest"
PASSWD = "root:x:0:0:root:/root:/bin/sh\ndev:x:1234:1235::/home/dev:/bin/sh\n"
GROUP = "root:x:0:\ndev:x:1235:\nextra:x:5000:dev\n"
GOOD = {"image": IMAGE}
FILE = ".devcontainer/devcontainer.json"


class Flow(rt.HostRuntime):
    """The host runtime with images, noting what is done to it in order, and able to hold a
    lifecycle command or a build until the test lets it go."""

    def __init__(self, *args, **more):
        super().__init__(*args, **more)
        self.events: list[tuple] = []
        self.execs: list[tuple[str, list[str], str | None]] = []
        self.gate: threading.Event | None = None  # a command with GATE in it waits for this
        self.hooking = threading.Event()
        self.pull_lines = ["Trying to pull the image", "Copying blob a"]
        self.after_pull = lambda: None  # called as a pull has succeeded
        # The planted builder (HostRuntime.build_script): it says its lines, may be held at
        # the gate until the test lets it go or the build is stopped, and makes an image with
        # the facts below.
        self.build_lines = ["[1/1] STEP 1/2: FROM scratch"]
        self.build_gate: threading.Event | None = None
        self.building = threading.Event()
        self.build_facts = rt.ImageFacts(
            passwd=PASSWD, group=GROUP, env={"PATH": "/built/bin:/bin"}
        )
        self.builds_at_once = self.most_builds_at_once = 0
        self.build_script = self.scripted
        self.images["registry.example.com/team/dev:latest"] = rt.ImageFacts(
            passwd=PASSWD, group=GROUP, env={"PATH": "/image/bin:/usr/bin:/bin"}
        )

    def scripted(self, request, progress, cancel):
        self.building.set()
        self.builds_at_once += 1
        self.most_builds_at_once = max(self.most_builds_at_once, self.builds_at_once)
        try:
            for line in self.build_lines:
                progress(line)
            if self.build_gate is not None:
                self.held(
                    self.build_gate,
                    lambda: (
                        (cancel is not None and cancel.is_set())
                        or request.id not in self.builders()
                    ),
                    "the build was stopped",
                )
            return self.build_facts
        finally:
            self.builds_at_once -= 1

    @staticmethod
    def held(gate: threading.Event, stopped, why: str) -> None:
        """Until the test opens ``gate``, or, once ``stopped()`` says the work was stopped, the
        runtime's failure ``why``. Past the hang bound it is held on, as work that never ends
        would be, so that what waits for it rather than stopping it fails the test."""

        def opened_or_stopped():
            if gate.is_set():
                return "opened"
            return "stopped" if stopped() else None

        try:
            seen = until(opened_or_stopped)
        except AssertionError:
            gate.wait(2 * HANG_GUARD)
            seen = "opened"
        if seen == "stopped":
            raise rt.RuntimeFailed(why)

    def remove_build(self, workspace, image=False, **more):
        self.events.append(("remove_build", workspace, image))
        super().remove_build(workspace, image, **more)

    def create(self, spec):
        self.events.append(("create", spec.image, spec.user))
        super().create(spec)

    def discard(self, name):
        self.events.append(("discard", name))
        super().discard(name)

    def remove(self, name):
        self.events.append(("remove", name))
        if self.gate is not None:
            self.gate.set()
        super().remove(name)

    def pull(self, image, progress=None, cancel=None):
        self.events.append(("pull", image))
        for line in self.pull_lines:
            if progress is not None:
                progress(line)
        super().pull(image, progress, cancel)
        self.after_pull()

    def exec(self, name, argv, stdin=None, timeout=HANG_GUARD, cwd=None, keep=None, again=False):
        self.execs.append((name, argv, cwd))
        if self.gate is not None and "GATE" in " ".join(argv):
            self.hooking.set()
            self.gate.wait(2 * HANG_GUARD)  # until the test opens it, or a removal does
            if not self.exists(name):
                return rt.Result(137, b"", b"Killed\n")
        return super().exec(name, argv, stdin, timeout, cwd, keep, again)

    def hooks_run(self) -> list[list[str]]:
        """The lifecycle commands run so far: the argv after the timeout tool."""
        found = []
        for _, argv, cwd in self.execs:
            if cwd is not None and "-k" in argv:  # git is not given a folder to run in
                found.append(argv[argv.index("-k") + 3 :])
        return found


@pytest.fixture
def runtime(tmp_path):
    with host_runtime(tmp_path, Flow) as made:
        yield made


@pytest.fixture
def service(settings, runtime):
    made = ws.Workspaces(settings, runtime)
    yield made
    made.close()


@pytest.fixture
def ready_log(settings, monkeypatch):
    """The log of each workspace as it was when the workspace became ready, which clears it."""
    seen: dict[int, str] = {}
    real = ws.Workspaces._set

    def set_(self, workspace_id, **columns):
        if columns.get("state") == ws.READY:
            seen[workspace_id] = row(settings, workspace_id)["log"]
        return real(self, workspace_id, **columns)

    monkeypatch.setattr(ws.Workspaces, "_set", set_)
    return seen


@pytest.fixture
def publish(github_stand_in, tmp_path):
    """``publish(files)``: the repository at URL, with ``files`` (name -> text or bytes, or a
    ``Link`` to a path) committed in it."""
    counter = iter(range(1000))

    def publish(files: dict) -> Path:
        repo = make_repo(
            tmp_path / f"src{next(counter)}", origin=github_stand_in / "octo" / "widget.git"
        )
        for name, content in files.items():
            path = repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, Link):
                path.symlink_to(content.target)
            else:
                path.write_bytes(content if isinstance(content, bytes) else content.encode())
        fixture_git(repo, "add", "-A")
        fixture_git(repo, "commit", "-q", "-m", "A devcontainer")
        fixture_git(repo, "push", "-q", "origin", "main")
        return repo

    return publish


class Link:
    def __init__(self, target):
        self.target = target


def config(**keys) -> dict:
    return {FILE: json.dumps({**GOOD, **keys})}


def building(**keys) -> dict:
    """A repository whose devcontainer.json builds its image from a Dockerfile."""
    return {
        FILE: json.dumps({"build": {"dockerfile": "Dockerfile"}, **keys}),
        ".devcontainer/Dockerfile": "FROM ubuntu:24.04\n",
    }


def featured(**keys) -> dict:
    """A repository whose devcontainer.json adds a feature of its own to an image."""
    return {
        FILE: json.dumps({**GOOD, "features": {"./feature": {"greeting": "hi"}}, **keys}),
        ".devcontainer/feature/devcontainer-feature.json": '{"id": "feature"}',
        ".devcontainer/feature/install.sh": "#!/bin/sh\n",
    }


def name_of(entry_: dict) -> str:
    return rt.container_name(entry_["id"])


def work_of(runtime, entry_: dict) -> Path:
    return Path(runtime.layout(name_of(entry_)).work)


def no_trace(runtime, entry_: dict) -> None:
    name = name_of(entry_)
    assert not runtime.exists(name) and name not in runtime.labelled()
    assert not (runtime.root / name).exists()  # nor its volume


# --- a file that is refused or invalid -------------------------------------------------------


def test_a_refused_file_fails_the_workspace_naming_the_key_and_what_to_change(
    client, runtime, publish, settings
):
    """The policy of docs/devcontainer.md: a key a file may not ask for fails the workspace before
    anything is pulled, in plain words that say what to change, and leaves nothing."""
    publish(config(capAdd=["SYS_ADMIN"]))
    failed = cloned(client)
    assert failed["state"] == "failed"
    assert (
        problems.DEVCONTAINER_MESSAGES["refused_key"].split("{reason}")[0].format(key="capAdd")
        in failed["error"]
    )
    assert dc.POLICY["capAdd"].reason in failed["error"]
    assert "make the workspace again" in failed["error"]
    assert failed["error"].count("\n") < 5  # plain words, not a trace
    no_trace(runtime, failed)
    assert row(settings, failed["id"])["environment"] is None  # no plan was kept
    assert runtime.pulled == []  # nothing was pulled for a file that was refused first


def test_the_log_names_the_keys_that_are_ignored_the_image_pulled_and_the_forwarded_ports(
    client, runtime, publish, ready_log
):
    runtime.images[IMAGE] = rt.ImageFacts(
        passwd=PASSWD, group=GROUP,
        labels={dc.METADATA_LABEL: json.dumps([{"id": "f", "forwardPorts": [700]}])},
    )  # fmt: skip
    label = {"680": {"label": "Pebble Emulator (noVNC)"}}
    publish(
        config(
            name="Planted",
            customizations={"vscode": {}},
            forwardPorts=[680, "localhost:300"],
            portsAttributes=label,
        )
    )
    done = cloned(client)
    assert done["state"] == "ready", done
    log = ready_log[done["id"]]
    assert dc.LOOKED_IN in log and f"Pulling {IMAGE}" in log
    [ignored] = [line for line in log.splitlines() if line.startswith("Ignored in")]
    assert "name" in ignored and "customizations" in ignored
    assert "forwardPorts" not in ignored  # honoured, so not called ignored
    assert "Planted" not in log and "vscode" not in log  # names only, never values
    # the image's port first, then the file's: the plan's, not the file's alone
    assert (
        "Forwarded ports: 700, 680 \u201cPebble Emulator (noVNC)\u201d, 300. They open from the "
        "workspace's Ports card once the workspace is ready, while an administrator has port "
        "forwarding on."
    ) in log.splitlines()
    assert not re.search(r"[0-9]", dc.FORWARDED_PORTS)  # no number but the ports themselves


def test_a_repository_with_no_file_is_made_as_before(client, runtime, publish, settings):
    publish({"README.md": "hello\n"})
    done = cloned(client)
    assert done["state"] == "ready"
    assert runtime.pulled == []
    assert [e for e in runtime.events if e[0] in ("pull", "discard")] == []
    assert row(settings, done["id"])["environment"] is None
    assert runtime.specs[name_of(done)].image is None  # the workspace image, not another


DEFAULT_ID = "d" * 64


def with_default_image(runtime, settings, **more) -> None:
    """The default image (settings.workspace_image) in the runtime, with an id of its own."""
    image = {"passwd": PASSWD, "group": GROUP, "env": {"PATH": "/default/bin:/bin"}}
    runtime.images[settings.workspace_image] = rt.ImageFacts(**{**image, "id": DEFAULT_ID, **more})


def test_a_file_with_no_image_makes_the_workspace_from_the_default_image(
    client, runtime, publish, settings, ready_log
):
    with_default_image(runtime, settings)
    publish({FILE: json.dumps({"name": "Pebble", "forwardPorts": [6080]})})
    done = cloned(client)
    assert done["state"] == "ready", done
    stored = dc.Plan.from_json(row(settings, done["id"])["environment"])
    assert (stored.image, stored.image_id) == (settings.workspace_image, DEFAULT_ID)  # pinned
    spec = runtime.specs[name_of(done)]
    assert (spec.image, spec.image_id) == (settings.workspace_image, DEFAULT_ID)
    assert runtime.pulled == [settings.workspace_image]  # asked of the runtime as any image is
    log = ready_log[done["id"]]
    assert dc.DEFAULT_IMAGE_USED in log
    [ignored] = [line for line in log.splitlines() if line.startswith("Ignored in")]
    assert "name" in ignored and "forwardPorts" not in ignored  # its port is honoured
    assert stored.ports == (dc.Port(6080),)


# --- the plan, the new container, the user ----------------------------------------------


def test_the_container_is_made_again_from_the_image_on_the_same_volume_from_the_stored_plan(
    client, runtime, publish, settings
):
    publish(
        {
            **config(
                remoteUser="dev",
                containerEnv={"GREETING": "hello"},
                forwardPorts=[1],
                postCreateCommand="true",
            ),
            "README.md": "x",
        }
    )
    done = cloned(client)
    assert done["state"] == "ready" and done["error"] is None and done["log"] is None
    name = name_of(done)
    order = [e[0] for e in runtime.events if e[0] in ("create", "pull", "discard")]
    assert order == ["create", "pull", "discard", "create"]  # made, pulled, discarded, made again
    assert runtime.pulled == [IMAGE]
    spec = runtime.specs[name]
    assert (spec.image, spec.user, spec.uid, spec.gid) == (IMAGE, "dev", 1234, 1235)
    assert dict(spec.env) == {"GREETING": "hello"}
    assert (spec.slot, spec.person, spec.workspace) == (1, 1, done["id"])
    assert (work_of(runtime, done) / "README.md").is_file()  # the clone survived on the volume
    assert runtime.running(name)
    assert runtime.labelled()[name][rt.LABEL_WORKSPACE] == str(done["id"])
    # the plan is stored with the row and is what the workspace was made from
    stored = dc.Plan.from_json(row(settings, done["id"])["environment"])
    assert (stored.image, stored.uid, stored.gid, stored.run_as) == (IMAGE, 1234, 1235, "dev")
    assert (stored.ignored, stored.ports) == ((), (dc.Port(1),))  # honoured, not ignored
    assert [c.shell for c in stored.commands] == ["true"]
    assert stored.image_path == "/image/bin:/usr/bin:/bin"


def test_the_plans_environment_user_and_path_are_what_every_program_of_rcman_gets(
    client, app, runtime, publish
):
    publish(
        config(
            remoteUser="dev",
            containerEnv={"A": "container"},
            remoteEnv={"A": "remote", "B": "b"},
        )
    )
    done = cloned(client)
    service = app.state.workspaces
    found = service.get(done["id"])
    layout = service._layout(found)
    assert layout.user == "dev"
    assert layout.env == {"A": "remote", "B": "b"}
    assert layout.path == "/image/bin:/usr/bin:/bin"  # the image's PATH; the host has no folder
    assert service.server(found).layout == layout
    git_env = service.repository(found).env
    assert git_env["A"] == "remote" and git_env["B"] == "b"
    assert git_env["HOME"] == layout.home  # the home stays the workspace's
    assert git_env["PATH"] == layout.path
    from rcman import claude_server

    claude = rt.child_env(layout)
    assert claude["A"] == "remote" and claude["USER"] == "dev"
    assert claude["HOME"] == layout.home and claude["CLAUDE_CONFIG_DIR"] == layout.config
    tmux = claude_server.tmux_env(layout)
    assert tmux["A"] == "remote" and tmux["HOME"] == layout.home


# --- lifecycle commands -----------------------------------------------------------------------


def test_the_create_commands_run_once_in_the_specs_order_before_the_workspace_is_ready(
    client, runtime, publish, settings, monkeypatch
):
    publish(
        config(
            postStartCommand="echo start >> ran.txt",
            postCreateCommand="echo post-create >> ran.txt",
            updateContentCommand=["sh", "-c", "echo update-content >> ran.txt"],
            onCreateCommand="echo on-create >> ran.txt",
        )
    )
    seen = {}
    real = ws.Workspaces._set

    def set_(self, workspace_id, **columns):
        if columns.get("state") == ws.READY:
            seen[workspace_id] = (work_of(runtime, {"id": workspace_id}) / "ran.txt").read_text()
        return real(self, workspace_id, **columns)

    monkeypatch.setattr(ws.Workspaces, "_set", set_)
    done = cloned(client)
    ran = (work_of(runtime, done) / "ran.txt").read_text().split()
    assert ran == ["on-create", "update-content", "post-create", "start"]
    assert seen[done["id"]].split() == ran  # all had run when it became ready
    assert done["state"] == "ready"


def test_the_commands_run_as_the_images_user_in_the_work_folder_with_exactly_the_plans_env(
    client, runtime, publish, monkeypatch
):
    monkeypatch.setenv("RCMAN_SECRET_FOR_THE_TEST", "do-not-leak")
    publish(
        config(
            containerEnv={"GREETING": "hello"},
            remoteEnv={"WHO": "${containerEnv:GREETING} there"},
            onCreateCommand="env > env.txt && pwd > pwd.txt",
        )
    )
    done = cloned(client)
    work = work_of(runtime, done)
    env = dict(line.split("=", 1) for line in (work / "env.txt").read_text().splitlines())
    assert env["GREETING"] == "hello"
    assert env["WHO"] == "hello there"  # ${containerEnv:} sees the file's own containerEnv
    for noise in ("PWD", "OLDPWD", "SHLVL", "_"):  # what the shell adds to what it runs
        env.pop(noise, None)
    assert set(env) == {
        "HOME", "USER", "LOGNAME", "PATH", "LANG", "DISABLE_AUTOUPDATER", "GH_CONFIG_DIR",
        "GREETING", "WHO",
    }  # fmt: skip
    layout = runtime.layout(name_of(done))
    assert env["HOME"] == layout.home  # the workspace's home, not the image user's
    assert env["GH_CONFIG_DIR"] == rt.gh_config_dir(layout)  # gh reads the person's token
    assert "RCMAN_SECRET_FOR_THE_TEST" not in env
    assert (work / "pwd.txt").read_text().strip() == str(work)
    assert runtime.hooks_run()  # positive control: the commands went through the runtime


def test_a_commands_output_tail_is_in_the_log_and_the_token_is_redacted_there_too(
    client, runtime, publish, ready_log
):
    save_token(client)
    publish(config(postCreateCommand=f"echo installed-things; echo {TOKEN}"))
    done = cloned(client)
    log = ready_log[done["id"]]
    assert "installed-things" in log and TOKEN not in log and ws.REDACTED in log
    assert "postCreateCommand" in log


# --- restarting ---------------------------------------------------------------------


def a_plan(**keys) -> dc.Plan:
    facts = rt.ImageFacts(passwd=PASSWD, group=GROUP, env={"PATH": "/image/bin:/bin"})
    return dc.plan({**GOOD, **keys}, facts, 1, "/rcman/work")


def set_environment(settings, workspace: int, text: str, **more) -> None:
    con = db.connect(settings.database)
    try:
        con.execute("update workspaces set environment = ? where id = ?", (text, workspace))
        for column, value in more.items():
            con.execute(f"update workspaces set {column} = ? where id = ?", (value, workspace))
        con.commit()
    finally:
        con.close()


def ready_with_plan(service, settings, plan: dc.Plan, make_container=True, name="planned") -> int:
    workspace = insert(settings, name, "ready", URL)
    set_environment(settings, workspace, plan.to_json())
    if make_container:
        service.runtime.create(service._spec(service.get(workspace), 1))
    return workspace


def test_a_lost_container_is_made_again_from_the_stored_plan_without_its_create_commands(
    service, settings, runtime
):
    plan = a_plan(
        remoteUser="dev",
        containerEnv={"A": "b"},
        onCreateCommand="echo create >> ran.txt",
        postCreateCommand="echo post >> ran.txt",
        postStartCommand="echo start >> ran.txt",
    )
    workspace = ready_with_plan(service, settings, plan, make_container=False)
    service.restore()
    name = rt.container_name(workspace)
    assert runtime.running(name)
    spec = runtime.specs[name]
    assert (spec.image, spec.user, spec.uid, spec.gid) == (IMAGE, "dev", 1234, 1235)
    assert dict(spec.env) == {"A": "b"}
    assert row(settings, workspace)["state"] == "ready"
    # pulled first: the image may be gone from the storage
    order = [e[0] for e in runtime.events if e[0] in ("pull", "create")]
    assert order == ["pull", "create"] and runtime.pulled == [IMAGE]
    work = Path(runtime.layout(name).work)
    assert (work / "ran.txt").read_text().split() == ["start"]  # only what runs at every start
    service.restore()  # rcman restarts again: the container runs, so nothing is started
    assert (work / "ran.txt").read_text().split() == ["start"]


def test_post_start_runs_after_every_start_of_a_container_that_was_stopped(
    service, settings, runtime
):
    plan = a_plan(
        postStartCommand="echo start >> ran.txt", onCreateCommand="echo create >> ran.txt"
    )
    workspace = ready_with_plan(service, settings, plan)
    name = rt.container_name(workspace)
    work = Path(runtime.layout(name).work)
    assert not (work / "ran.txt").exists()  # the container was made, not started by a restore
    runtime.stop(name)
    service.restore()
    assert runtime.running(name)
    assert (work / "ran.txt").read_text().split() == ["start"]
    runtime.stop(name)
    service.restore()
    assert (work / "ran.txt").read_text().split() == ["start", "start"]


def test_a_container_no_row_is_for_is_discarded_and_its_volume_is_kept(service, settings, runtime):
    insert(settings, "has-a-row", "ready", URL)
    name = rt.container_name(999)
    runtime.create(rt.Spec(name, slot=1, workspace=999, person=1))
    (runtime.root / name / rt.WORK / "notes.txt").write_text("someone's work")
    service.restore()
    assert ("discard", name) in runtime.events and ("remove", name) not in runtime.events
    assert not runtime.exists(name) and name not in runtime.labelled()
    assert (runtime.root / name / rt.WORK / "notes.txt").read_text() == "someone's work"


def lost_with_hook(service, settings, runtime, name="slow", command="echo GATE", wanted=True):
    """A ready workspace whose container is gone, whose plan has a postStartCommand."""
    workspace = insert(settings, name, "ready", URL)
    set_environment(
        settings, workspace, a_plan(postStartCommand=command).to_json(), wanted=int(wanted)
    )
    return workspace


# --- the plan pins the image ------------------------------------------------------------------

ID_A, ID_B = "a" * 64, "b" * 64
DIGEST = "sha256:" + "c" * 64
REPOSITORY = "registry.example.com/team/dev"


def pinned_plan(**keys) -> dc.Plan:
    facts = rt.ImageFacts(
        passwd=PASSWD, group=GROUP, env={"PATH": "/image/bin:/bin"}, id=ID_A, digest=DIGEST
    )
    return dc.plan({**GOOD, **keys}, facts, 1, "/rcman/work")


def test_a_lost_container_is_made_again_from_the_pinned_id_without_a_pull(
    service, settings, runtime
):
    runtime.images[IMAGE] = rt.ImageFacts(passwd=PASSWD, group=GROUP, id=ID_B)  # the tag moved on
    runtime.extra_ids.add(ID_A)  # and the image it was made from is still in the storage
    workspace = ready_with_plan(service, settings, pinned_plan(), make_container=False)
    service.restore()
    spec = runtime.specs[rt.container_name(workspace)]
    assert (spec.image, spec.image_id) == (IMAGE, ID_A)
    assert runtime.pulled == [] and row(settings, workspace)["state"] == "ready"


def test_an_image_gone_from_the_storage_is_pulled_by_its_digest_not_its_tag(
    service, settings, runtime
):
    runtime.images.pop(IMAGE)
    runtime.images[f"{REPOSITORY}@{DIGEST}"] = rt.ImageFacts(passwd=PASSWD, group=GROUP, id=ID_A)
    runtime.lost_ids.add(ID_A)  # the storage lacks it until the pull brings it back
    runtime.after_pull = lambda: runtime.lost_ids.discard(ID_A)
    workspace = ready_with_plan(service, settings, pinned_plan(), make_container=False)
    service.restore()
    assert runtime.pulled == [f"{REPOSITORY}@{DIGEST}"]
    assert runtime.specs[rt.container_name(workspace)].image_id == ID_A
    assert row(settings, workspace)["state"] == "ready"


def test_a_workspace_made_from_the_default_image_keeps_the_id_it_was_made_from(
    client, app, runtime
):
    runtime.default_id = ID_A
    made = made_workspace(client, ALICE)
    spec = runtime.specs[rt.container_name(made["id"])]
    assert spec.image is None and spec.image_id == ID_A
    assert row(app.state.settings, made["id"])["image_id"] == ID_A


def test_a_lost_default_container_is_made_again_from_the_pinned_image_not_the_newest(
    service, settings, runtime
):
    workspace = insert(settings, "default", "ready", None)
    set_environment(settings, workspace, None, image_id=ID_A)
    runtime.default_id = ID_B  # a newer default image, for new workspaces
    runtime.extra_ids.add(ID_A)  # the one this workspace was made from is still in the storage
    service.restore()
    spec = runtime.specs[rt.container_name(workspace)]
    assert spec.image_id == ID_A and row(settings, workspace)["image_id"] == ID_A


# --- images nothing refers to are collected ---------------------------------------------------

OLD_DEFAULT, STRAY = "localhost/rcman-workspace:old", "registry.example.com/stray:1"


def stray_images(runtime, *names: str) -> None:
    """Images the runtime holds that it pulled or built for someone."""
    for name in names:
        runtime.images[name] = rt.ImageFacts(passwd=PASSWD, group=GROUP, id=f"id-of-{name}")
        runtime.collectable.add(name)


def test_a_restart_collects_the_images_nothing_refers_to_and_keeps_the_rest(
    service, settings, runtime
):
    pinned_name, moved_name = "registry.example.com/pinned:1", "registry.example.com/moved:1"
    stray_images(runtime, STRAY, OLD_DEFAULT, pinned_name, moved_name, IMAGE)
    named = ready_with_plan(service, settings, a_plan(), name="named")  # names IMAGE
    pinned = insert(settings, "pinned", "ready", None)  # made from the default image of its day
    set_environment(settings, pinned, None, image_id=runtime.images[pinned_name].id)
    runtime.images[moved_name] = rt.ImageFacts(id=ID_A)  # the plan's id, under another name
    runtime.collectable.add(moved_name)
    moved = ready_with_plan(service, settings, pinned_plan(), name="moved")
    service.restore()
    assert STRAY not in runtime.images and OLD_DEFAULT not in runtime.images
    assert IMAGE in runtime.images  # a stored plan names it
    assert pinned_name in runtime.images  # a workspace is pinned to its id
    assert moved_name in runtime.images  # a stored plan holds its id
    assert runtime.exists(rt.container_name(named)) and moved


def test_deleting_a_workspace_collects_the_image_only_it_used(client, runtime, publish):
    publish(config())
    made = cloned(client)
    assert made["state"] == "ready" and IMAGE in runtime.images
    runtime.collectable.add(IMAGE)
    other = made_workspace(client, ALICE, "other")  # a workspace of the default image
    r = client.delete(f"/api/workspaces/{other['id']}", headers=changing(ALICE))
    assert r.status_code == 200 and IMAGE in runtime.images  # the plan of the first names it
    r = client.delete(f"/api/workspaces/{made['id']}", headers=changing(ALICE))
    assert r.status_code == 200
    assert IMAGE not in runtime.images  # nothing refers to it now


# --- building an image --------------------------------------------------------------------------


def test_a_dockerfile_is_built_and_the_workspace_is_made_from_the_image_built(
    client, runtime, publish, settings, ready_log
):
    """The builder is asked only the build-only configuration; the workspace is made from the
    image built for it, which its delete removes."""
    publish(
        building(
            postCreateCommand="echo after-the-build", containerEnv={"A": "b"}, remoteUser="dev"
        )
    )
    done = cloned(client)
    assert done["state"] == "ready", done
    (request,) = runtime.builds
    assert request.id == done["id"] and request.person == row(settings, done["id"])["person"]
    assert request.slot == people.BUILDER_SLOT and request.source == name_of(done)
    assert request.config_path == f".devcontainer/{dc.BUILD_CONFIG_NAME}"
    assert json.loads(request.config_text) == {"build": {"dockerfile": "Dockerfile"}}
    assert "after-the-build" not in request.config_text
    image = rt.built_name(done["id"])
    stored = dc.Plan.from_json(row(settings, done["id"])["environment"])
    assert stored.image == image and stored.container_env["A"] == "b"
    assert runtime.specs[name_of(done)].image == image
    assert runtime.pulled == []  # the builder pulls what it builds on; rcman pulls nothing
    assert runtime.builders() == set()  # the builder and its volume are gone
    assert runtime.built_images() == {done["id"]: image}  # the image is the workspace's
    assert [h for h in runtime.hooks_run() if "after-the-build" in " ".join(h)]
    assert len(runtime.hooks_run()) == 1  # once: the image carries none of the file's own
    assert "STEP 1/2" in ready_log[done["id"]]  # the builder's steps are in the log
    r = client.delete(f"/api/workspaces/{done['id']}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    cleared(runtime, client)
    assert runtime.built_images() == {} and runtime.builders() == set()


def test_features_on_an_image_are_built_and_the_base_is_left_to_the_builder_to_pull(
    client, runtime, publish
):
    publish(featured())
    done = cloned(client)
    assert done["state"] == "ready", done
    (request,) = runtime.builds
    assert json.loads(request.config_text) == {
        "image": IMAGE,
        "features": {"./feature": {"greeting": "hi"}},
    }
    assert runtime.pulled == []


@pytest.fixture
def one_build_at_a_time(monkeypatch):
    monkeypatch.setattr(ws, "MAX_BUILDS", 1)  # before the service starts: it takes the value once


def waiting_line(client, workspace: int) -> bool:
    return ws.WAITING_FOR_BUILD in (entry(client, workspace)["log"] or "")


def test_builds_beyond_the_bound_wait_their_turn_and_say_so_in_the_log(
    one_build_at_a_time, client, runtime, publish
):
    runtime.build_gate = threading.Event()
    publish(building())
    try:
        first = clone(client).json()["workspace"]["id"]
        assert runtime.building.wait(HANG_GUARD)
        second = clone(client).json()["workspace"]["id"]
        until(lambda: waiting_line(client, second))
        assert not waiting_line(client, first)  # the one that builds does not wait
        assert runtime.most_builds_at_once == 1 and len(runtime.builds) == 1
    finally:
        runtime.build_gate.set()  # nothing is left held should the test fail
    assert finished(client, first)["state"] == "ready"
    assert finished(client, second)["state"] == "ready"
    assert runtime.most_builds_at_once == 1 and len(runtime.builds) == 2


# --- a restart --------------------------------------------------------------------------------


def built_plan(workspace: int, **keys) -> dc.Plan:
    facts = rt.ImageFacts(passwd=PASSWD, group=GROUP, env={"PATH": "/built/bin:/bin"})
    return dc.plan(
        {**GOOD, **keys}, facts, workspace, "/rcman/work", image=rt.built_name(workspace)
    )


def plant_built(runtime, workspace: int) -> str:
    name = rt.built_name(workspace)
    runtime.images[name] = rt.ImageFacts(passwd=PASSWD, group=GROUP)
    runtime.built[workspace] = name
    return name


def test_a_restart_removes_the_builders_volumes_and_images_no_workspace_uses_and_keeps_the_rest(
    service, settings, runtime
):
    """And the lost container of the workspace that uses its built image is made again from it,
    without a pull: a built image is in the storage or it is nowhere."""
    used = insert(settings, "used", "ready", URL)
    set_environment(settings, used, built_plan(used).to_json())
    plant_built(runtime, used)
    failed = insert(settings, "failed", "failed", URL)  # a plan that names no built image
    plant_built(runtime, failed)
    plant_built(runtime, 4242)  # no row at all
    runtime.builder_ids.update({used, 4243})  # a builder left from before, of a row and of none
    service.restore()
    assert runtime.builders() == set()
    assert runtime.built_images() == {used: rt.built_name(used)}  # only the used one stays
    assert runtime.running(rt.container_name(used))
    assert runtime.pulled == [] and row(settings, used)["state"] == "ready"
