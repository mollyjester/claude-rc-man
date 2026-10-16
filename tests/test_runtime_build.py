"""What the Podman runtime asks of podman to build an image (backend/rcman/runtime.py,
``PodmanRuntime.build_image``), against a stand-in for podman that keeps a little state and
answers each call the way the real one was seen to in the builder: its calls are in order,
the bytes pass through the pump, and whatever happens the builder, its volume and (on a
failure) the image are removed. The real thing is the on-demand suite's
(tests/container/test_runtime_in_podman.py)."""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import threading
import time
from pathlib import Path

import pytest
from rcman import builder, devcontainer, people
from rcman import runtime as rt

from tests.test_builder import CONFIG, FEATURE_FAILED, IMAGE_ID, NO_NETWORK, STEP_FAILED

STAND_IN = r"""#!/usr/bin/env python3
import hashlib, io, json, os, sys, tarfile, time
from pathlib import Path

STATE = Path(__STATE__)
scenario = json.loads((STATE / "scenario.json").read_text())
args = sys.argv[1:]
assert args[:2] == ["--remote", "--url"], args
words = args[3:]
with open(STATE / "calls", "a") as handle:
    handle.write(json.dumps(words) + "\n")


def mark(name, present=True):
    path = STATE / name
    if present:
        path.write_text("")
    else:
        path.unlink(missing_ok=True)


def exists(name):
    return (STATE / name).exists()


def drain():
    total = 0
    while chunk := sys.stdin.buffer.read(1 << 16):
        total += len(chunk)
    return total


def zeros(count):
    out = sys.stdout.buffer
    while count > 0:
        piece = min(count, 1 << 16)
        out.write(b"\0" * piece)
        count -= piece
    out.flush()


verb = words[0]
sub = words[1] if len(words) > 1 else ""
if verb == "volume":
    if sub == "create":
        mark("volume-" + words[-1])
    elif sub == "exists":
        sys.exit(0 if exists("volume-" + words[-1]) else 1)
    elif sub == "rm":
        mark("volume-" + words[-1], False)
    elif sub == "ls":
        found = [{"Name": p.name[7:], "Labels": {"rcman.builder": p.name[19:]}}
                 for p in STATE.glob("volume-rcman-build-*")]
        print(json.dumps(found))
elif verb == "run":
    name = next(w for w in words if w.startswith("--name=")).split("=", 1)[1]
    mark("builder-" + name)
    print("0123abcd")
elif verb == "rm":
    name = words[-1]
    if exists("builder-" + name):
        mark("builder-" + name, False)
        mark("removed-" + name)
elif verb == "ps":
    found = [{"Names": [p.name[8:]], "Labels": {"rcman.builder": p.name[20:]}}
             for p in STATE.glob("builder-rcman-build-*")]
    print(json.dumps(found))
elif verb == "images":
    found = [{"Names": [p.name[4:].replace("%", "/")]} for p in STATE.glob("tag-*")]
    print(json.dumps(found))
elif verb == "tag":
    if scenario.get("tag_fails"):
        print("Error: tag went wrong", file=sys.stderr)
        sys.exit(125)
    mark("tag-" + words[-1].replace("/", "%"))
elif verb == "image" and sub == "exists":
    sys.exit(0 if exists("image-" + words[-1]) else 1)
elif verb == "image" and sub == "inspect":
    if not exists("image-" + words[-1]):
        sys.exit(1)
    print("sha256:" + words[-1])
elif verb == "image" and sub == "rm":
    if exists("image-" + words[-1]):
        mark("image-" + words[-1], False)
    mark("tag-" + words[-1].replace("/", "%"), False)
elif verb == "load":
    assert words[1] == "-i", words
    path = Path(words[2])
    (STATE / "load-info").write_text(json.dumps({
        "size": path.stat().st_size,
        "dir_mode": path.parent.stat().st_mode & 0o777,
        "dir": str(path.parent),
    }))
    waited = 0.0
    while waited < scenario.get("load_seconds", 0):
        time.sleep(0.05)
        waited += 0.05
    if scenario.get("load_fails"):
        print("Error: load went wrong", file=sys.stderr)
        sys.exit(125)
    if not scenario.get("loads_under_another_id"):
        mark("image-" + scenario["image_id"])
    print("Loaded image: sha256:" + scenario["image_id"])
elif verb == "exec":
    rest = [w for w in words[1:] if w not in ("--interactive",) and not w.startswith("--workdir")]
    container, cmd = rest[0], rest[1:]
    if container.startswith("rcman-ws-"):
        zeros(scenario["repository_bytes"])
    else:
        gone = "removed-" + container
        if cmd[0] == "mkdir":
            pass
        elif cmd[0] == "tar":
            if scenario.get("tar_fails"):
                print(scenario["tar_fails"], file=sys.stderr)
                sys.exit(2)
            (STATE / "copied-in").write_text(str(drain()))
        elif cmd[0] == "sh":
            (STATE / "config-path").write_text(cmd[-1])
            (STATE / "config-text").write_text(sys.stdin.read())
        elif cmd[0] == "env":
            (STATE / "cli").write_text(json.dumps(cmd))
            for line in scenario.get("stderr", []):
                sys.stderr.write(line)
                sys.stderr.flush()
            waited = 0.0
            while waited < scenario.get("cli_seconds", 0):
                if exists(gone):
                    sys.exit(137)
                time.sleep(0.05)
                waited += 0.05
            print(json.dumps({"outcome": scenario.get("outcome", "success"),
                              "pad": "x" * scenario.get("outcome_padding", 0)}))
            sys.exit(scenario.get("cli_code", 0))
        elif cmd[:2] == ["podman", "image"]:
            labels = dict(scenario.get("labels", {}))
            if scenario.get("label_padding"):
                labels["pad"] = "x" * scenario["label_padding"]
            print(json.dumps({"Id": scenario.get("inspect_id", scenario["image_id"]),
                              "Size": scenario["image_bytes"], "Labels": labels}))
        elif cmd[:2] == ["podman", "save"]:
            (STATE / "saved").write_text(json.dumps(cmd))
            config = bytes.fromhex(scenario["config_hex"])
            if scenario.get("other_config"):
                config += b" "
            manifest = json.dumps([{"Config": scenario["image_id"] + ".json",
                                    "RepoTags": scenario.get("repo_tags"),
                                    "Layers": ["layer.tar"]}]).encode()
            with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
                for name, content in (("layer.tar", b"\0" * scenario["image_bytes"]),
                                      (scenario["image_id"] + ".json", config),
                                      ("manifest.json", manifest)):
                    info = tarfile.TarInfo(name)
                    info.size = len(content)
                    archive.addfile(info, io.BytesIO(content))
else:
    sys.exit(0)
"""


class StandIn:
    def __init__(self, tmp_path: Path, **scenario):
        self.state = tmp_path / "podman-state"
        self.state.mkdir()
        self.scenario = {
            "repository_bytes": 20_000,
            "image_bytes": 200_000,
            "image_id": IMAGE_ID,
            "config_hex": CONFIG.hex(),
            "labels": {},
            **scenario,
        }
        self.scratch = tmp_path / "scratch"
        self.save()
        self.program = tmp_path / "podman"
        self.program.write_text(STAND_IN.replace("__STATE__", repr(str(self.state))))
        self.program.chmod(0o755)

    def save(self) -> None:
        (self.state / "scenario.json").write_text(json.dumps(self.scenario))

    def calls(self) -> list[list[str]]:
        path = self.state / "calls"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def verbs(self) -> list[str]:
        """Each call as its verb and, for those that have one, what it does."""
        found = []
        for call in self.calls():
            if call[0] == "exec":
                rest = [
                    w for w in call[1:] if w != "--interactive" and not w.startswith("--workdir")
                ]
                who = "builder" if rest[0].startswith("rcman-build") else "workspace"
                found.append(f"exec {who} {Path(rest[1]).name}")
            elif call[0] in ("volume", "image"):
                found.append(f"{call[0]} {call[1]}")
            else:
                found.append(call[0])
        return found

    def has(self, name: str) -> bool:
        return (self.state / name).exists()

    def tag(self, name: str) -> Path:
        """The file that stands for the image tagged ``name``."""
        return self.state / f"tag-{name.replace('/', '%')}"

    def read(self, name: str) -> str:
        return (self.state / name).read_text()


def runtime_with(stand_in: StandIn) -> rt.PodmanRuntime:
    return rt.PodmanRuntime(
        rt.PodmanConfig(
            socket=Path("/run/user/990/podman/podman.sock"),
            image="localhost/rcman-workspace:test",
            tools_dir=Path("/var/lib/rcman/tools"),
            claude_store=Path("/var/lib/rcman/claude"),
            podman=str(stand_in.program),
            builder_image="localhost/rcman-builder:test",
            scratch_dir=stand_in.scratch,
        )
    )


def request(**more) -> rt.BuildRequest:
    arguments = {
        "id": 7,
        "person": 2,
        "slot": people.BUILDER_SLOT,
        "source": rt.container_name(7),
        "config_path": ".devcontainer/rcman-build.json",
        "config_text": '{"build": {"dockerfile": "Dockerfile"}}',
    }
    return rt.BuildRequest(**{**arguments, **more})


def building(runtime, **more):
    said: list[str] = []
    vetted: list[dict] = []
    outcome = {}

    def vet(labels):
        vetted.append(dict(labels))
        if outcome.get("refuse"):
            raise devcontainer.Refused(
                "refused_by_image", key="privileged", entry="./f", reason="no"
            )

    def run(cancel=None, refuse=False):
        outcome["refuse"] = refuse
        return runtime.build_image(request(**more), said.append, cancel, vet)

    return run, said, vetted


STEPS = "[2026-10-15T21:59:54.800Z] [1/2] STEP 3/3: RUN echo hi\r\n"
SUCCESS = {"stderr": [STEPS], "labels": {"devcontainer.metadata": '[{"id": "./f"}]'}}


def test_a_build_goes_through_podman_in_order_and_ends_with_the_image_named_for_the_workspace(
    tmp_path,
):
    stand_in = StandIn(tmp_path, **SUCCESS)
    run, said, vetted = building(runtime_with(stand_in))
    name = run()
    assert name == rt.built_name(7) == f"localhost/{rt.BUILT_REPOSITORY}/7:latest"
    verbs = stand_in.verbs()
    for earlier, later in (
        ("volume create", "run"),
        ("run", "exec workspace sh"),
        ("exec workspace sh", "exec builder env"),
        ("exec builder env", "exec builder podman"),
        ("exec builder podman", "load"),
        ("load", "tag"),
    ):
        assert verbs.index(earlier) < verbs.index(later), (earlier, later, verbs)
    assert verbs[-1] != "tag"  # the builder and its volume are removed after it
    assert not stand_in.has("builder-rcman-build-7") and not stand_in.has("volume-rcman-build-7")
    assert stand_in.tag(name).exists()  # and the image stays
    assert vetted == [SUCCESS["labels"]]
    assert any("STEP 3/3" in line for line in said)  # the steps reach the log


def test_the_runtime_tells_the_copy_and_the_steps_but_not_the_building_the_service_already_told(
    tmp_path,
):
    run, said, _ = building(runtime_with(StandIn(tmp_path, **SUCCESS)))
    run()
    assert said[0] == "Copying the repository into the builder"
    assert "Building the image" not in said and any("STEP 3/3" in line for line in said)


def test_the_recorder_the_real_podman_suite_counts_copies_by_sees_a_copy_and_only_a_copy(tmp_path):
    """tests/container/test_builder_in_podman.py asserts that a refused image was not copied by
    what its ``Watched`` runtime records; it records the methods the build goes through, so this
    fails when they are renamed or take other arguments, which would make those absences pass
    whatever happens."""
    from tests.container.test_builder_in_podman import Watched

    stand_in = StandIn(tmp_path, **SUCCESS)
    config = dataclasses.replace(runtime_with(stand_in).config, scratch_dir=tmp_path / "scratch")
    watched = Watched(config)
    refused, _, _ = building(watched, id=7)
    with pytest.raises(devcontainer.Refused):
        refused(refuse=True)  # refused before the copy: the CLI ran, nothing was copied
    assert watched.ran == [builder.builder_name(7)] and watched.copied == []
    refused()
    assert watched.copied == [7]  # and a build that goes on is seen to copy


def test_the_copy_of_the_image_out_of_the_builder_is_logged_with_its_size_and_time(
    tmp_path, caplog
):
    caplog.set_level(logging.INFO, logger="rcman.runtime")
    stand_in = StandIn(tmp_path, image_bytes=3_000_000, **SUCCESS)
    building(runtime_with(stand_in))[0]()
    said = [r.getMessage() for r in caplog.records if r.name == "rcman.runtime"]
    copied = [line for line in said if "copied out" in line]
    assert len(copied) == 1 and "workspace 7" in copied[0], said
    counted = re.search(r"(\d+) bytes in \d+\.\d s", copied[0])
    assert counted and int(counted.group(1)) >= 3_000_000, copied  # the image and its headers


def test_the_builder_is_started_made_to_run_in_its_own_range_with_the_image_it_is_given(tmp_path):
    stand_in = StandIn(tmp_path, **SUCCESS)
    building(runtime_with(stand_in))[0]()
    run = next(c for c in stand_in.calls() if c[0] == "run")
    base = people.BUILDER_SLOT * rt.RANGE
    for option in (
        "--name=rcman-build-7",
        "--label=rcman.builder=7",
        "--label=rcman.person=2",
        f"--uidmap=0:{base}:{rt.RANGE}",
        "--volume=rcman-build-7:/var/lib/containers",
    ):
        assert option in run, option
    assert "localhost/rcman-builder:test" in run and "--privileged" not in run
    create = next(c for c in stand_in.calls() if c[:2] == ["volume", "create"])
    assert "--label=rcman.builder=7" in create and create[-1] == "rcman-build-7"


def test_the_builder_runs_in_the_slice_of_the_person_who_asked_for_the_build(tmp_path):
    from rcman import limits

    stand_in = StandIn(tmp_path, **SUCCESS)
    building(runtime_with(stand_in), person=5)[0]()
    run = next(c for c in stand_in.calls() if c[0] == "run")
    assert f"--cgroup-parent={limits.person_slice(5)}" in run
    assert [a for a in run if a.startswith("--cgroup-parent")] == [
        "--cgroup-parent=rcman-p5.slice"
    ]  # the person's, not a workspace's: the builder belongs to no workspace of theirs


def test_the_repository_goes_from_the_workspace_to_the_builder_and_the_config_after_it(tmp_path):
    stand_in = StandIn(tmp_path, repository_bytes=123_456, **SUCCESS)
    building(runtime_with(stand_in))[0]()
    assert stand_in.read("copied-in") == "123456"  # every byte, counted by the sink
    source = next(c for c in stand_in.calls() if c[0] == "exec" and "rcman-ws-7" in c)
    assert source[source.index("rcman-ws-7") + 1] == rt.PODMAN_LAYOUT.shell
    assert "/rcman/work" in source  # the repository is the work folder
    assert (
        stand_in.read("config-path")
        == f"{builder.REPOSITORY_FOLDER}/.devcontainer/rcman-build.json"
    )
    assert json.loads(stand_in.read("config-text")) == {"build": {"dockerfile": "Dockerfile"}}
    verbs = stand_in.verbs()
    assert verbs.index("exec builder tar") < verbs.index("exec builder sh")


def test_the_command_is_run_in_the_builder_with_the_argv_the_spike_found(tmp_path):
    stand_in = StandIn(tmp_path, **SUCCESS)
    building(runtime_with(stand_in))[0]()
    assert json.loads(stand_in.read("cli")) == builder.cli_argv(".devcontainer/rcman-build.json")


def test_the_label_is_vetted_before_the_image_is_copied(tmp_path):
    stand_in = StandIn(tmp_path, **SUCCESS)
    runtime = runtime_with(stand_in)
    seen: list[list[str]] = []

    def vet(labels):
        seen.append(stand_in.verbs())

    runtime.build_image(request(), lambda line: None, None, vet)
    assert seen and "exec builder podman" in seen[0]  # the label had been read
    assert "load" not in seen[0] and "tag" not in seen[0]  # nothing was copied yet


def test_an_image_the_policy_refuses_is_not_copied_and_nothing_is_left(tmp_path):
    stand_in = StandIn(tmp_path, **SUCCESS)
    run, _, vetted = building(runtime_with(stand_in))
    with pytest.raises(devcontainer.Refused):
        run(refuse=True)
    assert vetted and "load" not in stand_in.verbs() and "tag" not in stand_in.verbs()
    assert not stand_in.has("builder-rcman-build-7") and not stand_in.has("volume-rcman-build-7")
    assert not [p for p in stand_in.state.glob("tag-*")]


@pytest.mark.parametrize(
    ("said", "code"),
    [(STEP_FAILED, 1), (FEATURE_FAILED, 1), (NO_NETWORK, 1)],
)
def test_a_command_that_fails_is_a_build_failure_with_what_it_said_and_nothing_is_left(
    tmp_path, said, code
):
    stand_in = StandIn(tmp_path, stderr=[said], cli_code=code, outcome="error", **{"labels": {}})
    run, _, vetted = building(runtime_with(stand_in))
    with pytest.raises(rt.BuildFailed) as failed:
        run()
    assert said.strip().splitlines()[-1].split("] ")[-1] in failed.value.tail
    assert failed.value.kind is None  # the cause is read from the tail by the one that shows it
    assert not vetted and "load" not in stand_in.verbs()
    assert not stand_in.has("builder-rcman-build-7") and not stand_in.has("volume-rcman-build-7")
    assert not [p for p in stand_in.state.glob("tag-*")]


def test_a_command_that_ends_well_but_does_not_say_success_is_a_failure(tmp_path):
    stand_in = StandIn(tmp_path, outcome="error", cli_code=0, stderr=["odd\n"])
    with pytest.raises(rt.BuildFailed):
        building(runtime_with(stand_in))[0]()


def test_an_image_that_podman_cannot_load_is_not_taken(tmp_path):
    stand_in = StandIn(tmp_path, **SUCCESS)
    stand_in.scenario["load_fails"] = True
    stand_in.save()
    with pytest.raises(rt.RuntimeFailed) as failed:
        building(runtime_with(stand_in))[0]()
    assert "load went wrong" in str(failed.value)
    assert not [p for p in stand_in.state.glob("tag-*")]
    assert not stand_in.has("builder-rcman-build-7")


def test_a_repository_bigger_than_the_limit_is_a_failure_of_its_own_and_nothing_is_left(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(builder, "MAX_CONTEXT_BYTES", 10_000)
    stand_in = StandIn(tmp_path, repository_bytes=2_000_000, **SUCCESS)
    with pytest.raises(rt.BuildFailed) as failed:
        building(runtime_with(stand_in))[0]()
    assert failed.value.kind == "build_context_too_big"
    assert "exec builder env" not in stand_in.verbs()
    assert not stand_in.has("builder-rcman-build-7") and not stand_in.has("volume-rcman-build-7")


def test_an_image_bigger_than_the_limit_is_a_failure_of_its_own_and_is_not_left(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(builder, "MAX_IMAGE_BYTES", 10_000)
    stand_in = StandIn(tmp_path, image_bytes=2_000_000, **SUCCESS)
    with pytest.raises(rt.BuildFailed) as failed:
        building(runtime_with(stand_in))[0]()
    assert failed.value.kind == "build_image_too_big"
    assert "tag" not in stand_in.verbs()
    assert not stand_in.has("builder-rcman-build-7") and not [
        p for p in stand_in.state.glob("tag-*")
    ]


def test_a_build_that_takes_too_long_is_ended_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "BUILD_SECONDS", 0.5)
    stand_in = StandIn(tmp_path, cli_seconds=60, **SUCCESS)
    began = time.monotonic()
    with pytest.raises(rt.BuildFailed) as failed:
        building(runtime_with(stand_in))[0]()
    assert failed.value.kind == "build_timeout" and time.monotonic() - began < 30
    assert stand_in.has("removed-rcman-build-7")  # the builder was removed, which ends the command
    assert not stand_in.has("volume-rcman-build-7")


def test_a_cancel_removes_the_builder_which_ends_the_command_and_nothing_is_left(tmp_path):
    stand_in = StandIn(tmp_path, cli_seconds=60, **SUCCESS)
    run, _, _ = building(runtime_with(stand_in))
    cancel = threading.Event()

    def stop_when_building():
        deadline = time.monotonic() + 20
        while not stand_in.has("cli") and time.monotonic() < deadline:
            time.sleep(0.02)
        cancel.set()

    threading.Thread(target=stop_when_building, daemon=True).start()
    began = time.monotonic()
    with pytest.raises(rt.RuntimeFailed) as stopped:
        run(cancel)
    assert not isinstance(stopped.value, rt.BuildFailed)  # a stop is no failure of the build
    assert time.monotonic() - began < 30 and stand_in.has("removed-rcman-build-7")
    assert not stand_in.has("volume-rcman-build-7")


def test_a_build_begins_by_removing_what_an_earlier_one_of_the_same_id_left(tmp_path):
    stand_in = StandIn(tmp_path, **SUCCESS)
    (stand_in.state / "builder-rcman-build-7").write_text("")
    (stand_in.state / "volume-rcman-build-7").write_text("")
    stand_in.tag(rt.built_name(7)).write_text("")
    building(runtime_with(stand_in))[0]()
    verbs = stand_in.verbs()
    assert verbs.index("rm") < verbs.index("run")  # the stale builder is gone first
    assert verbs.index("image rm") < verbs.index("volume create")  # and the stale image
    assert stand_in.has("removed-rcman-build-7")


# --- removing and listing -------------------------------------------------------------------------


def test_removing_a_build_removes_its_builder_and_volume_and_the_image_only_when_asked(tmp_path):
    stand_in = StandIn(tmp_path)
    runtime = runtime_with(stand_in)
    runtime.remove_build(7)
    calls = stand_in.calls()
    removal = next(c for c in calls if c[0] == "rm")
    assert removal[-1] == "rcman-build-7" and "--force" in removal and "--ignore" in removal
    assert "--time=0" in removal  # killed at once: the build in it is not waited for
    assert ["volume", "exists", "rcman-build-7"] in calls
    assert not [c for c in calls if c[:2] == ["image", "rm"]]
    runtime.remove_build(7, image=True)
    image_rm = next(c for c in stand_in.calls() if c[:2] == ["image", "rm"])
    assert image_rm[-1] == rt.built_name(7) and "--ignore" in image_rm
    assert "--force" not in image_rm  # an image a container uses is not taken from under it


def test_removing_a_builder_that_is_there_removes_its_volume_too(tmp_path):
    stand_in = StandIn(tmp_path)
    (stand_in.state / "builder-rcman-build-7").write_text("")
    (stand_in.state / "volume-rcman-build-7").write_text("")
    runtime_with(stand_in).remove_build(7)
    assert not stand_in.has("builder-rcman-build-7") and not stand_in.has("volume-rcman-build-7")


def test_the_builders_there_are_are_found_by_their_label_on_containers_and_on_volumes(tmp_path):
    stand_in = StandIn(tmp_path)
    (stand_in.state / "builder-rcman-build-7").write_text("")
    (stand_in.state / "volume-rcman-build-9").write_text("")  # a volume whose builder is gone
    (stand_in.state / "volume-rcman-person-3").write_text("")  # not one of the builders'
    assert runtime_with(stand_in).builders() == {7, 9}
    calls = stand_in.calls()
    assert any(c[0] == "ps" and f"--filter=label={rt.LABEL_BUILDER}" in c for c in calls)
    assert any(
        c[:2] == ["volume", "ls"] and f"--filter=label={rt.LABEL_BUILDER}" in c for c in calls
    )


def test_the_built_images_there_are_are_found_by_the_repository_rcman_names_them_in(tmp_path):
    stand_in = StandIn(tmp_path)
    stand_in.tag(rt.built_name(7)).write_text("")
    stand_in.tag(f"localhost/{rt.BUILT_REPOSITORY}/notanumber:latest").write_text("")
    stand_in.tag("localhost/rcman-workspace:1").write_text("")
    assert re.fullmatch(r"localhost/[a-z-]+/\d+:\w+", rt.built_name(7))
    runtime = runtime_with(stand_in)
    assert runtime.built_images() == {7: rt.built_name(7)}
    images = next(c for c in stand_in.calls() if c[0] == "images")
    assert any(rt.BUILT_REPOSITORY in a for a in images)


# --- the image is staged, checked and only then loaded -------------------------------------------


def rm_by_id(stand_in: StandIn) -> list[list[str]]:
    return [c for c in stand_in.calls() if c[:2] == ["image", "rm"] and c[-1] == IMAGE_ID]


def test_the_saved_image_is_staged_in_a_private_folder_of_the_scratch_area_and_removed_after(
    tmp_path,
):
    stand_in = StandIn(tmp_path, **SUCCESS)
    building(runtime_with(stand_in))[0]()
    info = json.loads(stand_in.read("load-info"))
    assert Path(info["dir"]).parent == stand_in.scratch  # in the area given, and nowhere else
    assert info["dir_mode"] == 0o700 and info["size"] > stand_in.scenario["image_bytes"]
    load = next(c for c in stand_in.calls() if c[0] == "load")
    assert load[1] == "-i" and Path(load[2]).parent == Path(info["dir"])
    assert not Path(info["dir"]).exists() and list(stand_in.scratch.iterdir()) == []


def test_an_image_that_carries_a_name_is_refused_before_it_is_loaded_and_nothing_is_left(tmp_path):
    for number, scenario in enumerate(
        ({"repo_tags": ["localhost/rcman-workspace:0123456789ab"]}, {"other_config": 1})
    ):
        folder = tmp_path / str(number)
        folder.mkdir()
        stand_in = StandIn(folder, **SUCCESS, **scenario)
        with pytest.raises(rt.BuildFailed) as refused:
            building(runtime_with(stand_in))[0]()
        assert refused.value.kind == "build_image_refused" and refused.value.fields["reason"]
        verbs = stand_in.verbs()
        assert "exec builder podman" in verbs  # the control: it was saved, and read, then refused
        assert "load" not in verbs and "tag" not in verbs
        assert not [p for p in stand_in.state.glob("tag-*")] and not stand_in.has(
            f"image-{IMAGE_ID}"
        )
        assert list(stand_in.scratch.iterdir()) == []
        assert not stand_in.has("builder-rcman-build-7") and not stand_in.has(
            "volume-rcman-build-7"
        )


@pytest.mark.parametrize(
    "scenario",
    [
        {"tag_fails": True},
        {"load_fails": True},
        {"loads_under_another_id": True},
    ],
)
def test_an_image_that_was_loaded_and_not_kept_is_removed_by_its_id(tmp_path, scenario):
    stand_in = StandIn(tmp_path, **SUCCESS, **scenario)
    with pytest.raises(rt.RuntimeFailed):
        building(runtime_with(stand_in))[0]()
    assert rm_by_id(stand_in) and "--force" not in rm_by_id(stand_in)[0]
    assert not stand_in.has(f"image-{IMAGE_ID}") and not list(stand_in.state.glob("tag-*"))
    assert list(stand_in.scratch.iterdir()) == []


def test_an_image_whose_load_is_stopped_is_removed_by_its_id(tmp_path):
    stand_in = StandIn(tmp_path, load_seconds=60, **SUCCESS)
    cancel = threading.Event()

    def stop_when_loading():
        deadline = time.monotonic() + 20
        while not stand_in.has("load-info") and time.monotonic() < deadline:
            time.sleep(0.02)
        cancel.set()

    threading.Thread(target=stop_when_loading, daemon=True).start()
    began = time.monotonic()
    run, _, _ = building(runtime_with(stand_in))
    with pytest.raises(rt.RuntimeFailed) as stopped:
        run(cancel)
    assert not isinstance(stopped.value, rt.BuildFailed) and time.monotonic() - began < 30
    assert rm_by_id(stand_in) and list(stand_in.scratch.iterdir()) == []


def test_a_load_that_takes_too_long_is_ended_and_its_image_removed_by_its_id(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "COPY_SECONDS", 3.0)
    stand_in = StandIn(tmp_path, load_seconds=60, **SUCCESS)
    began = time.monotonic()
    with pytest.raises(rt.BuildFailed) as failed:
        building(runtime_with(stand_in))[0]()
    assert failed.value.kind == "build_timeout" and time.monotonic() - began < 30
    assert rm_by_id(stand_in) and list(stand_in.scratch.iterdir()) == []


def test_an_image_that_was_there_before_is_not_removed_by_a_build_that_fails(tmp_path):
    stand_in = StandIn(tmp_path, tag_fails=True, **SUCCESS)
    (stand_in.state / f"image-{IMAGE_ID}").write_text("")  # another workspace's, the same image
    with pytest.raises(rt.RuntimeFailed):
        building(runtime_with(stand_in))[0]()
    assert not rm_by_id(stand_in) and stand_in.has(f"image-{IMAGE_ID}")


def test_what_is_loaded_is_found_by_the_id_and_not_by_a_word_in_what_podman_said(tmp_path):
    # podman says the id in what it prints; the check is that an image of that id is there
    stand_in = StandIn(tmp_path, loads_under_another_id=True, **SUCCESS)
    with pytest.raises(rt.RuntimeFailed) as failed:
        building(runtime_with(stand_in))[0]()
    assert "another id" in str(failed.value)
    inspect = [c for c in stand_in.calls() if c[:2] == ["image", "inspect"]]
    assert inspect and inspect[0][-1] == IMAGE_ID


# --- the copies fail for the cause that is theirs ----------------------------------------------


def test_a_sink_that_ends_first_is_the_cause_not_the_source_the_pump_ended_with_it(tmp_path):
    stand_in = StandIn(
        tmp_path, repository_bytes=30_000_000, tar_fails="tar: x: Cannot write: Broken", **SUCCESS
    )
    with pytest.raises(rt.RuntimeFailed) as failed:
        building(runtime_with(stand_in))[0]()
    assert "Cannot write" in str(failed.value) and "could not be read" not in str(failed.value)
    assert not isinstance(failed.value, rt.BuildFailed)


def test_a_sink_that_has_no_space_is_the_cause_build_no_space(tmp_path):
    stand_in = StandIn(
        tmp_path,
        repository_bytes=30_000_000,
        tar_fails="tar: x: Cannot write: No space left on device",
        **SUCCESS,
    )
    with pytest.raises(rt.BuildFailed) as failed:
        building(runtime_with(stand_in))[0]()
    assert builder.explain(failed.value.tail).kind == "build_no_space"


def pumped(source_code: int, sink_code: int, source_err=b"", sink_err=b"") -> builder.Pumped:
    return builder.Pumped(10, source_code, sink_code, source_err, b"", sink_err)


@pytest.mark.parametrize(
    ("done", "said"),
    [
        (pumped(-9, 1, sink_err=b"sink said"), "sink said"),  # ended by the pump
        (pumped(0, 1, sink_err=b"sink said"), "sink said"),
        (pumped(2, 0, source_err=b"source said"), "source said"),
        (pumped(2, 1, b"source said", b"sink said"), "source said"),  # a real failure of its own
        (pumped(-9, 1), "the sink fallback"),
        (pumped(3, 0), "the source fallback"),
    ],
)
def test_the_failure_of_a_copy_is_the_sinks_when_the_source_was_ended_with_it(done, said):
    with pytest.raises(rt.RuntimeFailed) as failed:
        rt.PodmanRuntime._copy_failed(done, "the source fallback", "the sink fallback")
    assert str(failed.value) == said


def test_a_copy_that_went_well_raises_nothing():
    rt.PodmanRuntime._copy_failed(pumped(0, 0), "a", "b")


# --- the last line of what the command printed ----------------------------------------------------


def test_a_last_line_longer_than_the_error_tail_is_still_read_whole(tmp_path):
    stand_in = StandIn(tmp_path, outcome_padding=3 * builder.SAID_BYTES, **SUCCESS)
    assert building(runtime_with(stand_in))[0]() == rt.built_name(7)


def test_a_last_line_longer_than_the_limit_on_it_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "MAX_OUTCOME_BYTES", 200)
    stand_in = StandIn(tmp_path, outcome_padding=2000, **SUCCESS)
    with pytest.raises(rt.BuildFailed):
        building(runtime_with(stand_in))[0]()


# --- what the builder says about its image is bounded and checked --------------------------------


def test_a_description_of_the_image_bigger_than_the_limit_is_refused_unread(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "MAX_INSPECT_BYTES", 500)
    stand_in = StandIn(tmp_path, label_padding=5000, **SUCCESS)
    run, _, vetted = building(runtime_with(stand_in))
    with pytest.raises(rt.BuildFailed) as failed:
        run()
    assert failed.value.kind == "build_image_refused" and not vetted
    assert "exec builder podman" in stand_in.verbs() and not stand_in.has("saved")


@pytest.mark.parametrize("image_id", ["-v", "--all", "latest", "ABCDEF", IMAGE_ID[:-1], ""])
def test_an_id_that_is_no_id_is_refused_and_never_given_to_podman(tmp_path, image_id):
    stand_in = StandIn(tmp_path, inspect_id=image_id, **SUCCESS)
    run, _, vetted = building(runtime_with(stand_in))
    with pytest.raises(rt.BuildFailed) as failed:
        run()
    assert failed.value.kind == "build_image_refused"
    assert not stand_in.has("saved") and not [
        c for c in stand_in.calls() if image_id and image_id in c
    ]


# --- what an earlier rcman left in the scratch area ---------------------------------------------


def test_staging_folders_an_earlier_rcman_left_are_removed_when_the_old_are_swept(tmp_path):
    stand_in = StandIn(tmp_path)
    runtime = runtime_with(stand_in)
    stand_in.scratch.mkdir()
    old, new, other = (stand_in.scratch / n for n in ("rcman-image-old", "rcman-image-new", "keep"))
    for folder in (old, new, other):
        folder.mkdir()
        (folder / "image.tar").write_text("x")
    long_ago = time.time() - 4 * builder.COPY_SECONDS
    for folder in (old, other):
        os.utime(folder, (long_ago, long_ago))
    runtime.clean_probes()
    assert not old.exists()  # the plant took effect, and was swept
    assert new.exists()  # one that may belong to a build under way
    assert other.exists()  # not a staging folder
