"""A workspace made from a repository's devcontainer.json (backend/rcman/workspaces.py), on a host
runtime that has the images a test plants: reading and refusing the file, pulling the image,
making the container again from the plan on the same volume, the lifecycle commands, cancelling,
and bringing it all back when rcman starts. The repository is cloned by real git from the tests'
stand-in for github.com."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from rcman import db, people, problems
from rcman import devcontainer as dc
from rcman import runtime as rt
from rcman import workspaces as ws

from tests.conftest import MADE_WITHIN, as_user, changing, host_runtime, made_workspace
from tests.git_fixtures import fixture_git, make_repo
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
    listed,
    row,
    save_token,
)

IMAGE = "registry.example.com/team/dev:latest"
PASSWD = "root:x:0:0:root:/root:/bin/sh\ndev:x:1234:1235::/home/dev:/bin/sh\n"
GROUP = "root:x:0:\ndev:x:1235:\nextra:x:5000:dev\n"
GOOD = {"image": IMAGE}
FILE = ".devcontainer/devcontainer.json"


class Flow(rt.HostRuntime):
    """The host runtime with images, noting what is done to it in order, and able to hold the
    pull or a lifecycle command until the test lets it go, and to answer as an image that has
    musl, or no git, would."""

    def __init__(self, *args):
        super().__init__(*args)
        self.events: list[tuple] = []
        self.execs: list[tuple[str, list[str], str | None]] = []
        self.libc = b"ok\n"
        self.git: rt.Result | None = None
        self.gate: threading.Event | None = None  # a command with GATE in it waits for this
        self.hooking = threading.Event()
        self.pull_gate: threading.Event | None = None
        self.pulling = threading.Event()
        self.pull_lines = ["Trying to pull the image", "Copying blob a"]
        self.fail_image_create = False
        self.pull_error: str | None = None  # what every pull says, failing, when it is set
        self.after_pull = lambda: None  # called as a pull has succeeded
        self.create_gate: threading.Event | None = None  # a create from an image waits for this
        self.creating = threading.Event()
        self.release_ok = False  # a command held at the gate ends well, whatever became of it
        self.kept: int | None = None  # the ``keep`` of the last command
        self.concurrent = self.most_at_once = 0  # commands held at the gate now, and at most
        self.after_facts = lambda: None  # called as the image has been read
        self.after_discard = lambda: None  # called as the container has been discarded
        self.answers: list[rt.Result] = []  # what the lifecycle commands were answered
        # The planted builder (HostRuntime.build_script): it says its lines, may be held at
        # the gate until the test lets it go or the build is stopped, and makes an image with
        # the facts below or fails with ``build_error``.
        self.build_lines = ["[1/1] STEP 1/2: FROM scratch"]
        self.build_gate: threading.Event | None = None
        self.building = threading.Event()
        self.build_error: Exception | None = None
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
                deadline = time.monotonic() + MADE_WITHIN
                while not self.build_gate.is_set():
                    if (
                        cancel is not None and cancel.is_set()
                    ) or request.id not in self.builders():
                        raise rt.RuntimeFailed("the build was stopped")
                    assert time.monotonic() < deadline
                    time.sleep(0.02)
            if self.build_error is not None:
                raise self.build_error
            return self.build_facts
        finally:
            self.builds_at_once -= 1

    def remove_build(self, workspace, image=False):
        self.events.append(("remove_build", workspace, image))
        super().remove_build(workspace, image)

    def create(self, spec):
        self.events.append(("create", spec.image, spec.user))
        if spec.image and self.create_gate is not None:
            self.creating.set()
            self.create_gate.wait(MADE_WITHIN)
        if spec.image and self.fail_image_create:
            raise rt.RuntimeFailed("OCI runtime error: cannot run the image")
        super().create(spec)

    def image_facts(self, image, cancel=None):
        facts = super().image_facts(image, cancel)
        self.after_facts()
        return facts

    def discard(self, name):
        self.events.append(("discard", name))
        super().discard(name)
        self.after_discard()

    def remove(self, name):
        self.events.append(("remove", name))
        if self.gate is not None:
            self.gate.set()
        super().remove(name)

    def pull(self, image, progress=None, cancel=None):
        self.events.append(("pull", image))
        self.pulling.set()
        if self.pull_error is not None:
            raise rt.RuntimeFailed(self.pull_error)
        for line in self.pull_lines:
            if progress is not None:
                progress(line)
        if self.pull_gate is not None:
            deadline = time.monotonic() + MADE_WITHIN
            while not self.pull_gate.is_set():
                if cancel is not None and cancel.is_set():
                    raise rt.RuntimeFailed("the pull was stopped")
                assert time.monotonic() < deadline
                time.sleep(0.02)
        super().pull(image, progress, cancel)
        self.after_pull()

    def exec(self, name, argv, stdin=None, timeout=rt.PODMAN_TIMEOUT, cwd=None, keep=None):
        self.execs.append((name, argv, cwd))
        self.kept = keep
        joined = " ".join(argv)
        if "ld-musl" in joined:
            return rt.Result(0, self.libc, b"")
        mine = self.specs.get(name)
        if self.git is not None and argv[-1:] == ["--version"] and "git" in argv:
            if mine is not None and mine.image:
                return self.git
        if self.gate is not None and "GATE" in joined:
            self.hooking.set()
            self.concurrent += 1
            self.most_at_once = max(self.most_at_once, self.concurrent)
            try:
                self.gate.wait(MADE_WITHIN)
            finally:
                self.concurrent -= 1
            if self.release_ok:
                return rt.Result(0, b"", b"")
            if not self.exists(name):
                return rt.Result(137, b"", b"Killed\n")
        answer = super().exec(name, argv, stdin, timeout, cwd, keep)
        if cwd is not None and "-k" in argv:
            self.answers.append(answer)
        return answer

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

REFUSALS = [
    ({"capAdd": ["SYS_ADMIN"]}, "capAdd"),
    ({"privileged": True}, "privileged"),
    ({"mounts": ["type=bind,source=/,target=/host"]}, "mounts"),
    ({"features": {"../outside": {}}}, "features"),
    ({"build": {"dockerfile": "/etc/passwd"}}, "build.dockerfile"),
    ({"build": {"dockerfile": "Dockerfile", "options": ["--network=host"]}}, "build.options"),
    ({"build": {"dockerfile": "Dockerfile", "cacheFrom": ["x"]}}, "build.cacheFrom"),
    ({"runArgs": ["--net=host"]}, "runArgs"),
    ({"overrideCommand": False}, "overrideCommand"),
    ({"workspaceFolder": "/x"}, "workspaceFolder"),
    ({"aKeyOfTheFuture": True}, "aKeyOfTheFuture"),
    ({"containerEnv": {"HOME": "/x"}}, "HOME"),
]


@pytest.mark.parametrize(("keys", "named"), REFUSALS)
def test_a_refused_file_fails_the_workspace_naming_the_key_and_leaves_no_container_or_volume(
    client, runtime, publish, settings, keys, named
):
    publish(config(**keys))
    failed = cloned(client)
    assert failed["state"] == "failed"
    assert f'"{named}"' in failed["error"]
    assert failed["error"].count("\n") < 5  # plain words, not a trace
    no_trace(runtime, failed)
    assert row(settings, failed["id"])["environment"] is None  # no plan was kept
    assert runtime.pulled == []  # nothing was pulled for a file that was refused first


def test_a_user_the_image_does_not_have_is_refused_after_the_pull_and_nothing_is_left(
    client, runtime, publish
):
    publish(config(remoteUser="nobody-here"))
    failed = cloned(client)
    assert failed["state"] == "failed" and '"nobody-here"' in failed["error"]
    assert runtime.pulled == [IMAGE]  # the image had to be read to know
    no_trace(runtime, failed)


def test_a_runtime_that_goes_away_during_the_pull_fails_the_workspace_in_plain_words(
    client, runtime, publish
):
    """Not with what podman said, which holds paths of the machine, and not as a refusal of the
    file: the container and the clone in it stay for a person to look at, like those of any
    failure that is not a refusal."""

    def unreachable(image, progress=None, cancel=None):
        raise rt.RuntimeUnavailable("Cannot connect to Podman: unix:///run/user/990/podman.sock")

    runtime.pull = unreachable
    publish(config())
    failed = cloned(client)
    assert failed["state"] == "failed"
    assert failed["error"] == problems.DEVCONTAINER_MESSAGES["runtime_gone"]
    assert "990" not in failed["error"] and "podman.sock" not in failed["error"]
    assert runtime.exists(name_of(failed))  # kept


@pytest.mark.parametrize("label", ["rcman.probe", "rcman.workspace", "rcman.person", "rcman.x"])
def test_an_image_that_carries_a_label_of_rcmans_is_refused_and_nothing_is_left(
    client, runtime, publish, label
):
    """A container inherits its image's labels: one that said it was a probe would be removed
    at every restore, one that named another person's workspace would be taken for it."""
    runtime.images[IMAGE] = rt.ImageFacts(
        passwd=PASSWD, group=GROUP, labels={label: "1", "org.example.fine": "yes"}
    )
    publish(config())
    failed = cloned(client)
    assert failed["state"] == "failed"
    assert failed["error"] == problems.DEVCONTAINER_MESSAGES["image_labels"].format(
        image=IMAGE, label=label
    )
    no_trace(runtime, failed)


def test_an_image_with_labels_that_are_not_rcmans_is_taken(client, runtime, publish):
    runtime.images[IMAGE] = rt.ImageFacts(
        passwd=PASSWD, group=GROUP, labels={"org.example.rcman": "1", "my.rcman.thing": "2"}
    )
    publish(config())
    assert cloned(client)["state"] == "ready"


def test_a_refusal_says_what_to_change(client, runtime, publish):
    publish(config(capAdd=["SYS_ADMIN"]))
    failed = cloned(client)
    assert (
        problems.DEVCONTAINER_MESSAGES["refused_key"].split("{reason}")[0].format(key="capAdd")
        in failed["error"]
    )
    assert dc.POLICY["capAdd"].reason in failed["error"]
    assert "make the workspace again" in failed["error"]


def test_a_refused_workspace_can_be_deleted_and_nothing_is_left(client, runtime, publish):
    publish(config(privileged=True))
    failed = cloned(client)
    r = client.delete(f"/api/workspaces/{failed['id']}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    cleared(runtime, client)


def test_the_log_says_where_the_file_was_looked_for(client, runtime, publish):
    publish(config(privileged=True))
    failed = cloned(client)
    lines = failed["log"].splitlines()
    assert dc.LOOKED_IN in lines
    assert lines.index(dc.LOOKED_IN) > lines.index("Cloned")  # after the clone, in the log


def test_the_log_names_the_keys_that_are_ignored_and_the_image_pulled(
    client, runtime, publish, ready_log
):
    publish(config(forwardPorts=[3000], customizations={"vscode": {}}))
    done = cloned(client)
    log = ready_log[done["id"]]
    assert dc.LOOKED_IN in log and f"Pulling {IMAGE}" in log
    assert "forwardPorts" in log and "customizations" in log
    assert "3000" not in log and "vscode" not in log  # names only, never values


def test_a_file_that_is_not_json_says_where_and_never_what_it_holds(client, runtime, publish):
    publish({FILE: '{\n  "image": "x",\n  "secret-looking": sk-the-key-in-the-file\n}'})
    failed = cloned(client)
    assert failed["state"] == "failed"
    assert "line 3" in failed["error"] and "column" in failed["error"]
    assert "sk-the-key" not in failed["error"] + (failed["log"] or "")
    no_trace(runtime, failed)


def test_a_symbolic_link_is_refused_and_what_it_points_at_is_not_read(
    client, runtime, publish, tmp_path
):
    secret = tmp_path / "secret.json"
    secret.write_text('{"claudeAiOauth": "sk-ant-the-token-in-the-target"}')
    publish({".devcontainer.json": Link(secret)})
    failed = cloned(client)
    assert failed["state"] == "failed" and "symbolic link" in failed["error"]
    shown = failed["error"] + (failed["log"] or "")
    assert "sk-ant" not in shown and "claudeAiOauth" not in shown
    no_trace(runtime, failed)


def test_a_file_in_a_place_that_is_not_looked_in_is_not_read(client, runtime, publish, settings):
    publish(
        {
            "devcontainer.json": json.dumps({"capAdd": ["X"]}),
            ".devcontainer/sub/devcontainer.json": "{",
        }
    )
    done = cloned(client)
    assert done["state"] == "ready"
    assert runtime.pulled == [] and row(settings, done["id"])["environment"] is None


def test_the_file_in_the_folder_is_read_before_the_dotfile(client, runtime, publish):
    publish({**config(capAdd=["X"]), ".devcontainer.json": json.dumps(GOOD)})
    assert cloned(client)["state"] == "failed"


def test_the_dotfile_is_read_when_there_is_no_folder(client, runtime, publish):
    publish({".devcontainer.json": json.dumps({**GOOD, "capAdd": ["X"]})})
    failed = cloned(client)
    assert failed["state"] == "failed" and '"capAdd"' in failed["error"]


def test_a_file_that_is_too_big_is_refused(client, runtime, publish):
    publish({FILE: json.dumps({**GOOD, "name": "x" * dc.MAX_BYTES})})
    failed = cloned(client)
    assert failed["state"] == "failed" and "too big" in failed["error"]
    no_trace(runtime, failed)


def test_a_repository_with_no_file_is_made_as_before(client, runtime, publish, settings):
    publish({"README.md": "hello\n"})
    done = cloned(client)
    assert done["state"] == "ready"
    assert runtime.pulled == []
    assert [e for e in runtime.events if e[0] in ("pull", "discard")] == []
    assert row(settings, done["id"])["environment"] is None
    assert runtime.specs[name_of(done)].image is None  # the workspace image, not another


def test_a_workspace_with_no_source_never_reads_a_file(client, runtime, settings):
    r = client.post("/api/workspaces", json={"name": "blank"}, headers=changing(ALICE))
    blank = finished(client, r.json()["workspace"]["id"])
    assert blank["state"] == "ready"
    assert not [a for _, a, _ in runtime.execs if dc.PROBE in a]


def test_the_looking_is_done_in_the_work_folder_by_the_workspaces_own_tools(
    client, runtime, publish
):
    publish({"README.md": "x"})
    done = cloned(client)
    probes = [(a, cwd) for _, a, cwd in runtime.execs if dc.PROBE in a]
    assert probes and probes[0][1] == str(work_of(runtime, done))


# --- the image -------------------------------------------------------------------------------


def test_a_pull_that_fails_leaves_nothing_and_says_what_podman_said(client, runtime, publish):
    del runtime.images[IMAGE]
    publish(config())
    failed = cloned(client)
    assert failed["state"] == "failed"
    assert IMAGE in failed["error"] and "manifest unknown" in failed["error"]
    assert "sign-in" in failed["error"]
    no_trace(runtime, failed)


def test_the_pull_is_shown_in_the_log_with_its_progress(client, runtime, publish):
    runtime.pull_gate = threading.Event()
    publish(config())
    made = clone(client).json()["workspace"]
    assert runtime.pulling.wait(MADE_WITHIN)
    deadline = time.monotonic() + MADE_WITHIN
    while "Copying blob a" not in (entry(client, made["id"])["log"] or ""):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    log = entry(client, made["id"])["log"]
    assert f"Pulling {IMAGE}" in log and "Trying to pull the image" in log
    runtime.pull_gate.set()
    assert finished(client, made["id"])["state"] == "ready"


def test_an_image_on_musl_is_refused_as_alpine_is_and_nothing_is_left(client, runtime, publish):
    runtime.libc = b"musl\n"
    publish(config())
    failed = cloned(client)
    assert failed["state"] == "failed"
    assert "musl" in failed["error"] and "glibc" in failed["error"] and IMAGE in failed["error"]
    no_trace(runtime, failed)
    assert any(e[0] == "create" and e[1] == IMAGE for e in runtime.events)  # it had been made


def test_an_image_whose_libc_is_not_known_is_not_refused(client, runtime, publish):
    runtime.libc = b"ok\n"
    publish(config())
    assert cloned(client)["state"] == "ready"


def test_an_image_without_a_usable_git_is_refused_and_nothing_is_left(client, runtime, publish):
    runtime.git = rt.Result(127, b"", b"timeout: can't execute 'git'")
    publish(config())
    failed = cloned(client)
    assert failed["state"] == "failed"
    assert "git is not installed in the image" in failed["error"] and IMAGE in failed["error"]
    no_trace(runtime, failed)


def test_the_git_of_the_image_is_asked_again_for_the_new_image(client, runtime, publish):
    publish(config())
    done = cloned(client)
    asked = [(n, a) for n, a, _ in runtime.execs if "git" in a and a[-1:] == ["--version"]]
    assert len(asked) >= 2 and {n for n, _ in asked} == {name_of(done)}


def test_the_metadata_of_the_image_is_held_to_the_policy_naming_the_entry(client, runtime, publish):
    runtime.images[IMAGE] = rt.ImageFacts(
        passwd=PASSWD,
        group=GROUP,
        labels={
            dc.METADATA_LABEL: json.dumps(
                [{"id": "ghcr.io/o/f/docker-in-docker:2", "privileged": True}]
            )
        },
    )
    publish(config())
    failed = cloned(client)
    assert failed["state"] == "failed"
    assert "ghcr.io/o/f/docker-in-docker:2" in failed["error"] and '"privileged"' in failed["error"]
    no_trace(runtime, failed)


# --- the plan, the new container, the user ----------------------------------------------


def test_the_container_is_made_again_from_the_image_on_the_same_volume(
    client, runtime, publish, settings
):
    publish({**config(remoteUser="dev", containerEnv={"GREETING": "hello"}), "README.md": "x"})
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


def test_the_channel_to_the_old_container_is_closed_before_it_is_discarded(
    client, app, runtime, publish, monkeypatch
):
    real = ws.Workspaces.close_channel

    def closing(self, name):
        runtime.events.append(("close", name))
        return real(self, name)

    monkeypatch.setattr(ws.Workspaces, "close_channel", closing)
    publish(config())
    done = cloned(client)
    name = name_of(done)
    assert runtime.events.index(("close", name)) < runtime.events.index(("discard", name))


def test_the_plan_is_stored_with_the_row_and_is_what_the_workspace_is_made_from(
    client, runtime, publish, settings
):
    publish(config(remoteUser="dev", forwardPorts=[1], postCreateCommand="true"))
    done = cloned(client)
    stored = dc.Plan.from_json(row(settings, done["id"])["environment"])
    assert (stored.image, stored.uid, stored.gid, stored.run_as) == (IMAGE, 1234, 1235, "dev")
    assert stored.ignored == ("forwardPorts",)
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

    claude = claude_server.child_env(layout)
    assert claude["A"] == "remote" and claude["USER"] == "dev"
    assert claude["HOME"] == layout.home and claude["CLAUDE_CONFIG_DIR"] == layout.config
    tmux = claude_server.tmux_env(layout)
    assert tmux["A"] == "remote" and tmux["HOME"] == layout.home


def test_a_path_the_file_sets_is_used_with_rcmans_own_folder_first(client, app, runtime, publish):
    publish(config(containerEnv={"PATH": "/mine/bin:/usr/bin"}))
    done = cloned(client)
    service = app.state.workspaces
    layout = service._layout(service.get(done["id"]))
    assert layout.path == "/mine/bin:/usr/bin"
    assert "PATH" not in layout.env
    # The same on a runtime that has a folder of its own to keep first.
    prefixed = rt.PODMAN_LAYOUT.for_plan("dev", {}, "/mine/bin:/usr/bin")
    assert prefixed.path == f"{rt.CLAUDE_MOUNT}/bin:/mine/bin:/usr/bin"


def test_a_workspace_with_no_plan_has_the_runtimes_layout(client, app, runtime, publish):
    publish({"README.md": "x"})
    done = cloned(client)
    service = app.state.workspaces
    assert service._layout(service.get(done["id"])) == runtime.layout(name_of(done))


@pytest.mark.parametrize(
    ("user", "expected"),
    [
        ("dev", ("dev", 1234, 1235, ())),
        ("dev:dev", ("1234:1235", 1234, 1235, (5000,))),  # podman drops the groups; rcman adds
        ("dev:extra", ("1234:5000", 1234, 5000, ())),
        ("1234", ("1234", 1234, 1235, ())),
        ("root", ("root", 0, 0, ())),
    ],
)
def test_the_ids_the_user_has_in_the_image_reach_the_container(
    client, runtime, publish, user, expected
):
    publish(config(remoteUser=user))
    done = cloned(client)
    spec = runtime.specs[name_of(done)]
    assert (spec.user, spec.uid, spec.gid, spec.groups) == expected


def test_the_image_user_is_used_when_the_file_names_none(client, runtime, publish):
    runtime.images[IMAGE] = rt.ImageFacts(passwd=PASSWD, group=GROUP, user="dev")
    publish(config())
    done = cloned(client)
    assert runtime.specs[name_of(done)].uid == 1234


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
        "HOME", "USER", "LOGNAME", "PATH", "LANG", "DISABLE_AUTOUPDATER", "GREETING", "WHO",
    }  # fmt: skip
    layout = runtime.layout(name_of(done))
    assert env["HOME"] == layout.home  # the workspace's home, not the image user's
    assert "RCMAN_SECRET_FOR_THE_TEST" not in env
    assert (work / "pwd.txt").read_text().strip() == str(work)
    assert runtime.hooks_run()  # positive control: the commands went through the runtime


def test_every_command_runs_under_the_timeout_tool_with_named_limits(client, runtime, publish):
    publish(config(onCreateCommand="true", postCreateCommand=["true"]))
    cloned(client)
    hooks = [a for _, a, cwd in runtime.execs if cwd is not None and "-k" in a]
    assert len(hooks) == 2
    for argv in hooks:
        at = argv.index("-s")
        assert argv[at : at + 5] == [
            "-s",
            "TERM",
            "-k",
            str(ws.HOOK_KILL_GRACE_SECONDS),
            str(ws.HOOK_TIMEOUT_SECONDS),
        ]


def test_a_command_in_array_form_is_not_run_by_a_shell(client, runtime, publish):
    publish(config(onCreateCommand=["printf", "%s", "$HOME; touch pwned"]))
    done = cloned(client)
    assert done["state"] == "ready"
    assert not (work_of(runtime, done) / "pwned").exists()
    hook = runtime.hooks_run()[0]
    assert hook == ["printf", "%s", "$HOME; touch pwned"]


def test_the_entries_of_an_object_run_one_after_another(client, runtime, publish):
    publish(
        config(
            postCreateCommand={
                "first": "sleep 0.3; echo first >> ran.txt",
                "second": ["sh", "-c", "echo second >> ran.txt"],
            }
        )
    )
    done = cloned(client)
    assert (work_of(runtime, done) / "ran.txt").read_text().split() == ["first", "second"]


def test_a_failing_create_command_fails_the_workspace_with_its_exit_code_and_tail_and_keeps_it(
    client, runtime, publish, settings
):
    publish(config(postCreateCommand="echo building; echo broke >&2; exit 3"))
    failed = cloned(client)
    assert failed["state"] == "failed"
    assert "postCreateCommand failed, exit code 3" in failed["error"]
    assert "building" in failed["error"] and "broke" in failed["error"]
    name = name_of(failed)
    assert runtime.exists(name) and (runtime.root / name).is_dir()  # container and volume stay
    assert dc.Plan.from_json(row(settings, failed["id"])["environment"]).image == IMAGE
    r = client.delete(f"/api/workspaces/{failed['id']}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    cleared(runtime, client)
    assert not (runtime.root / name).exists()


def test_a_failing_command_stops_the_ones_after_it(client, runtime, publish):
    publish(
        config(
            onCreateCommand="exit 1",
            postCreateCommand="echo after >> ran.txt",
            postStartCommand="echo start >> ran.txt",
        )
    )
    failed = cloned(client)
    assert failed["state"] == "failed" and "onCreateCommand" in failed["error"]
    assert not (work_of(runtime, failed) / "ran.txt").exists()


def test_a_command_that_takes_too_long_is_stopped_and_the_message_names_no_limit(
    client, runtime, publish, monkeypatch
):
    monkeypatch.setattr(ws, "HOOK_TIMEOUT_SECONDS", 1)
    publish(config(postCreateCommand="echo started; sleep 30"))
    began = time.monotonic()
    failed = cloned(client)
    assert time.monotonic() - began < 25
    assert failed["state"] == "failed"
    assert (
        problems.DEVCONTAINER_MESSAGES["hook_timeout"].format(command="postCreateCommand")
        in failed["error"]
    )
    assert "started" in failed["error"]
    assert "took too long" in failed["error"] and "1 second" not in failed["error"]


def test_the_token_is_redacted_from_what_a_command_printed(client, runtime, publish, settings):
    save_token(client)
    publish(config(postCreateCommand=f"echo token is {TOKEN}; exit 2"))
    failed = cloned(client)
    assert failed["state"] == "failed" and "exit code 2" in failed["error"]
    shown = failed["error"] + (failed["log"] or "") + (row(settings, failed["id"])["log"] or "")
    assert TOKEN not in shown and ws.REDACTED in failed["error"]
    for entry_ in client.get("/api/workspaces", headers=as_user(ALICE)).json()["workspaces"]:
        assert TOKEN not in json.dumps(entry_)


def test_a_commands_output_tail_is_in_the_log_and_the_token_is_redacted_there_too(
    client, runtime, publish, ready_log
):
    save_token(client)
    publish(config(postCreateCommand=f"echo installed-things; echo {TOKEN}"))
    done = cloned(client)
    log = ready_log[done["id"]]
    assert "installed-things" in log and TOKEN not in log and ws.REDACTED in log
    assert "postCreateCommand" in log


def test_the_error_of_a_command_is_cut_to_the_limit_from_its_start(client, runtime, publish):
    publish(
        config(
            postCreateCommand=(
                "i=0; while [ $i -lt 400 ]; do echo line-$i-xxxxxxxxxxxx; i=$((i+1)); done; exit 4"
            )
        )
    )
    failed = cloned(client)
    assert len(failed["error"]) <= ws.MAX_ERROR_CHARS
    assert "exit code 4" in failed["error"] and "line-399-" in failed["error"]
    assert "line-0-" not in failed["error"]


# --- cancelling --------------------------------------------------------------------------------


def test_deleting_a_workspace_whose_command_runs_stops_it_by_removing_the_container(
    client, runtime, publish
):
    runtime.gate = threading.Event()
    publish(config(postCreateCommand="echo GATE"))
    made = clone(client).json()["workspace"]
    name = rt.container_name(made["id"])
    assert runtime.hooking.wait(MADE_WITHIN)
    removed_before = [e for e in runtime.events if e == ("remove", name)]
    assert removed_before == []  # positive control: nothing has removed it yet
    began = time.monotonic()
    r = client.delete(f"/api/workspaces/{made['id']}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert ("remove", name) in runtime.events  # the delete itself did it
    assert time.monotonic() - began < 15
    cleared(runtime, client)
    assert not (runtime.root / name).exists()


def test_deleting_a_workspace_whose_image_is_being_pulled_stops_the_pull(client, runtime, publish):
    runtime.pull_gate = threading.Event()
    publish(config())
    made = clone(client).json()["workspace"]
    name = rt.container_name(made["id"])
    assert runtime.pulling.wait(MADE_WITHIN)
    began = time.monotonic()
    r = client.delete(f"/api/workspaces/{made['id']}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    cleared(runtime, client)
    assert time.monotonic() - began < 15
    assert not (runtime.root / name).exists()
    assert runtime.pulled == []  # the pull did not finish
    assert not [e for e in runtime.events if e[0] == "create" and e[1] == IMAGE]


def test_deleting_a_workspace_before_the_file_is_read_makes_nothing_of_it(
    client, runtime, publish, monkeypatch
):
    publish(config())
    gate = threading.Event()
    real = ws.Workspaces._configure

    reached = threading.Event()

    def held(self, workspace, slot):
        reached.set()
        gate.wait(MADE_WITHIN)
        return real(self, workspace, slot)

    monkeypatch.setattr(ws.Workspaces, "_configure", held)
    try:
        made = clone(client).json()["workspace"]
        assert reached.wait(MADE_WITHIN)  # the clone is done
        client.delete(f"/api/workspaces/{made['id']}", headers=changing(ALICE))
        gate.set()
        cleared(runtime, client)
    finally:
        gate.set()
    assert runtime.pulled == []


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


def ready_with_plan(
    service, settings, runtime, plan: dc.Plan, make_container=True, name="planned"
) -> int:
    workspace = insert(settings, name, "ready", URL)
    set_environment(settings, workspace, plan.to_json())
    if make_container:
        service.runtime.create(service._spec(service.get(workspace), 1))
    return workspace


def test_a_lost_container_is_made_again_from_the_stored_plan_and_not_the_default_image(
    service, settings, runtime
):
    plan = a_plan(remoteUser="dev", containerEnv={"A": "b"})
    workspace = ready_with_plan(service, settings, runtime, plan, make_container=False)
    service.restore()
    name = rt.container_name(workspace)
    assert runtime.running(name)
    spec = runtime.specs[name]
    assert (spec.image, spec.user, spec.uid, spec.gid) == (IMAGE, "dev", 1234, 1235)
    assert dict(spec.env) == {"A": "b"}
    assert row(settings, workspace)["state"] == "ready"
    assert runtime.pulled == [IMAGE]  # pulled first: the image may be gone from the storage


def test_a_restore_does_not_run_the_create_commands_again(service, settings, runtime):
    plan = a_plan(
        onCreateCommand="echo create >> ran.txt",
        postCreateCommand="echo post >> ran.txt",
        postStartCommand="echo start >> ran.txt",
    )
    workspace = ready_with_plan(service, settings, runtime, plan, make_container=False)
    service.restore()
    work = Path(runtime.layout(rt.container_name(workspace)).work)
    assert (work / "ran.txt").read_text().split() == ["start"]  # only what runs at every start
    service.restore()  # rcman restarts again: the container runs, so nothing is started
    assert (work / "ran.txt").read_text().split() == ["start"]


def test_post_start_runs_after_every_start_of_a_container_that_was_stopped(
    service, settings, runtime
):
    plan = a_plan(
        postStartCommand="echo start >> ran.txt", onCreateCommand="echo create >> ran.txt"
    )
    workspace = ready_with_plan(service, settings, runtime, plan)
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


def test_post_start_runs_before_the_server_is_started(service, settings, runtime, monkeypatch):
    plan = a_plan(postStartCommand="echo start >> ran.txt")
    workspace = ready_with_plan(service, settings, runtime, plan)
    name = rt.container_name(workspace)
    runtime.stop(name)
    work = Path(runtime.layout(name).work)
    set_environment(settings, workspace, plan.to_json(), wanted=1)
    started = []
    monkeypatch.setattr(
        service, "start_server", lambda w, prefix, **k: started.append((work / "ran.txt").exists())
    )
    service.restore()
    assert started == [True]  # the server was started, and the hook had run by then


def test_a_post_start_that_fails_is_logged_and_the_workspace_stays_ready(
    service, settings, runtime, caplog
):
    plan = a_plan(postStartCommand="echo broke-badly; exit 5")
    workspace = ready_with_plan(service, settings, runtime, plan)
    runtime.stop(rt.container_name(workspace))
    with caplog.at_level("WARNING", logger="rcman.workspaces"):
        service.restore()
    assert (
        row(settings, workspace)["state"] == "ready" and row(settings, workspace)["error"] is None
    )
    assert runtime.running(rt.container_name(workspace))
    assert "postStartCommand" in caplog.text and "5" in caplog.text


def test_a_running_container_is_left_alone_by_a_restore(service, settings, runtime):
    plan = a_plan(postStartCommand="echo start >> ran.txt")
    ready_with_plan(service, settings, runtime, plan)
    before = len(runtime.execs)
    service.restore()
    assert runtime.pulled == []  # a container that is there needs no image
    assert not [a for _, a, _ in runtime.execs[before:] if "ran.txt" in " ".join(a)]
    assert [e for e in runtime.events if e[0] == "create"] == [("create", IMAGE, "root")]


def test_a_workspace_made_from_a_clone_that_rcman_restarted_in_is_failed_in_generic_words(
    service, settings, runtime
):
    cloning = insert(settings, "cloning", "creating", URL, "Making the workspace")
    runtime.create(rt.Spec(rt.container_name(cloning), slot=1, workspace=cloning, person=1))
    service.restore()
    now = row(settings, cloning)
    assert now["state"] == "failed"
    assert now["error"] == problems.CLONE_MESSAGES["restarted"]
    assert "while this workspace was being made" in now["error"]
    assert runtime.hooks_run() == [] and runtime.pulled == []


def test_a_damaged_plan_is_not_replaced_by_the_default_image(service, settings, runtime):
    workspace = insert(settings, "damaged", "ready", URL)
    set_environment(settings, workspace, "not json")
    service.restore()
    assert not runtime.exists(rt.container_name(workspace))  # nothing was made from a guess
    with pytest.raises(rt.RuntimeFailed):
        service._layout(service.get(workspace))


def test_the_orphan_probe_containers_of_an_earlier_rcman_are_cleaned_when_restoring(
    service, runtime, monkeypatch
):
    cleaned = []
    monkeypatch.setattr(runtime, "clean_probes", lambda: cleaned.append(True))
    service.restore()
    assert cleaned == [True]


def test_a_container_no_row_is_for_is_discarded_and_its_volume_is_kept(service, settings, runtime):
    insert(settings, "has-a-row", "ready", URL)
    name = rt.container_name(999)
    runtime.create(rt.Spec(name, slot=1, workspace=999, person=1))
    (runtime.root / name / rt.WORK / "notes.txt").write_text("someone's work")
    service.restore()
    assert ("discard", name) in runtime.events and ("remove", name) not in runtime.events
    assert not runtime.exists(name) and name not in runtime.labelled()
    assert (runtime.root / name / rt.WORK / "notes.txt").read_text() == "someone's work"


def test_nothing_is_removed_when_the_table_is_empty_and_workspaces_are_there(
    service, settings, runtime, caplog
):
    """A database that was lost, or is not the one these containers were made for, must not
    take every workspace and its files with it."""
    name = rt.container_name(5)
    runtime.create(rt.Spec(name, slot=1, workspace=5, person=1))
    runtime.builder_ids.add(5)
    runtime.built[5] = rt.built_name(5)
    runtime.images[rt.built_name(5)] = rt.ImageFacts()
    with caplog.at_level("ERROR", logger="rcman.workspaces"):
        service.restore()
    assert runtime.exists(name) and name in runtime.labelled()
    assert [e for e in runtime.events if e[0] in ("remove", "discard")] == []
    assert runtime.builders() == {5} and runtime.built_images() == {5: rt.built_name(5)}
    said = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("no workspaces" in line and name in line for line in said), said


def test_the_control_for_the_empty_table_is_that_a_row_makes_the_orphans_go(
    service, settings, runtime
):
    insert(settings, "has-a-row", "ready", URL)
    name = rt.container_name(5)
    runtime.create(rt.Spec(name, slot=1, workspace=5, person=1))
    service.restore()
    assert ("discard", name) in runtime.events


def test_the_database_column_exists_and_is_null_for_old_rows(settings):
    from rcman import db

    con = db.connect(settings.database)
    try:
        columns = [r["name"] for r in con.execute("pragma table_info(workspaces)")]
    finally:
        con.close()
    assert "environment" in columns


# --- a restore does not wait for one workspace's slow step ----------------------------------


def lost_with_hook(service, settings, runtime, name="slow", command="echo GATE", wanted=True):
    """A ready workspace whose container is gone, whose plan has a postStartCommand."""
    workspace = insert(settings, name, "ready", URL)
    set_environment(
        settings, workspace, a_plan(postStartCommand=command).to_json(), wanted=int(wanted)
    )
    return workspace


def restoring(service) -> threading.Thread:
    thread = threading.Thread(target=service.restore, daemon=True)
    thread.start()
    return thread


def test_a_start_command_that_hangs_holds_up_neither_other_workspaces_nor_its_own_lock(
    service, settings, runtime, monkeypatch
):
    runtime.gate = threading.Event()
    slow = lost_with_hook(service, settings, runtime)
    quick = insert(settings, "quick", "ready", None)
    set_environment(settings, quick, None, wanted=1)
    started = []
    monkeypatch.setattr(service, "start_server", lambda w, prefix, **k: started.append(w.id))
    thread = restoring(service)
    try:
        assert runtime.hooking.wait(MADE_WITHIN)
        deadline = time.monotonic() + MADE_WITHIN
        while quick not in started:  # the other workspace's server came back meanwhile
            assert time.monotonic() < deadline
            time.sleep(0.02)
        assert slow not in started and thread.is_alive()  # positive control: it is still held
        lock = service.lock(slow)
        assert lock.acquire(blocking=False)  # nor does it hold its own workspace's lock
        lock.release()
        assert runtime.running(rt.container_name(slow))  # its container was made already
    finally:
        runtime.gate.set()
    thread.join(MADE_WITHIN)
    assert not thread.is_alive()
    assert started == [quick, slow]  # its server came after its command, once that ended


def test_closing_while_a_start_command_hangs_ends_the_restore_at_once(service, settings, runtime):
    runtime.gate = threading.Event()
    lost_with_hook(service, settings, runtime)
    thread = restoring(service)
    try:
        assert runtime.hooking.wait(MADE_WITHIN)
        service._closing.set()
        thread.join(15)
        assert not thread.is_alive()
        assert service.restored.is_set()
    finally:
        runtime.gate.set()


def test_deleting_a_workspace_whose_start_command_hangs_in_a_restore_stops_it(
    service, settings, runtime
):
    runtime.gate = threading.Event()
    slow = lost_with_hook(service, settings, runtime)
    thread = restoring(service)
    try:
        assert runtime.hooking.wait(MADE_WITHIN)
        began = time.monotonic()
        service.delete(service.get(slow))  # does not wait for the command
        assert time.monotonic() - began < 15
    finally:
        runtime.gate.set()
    thread.join(MADE_WITHIN)
    assert not thread.is_alive()
    assert row(settings, slow) is None
    assert rt.container_name(slow) not in runtime.labelled()  # nothing is left of it


def test_no_more_workspaces_are_restored_at_once_than_the_bound_allows(
    service, settings, runtime, monkeypatch
):
    monkeypatch.setattr(ws, "RESTORE_WORKERS", 2)
    runtime.gate = threading.Event()
    for number in range(4):
        lost_with_hook(service, settings, runtime, name=f"slow{number}", wanted=False)
    thread = restoring(service)
    try:
        deadline = time.monotonic() + MADE_WITHIN
        while runtime.concurrent < 2:
            assert time.monotonic() < deadline
            time.sleep(0.02)
        time.sleep(0.5)  # long enough for a third to start, if one could
        assert runtime.most_at_once == 2
    finally:
        runtime.gate.set()
    thread.join(MADE_WITHIN)
    assert not thread.is_alive() and runtime.most_at_once == 2
    assert len([e for e in runtime.events if e[0] == "create"]) == 4


def test_a_lost_container_whose_image_is_gone_fails_the_workspace_in_plain_words(
    service, settings, runtime
):
    workspace = ready_with_plan(service, settings, runtime, a_plan(), make_container=False)
    del runtime.images[IMAGE]
    service.restore()
    now = row(settings, workspace)
    assert now["state"] == "failed"
    assert now["error"] == problems.DEVCONTAINER_MESSAGES["image_gone"].format(image=IMAGE)
    assert "delete the workspace" in now["error"].lower()
    assert "make it again" in now["error"].lower()
    assert not runtime.exists(rt.container_name(workspace))
    service.restore()  # a second restart leaves it as it is, failing nothing again
    assert row(settings, workspace)["error"] == now["error"]
    service.delete(service.get(workspace))
    assert row(settings, workspace) is None


def test_the_host_runtime_cannot_make_a_container_from_an_image_it_does_not_have(runtime):
    spec = rt.Spec("rcman-ws-1", slot=1, workspace=1, person=1, image="not/planted:1")
    with pytest.raises(rt.RuntimeFailed):
        runtime.create(spec)
    assert not runtime.exists("rcman-ws-1")


def test_a_lost_container_is_made_again_when_its_image_was_pulled_first(service, settings, runtime):
    workspace = ready_with_plan(service, settings, runtime, a_plan(), make_container=False)
    service.restore()
    name = rt.container_name(workspace)
    order = [e[0] for e in runtime.events if e[0] in ("pull", "create")]
    assert order == ["pull", "create"] and runtime.running(name)


# --- the plan pins the image ------------------------------------------------------------------

ID_A, ID_B = "a" * 64, "b" * 64
DIGEST = "sha256:" + "c" * 64
REPOSITORY = "registry.example.com/team/dev"


def pinned_plan(**keys) -> dc.Plan:
    facts = rt.ImageFacts(
        passwd=PASSWD, group=GROUP, env={"PATH": "/image/bin:/bin"}, id=ID_A, digest=DIGEST
    )
    return dc.plan({**GOOD, **keys}, facts, 1, "/rcman/work")


def test_a_plan_keeps_the_id_and_digest_of_the_image_it_was_made_from():
    made = pinned_plan()
    assert (made.image_id, made.image_digest) == (ID_A, DIGEST)
    again = dc.Plan.from_json(made.to_json())
    assert (again.image_id, again.image_digest) == (ID_A, DIGEST) and again == made


def test_a_lost_container_is_made_again_from_the_pinned_id_without_a_pull(
    service, settings, runtime
):
    runtime.images[IMAGE] = rt.ImageFacts(passwd=PASSWD, group=GROUP, id=ID_B)  # the tag moved on
    runtime.extra_ids.add(ID_A)  # and the image it was made from is still in the storage
    workspace = ready_with_plan(service, settings, runtime, pinned_plan(), make_container=False)
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
    workspace = ready_with_plan(service, settings, runtime, pinned_plan(), make_container=False)
    service.restore()
    assert runtime.pulled == [f"{REPOSITORY}@{DIGEST}"]
    assert runtime.specs[rt.container_name(workspace)].image_id == ID_A
    assert row(settings, workspace)["state"] == "ready"


def test_an_image_gone_from_the_storage_with_no_digest_fails_the_workspace_in_plain_words(
    service, settings, runtime
):
    facts = rt.ImageFacts(passwd=PASSWD, group=GROUP, env={"PATH": "/image/bin:/bin"}, id=ID_A)
    plan = dc.plan(GOOD, facts, 1, "/rcman/work")
    workspace = ready_with_plan(service, settings, runtime, plan, make_container=False)
    runtime.images.pop(IMAGE)
    service.restore()
    now = row(settings, workspace)
    assert now["state"] == "failed"
    assert now["error"] == problems.DEVCONTAINER_MESSAGES["image_gone"].format(image=IMAGE)
    assert runtime.pulled == []


def test_a_digest_that_gives_another_image_than_the_pinned_one_fails_the_workspace(
    service, settings, runtime
):
    runtime.images.pop(IMAGE)
    runtime.images[f"{REPOSITORY}@{DIGEST}"] = rt.ImageFacts(passwd=PASSWD, group=GROUP, id=ID_B)
    runtime.lost_ids.add(ID_A)  # and the pull does not bring it back: it brings another image
    workspace = ready_with_plan(service, settings, runtime, pinned_plan(), make_container=False)
    service.restore()
    now = row(settings, workspace)
    assert now["state"] == "failed" and now["error"].startswith('The image "')


def test_a_built_image_whose_id_is_gone_fails_the_workspace_though_its_name_is_there(
    service, settings, runtime
):
    workspace = insert(settings, "built", "ready", URL)
    facts = rt.ImageFacts(passwd=PASSWD, group=GROUP, env={"PATH": "/b:/bin"}, id=ID_A)
    plan = dc.plan(GOOD, facts, workspace, "/rcman/work", image=rt.built_name(workspace))
    set_environment(settings, workspace, plan.to_json())
    runtime.images[rt.built_name(workspace)] = rt.ImageFacts(passwd=PASSWD, group=GROUP, id=ID_B)
    runtime.built[workspace] = rt.built_name(workspace)  # the name is there; the id is not
    service.restore()
    now = row(settings, workspace)
    assert (
        now["state"] == "failed"
        and now["error"] == problems.DEVCONTAINER_MESSAGES["built_image_gone"]
    )


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


def test_a_lost_default_container_whose_pinned_image_is_gone_fails_the_workspace(
    service, settings, runtime
):
    workspace = insert(settings, "default", "ready", None)
    set_environment(settings, workspace, None, image_id=ID_A)
    runtime.default_id = ID_B
    runtime.lost_ids.add(ID_A)
    service.restore()
    now = row(settings, workspace)
    assert now["state"] == "failed"
    assert now["error"] == problems.DEVCONTAINER_MESSAGES["default_image_gone"]
    assert not runtime.exists(rt.container_name(workspace))


def test_a_default_workspace_with_no_pinned_image_is_pinned_to_the_default_when_it_is_remade(
    service, settings, runtime
):
    workspace = insert(settings, "old", "ready", None)
    runtime.default_id = ID_B
    service.restore()
    assert runtime.specs[rt.container_name(workspace)].image_id == ID_B
    assert row(settings, workspace)["image_id"] == ID_B


def test_workspaces_of_a_database_that_was_restored_are_not_made_on_the_volumes_of_the_old(
    service, settings, runtime
):
    workspace = insert(settings, "restored", "ready", None)
    service.restore()
    name = rt.container_name(workspace)
    assert runtime.exists(name)
    assert runtime.specs[name].install  # the spec holds the database's install id
    runtime.discard(name)
    con = db.connect(settings.database)
    try:
        con.execute("update meta set install = 'the-install-of-a-backup'")
        con.commit()
    finally:
        con.close()
    with pytest.raises(rt.RuntimeFailed) as refused:
        service._remake(service.get(workspace), threading.Event())
    assert "another" in str(refused.value) and not runtime.exists(name)


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
    named = ready_with_plan(service, settings, runtime, a_plan(), name="named")  # names IMAGE
    pinned = insert(settings, "pinned", "ready", None)  # made from the default image of its day
    set_environment(settings, pinned, None, image_id=runtime.images[pinned_name].id)
    runtime.images[moved_name] = rt.ImageFacts(id=ID_A)  # the plan's id, under another name
    runtime.collectable.add(moved_name)
    moved = ready_with_plan(service, settings, runtime, pinned_plan(), name="moved")
    service.restore()
    assert STRAY not in runtime.images and OLD_DEFAULT not in runtime.images
    assert IMAGE in runtime.images  # a stored plan names it
    assert pinned_name in runtime.images  # a workspace is pinned to its id
    assert moved_name in runtime.images  # a stored plan holds its id
    assert runtime.exists(rt.container_name(named)) and moved


def test_an_image_a_container_is_made_from_is_not_collected(service, settings, runtime):
    stray_images(runtime, STRAY)
    runtime.create(rt.Spec("rcman-ws-9", slot=1, workspace=9, person=1, image=STRAY))
    insert(settings, "has-a-row", "ready", None)
    service._collect_images()
    assert STRAY in runtime.images  # a container is made from it
    runtime.remove("rcman-ws-9")
    service._collect_images()
    assert STRAY not in runtime.images  # the control: with the container gone it goes


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


def test_nothing_is_collected_while_a_workspace_is_being_made_or_a_plan_cannot_be_read(
    service, settings, runtime
):
    stray_images(runtime, STRAY)
    making = insert(settings, "making", "creating", None)
    service._collect_images()
    assert STRAY in runtime.images  # its image may be pulled and not yet in its plan
    con = db.connect(settings.database)
    con.execute("update workspaces set state = 'ready' where id = ?", (making,))
    con.commit()
    con.close()
    set_environment(settings, making, "not json")
    service._collect_images()
    assert STRAY in runtime.images  # what it is made from is not known
    set_environment(settings, making, None)
    service._collect_images()
    assert STRAY not in runtime.images  # the control: with nothing in the way it goes


def test_a_workspace_is_not_recorded_while_images_are_being_collected(service, settings, runtime):
    """The collection reads which workspaces are being made, then removes images; a workspace
    recorded between the two would lose the image it is about to be made from."""
    stray_images(runtime, STRAY)
    inside, release = threading.Event(), threading.Event()
    real = runtime.collect_images

    def slow(keep_ids, keep_names):
        inside.set()
        assert release.wait(MADE_WITHIN)
        return real(keep_ids, keep_names)

    runtime.collect_images = slow
    collecting = threading.Thread(target=service._collect_images, daemon=True)
    collecting.start()
    assert inside.wait(MADE_WITHIN)
    con = db.connect(settings.database)
    try:
        alice = people.by_email(con, ALICE)
    finally:
        con.close()
    made = []
    creating = threading.Thread(
        target=lambda: made.append(service.create(alice, "late")), daemon=True
    )
    creating.start()
    creating.join(1.0)
    assert creating.is_alive() and service.everything() == []  # held back: no row yet
    release.set()
    collecting.join(MADE_WITHIN)
    creating.join(MADE_WITHIN)
    assert made and made[0].name == "late" and STRAY not in runtime.images  # then it goes on


def test_the_shell_does_not_collect_images_the_service_collects_at_its_next_opportunity(
    settings, runtime
):
    stray_images(runtime, STRAY)
    quiet = ws.Workspaces(settings, runtime, prunes=False)
    try:
        quiet._collect_images()
        assert STRAY in runtime.images
    finally:
        quiet.close()
    loud = ws.Workspaces(settings, runtime)
    try:
        loud._collect_images()
        assert STRAY not in runtime.images  # the control
    finally:
        loud.close()


def test_nothing_is_collected_on_the_word_of_an_empty_table(service, settings, runtime):
    stray_images(runtime, STRAY)
    runtime.create(rt.Spec("rcman-ws-5", slot=1, workspace=5, person=1))
    service.restore()
    assert STRAY in runtime.images


def test_a_collection_that_fails_is_logged_and_fails_nothing(service, settings, runtime, caplog):
    insert(settings, "has-a-row", "ready", None)

    def broken(keep_ids, keep_names):
        raise rt.RuntimeFailed("planted")

    runtime.collect_images = broken
    with caplog.at_level("WARNING", logger="rcman.workspaces"):
        service.restore()
    assert "images" in caplog.text and service.restored.is_set()


# --- a delete that lands between the steps of making --------------------------------------


def delete_the_first_workspace(app):
    service = app.state.workspaces
    return lambda: service.delete(service.get(1))


def test_a_delete_after_the_image_is_read_stops_before_the_container_is_discarded(
    client, app, runtime, publish
):
    runtime.after_facts = delete_the_first_workspace(app)
    publish(config())
    made = clone(client).json()["workspace"]
    cleared(runtime, client)
    assert made["id"] == 1
    assert runtime.pulled == [IMAGE]  # positive control: it got as far as reading the image
    assert not [e for e in runtime.events if e[0] == "discard"]
    assert [e[0] for e in runtime.events if e[0] == "create"] == ["create"]  # the first only


def test_a_delete_after_the_container_is_discarded_stops_before_it_is_made_again(
    client, app, runtime, publish
):
    runtime.after_discard = delete_the_first_workspace(app)
    publish(config())
    clone(client)
    cleared(runtime, client)
    assert [e[0] for e in runtime.events if e[0] == "discard"] == ["discard"]  # control
    assert [e[0] for e in runtime.events if e[0] == "create"] == ["create"]


def test_a_container_that_cannot_be_run_fails_the_workspace_and_leaves_nothing(
    client, runtime, publish
):
    runtime.fail_image_create = True
    publish(config())
    failed = cloned(client)
    assert failed["state"] == "failed"
    assert IMAGE in failed["error"] and "cannot run the image" in failed["error"]
    assert ("discard", name_of(failed)) in runtime.events  # it had got that far
    no_trace(runtime, failed)


# --- bounded memory ------------------------------------------------------------------------


def test_only_the_tail_of_what_a_command_printed_is_ever_held(
    client, runtime, publish, monkeypatch
):
    monkeypatch.setattr(ws, "HOOK_KEEP_BYTES", 3000)
    publish(
        config(
            postCreateCommand=(
                "i=0; while [ $i -lt 20000 ]; do echo line-$i-xxxxxxxxxxx; i=$((i+1)); done; exit 4"
            )
        )
    )
    failed = cloned(client)
    assert runtime.kept == 3000
    (answer,) = runtime.answers
    assert len(answer.out) <= 3000 and len(answer.err) <= 3000
    assert b"line-19999-" in answer.out and b"line-0-" not in answer.out  # the tail, not the head
    assert "line-19999-" in failed["error"]


# --- a file that raised something that was not a refusal ---------------------------------


def nested(depth: int) -> str:
    return "[" * depth + "]" * depth


def file_with(**keys) -> str:
    return json.dumps({"image": IMAGE, **keys})


NOT_REFUSALS = [
    ('{"image": "' + IMAGE + '", "appPort": ' + "9" * 5000 + "}", "number"),
    ('{"image": "' + IMAGE + '", "forwardPorts": ' + nested(500) + "}", "deeply"),
    ('{"image": "' + IMAGE + '", "forwardPorts": ' + nested(5000) + "}", "deeply"),
    (file_with(remoteUser="\u00b2"), "user"),
    (file_with(remoteUser="dev:\u0663"), "user"),
]


@pytest.mark.parametrize(("text", "word"), NOT_REFUSALS)
def test_a_file_that_would_raise_something_else_is_refused_and_nothing_is_left(
    client, runtime, publish, text, word
):
    publish({FILE: text})
    failed = cloned(client)
    assert failed["state"] == "failed"
    assert word in failed["error"] and "Traceback" not in failed["error"]
    assert "Error" not in failed["error"]  # not an exception's name
    no_trace(runtime, failed)


# --- an image that cannot be had now is not an image that is gone ---------------------------

GONE = [
    "Error: initializing source docker://registry.example.com/team/dev:latest: reading manifest "
    "latest in registry.example.com/team/dev: manifest unknown",
    "Error: name unknown: repository name not known to registry",
    "Error: requested access to the resource is denied",
    "Error: unauthorized: authentication required",
    "Error: determining manifest MIME type: 404 Not Found: repository not found",
    "Error: initializing source: pinging container registry: not found",
]
TRANSIENT = [
    "Error: pinging container registry registry.example.com: Get https://registry.example.com/v2/: "
    "dial tcp: lookup registry.example.com: no such host",
    "Error: pinging container registry: i/o timeout",
    "Error: received unexpected HTTP status: 503 Service Unavailable",
    "Error: initializing source: net/http: TLS handshake timeout",
    "Error: unexpected EOF",
    "podman pull did not finish in time",
    "the pull was stopped",
]


@pytest.mark.parametrize("said", GONE)
def test_a_registry_that_says_the_image_is_not_there_fails_the_workspace(
    service, settings, runtime, said
):
    workspace = ready_with_plan(service, settings, runtime, a_plan(), make_container=False)
    runtime.pull_error = said
    service.restore()
    now = row(settings, workspace)
    assert now["state"] == "failed"
    assert now["error"] == problems.DEVCONTAINER_MESSAGES["image_gone"].format(image=IMAGE)
    assert not runtime.exists(rt.container_name(workspace))


@pytest.mark.parametrize("said", TRANSIENT)
def test_a_pull_that_fails_for_any_other_reason_leaves_the_workspace_ready_to_try_again(
    service, settings, runtime, said, caplog
):
    workspace = ready_with_plan(service, settings, runtime, a_plan(), make_container=False)
    name = rt.container_name(workspace)
    runtime.pull_error = said
    with caplog.at_level("WARNING", logger="rcman.workspaces"):
        service.restore()
    now = row(settings, workspace)
    assert (now["state"], now["error"]) == ("ready", None)
    assert not runtime.exists(name)  # nothing was made of a guess
    assert str(workspace) in caplog.text  # and it was said
    runtime.pull_error = None  # the registry is back
    service.restore()
    assert runtime.running(name) and row(settings, workspace)["state"] == "ready"
    assert runtime.specs[name].image == IMAGE


def test_the_words_that_mean_the_image_is_gone_are_not_found_in_the_ones_that_do_not():
    for said in GONE:
        assert ws.image_is_gone(said), said
    for said in TRANSIENT:
        assert not ws.image_is_gone(said), said
    assert ws.image_is_gone("MANIFEST UNKNOWN")  # whatever the case


# --- the races of making a container again ------------------------------------------------


def lost_with_plan(service, settings, name="lost", **keys):
    return ready_with_plan(
        service, settings, service.runtime, a_plan(**keys), make_container=False, name=name
    )


def test_a_delete_during_the_pull_of_a_restore_stops_it_and_makes_nothing(
    service, settings, runtime
):
    runtime.pull_gate = threading.Event()
    workspace = lost_with_plan(service, settings)
    thread = restoring(service)
    try:
        assert runtime.pulling.wait(MADE_WITHIN)
        lock = service.lock(workspace)  # the pull holds nothing
        assert lock.acquire(blocking=False)
        lock.release()
        began = time.monotonic()
        service.delete(service.get(workspace))
        assert time.monotonic() - began < 15
    finally:
        thread.join(MADE_WITHIN)
    assert not thread.is_alive()
    assert row(settings, workspace) is None
    assert not [e for e in runtime.events if e[0] == "create"]
    assert runtime.labelled() == {}


def test_a_delete_after_the_pull_and_before_the_container_is_made_makes_none(
    service, settings, runtime
):
    workspace = lost_with_plan(service, settings)
    runtime.after_pull = lambda: service.delete(service.get(workspace))
    service.restore()
    assert runtime.pulled == [IMAGE]  # positive control: it got as far as the pull
    assert not [e for e in runtime.events if e[0] == "create"]
    assert row(settings, workspace) is None and runtime.labelled() == {}


def test_a_delete_while_the_container_is_being_made_leaves_none_behind(service, settings, runtime):
    runtime.create_gate = threading.Event()
    workspace = lost_with_plan(service, settings)
    thread = restoring(service)
    try:
        assert runtime.creating.wait(MADE_WITHIN)
        lock = service.lock(workspace)
        assert lock.acquire(blocking=False)  # making it holds no lock
        lock.release()
        service.delete(service.get(workspace))  # the row goes; the container is not there yet
        assert row(settings, workspace) is None
    finally:
        runtime.create_gate.set()
    thread.join(MADE_WITHIN)
    assert not thread.is_alive()
    assert runtime.labelled() == {} and not runtime.exists(rt.container_name(workspace))
    assert [e for e in runtime.events if e[0] == "create"]  # positive control: it was made


def test_one_workspace_that_raises_leaves_the_others_restored_and_its_slot_free(
    service, settings, runtime, monkeypatch
):
    monkeypatch.setattr(ws, "RESTORE_WORKERS", 1)  # one slot: a slot not freed stops the rest
    damaged = insert(settings, "damaged", "ready", URL)
    set_environment(settings, damaged, "not json")
    broken = lost_with_plan(service, settings, "broken")
    real = Flow.create

    def create(self, spec):
        if spec.name == rt.container_name(broken):
            raise rt.RuntimeFailed("cannot make this one")
        return real(self, spec)

    monkeypatch.setattr(Flow, "create", create)
    fine = insert(settings, "fine", "ready", None)
    after = lost_with_plan(service, settings, "after")
    thread = restoring(service)
    thread.join(MADE_WITHIN)
    assert not thread.is_alive() and service.restored.is_set()
    assert runtime.running(rt.container_name(fine))
    assert runtime.running(rt.container_name(after))
    assert not runtime.exists(rt.container_name(damaged))
    assert not runtime.exists(rt.container_name(broken))
    assert row(settings, fine)["state"] == "ready"


def test_a_start_asked_for_during_a_restore_is_answered_and_not_held_up_by_it(
    service, settings, runtime
):
    runtime.pull_gate = threading.Event()
    workspace = lost_with_plan(service, settings)
    thread = restoring(service)
    answers = []
    try:
        assert runtime.pulling.wait(MADE_WITHIN)

        def ask():
            try:
                service.start_server(service.get(workspace), "prefix")
            except Exception as refused:  # noqa: BLE001 - whatever it is, it answered
                answers.append(refused)

        asking = threading.Thread(target=ask, daemon=True)
        asking.start()
        asking.join(15)
        assert not asking.is_alive()  # it neither waited for the pull nor deadlocked
        assert answers and getattr(answers[0], "code", None) == "claude_not_logged_in"
    finally:
        runtime.pull_gate.set()
        thread.join(MADE_WITHIN)
    assert not thread.is_alive()


def test_a_command_held_when_its_workspace_is_deleted_is_not_followed_by_the_next(
    service, settings, runtime
):
    """The command ends well whatever became of its workspace, so that only the checks of
    ``_run_commands`` stop the next one."""
    runtime.gate = threading.Event()
    runtime.release_ok = True
    workspace = insert(settings, "twice", "ready", URL)
    plan = a_plan(postStartCommand={"first": "echo GATE", "second": "echo second >> ran.txt"})
    set_environment(settings, workspace, plan.to_json())
    thread = restoring(service)
    try:
        assert runtime.hooking.wait(MADE_WITHIN)
        assert len(runtime.hooks_run()) == 1  # positive control: the first is the one held
        service.delete(service.get(workspace))
    finally:
        runtime.gate.set()
    thread.join(MADE_WITHIN)
    assert not thread.is_alive()
    assert len(runtime.hooks_run()) == 1  # the second never ran
    assert runtime.labelled() == {}


# --- building an image --------------------------------------------------------------------------

from tests.test_builder import (  # noqa: E402 - the recorded words of the builder
    DENIED,
    FEATURE_FAILED,
    NO_MANIFEST,
    NO_NETWORK,
    NO_SPACE,
    STEP_FAILED,
)


def nothing_of_a_build_is_left(runtime, entry_: dict) -> None:
    no_trace(runtime, entry_)
    assert runtime.builders() == set() and runtime.built_images() == {}


def test_a_dockerfile_is_built_and_the_workspace_is_made_from_the_image_built(
    client, runtime, publish, settings, ready_log
):
    publish(building(postCreateCommand="echo after-the-build", containerEnv={"A": "b"}))
    done = cloned(client)
    assert done["state"] == "ready", done
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


def test_what_the_builder_is_asked_is_the_build_only_configuration_of_the_workspace(
    client, runtime, publish, settings
):
    publish(building(postCreateCommand="echo secret-hook", remoteUser="dev", forwardPorts=[80]))
    done = cloned(client)
    (request,) = runtime.builds
    assert request.id == done["id"] and request.person == row(settings, done["id"])["person"]
    assert request.slot == people.BUILDER_SLOT and request.source == name_of(done)
    assert request.config_path == f".devcontainer/{dc.BUILD_CONFIG_NAME}"
    assert json.loads(request.config_text) == {"build": {"dockerfile": "Dockerfile"}}
    assert "secret-hook" not in request.config_text


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


def test_the_path_of_the_dotfile_is_where_its_build_only_file_goes(client, runtime, publish):
    publish({".devcontainer.json": json.dumps({"build": {"dockerfile": "Dockerfile"}}),
             "Dockerfile": "FROM ubuntu\n"})  # fmt: skip
    cloned(client)
    (request,) = runtime.builds
    assert request.config_path == dc.BUILD_CONFIG_NAME


def test_an_image_whose_label_asks_for_what_a_file_may_not_is_refused_naming_the_feature(
    client, runtime, publish, settings
):
    runtime.build_facts = rt.ImageFacts(
        labels={dc.METADATA_LABEL: json.dumps([{"id": "./feature", "privileged": True}])}
    )
    publish(featured())
    failed = cloned(client)
    assert failed["state"] == "failed"
    assert '"privileged"' in failed["error"] and "./feature" in failed["error"]
    nothing_of_a_build_is_left(runtime, failed)
    assert row(settings, failed["id"])["environment"] is None
    assert not [e for e in runtime.events if e[0] == "create" and e[1]]  # no container of it


FAILURES = [
    (FEATURE_FAILED, "build_feature_failed", {"feature": "./featurebad"}),
    (STEP_FAILED, "build_step_failed", {}),
    (NO_NETWORK, "build_registry_unreachable", {}),
    (NO_MANIFEST, "build_image_missing", {}),
    (DENIED, "build_sign_in", {}),
    (NO_SPACE, "build_no_space", {}),
    ("something nobody has heard of\n", "build_failed", {}),
]


@pytest.mark.parametrize(("said", "kind", "fields"), FAILURES)
def test_a_build_that_fails_gives_its_cause_in_plain_words_and_the_end_of_what_it_said(
    client, runtime, publish, settings, said, kind, fields
):
    runtime.build_error = rt.BuildFailed(tail=said)
    publish(building())
    failed = cloned(client)
    assert failed["state"] == "failed"
    head = problems.DEVCONTAINER_MESSAGES[kind].format(tail="", **fields).strip()
    assert failed["error"].startswith(head), failed["error"]
    assert said.strip().splitlines()[-1].split("] ")[-1].strip() in failed["error"]  # the tail
    assert len(failed["error"]) <= ws.MAX_ERROR_CHARS
    nothing_of_a_build_is_left(runtime, failed)
    assert row(settings, failed["id"])["environment"] is None


def test_the_name_of_a_feature_that_failed_is_shown_cleaned_and_cut(client, runtime, publish):
    name = "./f\u0007" + "y" * 500
    runtime.build_error = rt.BuildFailed(
        tail=f'ERROR: Feature "{name}" (Unknown) failed to install!\n'
    )
    publish(building())
    failed = cloned(client)
    assert "\u0007" not in failed["error"]
    assert failed["error"].startswith(
        problems.DEVCONTAINER_MESSAGES["build_feature_failed"].split("{feature}")[0]
    )
    assert "./f?" in failed["error"] and "yyyy" in failed["error"]
    assert len(failed["error"]) <= ws.MAX_ERROR_CHARS


@pytest.mark.parametrize("kind", ["build_timeout", "build_context_too_big", "build_image_too_big"])
def test_a_build_that_the_runtime_stopped_for_its_size_or_time_says_so(
    client, runtime, publish, kind
):
    runtime.build_error = rt.BuildFailed(tail="", kind=kind)
    publish(building())
    failed = cloned(client)
    assert failed["error"].strip() == problems.DEVCONTAINER_MESSAGES[kind].format(tail="").strip()
    nothing_of_a_build_is_left(runtime, failed)


def test_a_builder_that_could_not_be_run_is_a_failed_build_with_what_podman_said(
    client, runtime, publish
):
    runtime.build_error = rt.RuntimeFailed("localhost/rcman-builder: image not known")
    publish(building())
    failed = cloned(client)
    assert failed["state"] == "failed" and "image not known" in failed["error"]
    nothing_of_a_build_is_left(runtime, failed)


def test_the_token_is_redacted_from_a_failed_builds_tail_and_from_its_lines(
    client, runtime, publish, settings
):
    save_token(client)
    runtime.build_lines = [f"[1/1] STEP 1/2: RUN echo {TOKEN}"]
    runtime.build_error = rt.BuildFailed(tail=f'Error: building at STEP "RUN {TOKEN}"\n')
    publish(building())
    failed = cloned(client)
    shown = failed["error"] + (failed["log"] or "") + (row(settings, failed["id"])["log"] or "")
    assert TOKEN not in shown and ws.REDACTED in shown


def test_the_token_is_redacted_from_the_lines_of_a_build_that_succeeded(
    client, runtime, publish, ready_log
):
    save_token(client)
    runtime.build_lines = [f"[1/1] STEP 1/2: RUN echo {TOKEN}"]
    publish(building())
    done = cloned(client)
    assert done["state"] == "ready"
    assert TOKEN not in ready_log[done["id"]] and ws.REDACTED in ready_log[done["id"]]


def test_an_image_on_musl_that_was_built_is_refused_and_the_image_is_removed(
    client, runtime, publish
):
    runtime.libc = b"musl\n"
    publish(building())
    failed = cloned(client)
    assert failed["state"] == "failed" and "musl" in failed["error"]
    nothing_of_a_build_is_left(runtime, failed)


def test_a_create_command_that_fails_keeps_the_container_and_the_image_it_was_made_from(
    client, runtime, publish
):
    publish(building(postCreateCommand="exit 3"))
    failed = cloned(client)
    assert failed["state"] == "failed" and "exit code 3" in failed["error"]
    assert runtime.exists(name_of(failed))
    assert runtime.built_images() == {failed["id"]: rt.built_name(failed["id"])}


def test_deleting_a_workspace_that_is_being_built_removes_its_builder_and_stops_the_build(
    client, runtime, publish
):
    runtime.build_gate = threading.Event()
    publish(building())
    made = clone(client).json()["workspace"]
    assert runtime.building.wait(MADE_WITHIN)
    assert runtime.builders() == {made["id"]}  # the positive control
    began = time.monotonic()
    r = client.delete(f"/api/workspaces/{made['id']}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    assert ("remove_build", made["id"], False) in runtime.events  # the delete itself did it
    cleared(runtime, client)
    assert time.monotonic() - began < 15
    assert runtime.builders() == set() and runtime.built_images() == {}
    assert not [e for e in runtime.events if e[0] == "create" and e[1]]


def test_deleting_a_ready_workspace_removes_the_image_built_for_it(client, runtime, publish):
    publish(building())
    done = cloned(client)
    assert runtime.built_images() == {done["id"]: rt.built_name(done["id"])}  # the control
    r = client.delete(f"/api/workspaces/{done['id']}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    cleared(runtime, client)
    assert runtime.built_images() == {} and runtime.builders() == set()


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
    first = clone(client).json()["workspace"]["id"]
    assert runtime.building.wait(MADE_WITHIN)
    second = clone(client).json()["workspace"]["id"]
    deadline = time.monotonic() + MADE_WITHIN
    while not waiting_line(client, second):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert not waiting_line(client, first)  # the one that builds does not wait
    assert runtime.most_builds_at_once == 1 and len(runtime.builds) == 1
    runtime.build_gate.set()
    assert finished(client, first)["state"] == "ready"
    assert finished(client, second)["state"] == "ready"
    assert runtime.most_builds_at_once == 1 and len(runtime.builds) == 2


def test_deleting_a_workspace_that_waits_for_a_build_ends_its_wait_and_builds_nothing(
    one_build_at_a_time, client, runtime, publish
):
    runtime.build_gate = threading.Event()
    publish(building())
    first = clone(client).json()["workspace"]["id"]
    assert runtime.building.wait(MADE_WITHIN)
    second = clone(client).json()["workspace"]["id"]
    deadline = time.monotonic() + MADE_WITHIN
    while not waiting_line(client, second):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    r = client.delete(f"/api/workspaces/{second}", headers=changing(ALICE))
    assert r.status_code == 200, r.text
    deadline = time.monotonic() + MADE_WITHIN
    while any(w["id"] == second for w in listed(client)):
        assert time.monotonic() < deadline
        time.sleep(0.05)
    runtime.build_gate.set()
    assert finished(client, first)["state"] == "ready"
    assert [b.id for b in runtime.builds] == [first]


def test_the_bound_on_builds_is_a_place_each_build_gives_back_even_when_it_fails(
    one_build_at_a_time, client, runtime, publish
):
    runtime.build_error = rt.BuildFailed(tail="boom\n")
    publish(building())
    for _ in range(3):  # more failures than the bound has places
        assert cloned(client)["state"] == "failed"
    runtime.build_error = None
    assert cloned(client)["state"] == "ready"


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
    used = insert(settings, "used", "ready", URL)
    set_environment(settings, used, built_plan(used).to_json())
    plant_built(runtime, used)
    runtime.create(service._spec(service.get(used), 1, built_plan(used)))
    failed = insert(settings, "failed", "failed", URL)  # a plan that names no built image
    plant_built(runtime, failed)
    plant_built(runtime, 4242)  # no row at all
    runtime.builder_ids.update({used, 4243})  # a builder left from before, of a row and of none
    service.restore()
    assert runtime.builders() == set()
    assert runtime.built_images() == {used: rt.built_name(used)}  # only the used one stays
    assert runtime.running(rt.container_name(used))


def test_a_restart_leaves_the_builder_of_a_workspace_that_is_being_made(
    service, settings, runtime, monkeypatch
):
    making = insert(settings, "making", "creating", None)
    runtime.builder_ids.add(making)
    plant_built(runtime, making)
    monkeypatch.setattr(service, "_being_made", lambda workspace: workspace == making)
    service.restore()
    assert runtime.builders() == {making}
    assert runtime.built_images() == {making: rt.built_name(making)}


def test_a_restart_that_finds_a_lost_container_makes_it_again_from_the_image_without_a_pull(
    service, settings, runtime
):
    workspace = insert(settings, "built", "ready", URL)
    set_environment(settings, workspace, built_plan(workspace).to_json())
    plant_built(runtime, workspace)
    service.restore()
    assert runtime.running(rt.container_name(workspace))
    assert runtime.pulled == []  # a built image is in the storage or it is nowhere
    assert row(settings, workspace)["state"] == "ready"


def test_a_lost_container_whose_built_image_is_gone_fails_the_workspace_and_pulls_nothing(
    service, settings, runtime
):
    workspace = insert(settings, "built", "ready", URL)
    set_environment(settings, workspace, built_plan(workspace).to_json())
    runtime.images[rt.built_name(workspace)] = rt.ImageFacts()  # a pull would find this
    assert runtime.built_images() == {}  # the control: the storage holds no built image
    service.restore()
    now = row(settings, workspace)
    assert now["state"] == "failed"
    assert now["error"] == problems.DEVCONTAINER_MESSAGES["built_image_gone"]
    assert "delete the workspace" in now["error"].lower()
    assert runtime.pulled == [] and not runtime.exists(rt.container_name(workspace))
