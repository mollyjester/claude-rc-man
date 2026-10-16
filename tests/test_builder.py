"""backend/rcman/builder.py: what the devcontainer builder is run with and asked to do, how its
words are read, and the pump that copies bytes between two commands. Pure: no Podman is run here;
the copy of a repository and the pump are tried with the host's own tar and shell."""

from __future__ import annotations

import hashlib
import io
import json
import os
import tarfile
import threading
import time
from pathlib import Path

import pytest
from rcman import builder, people
from rcman import runtime as rt

# --- what the builder is run with ----------------------------------------------------------------

LABELS = {rt.LABEL_BUILDER: "7", rt.LABEL_PERSON: "2"}


def run_argv(**more) -> list[str]:
    arguments = {
        "workspace": 7,
        "labels": LABELS,
        "ids": rt.PodmanRuntime._ids(people.BUILDER_SLOT),
        "image": "localhost/rcman-builder:test",
    }
    return builder.run_argv(**{**arguments, **more})


def test_the_builder_is_named_after_the_workspace_and_so_is_its_volume():
    assert builder.builder_name(7) == "rcman-build-7"
    assert builder.builder_name(7) != rt.container_name(7)
    assert builder.builder_name(7) != builder.builder_name(77)
    argv = run_argv()
    assert "--name=rcman-build-7" in argv
    assert "--volume=rcman-build-7:/var/lib/containers" in argv


def test_the_builder_has_no_limit_on_processes_of_its_own_as_a_workspace_has_none():
    """Podman gives a container a limit on processes of its own unless it is told there is
    none, and the limit that holds is the slice's, the person's (limits.py)."""
    argv = run_argv()
    assert "--pids-limit=-1" in argv
    assert argv.index("--pids-limit=-1") < argv.index("--device=/dev/fuse")


def test_the_builder_runs_as_the_spike_found_it_to_work():
    argv = run_argv()
    for option in (
        "--detach",
        "--init",
        "--network=slirp4netns:allow_host_loopback=false",
        "--device=/dev/fuse",
        "--cap-add=SYS_ADMIN",
        "--security-opt=label=disable",
        "--pull=never",
        "--label=rcman.builder=7",
        "--label=rcman.person=2",
    ):
        assert option in argv, option
    assert argv[0] == "run"
    assert argv[-3:] == ["localhost/rcman-builder:test", "sleep", "infinity"]
    # one capability, the one the nested engine needs, and nothing that turns the limits off
    assert [a for a in argv if a.startswith(("--cap", "--privileged"))] == ["--cap-add=SYS_ADMIN"]
    assert not [a for a in argv if a.startswith(("--publish", "-p", "--userns", "--pid=", "--ipc"))]
    assert not [
        a for a in argv if a.startswith("--security-opt") and a != "--security-opt=label=disable"
    ]


def test_the_builder_has_a_range_of_ids_no_person_and_not_rcman_has():
    assert people.BUILDER_SLOT == people.MAX_PEOPLE + 1
    assert people.BUILDER_SLOT >= 1  # never the range of rcman's own ids
    argv = run_argv()
    base = people.BUILDER_SLOT * rt.RANGE
    assert f"--uidmap=0:{base}:{rt.RANGE}" in argv and f"--gidmap=0:{base}:{rt.RANGE}" in argv
    # the range is above the end of the last person's
    last_person_end = (people.MAX_PEOPLE + 1) * rt.RANGE
    assert base >= last_person_end
    maps = [a for a in argv if a.startswith(("--uidmap", "--gidmap"))]
    assert len(maps) == 2  # one range, mapped whole, for each of ids and groups


def test_the_builders_slot_is_not_rcmans_own_slot_zero_which_stays_refused():
    with pytest.raises(rt.RuntimeFailed):
        rt.PodmanRuntime._ids(0)
    assert rt.PodmanRuntime._ids(people.BUILDER_SLOT)  # and the builder's is not


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


def test_the_config_path_cannot_be_taken_for_an_option():
    argv = builder.cli_argv("rcman-build.json")
    assert argv[argv.index("--config") + 1].startswith("/")


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
        source_argv, sink_argv, env=os.environ, limit=None, cancel=None, seconds=30
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


def test_the_source_of_the_copy_does_not_follow_a_link_that_stands_where_the_git_folder_does(
    tmp_path,
):
    source, destination = tmp_path / "repo", tmp_path / "copy"
    source.mkdir()
    destination.mkdir()
    secret = tmp_path / "secret"
    secret.write_text("the secret")
    (source / "Dockerfile").write_text("FROM x\n")
    (source / ".git").symlink_to(secret)  # skipped by name, link or not
    argv = builder.source_argv("/bin/sh", str(source), host_tar("-c"))
    copy_through(argv, builder.sink_argv(host_tar(), str(destination)))
    assert [p.name for p in destination.iterdir()] == ["Dockerfile"]


def test_the_sink_of_the_copy_makes_files_the_builders_and_refuses_members_that_escape(tmp_path):
    argv = builder.sink_argv(["tar"], "/work/repo")
    assert argv[:2] == ["tar", "-x"] and "--no-same-owner" in argv
    assert argv[argv.index("-C") + 1] == "/work/repo"


def test_a_member_with_a_parent_folder_in_its_name_is_not_written_outside(tmp_path):
    destination = tmp_path / "copy"
    destination.mkdir()
    evil = tmp_path / "evil"
    evil.mkdir()
    (evil / "payload").write_text("x")
    crafted = ["tar", "-c", "--transform=s,^evil/,../,", "-C", str(tmp_path), "evil/payload"]
    pumped = builder.pump(crafted, builder.sink_argv(["tar"], str(destination)),
                          env=os.environ, limit=None, cancel=None, seconds=30)  # fmt: skip
    assert not (tmp_path / "payload").exists()  # GNU tar's own refusal is what holds this
    assert pumped.sink_code != 0 or not (tmp_path / "payload").exists()


# --- the pump -------------------------------------------------------------------------------------


def zeros(count: int) -> list[str]:
    return ["head", "-c", str(count), "/dev/zero"]


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_the_pump_hands_every_byte_over_and_counts_them(tmp_path):
    out = tmp_path / "out"
    seen: list[int] = []
    pumped = builder.pump(
        zeros(3_000_000), ["sh", "-c", f"cat > {out}"],
        env=os.environ, limit=None, cancel=None, seconds=30, progress=seen.append,
    )  # fmt: skip
    assert pumped.sent == 3_000_000 == out.stat().st_size
    assert pumped.source_code == 0 and pumped.sink_code == 0
    assert seen and seen == sorted(seen) and seen[-1] == 3_000_000


def test_the_pump_hands_bytes_over_unchanged(tmp_path):
    data = bytes(range(256)) * 5000
    (tmp_path / "in").write_bytes(data)
    pumped = builder.pump(
        ["cat", str(tmp_path / "in")], ["sh", "-c", f"cat > {tmp_path / 'out'}"],
        env=os.environ, limit=None, cancel=None, seconds=30,
    )  # fmt: skip
    assert (tmp_path / "out").read_bytes() == data and pumped.sent == len(data)


def test_the_pump_stops_at_the_limit_and_ends_both_commands(tmp_path):
    source = ["sh", "-c", f"echo $$ > {tmp_path}/source.pid; exec cat /dev/zero"]
    sink = ["sh", "-c", f"echo $$ > {tmp_path}/sink.pid; exec cat > /dev/null"]
    with pytest.raises(builder.PumpStopped) as stopped:
        builder.pump(source, sink, env=os.environ, limit=2_000_000, cancel=None, seconds=30)
    assert stopped.value.reason == builder.TOO_BIG
    assert 2_000_000 <= stopped.value.sent < 2_000_000 + 4 * builder.CHUNK_BYTES
    for name in ("source", "sink"):
        assert not alive(int((tmp_path / f"{name}.pid").read_text()))


def test_a_cancel_ends_both_commands_at_once(tmp_path):
    source = ["sh", "-c", f"echo $$ > {tmp_path}/source.pid; exec sleep 60"]
    sink = ["sh", "-c", f"echo $$ > {tmp_path}/sink.pid; exec cat > /dev/null"]
    cancel = threading.Event()
    threading.Timer(0.5, cancel.set).start()
    began = time.monotonic()
    with pytest.raises(builder.PumpStopped) as stopped:
        builder.pump(source, sink, env=os.environ, limit=None, cancel=cancel, seconds=60)
    assert stopped.value.reason == builder.CANCELLED
    assert time.monotonic() - began < 20
    for name in ("source", "sink"):
        assert not alive(int((tmp_path / f"{name}.pid").read_text()))


def test_a_pump_that_takes_too_long_is_ended_with_both_commands(tmp_path):
    source = ["sh", "-c", f"echo $$ > {tmp_path}/source.pid; exec sleep 60"]
    sink = ["sh", "-c", f"echo $$ > {tmp_path}/sink.pid; exec cat > /dev/null"]
    began = time.monotonic()
    with pytest.raises(builder.PumpStopped) as stopped:
        builder.pump(source, sink, env=os.environ, limit=None, cancel=None, seconds=0.5)
    assert stopped.value.reason == builder.TIMED_OUT
    assert time.monotonic() - began < 20
    for name in ("source", "sink"):
        assert not alive(int((tmp_path / f"{name}.pid").read_text()))


def test_a_cancel_set_before_the_pump_starts_stops_it_without_running_anything(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(builder.PumpStopped) as stopped:
        builder.pump(["sh", "-c", f"touch {tmp_path}/ran"], ["cat"], env=os.environ,
                     limit=None, cancel=cancel, seconds=30)  # fmt: skip
    assert stopped.value.reason == builder.CANCELLED and not (tmp_path / "ran").exists()


def test_a_sink_that_fails_early_does_not_hang_the_pump_and_says_why():
    pumped = builder.pump(
        zeros(50_000_000), ["sh", "-c", "echo sink-said-this >&2; exit 3"],
        env=os.environ, limit=None, cancel=None, seconds=30,
    )  # fmt: skip
    assert pumped.sink_code == 3 and b"sink-said-this" in pumped.sink_err


def test_a_source_that_fails_has_its_code_and_what_it_said():
    pumped = builder.pump(
        ["sh", "-c", "printf partial; echo oops >&2; exit 4"], ["cat"],
        env=os.environ, limit=None, cancel=None, seconds=30,
    )  # fmt: skip
    assert pumped.source_code == 4 and b"oops" in pumped.source_err
    assert pumped.sent == len(b"partial") and pumped.sink_out == b"partial"


def test_what_the_commands_say_is_kept_to_the_tail_it_is_asked_for():
    pumped = builder.pump(
        ["sh", "-c", "yes noise | head -c 5000000 >&2"], ["cat"],
        env=os.environ, limit=None, cancel=None, seconds=30,
    )  # fmt: skip
    assert 0 < len(pumped.source_err) <= builder.SAID_BYTES


def test_the_environment_of_the_pump_is_exactly_the_one_given(tmp_path):
    pumped = builder.pump(
        ["sh", "-c", 'printf %s "${ONLY:-unset}-${HOME:-nohome}"'], ["cat"],
        env={"PATH": "/usr/bin:/bin", "ONLY": "this"}, limit=None, cancel=None, seconds=30,
    )  # fmt: skip
    assert pumped.sink_out == b"this-nohome"


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
# The words of an unreachable registry and of an image that is not there, as the spike read them.
NO_NETWORK = (
    "[2026-10-15T22:30:01.000Z] Error fetching image details: getaddrinfo EAI_AGAIN "
    "registry-1.docker.io\r\n"
)
NO_MANIFEST = (
    "[2026-10-15T22:31:01.000Z] Error fetching image details: No manifest found for "
    "docker.io/library/doesnotexist-zzz:1.\r\n"
    "[2026-10-15T22:31:17.000Z] Error: docker.io/library/doesnotexist-zzz:1: image not known\r\n"
)
DENIED = (
    "[2026-10-15T22:32:01.000Z] [httpOci] Request: GET https://ghcr.io/v2/o/f/manifests/1 "
    "403 requested access to the resource is denied\r\n"
)
# A line of a build that succeeded, which says "denied" of something else.
POLICY_RC = "[2026-10-15T22:03:00.424Z] invoke-rc.d: policy-rc.d denied execution of start.\r\n"
NO_SPACE = "Error: writing blob: storing blob to file: no space left on device\n"


@pytest.mark.parametrize(
    ("said", "kind", "fields"),
    [
        (FEATURE_FAILED, "build_feature_failed", {"feature": "./featurebad"}),
        (STEP_FAILED, "build_step_failed", {}),
        (NO_NETWORK, "build_registry_unreachable", {}),
        (NO_MANIFEST, "build_image_missing", {}),
        (DENIED, "build_sign_in", {}),
        (NO_SPACE, "build_no_space", {}),
    ],
)
def test_the_cause_of_a_failed_build_is_recognised_from_what_the_builder_said(said, kind, fields):
    found = builder.explain(said)
    assert found is not None
    assert (found.kind, found.fields) == (kind, fields)


def test_what_is_recognised_is_what_the_table_names():
    from rcman import problems

    assert len(builder.KINDS) == len(set(builder.KINDS)) >= 6  # non-vacuity
    for kind in builder.KINDS:
        assert kind in problems.DEVCONTAINER_MESSAGES


@pytest.mark.parametrize("said", ["", "everything went fine\n", POLICY_RC, "Exit code 1\n"])
def test_words_that_are_not_a_known_cause_are_not_taken_for_one(said):
    assert builder.explain(said) is None


def test_a_feature_that_failed_is_named_whatever_else_the_tail_says():
    # a failing feature also ends in a failing step; the feature is the cause
    assert builder.explain(FEATURE_FAILED).kind == "build_feature_failed"
    assert builder.explain(STEP_FAILED + FEATURE_FAILED).fields == {"feature": "./featurebad"}


def test_a_name_of_a_feature_is_cut_to_the_most_of_it_that_is_kept():
    said = 'ERROR: Feature "' + "x" * 5000 + '" (Unknown) failed to install!\n'
    found = builder.explain(said)
    assert found is not None and len(found.fields["feature"]) <= builder.MAX_FEATURE_CHARS


def test_a_control_connection_the_builder_could_not_make_is_no_cause_of_a_failure():
    # the CLI looks for its control manifest on the network and says so when it cannot, which
    # it does in builds that go on to fail for another reason
    said = (
        "[2026-10-15T22:00:01.000Z] Failed to fetch control manifest: getaddrinfo EAI_AGAIN "
        "containers.dev\r\n" + STEP_FAILED
    )
    assert builder.explain(said).kind == "build_step_failed"
    assert builder.explain(said.split("\n")[0]) is None


def test_an_unclosed_feature_name_is_not_a_cause_by_itself():
    assert builder.explain('ERROR: Feature "never closed failed to install!\n') is None


# --- reading its lines ----------------------------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def lines(clock=None, **more):
    reported: list[str] = []
    return builder.ProgressLines(reported.append, clock or Clock(), **more), reported


def test_lines_end_in_a_carriage_return_and_a_newline_and_start_with_a_time():
    progress, _ = lines()
    progress.feed(
        b"[2026-10-15T22:00:16.410Z] installing-bad\r\n[2026-10-15T22:00:16.411Z] two\r\n\n"
    )
    assert progress.tail() == "installing-bad\ntwo"


def test_a_line_that_arrives_in_pieces_is_one_line():
    progress, _ = lines()
    for piece in (b"[2026-10-15T22:00:16.410Z] (Read", b"ing database ... 5%", b") done\r\nnext"):
        progress.feed(piece)
    assert progress.tail() == "(Reading database ... 5%) done"
    progress.close()
    assert progress.tail().endswith("next")  # what was left at the end is a line too


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


def test_a_step_that_waited_is_reported_when_the_interval_has_passed_without_another_step():
    clock = Clock()
    progress, reported = lines(clock)
    progress.feed(b"[1/3] STEP 1/4: FROM x\n")
    progress.feed(b"[1/3] STEP 2/4: RUN a\n")
    assert reported == ["[1/3] STEP 1/4: FROM x"]
    clock.now += builder.PROGRESS_SECONDS * 2
    progress.feed(b"some other line\n")
    assert reported[-1] == "[1/3] STEP 2/4: RUN a"


def test_a_reported_line_is_cut_short():
    progress, reported = lines()
    progress.feed(b"[1/2] STEP 2/9: RUN " + b"x" * 5000 + b"\n")
    assert len(reported) == 1 and len(reported[0]) <= builder.MAX_LINE_CHARS + 3


def test_the_tail_keeps_the_last_lines_only_and_each_cut_short():
    progress, _ = lines(keep=3)
    for number in range(10):
        progress.feed(f"line {number}\n".encode())
    progress.feed(b"y" * 5000 + b"\n")
    tail = progress.tail().splitlines()
    assert tail[:2] == ["line 8", "line 9"] and len(tail) == 3
    assert len(tail[2]) <= builder.MAX_LINE_CHARS + 3


def test_bytes_that_are_not_text_are_shown_not_failed_on():
    progress, _ = lines()
    progress.feed(b"caf\xc3\xa9 \xff\xfe broken\n")
    assert "broken" in progress.tail()


def test_a_line_that_is_never_ended_cannot_grow_without_bound():
    progress, _ = lines()
    for _ in range(100):
        progress.feed(b"z" * 100_000)
    assert len(progress.tail().splitlines()) <= builder.TAIL_LINES
    assert len(progress.tail()) <= builder.TAIL_LINES * (builder.MAX_LINE_CHARS + 4)
    assert len(progress._partial) <= builder.MAX_LINE_BYTES  # the held part of the line


def test_the_constants_the_prose_would_otherwise_copy_are_named():
    for name in ("PROGRESS_SECONDS", "TAIL_LINES", "MAX_LINE_CHARS", "MAX_CONTEXT_BYTES",
                 "MAX_IMAGE_BYTES", "BUILD_SECONDS", "COPY_SECONDS", "ARCHIVE_FORMAT"):  # fmt: skip
        assert hasattr(builder, name), name
    assert builder.ARCHIVE_FORMAT == "docker-archive"  # uncompressed: the fastest the spike found


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


def write_archive(
    folder: Path, members: dict[str, bytes], name: str = "image.tar", mode: str = "w"
) -> Path:
    """A tar archive holding ``members`` (name -> content) in order, as bytes only."""
    path = folder / name
    with tarfile.open(path, mode) as archive:
        for member, content in members.items():
            info = tarfile.TarInfo(member)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return path


def manifest(tags=None, config: str | None = None, **more) -> bytes:
    entry = {"Config": config or f"{IMAGE_ID}.json", "RepoTags": tags, "Layers": ["layer.tar"]}
    return json.dumps([{**entry, **more}]).encode()


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


@pytest.mark.parametrize("tags", [None, []])
def test_an_image_with_no_names_is_accepted_whether_the_key_says_null_or_nothing(tmp_path, tags):
    builder.verify_archive(docker_archive(tmp_path, tags=tags), IMAGE_ID)


def test_a_manifest_with_no_repo_tags_key_and_names_for_the_parts_of_the_archive_are_accepted(
    tmp_path,
):
    body = json.dumps([{"Config": f"./{IMAGE_ID}.json", "Layers": ["./layer.tar"]}]).encode()
    path = write_archive(
        tmp_path, {"layer.tar": b"", f"{IMAGE_ID}.json": CONFIG, "./manifest.json": body}
    )
    builder.verify_archive(path, IMAGE_ID)


@pytest.mark.parametrize(
    "tags",
    [
        ["localhost/rcman-workspace:0123456789ab"],
        [f"localhost/{rt.BUILT_REPOSITORY}/3:latest"],
        ["docker.io/library/ubuntu:24.04"],
        ["x"],
        ["", "x"],
        "localhost/rcman-workspace:1",
        [None],
    ],
)
def test_an_image_that_carries_any_name_is_refused_so_none_of_rcmans_own_can_be_retagged(
    tmp_path, tags
):
    assert "name" in refused_with(docker_archive(tmp_path, tags=tags))


def test_two_images_or_two_configurations_are_refused(tmp_path):
    other = b'{"os": "other"}'
    two = json.dumps(
        [
            {"Config": f"{IMAGE_ID}.json", "RepoTags": None, "Layers": []},
            {"Config": "other.json", "RepoTags": None, "Layers": []},
        ]
    ).encode()
    path = docker_archive(tmp_path, **{"manifest.json": two, "other.json": other})
    assert "one image" in refused_with(path)
    empty = tmp_path / "empty"
    empty.mkdir()
    assert "one image" in refused_with(docker_archive(empty, **{"manifest.json": b"[]"}))


def test_a_name_that_two_members_have_is_refused_for_the_manifest_and_the_configuration(tmp_path):
    clean = manifest()
    named = manifest(tags=["localhost/rcman-workspace:1"])
    path = tmp_path / "twice.tar"
    with tarfile.open(path, "w") as archive:
        for name, content in (("manifest.json", clean), (f"{IMAGE_ID}.json", CONFIG),
                              ("manifest.json", named)):  # fmt: skip
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    assert "two manifests" in refused_with(path)
    other = tmp_path / "other"
    other.mkdir()
    path = write_archive(other, {f"{IMAGE_ID}.json": CONFIG, "manifest.json": clean})
    with tarfile.open(path, "a") as archive:  # a second configuration of the same name
        info = tarfile.TarInfo(f"{IMAGE_ID}.json")
        info.size = 2
        archive.addfile(info, io.BytesIO(b"{}"))
    assert "two configurations" in refused_with(path)
    layers = tmp_path / "layers"
    layers.mkdir()
    path = write_archive(
        layers, {"layer.tar": b"", f"{IMAGE_ID}.json": CONFIG, "manifest.json": clean}
    )
    with tarfile.open(path, "a") as archive:  # a layer written twice is an image's own business
        info = tarfile.TarInfo("layer.tar")
        archive.addfile(info, io.BytesIO(b""))
    builder.verify_archive(path, IMAGE_ID)


def test_a_configuration_that_is_not_the_image_that_was_saved_is_refused(tmp_path):
    other = b'{"os": "windows"}'
    path = docker_archive(tmp_path, **{f"{IMAGE_ID}.json": other})  # named as the id, not it
    assert "id" in refused_with(path)


def test_a_manifest_whose_configuration_is_not_in_the_archive_is_refused(tmp_path):
    members = {"layer.tar": b"", "manifest.json": manifest(config="missing.json")}
    assert "configuration" in refused_with(write_archive(tmp_path, members))


def test_an_archive_with_no_manifest_or_in_the_oci_layout_is_refused(tmp_path):
    assert "manifest" in refused_with(write_archive(tmp_path, {f"{IMAGE_ID}.json": CONFIG}))
    oci = tmp_path / "oci"
    oci.mkdir()
    index = json.dumps({"manifests": [{"annotations": {"org.opencontainers.image.ref.name": "x"}}]})
    path = write_archive(oci, {"index.json": index.encode(), "oci-layout": b"{}"})
    assert "OCI" in refused_with(path)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b'{"a": 1}',
        b"[1]",
        b"[[]]",
        b'[{"Config": 5}]',
        b'[{"RepoTags": null}]',
        b"[" * 100_000,
        b"\xff\xfe",
    ],  # fmt: skip
)
def test_a_manifest_that_is_not_a_list_of_one_object_naming_its_configuration_is_refused(
    tmp_path, body
):
    assert refused_with(docker_archive(tmp_path, **{"manifest.json": body}))


def test_a_manifest_bigger_than_the_limit_is_refused_unread(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "MAX_MANIFEST_BYTES", 100)
    path = docker_archive(tmp_path, **{"manifest.json": manifest(padding="x" * 1000)})
    assert "too big" in refused_with(path)


def test_a_configuration_bigger_than_the_limit_is_refused_unread(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "MAX_CONFIG_BYTES", 10)
    assert "too big" in refused_with(docker_archive(tmp_path))


def test_an_archive_of_more_members_than_an_image_has_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(builder, "MAX_ARCHIVE_MEMBERS", 3)
    extra = {f"layer{n}.tar": b"" for n in range(10)}
    assert "members" in refused_with(docker_archive(tmp_path, **extra))


def test_an_archive_that_is_cut_short_or_is_no_tar_or_is_compressed_is_refused(tmp_path):
    whole = docker_archive(tmp_path)
    builder.verify_archive(whole, IMAGE_ID)  # the control
    cut = tmp_path / "cut.tar"
    cut.write_bytes(whole.read_bytes()[:700])
    assert refused_with(cut)
    junk = tmp_path / "junk.tar"
    junk.write_bytes(b"this is not a tar archive" * 100)
    assert refused_with(junk)
    packed = tmp_path / "packed"
    packed.mkdir()
    path = write_archive(
        packed, {f"{IMAGE_ID}.json": CONFIG, "manifest.json": manifest()}, "a.tar.gz", "w:gz"
    )
    assert refused_with(path)
    assert refused_with(tmp_path / "missing.tar")


def test_names_of_members_that_climb_out_are_never_acted_on_since_nothing_is_extracted(tmp_path):
    inside = tmp_path / "in"
    inside.mkdir()
    members = {
        "../../escape.txt": b"x",
        "/etc/cron.d/evil": b"x",
        "layer.tar": LAYER,
        f"{IMAGE_ID}.json": CONFIG,
        "manifest.json": manifest(),
    }
    path = write_archive(inside, members)
    before = sorted(p.name for p in tmp_path.rglob("*"))
    builder.verify_archive(path, IMAGE_ID)
    assert sorted(p.name for p in tmp_path.rglob("*")) == before  # nothing was written anywhere
    again = inside / "again.tar"
    builder.repack_archive(path, IMAGE_ID, again)  # and the fresh archive holds none of them
    assert sorted(names_of(again)) == sorted(["layer0.tar", "manifest.json", f"{IMAGE_ID}.json"])
    assert not (tmp_path.parent / "escape.txt").exists()


def test_a_link_or_a_folder_in_the_place_of_the_manifest_or_configuration_is_refused(tmp_path):
    path = tmp_path / "links.tar"
    with tarfile.open(path, "w") as archive:
        for name, kind in (
            ("manifest.json", tarfile.SYMTYPE),
            (f"{IMAGE_ID}.json", tarfile.DIRTYPE),
        ):
            info = tarfile.TarInfo(name)
            info.type = kind
            info.linkname = "/etc/passwd" if kind == tarfile.SYMTYPE else ""
            archive.addfile(info)
    assert refused_with(path)


def names_of(path: Path) -> list[str]:
    with tarfile.open(path, "r:") as archive:
        return [member.name for member in archive]


def test_a_manifest_naming_a_layer_the_archive_lacks_or_holds_as_a_link_is_refused(tmp_path):
    missing = tmp_path / "missing"
    missing.mkdir()
    path = write_archive(missing, {f"{IMAGE_ID}.json": CONFIG, "manifest.json": manifest()})
    assert "layer" in refused_with(path)
    link = tmp_path / "link.tar"
    with tarfile.open(link, "w") as archive:
        info = tarfile.TarInfo("layer.tar")
        info.type, info.linkname = tarfile.SYMTYPE, "/etc/passwd"
        archive.addfile(info)
        for name, content in ((f"{IMAGE_ID}.json", CONFIG), ("manifest.json", manifest())):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    assert "layer" in refused_with(link)


@pytest.mark.parametrize("layers", ["a string", [1], [None], {"x": 1}])
def test_a_manifest_whose_layers_are_not_a_list_of_names_is_refused(tmp_path, layers):
    body = json.dumps([{"Config": f"{IMAGE_ID}.json", "RepoTags": None, "Layers": layers}])
    assert "layer" in refused_with(docker_archive(tmp_path, **{"manifest.json": body.encode()}))


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


@pytest.mark.parametrize(
    "extra",
    [
        {"index.json": OCI_INDEX},
        {"oci-layout": b'{"imageLayoutVersion": "1.0.0"}'},
        {"./index.json": OCI_INDEX},
        {"blobs/sha256/" + "0" * 64: b"x"},
        {"blobs": b""},
        {"index.json": OCI_INDEX, "oci-layout": b"{}", "blobs/sha256/x": b""},
    ],
)
def test_an_archive_that_is_also_an_oci_layout_is_refused_whatever_its_docker_archive_says(
    tmp_path, extra
):
    control = tmp_path / "control"
    control.mkdir()
    builder.verify_archive(docker_archive(control), IMAGE_ID)  # the docker-archive alone is fine
    both = tmp_path / "both"
    both.mkdir()
    assert "OCI" in refused_with(docker_archive(both, **extra))


def test_a_directory_in_the_place_of_an_oci_marker_is_refused_as_well(tmp_path):
    path = tmp_path / "dir.tar"
    with tarfile.open(path, "w") as archive:
        for name in ("blobs/", "blobs/sha256/"):
            info = tarfile.TarInfo(name)
            info.type = tarfile.DIRTYPE
            archive.addfile(info)
        for name, content in (("layer.tar", b""), (f"{IMAGE_ID}.json", CONFIG),
                              ("manifest.json", manifest())):  # fmt: skip
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    assert "OCI" in refused_with(path)


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


def test_layers_whose_bytes_are_not_the_ones_the_configuration_lists_are_refused(tmp_path):
    """The storage takes a layer it already has by the digest the configuration gives, without
    reading the archive's bytes for it: a layer's bytes must be what its digest says."""
    path = docker_archive(tmp_path)
    fresh = tmp_path / "fresh.tar"
    builder.repack_archive(path, IMAGE_ID, fresh)  # the control: the bytes are the digest's
    assert fresh.exists()
    other = tmp_path / "other"
    other.mkdir()
    crafted = docker_archive(other, **{"layer.tar": b"somebody else's layer"})
    target = other / "fresh.tar"
    with pytest.raises(builder.ArchiveRefused) as refused:
        builder.repack_archive(crafted, IMAGE_ID, target)
    assert "layer" in str(refused.value) and not target.exists()


def test_a_configuration_that_lists_other_layers_than_the_manifest_does_is_refused(tmp_path):
    config, image = config_of(LAYER, LAYER)  # two layers
    body = json.dumps([{"Config": f"{image}.json", "RepoTags": None, "Layers": ["layer.tar"]}])
    path = write_archive(
        tmp_path, {"layer.tar": LAYER, f"{image}.json": config, "manifest.json": body.encode()}
    )
    with pytest.raises(builder.ArchiveRefused) as refused:
        builder.verify_archive(path, image)
    assert "layer" in str(refused.value)
    for bad in (b'{"rootfs": 5}', b"{}", b'{"rootfs": {"diff_ids": [5]}}'):
        digest = hashlib.sha256(bad).hexdigest()
        body = json.dumps([{"Config": f"{digest}.json", "RepoTags": None, "Layers": []}])
        folder = tmp_path / digest[:8]
        folder.mkdir()
        path = write_archive(folder, {f"{digest}.json": bad, "manifest.json": body.encode()})
        with pytest.raises(builder.ArchiveRefused):
            builder.verify_archive(path, digest)


def test_a_fresh_archive_is_not_made_of_an_archive_that_is_refused(tmp_path):
    path = docker_archive(tmp_path, **{"index.json": OCI_INDEX})
    fresh = tmp_path / "fresh.tar"
    with pytest.raises(builder.ArchiveRefused):
        builder.repack_archive(path, IMAGE_ID, fresh)
    assert not fresh.exists()


def test_a_fresh_archive_from_an_archive_cut_short_in_a_layer_is_refused_and_not_left(tmp_path):
    whole = docker_archive(tmp_path, **{"layer.tar": b"x" * 5000})
    cut = tmp_path / "cut.tar"
    cut.write_bytes(whole.read_bytes()[:2000])
    fresh = tmp_path / "fresh.tar"
    with pytest.raises(builder.ArchiveRefused):
        builder.repack_archive(cut, IMAGE_ID, fresh)
    assert not fresh.exists()


def test_a_stop_during_the_copy_into_the_fresh_archive_ends_it_and_leaves_nothing(tmp_path):
    path = docker_archive(tmp_path, **{"layer.tar": b"x" * 5 * builder.CHUNK_BYTES})
    fresh = tmp_path / "fresh.tar"
    with pytest.raises(builder.PumpStopped) as stopped:
        builder.repack_archive(path, IMAGE_ID, fresh, stop=lambda: True)
    assert stopped.value.reason == builder.CANCELLED and not fresh.exists()


def odd_size_header(folder: Path, size_field: bytes) -> Path:
    """An archive whose last member, the layer, has the header ``size_field`` for its size;
    the checksum is made right again, so only the size is odd."""
    path = folder / "odd.tar"
    with tarfile.open(path, "w") as archive:
        for name, content in ((f"{IMAGE_ID}.json", CONFIG), ("manifest.json", manifest())):
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        header = bytearray(tarfile.TarInfo("layer.tar").tobuf(tarfile.USTAR_FORMAT))
        header[124:136] = size_field
        header[148:156] = b" " * 8
        header[148:156] = b"%06o\0 " % sum(header)
        archive.fileobj.write(bytes(header))
        archive.offset += len(header)
    return path


def test_a_size_field_that_python_and_go_read_differently_is_refused(tmp_path):
    """A leading NUL: Python reads the size as nothing, Go's tar trims it and reads the number.
    What one believes is a member's end the other believes is somewhere else."""
    plain = tmp_path / "plain"
    plain.mkdir()
    builder.verify_archive(odd_size_header(plain, b"00000000000\0"), IMAGE_ID)  # the control
    odd = tmp_path / "odd"
    odd.mkdir()
    assert "header" in refused_with(odd_size_header(odd, b"\0" + b"0000000012" + b"\0"))
    binary = tmp_path / "binary"
    binary.mkdir()
    assert "header" in refused_with(odd_size_header(binary, b"\x80" + b"\0" * 10 + b"\x0c"))


def test_the_id_to_match_is_the_whole_id_the_configuration_hashes_to(tmp_path):
    path = docker_archive(tmp_path)
    with pytest.raises(builder.ArchiveRefused):
        builder.verify_archive(path, IMAGE_ID[:12])  # a short id names no digest
    with pytest.raises(builder.ArchiveRefused):
        builder.verify_archive(path, "0" * len(IMAGE_ID))


def test_an_id_that_could_be_taken_for_an_option_or_a_name_is_not_an_id():
    assert builder.is_image_id(IMAGE_ID)
    for bad in (
        "",
        "-v",
        "--all",
        IMAGE_ID[:-1],
        IMAGE_ID.upper(),
        IMAGE_ID + "0",
        "x" * 64,
        5,
        None,
    ):
        assert not builder.is_image_id(bad), bad


def test_the_builder_goes_under_a_slice_when_it_is_given_one_and_under_none_otherwise():
    assert not [a for a in run_argv() if a.startswith("--cgroup-parent")]
    argv = run_argv(cgroup_parent="rcman-p2.slice")
    assert "--cgroup-parent=rcman-p2.slice" in argv
    assert argv[0] == "run" and argv[-3:] == ["localhost/rcman-builder:test", "sleep", "infinity"]
