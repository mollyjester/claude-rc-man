"""What the Podman runtime asks of podman to build an image (backend/rcman/runtime.py,
``PodmanRuntime.build_image``), against a stand-in for podman that keeps a little state and
answers each call the way the real one was seen to in the builder: its calls are in order,
the bytes pass through the pump, and whatever happens the builder, its volume and (on a
failure) the image are removed. An image is built for real on the tests' engine by
tests/container/test_builder_in_podman.py."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rcman import builder, people
from rcman import runtime as rt

from tests.test_builder import (
    CONFIG,
    IMAGE_ID,
    LAYER_BYTES,
    STEP_FAILED,
    config_of,
)

STAND_IN = r"""#!/usr/bin/env python3
import io, json, sys, tarfile
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
elif verb == "ps":
    found = [{"Names": [p.name[8:]], "Labels": {"rcman.builder": p.name[20:]}}
             for p in STATE.glob("builder-rcman-build-*")]
    print(json.dumps(found))
elif verb == "images":
    found = [{"Names": [p.name[4:].replace("%", "/")], "Id": p.read_text()}
             for p in STATE.glob("tag-*")]
    print(json.dumps(found))
elif verb == "tag":
    (STATE / ("tag-" + words[-1].replace("/", "%"))).write_text(words[-2])
elif verb == "untag":
    mark("tag-" + words[-1].replace("/", "%"), False)
elif verb == "image" and sub == "exists":
    sys.exit(0 if exists("image-" + words[-1]) else 1)
elif verb == "image" and sub == "inspect":
    tagged = STATE / ("tag-" + words[-1].replace("/", "%"))
    if tagged.exists():
        print("sha256:" + tagged.read_text())
    elif not exists("image-" + words[-1]):
        sys.exit(1)
    else:
        print("sha256:" + words[-1])
elif verb == "image" and sub == "rm":
    if exists("image-" + words[-1]):
        mark("image-" + words[-1], False)
    mark("tag-" + words[-1].replace("/", "%"), False)
elif verb == "container" and sub == "exists":
    sys.exit(0 if exists("container-" + words[-1]) else 1)
elif verb == "load":
    assert words[1] == "-i", words
    path = Path(words[2])
    with tarfile.open(path) as loading:
        members = loading.getnames()
    (STATE / "load-info").write_text(json.dumps({
        "size": path.stat().st_size,
        "dir_mode": path.parent.stat().st_mode & 0o777,
        "dir": str(path.parent),
        "members": members,
    }))
    mark("image-" + scenario["image_id"])
    for name, image in scenario.get("load_names", {}).items():
        (STATE / ("tag-" + name.replace("/", "%"))).write_text(
            scenario["image_id"] if image == "loaded" else image)
    print("Loaded image: sha256:" + scenario["image_id"])
elif verb == "exec":
    rest = [w for w in words[1:] if w not in ("--interactive",) and not w.startswith("--workdir")]
    container, cmd = rest[0], rest[1:]
    if container.startswith("rcman-ws-"):
        zeros(scenario["repository_bytes"])
    else:
        if cmd[0] == "mkdir":
            pass
        elif cmd[0] == "tar":
            (STATE / "copied-in").write_text(str(drain()))
        elif cmd[0] == "sh":
            (STATE / "config-path").write_text(cmd[-1])
            (STATE / "config-text").write_text(sys.stdin.read())
        elif cmd[0] == "env":
            (STATE / "cli").write_text(json.dumps(cmd))
            for line in scenario.get("stderr", []):
                sys.stderr.write(line)
                sys.stderr.flush()
            print(json.dumps({"outcome": scenario.get("outcome", "success")}))
            sys.exit(scenario.get("cli_code", 0))
        elif cmd[:2] == ["podman", "image"]:
            print(json.dumps({"Id": scenario["image_id"], "Size": scenario["image_bytes"],
                              "Labels": scenario.get("labels", {})}))
        elif cmd[:2] == ["podman", "save"]:
            (STATE / "saved").write_text(json.dumps(cmd))
            config = bytes.fromhex(scenario["config_hex"])
            manifest = json.dumps([{"Config": scenario["image_id"] + ".json",
                                    "RepoTags": None,
                                    "Layers": ["layer.tar"]}]).encode()
            extras = {k: v.encode() for k, v in scenario.get("extra_members", {}).items()}
            with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as archive:
                layer = b"\0" * scenario["image_bytes"]
                for name, content in (("layer.tar", layer),
                                      (scenario["image_id"] + ".json", config),
                                      *extras.items(),
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
        size = scenario.get("image_bytes", LAYER_BYTES)
        config, image = (CONFIG, IMAGE_ID) if size == LAYER_BYTES else config_of(b"\0" * size)
        self.scenario = {
            "repository_bytes": 20_000,
            "image_bytes": size,
            "image_id": image,
            "config_hex": config.hex(),
            "labels": {},
            **scenario,
        }
        self.scratch = tmp_path / "scratch"
        self.save()
        for name, image in self.scenario.get("preexisting", {}).items():
            self.tag(name).write_text(image)
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
        """The file that stands for the image tagged ``name``; it holds the image's id."""
        return self.state / f"tag-{name.replace('/', '%')}"

    def read(self, name: str) -> str:
        return (self.state / name).read_text()


def runtime_with(stand_in: StandIn, **more) -> rt.PodmanRuntime:
    return rt.PodmanRuntime(
        rt.PodmanConfig(
            socket=Path("/run/user/990/podman/podman.sock"),
            image="localhost/rcman-workspace:test",
            tools_dir=Path("/opt/rcman/tools"),
            claude_store=Path("/var/lib/rcman/claude"),
            podman=str(stand_in.program),
            builder_image="localhost/rcman-builder:test",
            scratch_dir=stand_in.scratch,
            **more,
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

    def vet(labels):
        vetted.append(dict(labels))

    def run():
        return runtime.build_image(request(**more), said.append, None, vet)

    return run, said, vetted


STEPS = "[2026-10-15T21:59:54.800Z] [1/2] STEP 3/3: RUN echo hi\r\n"
SUCCESS = {"stderr": [STEPS], "labels": {"devcontainer.metadata": '[{"id": "./f"}]'}}


def test_a_build_goes_through_podman_in_order_and_ends_with_the_image_named_for_the_workspace(
    tmp_path,
):
    """And the runtime tells the copy and the steps, but not the building the service already
    told."""
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
    assert said[0] == "Copying the repository into the builder"
    assert "Building the image" not in said and any("STEP 3/3" in line for line in said)


def test_the_builder_runs_in_its_own_range_and_the_persons_slice_and_is_given_the_repository(
    tmp_path,
):
    """Started with the image it is given, in its own range and in the slice of the person who
    asked for the build; the repository goes from the workspace into it, the config after it,
    and the command is run in it with the argv the spike found."""
    from rcman import limits

    stand_in = StandIn(tmp_path, repository_bytes=123_456, **SUCCESS)
    building(runtime_with(stand_in), person=5)[0]()
    run = next(c for c in stand_in.calls() if c[0] == "run")
    base = people.BUILDER_SLOT * rt.RANGE
    for option in (
        "--name=rcman-build-7",
        "--label=rcman.builder=7",
        "--label=rcman.person=5",
        f"--uidmap=0:{base}:{rt.RANGE}",
        "--volume=rcman-build-7:/var/lib/containers",
        f"--network={rt.NETWORK}",
    ):
        assert option in run, option
    assert "localhost/rcman-builder:test" in run and "--privileged" not in run
    assert [a for a in run if a.startswith("--cgroup-parent")] == [
        f"--cgroup-parent={limits.person_slice(5)}"
    ]  # the person's, not a workspace's: the builder belongs to no workspace of theirs
    create = next(c for c in stand_in.calls() if c[:2] == ["volume", "create"])
    assert "--label=rcman.builder=7" in create and create[-1] == "rcman-build-7"
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
    assert json.loads(stand_in.read("cli")) == builder.cli_argv(".devcontainer/rcman-build.json")


def test_a_command_that_fails_is_a_build_failure_with_what_it_said_and_nothing_is_left(tmp_path):
    stand_in = StandIn(tmp_path, stderr=[STEP_FAILED], cli_code=1, outcome="error", labels={})
    run, _, vetted = building(runtime_with(stand_in))
    with pytest.raises(rt.BuildFailed) as failed:
        run()
    assert STEP_FAILED.strip().splitlines()[-1].split("] ")[-1] in failed.value.tail
    assert failed.value.kind is None  # the cause is read from the tail by the one that shows it
    assert not vetted and "load" not in stand_in.verbs()
    assert not stand_in.has("builder-rcman-build-7") and not stand_in.has("volume-rcman-build-7")
    assert not [p for p in stand_in.state.glob("tag-*")]


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


def test_the_builders_and_the_built_and_next_images_there_are_are_found(tmp_path):
    """Builders by their label on containers and on volumes; images by the repository rcman
    names them in, the next images apart from the built ones."""
    stand_in = StandIn(tmp_path)
    (stand_in.state / "builder-rcman-build-7").write_text("")
    (stand_in.state / "volume-rcman-build-9").write_text("")  # a volume whose builder is gone
    (stand_in.state / "volume-rcman-person-3").write_text("")  # not one of the builders'
    for name in (rt.built_name(7), rt.built_name(8, rt.NEXT_TAG), "localhost/rcman-workspace:1"):
        stand_in.tag(name).write_text(OLD)
    runtime = runtime_with(stand_in)
    assert runtime.builders() == {7, 9}
    calls = stand_in.calls()
    assert any(c[0] == "ps" and f"--filter=label={rt.LABEL_BUILDER}" in c for c in calls)
    assert any(
        c[:2] == ["volume", "ls"] and f"--filter=label={rt.LABEL_BUILDER}" in c for c in calls
    )
    assert runtime.built_images() == {7: rt.built_name(7)}
    assert runtime.next_images() == {8: rt.built_name(8, rt.NEXT_TAG)}


# --- a build of the next image, and its promotion ------------------------------------------------


def test_a_next_build_names_the_next_image_and_never_touches_the_built_one(tmp_path):
    stand_in = StandIn(tmp_path, preexisting={rt.built_name(7): OLD}, **SUCCESS)
    stale = rt.built_name(7, rt.NEXT_TAG)
    stand_in.tag(stale).write_text("c" * 64)  # what an earlier next build left
    name = building(runtime_with(stand_in), tag=rt.NEXT_TAG)[0]()
    assert name == stale and stand_in.tag(name).read_text() == IMAGE_ID
    assert stand_in.tag(rt.built_name(7)).read_text() == OLD
    assert not [c for c in stand_in.calls() if rt.built_name(7) in c]
    removals = [c for c in stand_in.calls() if c[:2] == ["image", "rm"]]
    assert removals and removals[0][-1] == stale  # the stale next image went first
    assert ["tag", IMAGE_ID, stale] in stand_in.calls()


def test_promoting_tags_the_next_image_with_the_built_name_and_untags_the_next_one(tmp_path):
    upcoming, latest = rt.built_name(7, rt.NEXT_TAG), rt.built_name(7)
    stand_in = StandIn(tmp_path, preexisting={latest: OLD, upcoming: IMAGE_ID})
    runtime = runtime_with(stand_in)
    assert runtime.promote_build(7) == latest
    assert stand_in.tag(latest).read_text() == IMAGE_ID and not stand_in.tag(upcoming).exists()
    calls = stand_in.calls()
    assert calls.index(["tag", IMAGE_ID, latest]) < calls.index(["untag", IMAGE_ID, upcoming])
    done = len(calls)
    assert runtime.promote_build(7) == latest  # again: nothing to move, nothing moved
    assert not [c for c in stand_in.calls()[done:] if c[0] in ("tag", "untag")]
    assert stand_in.tag(latest).read_text() == IMAGE_ID


# --- the image is staged, checked and only then loaded -------------------------------------------


def rm_by_id(stand_in: StandIn) -> list[list[str]]:
    return [c for c in stand_in.calls() if c[:2] == ["image", "rm"] and c[-1] == IMAGE_ID]


def test_the_saved_image_is_staged_in_a_private_folder_rewritten_and_removed_after(tmp_path):
    """What is loaded is the archive rcman wrote from the parts it checked, staged in a private
    folder of the scratch area."""
    legacy = {"abc/VERSION": "1.0", "abc/json": "{}", "repositories": "{}"}
    stand_in = StandIn(tmp_path, extra_members=legacy, **SUCCESS)
    building(runtime_with(stand_in))[0]()
    info = json.loads(stand_in.read("load-info"))
    assert Path(info["dir"]).parent == stand_in.scratch  # in the area given, and nowhere else
    assert info["dir_mode"] == 0o700 and info["size"] > stand_in.scenario["image_bytes"]
    load = next(c for c in stand_in.calls() if c[0] == "load")
    assert load[1] == "-i" and Path(load[2]).parent == Path(info["dir"])
    assert not Path(info["dir"]).exists() and list(stand_in.scratch.iterdir()) == []
    assert sorted(info["members"]) == sorted(["manifest.json", f"{IMAGE_ID}.json", "layer0.tar"])


# --- the archive that is loaded is the one rcman writes, and rcman's own names stay put ----------

OWN = {
    "default": "localhost/rcman-workspace:test",
    "builder": "localhost/rcman-builder:test",
    "another's built image": rt.built_name(9),
    "another's next image": rt.built_name(9, rt.NEXT_TAG),
}
OLD = "a" * 64


def test_a_load_that_moves_a_name_of_rcmans_own_is_put_back_and_the_build_refused(tmp_path):
    name = OWN["default"]
    stand_in = StandIn(
        tmp_path, preexisting={n: OLD for n in OWN.values()}, load_names={name: "loaded"}, **SUCCESS
    )
    with pytest.raises(rt.BuildFailed) as refused:
        building(runtime_with(stand_in))[0]()
    assert refused.value.kind == "build_image_refused"
    assert {n: stand_in.tag(n).read_text() for n in OWN.values()} == {n: OLD for n in OWN.values()}
    assert not stand_in.tag(rt.built_name(7)).exists() and "tag" not in stand_in.verbs()[-3:]
    assert rm_by_id(stand_in)  # and the image it put there is removed by its id
    assert list(stand_in.scratch.iterdir()) == []
