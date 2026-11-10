"""backend/rcman/builder.py: what the devcontainer builder is run with and asked to do, how its
words are read, and the pump that copies bytes between two commands. Pure: no Podman is run here;
the copy of a repository and the pump are tried with the host's own tar and shell."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
from pathlib import Path

import pytest
from rcman import builder, people
from rcman import runtime as rt

from tests.hang_guard import HANG_GUARD

# --- what the builder is run with ----------------------------------------------------------------

LABELS = {rt.LABEL_BUILDER: "7", rt.LABEL_PERSON: "2"}


# The uid maps of the product's own range, from a runtime with the product's defaults.
IDS = rt.PodmanRuntime(rt.PodmanConfig(Path("/s"), "i", Path("/t"), Path("/c")))._ids


def run_argv(**more) -> list[str]:
    arguments = {
        "workspace": 7,
        "labels": LABELS,
        "ids": IDS(people.BUILDER_SLOT),
        "image": "localhost/rcman-builder:test",
        "network": rt.NETWORK,
    }
    return builder.run_argv(**{**arguments, **more})


def test_the_builder_runs_named_for_the_workspace_with_one_capability_and_a_range_of_its_own():
    """Named after the workspace, as its volume is; no limit on processes of its own, as a
    workspace has none (the slice's, the person's, holds); one capability and no device, for the
    Podman inside to mount overlay itself in the builder's own user namespace; and a range of ids
    above the last person's, never rcman's own."""
    argv = run_argv()
    assert argv[0] == "run"
    assert argv[-3:] == ["localhost/rcman-builder:test", "sleep", "infinity"]
    assert builder.builder_name(7) == "rcman-build-7" != rt.container_name(7)
    for option in (
        "--name=rcman-build-7",
        "--volume=rcman-build-7:/var/lib/containers",
        "--pids-limit=-1",
        "--detach",
        "--init",
        f"--network={rt.NETWORK}",
        "--security-opt=label=disable",
        "--pull=never",
        "--label=rcman.builder=7",
        "--label=rcman.person=2",
    ):
        assert option in argv, option
    assert [a for a in argv if a.startswith(("--cap", "--privileged"))] == ["--cap-add=SYS_ADMIN"]
    assert not [a for a in argv if a.startswith("--device")]  # not even /dev/fuse
    assert people.BUILDER_SLOT == people.MAX_PEOPLE + 1
    base = people.BUILDER_SLOT * rt.RANGE
    assert f"--uidmap=0:{base}:{rt.RANGE}" in argv and f"--gidmap=0:{base}:{rt.RANGE}" in argv


def test_the_command_that_builds_is_run_with_a_clean_environment_and_no_lockfile():
    argv = builder.cli_argv(".devcontainer/rcman-build.json")
    assert argv[:3] == ["env", "-i", f"PATH={builder.BUILDER_PATH}"]
    rest = argv[3:]
    assert rest[:2] == ["devcontainer", "build"]
    assert rest[rest.index("--workspace-folder") + 1] == builder.REPOSITORY_FOLDER
    assert (
        rest[rest.index("--config") + 1]
        == f"{builder.REPOSITORY_FOLDER}/.devcontainer/rcman-build.json"
    )
    assert "--no-lockfile" in rest
    assert rest[rest.index("--docker-path") + 1] == "podman"
    assert rest[rest.index("--image-name") + 1] == builder.IN_BUILDER_IMAGE
    assert builder.IN_BUILDER_IMAGE.startswith("localhost/")
    assert rt.BUILT_REPOSITORY not in builder.IN_BUILDER_IMAGE  # not a name a person's may be
    assert not [a for a in rest if "HOME" in a or "DOCKER" in a]


# --- the repository, copied into the builder ----------------------------------------------------


def host_tar(*args: str) -> list[str]:
    return ["tar", *args]


def make_repository(root: Path) -> None:
    (root / ".git" / "objects").mkdir(parents=True)
    (root / ".git" / "config").write_text("[core]\n")
    (root / ".devcontainer").mkdir()
    (root / ".devcontainer" / "devcontainer.json").write_text("{}")
    (root / "Dockerfile").write_text("FROM x\n")
    (root / "-dash").write_text("starts with a dash")
    (root / "file with space").write_text("spaces")
    (root / "..dots").write_text("two dots")
    (root / ".hidden").write_text("hidden")
    (root / "sub" / ".git").mkdir(parents=True)
    (root / "sub" / ".git" / "nested").write_text("nested git folders are not left out")
    (root / "link").symlink_to("../outside")
    (root / "abs-link").symlink_to("/etc/passwd")
    (root / "mode").write_text("x")
    (root / "mode").chmod(0o751)


def copy_through(source_argv: list[str], sink_argv: list[str]) -> None:
    pumped = builder.pump(
        source_argv, sink_argv, env=os.environ, limit=None, cancel=None, seconds=HANG_GUARD
    )
    assert pumped.source_code == 0 and pumped.sink_code == 0, pumped


def test_the_source_of_the_copy_leaves_the_top_level_git_folder_and_nothing_else_out(tmp_path):
    source, destination = tmp_path / "repo", tmp_path / "copy"
    source.mkdir()
    destination.mkdir()
    make_repository(source)
    argv = builder.source_argv("/bin/sh", str(source), host_tar("-c"))
    copy_through(argv, builder.sink_argv(host_tar(), str(destination)))
    found = {p.relative_to(destination).as_posix() for p in destination.rglob("*")}
    assert ".git" not in found and not any(f.startswith(".git/") for f in found)
    assert {"Dockerfile", "-dash", "file with space", "..dots", ".hidden", "mode"} <= found
    assert ".devcontainer/devcontainer.json" in found
    assert "sub/.git/nested" in found  # a nested one is kept: tar cannot be told otherwise
    assert (destination / "link").is_symlink() and os.readlink(destination / "link") == "../outside"
    assert os.readlink(destination / "abs-link") == "/etc/passwd"  # links stay links
    assert (destination / "mode").stat().st_mode & 0o700 == 0o700  # modes are kept
    assert (destination / "-dash").read_text() == "starts with a dash"


# --- the pump -------------------------------------------------------------------------------------


def zeros(count: int) -> list[str]:
    return ["head", "-c", str(count), "/dev/zero"]


def test_the_pump_hands_every_byte_over_and_counts_them(tmp_path):
    out = tmp_path / "out"
    seen: list[int] = []
    pumped = builder.pump(
        zeros(3_000_000), ["sh", "-c", f"cat > {out}"],
        env=os.environ, limit=None, cancel=None, seconds=HANG_GUARD, progress=seen.append,
    )  # fmt: skip
    assert pumped.sent == 3_000_000 == out.stat().st_size
    assert pumped.source_code == 0 and pumped.sink_code == 0
    assert seen and seen == sorted(seen) and seen[-1] == 3_000_000


# --- reading the builder's words ---------------------------------------------------------------

# Recorded from the CLI 's own standard error in a builder (short ends of real runs).
FEATURE_FAILED = (
    "[2026-10-15T22:00:16.410Z] installing-bad\r\n"
    "[2026-10-15T22:00:16.411Z] oops-from-install\r\n\n"
    '[2026-10-15T22:00:16.412Z] ERROR: Feature "./featurebad" (Unknown) failed to install!\r\n'
    '[2026-10-15T22:00:18.570Z] Error: building at STEP "RUN --mount=type=bind,from=dev_containers'
    "_feature_content_source,source=featurebad_1 cp -ar /tmp/build-features-src/featurebad_1 "
    '/tmp/dev-container-features  && chm": while running runtime: exit status 3\r\n\n'
    "[2026-10-15T22:00:18.592Z] Exit code 3\r\n"
)
STEP_FAILED = (
    "[2026-10-15T21:59:54.800Z] [1/2] STEP 3/3: RUN echo failing-now >&2; exit 7\r\n"
    "[2026-10-15T21:59:55.193Z] failing-now\r\n\n"
    '[2026-10-15T21:59:55.339Z] Error: building at STEP "RUN echo failing-now >&2; exit 7": '
    "while running runtime: exit status 7\r\n\n"
    "[2026-10-15T21:59:55.365Z] Exit code 7\r\n"
)


def test_the_cause_of_a_failed_build_is_recognised_from_what_the_builder_said():
    from rcman import problems

    found = builder.explain(FEATURE_FAILED)
    assert found is not None
    assert (found.kind, found.fields) == ("build_feature_failed", {"feature": "./featurebad"})
    # and every cause recognised has its words for the person
    assert len(builder.KINDS) == len(set(builder.KINDS)) >= 6  # non-vacuity
    for kind in builder.KINDS:
        assert kind in problems.DEVCONTAINER_MESSAGES


# --- reading its lines ----------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def lines(clock=None, **more):
    reported: list[str] = []
    return builder.ProgressLines(reported.append, clock or Clock(), **more), reported


def test_only_podmans_own_step_lines_are_reported_the_rest_is_kept_for_the_tail():
    progress, reported = lines()
    progress.feed(b"[2026-10-15T21:59:54.800Z] [1/2] STEP 3/3: RUN echo hi\r\n")
    progress.feed(b"[2026-10-15T21:59:54.900Z] (Reading database ... 50%\r\n")
    progress.feed(b"[2026-10-15T21:59:54.950Z] Setting up foo (1.0) ...\r\n")
    assert reported == ["[1/2] STEP 3/3: RUN echo hi"]
    assert "Setting up foo" in progress.tail()


def test_step_lines_are_reported_at_most_once_in_the_interval_and_the_last_is_not_lost():
    clock = Clock()
    progress, reported = lines(clock)
    progress.feed(b"[1/3] STEP 1/4: FROM x\n")
    clock.now += builder.PROGRESS_SECONDS / 4
    progress.feed(b"[1/3] STEP 2/4: RUN a\n")
    clock.now += builder.PROGRESS_SECONDS / 4
    progress.feed(b"[1/3] STEP 3/4: RUN b\n")
    assert reported == ["[1/3] STEP 1/4: FROM x"]  # the others wait
    clock.now += builder.PROGRESS_SECONDS
    progress.feed(b"[1/3] STEP 4/4: RUN c\n")
    assert reported == ["[1/3] STEP 1/4: FROM x", "[1/3] STEP 4/4: RUN c"]
    clock.now += builder.PROGRESS_SECONDS / 4
    progress.feed(b"[2/3] STEP 1/2: FROM y\n")
    assert len(reported) == 2
    progress.close()
    assert reported[-1] == "[2/3] STEP 1/2: FROM y"  # the closing reports what was waiting


# --- the archive of an image copied out of the builder ------------------------------------------


def diff_id(layer: bytes) -> str:
    return "sha256:" + hashlib.sha256(layer).hexdigest()


def config_of(*layers: bytes) -> tuple[bytes, str]:
    """The configuration of an image of these layers, in order, and its id."""
    config = json.dumps(
        {
            "architecture": "amd64",
            "os": "linux",
            "rootfs": {"type": "layers", "diff_ids": [diff_id(layer) for layer in layers]},
        }
    ).encode()
    return config, hashlib.sha256(config).hexdigest()


# The layer of the image most tests save, and its size: the stand-in for podman saves layers of
# zeros of the size a scenario gives, the size of this one by default.
LAYER_BYTES = 200_000
LAYER = b"\0" * LAYER_BYTES
CONFIG, IMAGE_ID = config_of(LAYER)


def write_archive(folder: Path, members: dict[str, bytes]) -> Path:
    """A tar archive holding ``members`` (name -> content) in order, as bytes only."""
    path = folder / "image.tar"
    with tarfile.open(path, "w") as archive:
        for member, content in members.items():
            info = tarfile.TarInfo(member)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return path


def manifest(tags=None) -> bytes:
    entry = {"Config": f"{IMAGE_ID}.json", "RepoTags": tags, "Layers": ["layer.tar"]}
    return json.dumps([entry]).encode()


def docker_archive(folder: Path, tags=None, **members_more) -> Path:
    """What ``podman save --format docker-archive <id>`` writes for an image kept by its id:
    the layers, the configuration named for its digest, and a manifest with no names."""
    members = {
        "layer.tar": LAYER,
        f"{IMAGE_ID}.json": CONFIG,
        "manifest.json": manifest(tags),
        **members_more,
    }
    return write_archive(folder, members)


def refused_with(path: Path) -> str:
    with pytest.raises(builder.ArchiveRefused) as refused:
        builder.verify_archive(path, IMAGE_ID)
    return str(refused.value)


def test_the_archive_of_an_image_saved_by_its_id_is_accepted(tmp_path):
    builder.verify_archive(docker_archive(tmp_path), IMAGE_ID)


def test_an_image_that_carries_a_name_is_refused_so_none_of_rcmans_own_can_be_retagged(tmp_path):
    assert "name" in refused_with(docker_archive(tmp_path, tags=["localhost/rcman-workspace:1"]))


# An archive that is both a docker-archive and an OCI layout: Podman tries the OCI reading first,
# which applies the names the layout's index carries to the image it loads.
OCI_INDEX = json.dumps(
    {
        "schemaVersion": 2,
        "manifests": [
            {
                "digest": f"sha256:{IMAGE_ID}",
                "annotations": {"org.opencontainers.image.ref.name": "localhost/rcman-workspace:1"},
            }
        ],
    }
).encode()


def test_an_archive_that_is_also_an_oci_layout_is_refused_whatever_its_docker_archive_says(
    tmp_path,
):
    assert "OCI" in refused_with(docker_archive(tmp_path, **{"index.json": OCI_INDEX}))


def test_the_fresh_archive_holds_the_manifest_the_configuration_and_the_layers_and_nothing_else(
    tmp_path,
):
    """What a docker-archive saved by Podman is seen to carry besides - a directory per layer
    with a VERSION, a json and a link, and the repositories file - is read past and not
    carried over: the archive that is loaded is the one rcman writes."""
    legacy = tmp_path / "legacy.tar"
    layer = b"layer one" * 100
    config, image = config_of(layer, b"two", layer)
    manifest_body = json.dumps(
        [{"Config": f"{image}.json", "RepoTags": None, "Layers": ["a.tar", "b.tar", "a.tar"]}]
    ).encode()
    with tarfile.open(legacy, "w") as archive:
        members = (
            ("a.tar", layer), ("b.tar", b"two"), (f"{image}.json", config),
            ("abc/VERSION", b"1.0"), ("abc/json", b"{}"), ("repositories", b"{}"),
            ("manifest.json", manifest_body),
        )  # fmt: skip
        for name, content in members:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        info = tarfile.TarInfo("abc/layer.tar")
        info.type, info.linkname = tarfile.SYMTYPE, "../a.tar"
        archive.addfile(info)
    fresh = tmp_path / "fresh.tar"
    builder.repack_archive(legacy, image, fresh)
    with tarfile.open(fresh, "r:") as archive:
        found = {m.name: archive.extractfile(m).read() for m in archive}
        assert all(m.isreg() and m.mode == 0o644 and m.uid == 0 for m in archive.getmembers())
    assert sorted(found) == sorted(["manifest.json", f"{image}.json", "layer0.tar", "layer1.tar"])
    assert found[f"{image}.json"] == config and found["layer0.tar"] == layer
    assert found["layer1.tar"] == b"two"
    assert json.loads(found["manifest.json"]) == [
        {
            "Config": f"{image}.json",
            "RepoTags": None,
            "Layers": ["layer0.tar", "layer1.tar", "layer0.tar"],
        }
    ]
