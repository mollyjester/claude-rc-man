"""The runtimes: the contract every runtime keeps, against the host runtime here, and what
the Podman runtime asks of podman, from a recording of its calls.

tests/container/test_runtime_in_podman.py runs the same contract against real Podman.
"""

import dataclasses
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from rcman import runtime as rt

from devtools.held_folders import tmux_sockets
from helpers import instructions
from tests import runtime_contract

REPO = Path(__file__).resolve().parent.parent
FRAGMENTS = REPO / "backend" / "rcman" / instructions.FOLDER


def told(tools: Path) -> Path:
    """``tools`` with the instructions for Claude in it, each file as the installer writes it."""
    folder = tools / instructions.FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    for name in instructions.FILES:
        (folder / name).write_text(instructions.compose(FRAGMENTS, name))
    return tools


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    with tmux_sockets("rcr-") as sockets:
        yield rt.HostRuntime(root, sockets, tmp_path / "claude", REPO / "backend/rcman/tmux.conf")


def spec(n: int) -> rt.Spec:
    return rt.Spec(name=rt.container_name(100 + n), slot=n, workspace=100 + n, person=n)


ALL_CASES = [*runtime_contract.CASES, *runtime_contract.CANARY_CASES]


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda case: case.__name__)
def test_the_host_runtime_keeps_the_contract(host, case):
    case(host, spec)


def test_every_contract_case_is_run_here():
    from tests.collected import collect

    test = "tests/test_runtime.py::test_the_host_runtime_keeps_the_contract"
    cases = {n.removeprefix(test) for n in collect(["tests/test_runtime.py"]) if n.startswith(test)}
    assert len(runtime_contract.CASES) >= 5 and runtime_contract.CANARY_CASES  # non-vacuity
    assert cases == {f"[{case.__name__}]" for case in ALL_CASES}


def test_a_host_runtime_whose_folder_is_gone_is_unavailable(host):
    host.root.rmdir()
    with pytest.raises(rt.RuntimeUnavailable):
        host.exists("rcman-ws-1")
    with pytest.raises(rt.RuntimeUnavailable):
        host.wait_ready(0.1)


def test_no_workspace_runs_in_slot_zero(host):
    with pytest.raises(rt.RuntimeFailed):
        host.create(rt.Spec(name="rcman-ws-1", slot=0, workspace=1, person=1))
    assert not host.exists("rcman-ws-1")


# --- what the Podman runtime asks of podman ---------------------------------------------


class Recorder:
    """Stands in for podman: records each argv and answers from a script by verb."""

    def __init__(self, tmp_path: Path, answers: dict[str, tuple[int, str, str]]):
        self.log = tmp_path / "calls"
        self.program = tmp_path / "podman"
        # No container is there unless a test says so.
        script = {"container exists": [rt.MISSING, "", ""]}
        script |= {verb: list(answer) for verb, answer in answers.items()}
        self.program.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"open({str(self.log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            f"open({str(self.log) + '.stdin'!r}, 'ab').write(sys.stdin.buffer.read())\n"
            f"answers = {json.dumps(script)}\n"
            "words = [a for a in sys.argv[1:] if not a.startswith(('-', 'unix:'))]\n"
            "verb = ' '.join(words[:2]) if ' '.join(words[:2]) in answers else words[0]\n"
            "if 'json .Labels' in ' '.join(sys.argv[1:]) and verb + ' labels' in answers:\n"
            "    verb += ' labels'  # asking a volume for its labels\n"
            "code, out, err = answers.get(verb, [0, '', ''])\n"
            "sys.stdout.write(out); sys.stderr.write(err); sys.exit(code)\n"
        )
        self.program.chmod(0o755)

    def stdin(self) -> bytes:
        """Everything the recorded calls were given on standard input."""
        found = Path(str(self.log) + ".stdin")
        return found.read_bytes() if found.exists() else b""

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


def podman_runtime(tmp_path, recorder, **more) -> rt.PodmanRuntime:
    return rt.PodmanRuntime(
        rt.PodmanConfig(
            socket=Path("/run/user/990/podman/podman.sock"),
            image="localhost/rcman-workspace:test",
            tools_dir=told(tmp_path / "tools"),
            claude_store=Path("/var/lib/rcman/claude"),
            podman=str(recorder.program),
            **more,
        )
    )


# The uid maps of the product's own range, from a runtime with the product's defaults.
IDS = rt.PodmanRuntime(rt.PodmanConfig(Path("/s"), "i", Path("/t"), Path("/c")))._ids


def test_every_podman_call_is_remote_through_rcmans_own_socket(tmp_path):
    recorder = account_recorder(tmp_path)
    runtime = podman_runtime(tmp_path, recorder)
    runtime.create(rt.Spec(name="rcman-ws-7", slot=3, workspace=7, person=2))
    calls = recorder.calls()
    assert calls  # non-vacuity
    for call in calls:
        assert call[:3] == ["--remote", "--url", "unix:///run/user/990/podman/podman.sock"]


def test_a_workspace_container_is_made_as_the_design_says(tmp_path):
    recorder = account_recorder(tmp_path)
    runtime = podman_runtime(tmp_path, recorder)
    runtime.create(rt.Spec(name="rcman-ws-7", slot=3, workspace=7, person=2))
    run = next(c[3:] for c in recorder.calls() if c[3] == "run" and "--name=rcman-ws-7" in c)
    base = 3 * rt.RANGE
    for option in (
        "--init",
        "--network=slirp4netns:allow_host_loopback=false",
        f"--uidmap=0:{base}:{rt.RANGE}",
        f"--gidmap=0:{base}:{rt.RANGE}",
        "--volume=rcman-ws-7:/rcman",
        f"--volume={runtime.config.tools_dir}:/opt/rcman:ro",
        "--volume=/var/lib/rcman/claude:/opt/claude:ro",
        "--tmpfs=/run/rcman:mode=0700",
        "--label=rcman.workspace=7",
        "--label=rcman.person=2",
        "--name=rcman-ws-7",
    ):
        assert option in run, option
    assert not [a for a in run if a.startswith(("-p", "--publish", "--privileged", "--cap"))]
    assert "localhost/rcman-workspace:test" in run
    volume = next(c[3:] for c in recorder.calls() if c[3:5] == ["volume", "create"])
    assert volume[-1] == "rcman-ws-7" and "--label=rcman.person=2" in volume


@pytest.mark.parametrize(
    ("labels", "adopted"),
    [
        ({"rcman.person": "2"}, True),
        ({"rcman.person": "102"}, False),  # another person's
        ({"rcman.person": "1"}, False),
        ({}, False),
        (None, False),
    ],
)
def test_a_volume_that_is_there_is_adopted_only_when_it_is_labelled_for_the_same_person(
    tmp_path, labels, adopted
):
    """The labels are read from the volume and compared, whatever a listing would filter."""
    recorder = Recorder(
        tmp_path,
        {
            "volume exists": (0, "", ""),
            "volume inspect labels": (0, json.dumps(labels) + "\n", ""),
            "volume inspect": (0, MOUNTPOINT + "\n", ""),
        },
    )
    runtime = podman_runtime(tmp_path, recorder)
    spec_ = rt.Spec(name="rcman-ws-7", slot=3, workspace=7, person=2)
    if adopted:
        runtime.create(spec_)
        assert [c for c in recorder.calls() if c[3] == "run" and "--name=rcman-ws-7" in c]
    else:
        with pytest.raises(rt.RuntimeFailed) as refused:
            runtime.create(spec_)
        assert "another person" in str(refused.value)
        assert not [c for c in recorder.calls() if c[3] == "run"]  # nothing was made on it
    asked = next(c for c in recorder.calls() if "{{json .Labels}}" in c and c[3] == "volume")
    assert asked[3:5] == ["volume", "inspect"] and asked[-1] == "rcman-ws-7"
    assert not [c for c in recorder.calls() if c[3:5] == ["volume", "ls"]]  # no filter to trust


def test_a_volume_whose_labels_cannot_be_read_is_not_adopted(tmp_path):
    recorder = Recorder(
        tmp_path, {"volume exists": (0, "", ""), "volume inspect labels": (0, "not json", "")}
    )
    with pytest.raises(rt.RuntimeFailed):
        podman_runtime(tmp_path, recorder).create(
            rt.Spec(name="rcman-ws-7", slot=3, workspace=7, person=2)
        )


ID_A = "a" * 64


def test_a_container_made_again_is_made_from_the_image_id_and_pulls_nothing(tmp_path):
    recorder = account_recorder(tmp_path)
    runtime = podman_runtime(tmp_path, recorder)
    runtime.create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2, image_id=ID_A))
    run = next(c[3:] for c in recorder.calls() if c[3] == "run" and "--name=rcman-ws-7" in c)
    assert run[-3] == ID_A and "--pull=never" in run  # the default image's name is not used
    assert "localhost/rcman-workspace:test" not in run
    named = rt.Spec("rcman-ws-8", slot=3, workspace=8, person=2, image="x/y:1", image_id=ID_A)
    runtime.create(named)
    run = next(c[3:] for c in recorder.calls() if c[3] == "run" and "--name=rcman-ws-8" in c)
    assert run[-3] == ID_A and "x/y:1" not in run  # the id, not the tag that may have moved


@pytest.mark.parametrize("bad", ["-v", "latest", ID_A[:-1], "--all", "A" * 64])
def test_an_image_id_that_is_no_id_is_never_given_to_podman(tmp_path, bad):
    recorder = account_recorder(tmp_path)
    with pytest.raises(rt.RuntimeFailed):
        podman_runtime(tmp_path, recorder).create(
            rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2, image_id=bad)
        )
    assert not [c for c in recorder.calls() if bad in c]


def test_the_ids_and_digests_podman_names_are_read_and_a_local_image_has_no_digest(tmp_path):
    digest = "sha256:" + "d" * 64
    inspected = {"Id": ID_A, "Config": {}, "RepoDigests": [f"x/y@{digest}"], "Digest": digest}
    recorder = (
        Recorder(
            tmp_path / "pulled",
            {"image inspect": (0, json.dumps(inspected), "")},
        )
        if (tmp_path / "pulled").mkdir() is None
        else None
    )
    facts = podman_runtime(tmp_path, recorder).image_facts("x/y:1")
    assert (facts.id, facts.digest) == (ID_A, digest)
    (tmp_path / "local").mkdir()
    local = {"Id": "sha256:" + ID_A, "Config": {}, "RepoDigests": [], "Digest": digest}
    recorder = Recorder(tmp_path / "local", {"image inspect": (0, json.dumps(local), "")})
    facts = podman_runtime(tmp_path, recorder).image_facts("localhost/rcman-built/7:latest")
    assert (facts.id, facts.digest) == (ID_A, "")  # it was not pulled: no digest to pull it by


def test_the_default_image_id_is_the_configured_images_and_empty_when_it_is_not_there(tmp_path):
    recorder = Recorder(tmp_path, {"image inspect": (0, "sha256:" + ID_A + "\n", "")})
    runtime = podman_runtime(tmp_path, recorder)
    assert runtime.default_image_id() == ID_A
    call = next(c for c in recorder.calls() if "inspect" in c)
    assert call[-1] == "localhost/rcman-workspace:test"
    (tmp_path / "gone").mkdir()
    missing = Recorder(tmp_path / "gone", {"image inspect": (125, "", "no such image")})
    assert podman_runtime(tmp_path, missing).default_image_id() == ""
    assert (
        podman_runtime(
            tmp_path, Recorder(tmp_path / "gone", {"image exists": (1, "", "")})
        ).has_image("x")
        is False
    )
    assert (
        podman_runtime(
            tmp_path, Recorder(tmp_path / "gone", {"image exists": (0, "", "")})
        ).has_image("x")
        is True
    )


def removals(recorder) -> list[list[str]]:
    return [c[3:] for c in recorder.calls() if c[3] == "rm"]


def test_every_container_rcman_removes_goes_with_the_anonymous_volumes_of_its_image(tmp_path):
    inspected = json.dumps({"Id": ID_A, "Config": {}, "RepoDigests": []})
    recorder = Recorder(
        tmp_path,
        {
            "volume exists": (0, "", ""),
            "image inspect": (0, inspected, ""),
            "ps": (0, json.dumps([{"Names": ["rcman-probe-x"]}]), ""),
        },
    )
    runtime = podman_runtime(tmp_path, recorder)
    runtime.discard("rcman-ws-1")
    runtime.remove("rcman-ws-1")
    runtime.remove_build(7)
    runtime.end_sign_in("rcman-sign-in-1")
    runtime.end_canary("rcman-canary")
    runtime.clean_probes()  # what a killed image read left
    try:  # the probe of an image is removed after it, whatever the reading came to
        runtime.image_facts("x/y:1")
    except rt.RuntimeFailed:
        pass
    made = removals(recorder)
    assert len(made) >= 7  # non-vacuity: every one of those asked for a removal
    for call in made:
        assert "--volumes" in call and "--force" in call and "--time=0" in call, call


def test_a_volume_nothing_uses_is_removed_only_if_it_is_unlabelled_and_has_a_random_name(tmp_path):
    random_a, random_b, random_c = "a" * 64, "b" * 64, "c" * 64
    listed = [
        {"Name": random_a, "Labels": {}},
        {"Name": random_b, "Labels": {"rcman.person": "1"}},  # labelled: rcman's own
        {"Name": random_c, "Labels": None},
        {"Name": "rcman-ws-1", "Labels": {}},  # named by rcman
        {"Name": "d" * 63, "Labels": {}},  # not a random name
        {"Name": "A" * 64, "Labels": {}},
    ]
    recorder = Recorder(tmp_path, {"volume ls": (0, json.dumps(listed), "")})
    podman_runtime(tmp_path, recorder).clean_probes()
    asked = next(c for c in recorder.calls() if c[3:5] == ["volume", "ls"])
    assert "--filter=dangling=true" in asked  # only those no container has
    gone = [c[-1] for c in recorder.calls() if c[3:5] == ["volume", "rm"]]
    assert gone == [random_a, random_c]


def image_entry(image_id: str, *names: str) -> dict:
    return {"Id": image_id, "Names": list(names) or None}


def test_images_nothing_keeps_are_removed_by_name_without_force_and_the_fixed_ones_stay(tmp_path):
    keep_id, other_id, none_id = "1" * 64, "2" * 64, "3" * 64
    listed = [
        image_entry("4" * 64, "localhost/rcman-workspace:test"),  # the default image
        image_entry("5" * 64, "localhost/rcman-builder:latest"),  # the builder's
        image_entry(keep_id, "registry.example.com/kept:1"),  # kept by its name
        image_entry(keep_id[::-1], "registry.example.com/moved:1"),  # kept by its id
        image_entry(other_id, "registry.example.com/old:1", "registry.example.com/old:2"),
        image_entry(none_id),  # a named-nothing image: what a killed load leaves
    ]
    recorder = Recorder(tmp_path, {"images": (0, json.dumps(listed), "")})
    removed = podman_runtime(tmp_path, recorder).collect_images(
        {keep_id[::-1]}, {"registry.example.com/kept:1"}
    )
    asked = [c[3:] for c in recorder.calls() if c[3:5] == ["image", "rm"]]
    assert asked == [
        ["image", "rm", "registry.example.com/old:1"],
        ["image", "rm", "registry.example.com/old:2"],
        ["image", "rm", none_id],
    ]
    assert removed == ["registry.example.com/old:1", "registry.example.com/old:2", none_id]
    assert not [a for call in asked for a in call if a.startswith("--force") or a == "-f"]


def test_every_tag_of_the_builder_repository_stays(tmp_path):
    """An upgrade builds the next builder image under a tag this service's settings do not
    name yet."""
    listed = [
        image_entry("2" * 64, "localhost/rcman-builder:next", "localhost/rcman-builder:other"),
        image_entry("3" * 64, "localhost/rcman-builderx:1"),  # another repository
    ]
    recorder = Recorder(tmp_path, {"images": (0, json.dumps(listed), "")})
    removed = podman_runtime(tmp_path, recorder).collect_images(set(), set())
    assert removed == ["localhost/rcman-builderx:1"]  # the control: others go


def dated(entry: dict, created: int) -> dict:
    return {**entry, "Created": created}


# The configured default image, made at a time the other default images are dated against.
CONFIGURED = dated(image_entry("4" * 64, "localhost/rcman-workspace:test"), 2_000_000_000)


def test_an_old_default_image_a_workspace_is_pinned_to_stays_with_no_container(tmp_path):
    pinned = dated(image_entry("1" * 64, "localhost/rcman-workspace:old"), 1_000_000_000)
    other = dated(image_entry("2" * 64, "localhost/rcman-workspace:older"), 1_000_000_000)
    recorder = Recorder(tmp_path, {"images": (0, json.dumps([CONFIGURED, pinned, other]), "")})
    removed = podman_runtime(tmp_path, recorder).collect_images({"1" * 64}, set())
    assert removed == ["localhost/rcman-workspace:older"]  # the control: one not pinned goes


def test_an_old_default_image_nothing_pins_is_removed(tmp_path):
    old = dated(image_entry("1" * 64, "localhost/rcman-workspace:old"), 1_000_000_000)
    recorder = Recorder(tmp_path, {"images": (0, json.dumps([CONFIGURED, old]), "")})
    removed = podman_runtime(tmp_path, recorder).collect_images(set(), set())
    assert removed == ["localhost/rcman-workspace:old"]
    asked = [c[3:] for c in recorder.calls() if c[3:5] == ["image", "rm"]]
    assert asked == [["image", "rm", "localhost/rcman-workspace:old"]]  # the configured stays


def test_a_newer_default_image_under_a_tag_not_configured_yet_stays(tmp_path):
    """An upgrade builds the next default image under a tag this service's settings do not
    name until it restarts."""
    newer = dated(image_entry("1" * 64, "localhost/rcman-workspace:next"), 2_000_000_001)
    same = dated(image_entry("2" * 64, "localhost/rcman-workspace:same"), 2_000_000_000)
    recorder = Recorder(tmp_path, {"images": (0, json.dumps([CONFIGURED, newer, same]), "")})
    removed = podman_runtime(tmp_path, recorder).collect_images(set(), set())
    assert removed == ["localhost/rcman-workspace:same"]  # the control: not later is not kept


def test_without_the_configured_image_the_default_repository_is_kept_whole(tmp_path):
    listed = [
        dated(image_entry("1" * 64, "localhost/rcman-workspace:old"), 1_000_000_000),
        dated(image_entry("2" * 64, "localhost/rcman-workspacey:1"), 1_000_000_000),
    ]
    recorder = Recorder(tmp_path, {"images": (0, json.dumps(listed), "")})
    removed = podman_runtime(tmp_path, recorder).collect_images(set(), set())
    assert removed == ["localhost/rcman-workspacey:1"]  # the control: another repository goes


def test_an_image_with_no_name_stays_while_it_is_young_enough_to_belong_to_a_build(tmp_path):
    import time

    young = {**image_entry("1" * 64), "Created": int(time.time())}
    old = {**image_entry("2" * 64), "Created": int(time.time()) - 2 * rt.IMAGE_GRACE_SECONDS}
    unknown = image_entry("3" * 64)  # no age given: not young
    recorder = Recorder(tmp_path, {"images": (0, json.dumps([young, old, unknown]), "")})
    removed = podman_runtime(tmp_path, recorder).collect_images(set(), set())
    assert removed == ["2" * 64, "3" * 64]


def test_names_are_compared_fully_qualified_as_podman_lists_them(tmp_path):
    listed = [
        image_entry("1" * 64, "docker.io/library/ubuntu:24.04"),
        image_entry("2" * 64, "docker.io/team/dev:latest"),
        image_entry("3" * 64, "ghcr.io/o/r:1"),
        image_entry("4" * 64, "docker.io/library/gone:latest"),
    ]
    recorder = Recorder(tmp_path, {"images": (0, json.dumps(listed), "")})
    kept = {"ubuntu:24.04", "team/dev", "index.docker.io/team/dev", "ghcr.io/o/r:1"}
    removed = podman_runtime(tmp_path, recorder).collect_images(set(), kept)
    assert removed == ["docker.io/library/gone:latest"]


@pytest.mark.parametrize(
    ("name", "qualified"),
    [
        ("ubuntu", "docker.io/library/ubuntu:latest"),
        ("ubuntu:24.04", "docker.io/library/ubuntu:24.04"),
        ("team/dev:1", "docker.io/team/dev:1"),
        ("index.docker.io/team/dev", "docker.io/team/dev:latest"),
        ("ghcr.io/o/r@sha256:" + "a" * 64, "ghcr.io/o/r@sha256:" + "a" * 64),
        ("localhost/x:2", "localhost/x:2"),
        ("registry.example.com:443/a/b", "registry.example.com:443/a/b:latest"),
    ],
)
def test_a_name_as_written_and_as_listed_are_one_name(name, qualified):
    assert rt.qualified_name(name) == qualified


def test_an_image_podman_will_not_remove_stays_and_is_not_an_error(tmp_path):
    listed = [image_entry("2" * 64, "registry.example.com/used:1")]
    recorder = Recorder(
        tmp_path,
        {
            "images": (0, json.dumps(listed), ""),
            "image rm": (2, "", "image is in use by a container"),
        },
    )
    assert podman_runtime(tmp_path, recorder).collect_images(set(), set()) == []


def test_the_registry_of_an_image_name_is_the_first_part_only_when_it_is_a_host():
    host = rt.registry_host
    assert host("localhost/img:1") == "localhost" and host("LocalHost:443/a/b") == "localhost"
    assert host("registry.example.com:443/a@sha256:" + "a" * 64) == "registry.example.com"
    assert host("ghcr.io/o/r") == "ghcr.io" and host("10.0.0.1/x") == "10.0.0.1"
    assert host("ubuntu") is None and host("library/ubuntu:24.04") is None
    assert host("ubuntu:24.04") is None


def test_an_image_named_localhost_is_never_pulled_from_this_machine(tmp_path):
    """Podman reads ``localhost/name`` as the registry on the loopback of the machine it
    pulls from; a name like that is an image of rcman's storage or nothing."""
    recorder = Recorder(tmp_path, {"image exists": (0, "", "")})
    messages = []
    podman_runtime(tmp_path, recorder).pull("localhost/mine:1", messages.append)
    assert messages == [rt.IN_STORAGE]
    assert not [c for c in recorder.calls() if "pull" in c]  # the control follows
    assert [c for c in recorder.calls() if c[3:5] == ["image", "exists"]]
    missing = tmp_path / "missing"
    missing.mkdir()
    recorder = Recorder(missing, {"image exists": (1, "", "")})
    with pytest.raises(rt.RuntimeFailed) as failed:
        podman_runtime(missing, recorder).pull("localhost/mine:1")
    assert "storage" in str(failed.value) and not [c for c in recorder.calls() if "pull" in c]
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    recorder = Recorder(elsewhere, {})
    podman_runtime(elsewhere, recorder).pull("ghcr.io/o/r:1")
    assert [c for c in recorder.calls() if "pull" in c]  # the control: other names are pulled


def test_a_volume_is_adopted_only_for_the_person_and_the_install_that_made_it(tmp_path):
    """A database restored from a backup hands the same ids out again; the install id is what
    the volumes of the database that was replaced do not have."""
    spec_ = rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2, install="abc123")
    for number, (labels, adopted) in enumerate(
        [
            ({"rcman.person": "2", "rcman.install": "abc123"}, True),
            ({"rcman.person": "2", "rcman.install": "other"}, False),
            ({"rcman.person": "2"}, False),
            ({"rcman.person": "3", "rcman.install": "abc123"}, False),
        ]
    ):
        folder = tmp_path / str(number)
        folder.mkdir()
        recorder = Recorder(
            folder,
            {"volume exists": (0, "", ""), "volume inspect labels": (0, json.dumps(labels), ""),
             "volume inspect": (0, MOUNTPOINT + "\n", "")},
        )  # fmt: skip
        if not adopted:
            with pytest.raises(rt.RuntimeFailed):
                podman_runtime(folder, recorder).create(spec_)
            assert not [c for c in recorder.calls() if c[3] == "run"]
            continue
        podman_runtime(folder, recorder).create(spec_)
        run = next(c for c in recorder.calls() if c[3] == "run" and "--name=rcman-ws-7" in c)
        assert f"--label={rt.LABEL_INSTALL}=abc123" in run
        assert f"--label={rt.LABEL_PERSON}=2" in run
    (tmp_path / "new").mkdir()
    fresh = Recorder(
        tmp_path / "new", {"volume exists": (1, "", ""), "volume inspect": (0, MOUNTPOINT, "")}
    )
    podman_runtime(tmp_path, fresh).create(spec_)
    created = next(c for c in fresh.calls() if c[3:5] == ["volume", "create"])
    assert f"--label={rt.LABEL_INSTALL}=abc123" in created


# --- a person's Claude sign-in --------------------------------------------------------

MOUNTPOINT = "/var/lib/rcman/.local/share/containers/storage/volumes/rcman-person-2/_data"


def account_recorder(tmp_path: Path, volume_exists: int = 1) -> Recorder:
    return Recorder(
        tmp_path,
        {
            "volume exists": (volume_exists, "", ""),
            "volume inspect": (0, MOUNTPOINT + "\n", ""),
            # the workspace's volume is its person's
            "volume inspect labels": (0, '{"rcman.person": "2"}', ""),
        },
    )


def runs(recorder: Recorder) -> list[list[str]]:
    return [c[3:] for c in recorder.calls() if c[3] == "run"]


def test_a_persons_account_is_a_labelled_volume_holding_a_private_file(tmp_path):
    recorder = account_recorder(tmp_path)
    podman_runtime(tmp_path, recorder).ensure_account(2, 3)
    created = next(c[3:] for c in recorder.calls() if c[3:5] == ["volume", "create"])
    assert created[-1] == rt.account_volume(2) == "rcman-person-2"
    assert "--label=rcman.person=2" in created
    (made,) = runs(recorder)
    base = 3 * rt.RANGE
    assert "--rm" in made and "--network=none" in made
    assert f"--uidmap=0:{base}:{rt.RANGE}" in made and f"--gidmap=0:{base}:{rt.RANGE}" in made
    assert not [a for a in made if a.startswith("--label")]  # never taken for a workspace
    script = made[made.index("-c") + 1]
    assert "umask 077" in script and ": >>" in script  # made 0600, never truncated
    assert made[-2].endswith(f"/{rt.CREDENTIALS}")
    assert made[-1] == f"{rt.ACCOUNT_MOUNT}/{rt.GIT_DIR}"  # the git directory is made sure of too
    assert 'mkdir -p "$2"' in script and ': >> "$2/credential"' in script


def test_an_existing_account_volume_is_not_made_again(tmp_path):
    recorder = account_recorder(tmp_path, volume_exists=0)
    podman_runtime(tmp_path, recorder).ensure_account(2, 3)
    assert not [c for c in recorder.calls() if c[3:5] == ["volume", "create"]]
    assert len(runs(recorder)) == 1  # the file is still made sure of


def test_a_workspace_binds_only_its_persons_credentials_file(tmp_path):
    recorder = account_recorder(tmp_path)
    podman_runtime(tmp_path, recorder).create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    workspace = next(r for r in runs(recorder) if "--name=rcman-ws-7" in r)
    target = f"{rt.VOLUME_MOUNT}/{rt.CONFIG}/{rt.CREDENTIALS}"
    assert f"--volume={MOUNTPOINT}/{rt.CREDENTIALS}:{target}" in workspace
    git = f"{rt.VOLUME_MOUNT}/{rt.HOME}/.config/git"
    assert f"--volume={MOUNTPOINT}/{rt.GIT_DIR}:{git}" in workspace  # a folder, not a file
    assert not [a for a in workspace if a.startswith("--volume=rcman-person-")]  # not by name
    made_first = [i for i, r in enumerate(runs(recorder)) if "--rm" in r]
    assert made_first and made_first[0] < runs(recorder).index(workspace)


def test_a_sign_in_container_removes_itself_and_is_labelled_with_its_person_only(tmp_path):
    recorder = account_recorder(tmp_path, volume_exists=0)
    name = podman_runtime(tmp_path, recorder).start_sign_in(2, 3, seconds=900)
    assert name == rt.sign_in_name(2)
    started = next(r for r in runs(recorder) if f"--name={name}" in r)
    assert "--rm" in started and "--detach" in started and "--init" in started
    assert started[-2:] == ["sleep", "900"]
    labels = [a for a in started if a.startswith("--label")]
    assert labels == ["--label=rcman.person=2"]
    assert f"--uidmap=0:{3 * rt.RANGE}:{rt.RANGE}" in started
    target = f"{rt.VOLUME_MOUNT}/{rt.CONFIG}/{rt.CREDENTIALS}"
    assert f"--volume={MOUNTPOINT}/{rt.CREDENTIALS}:{target}" in started
    git = f"{rt.VOLUME_MOUNT}/{rt.HOME}/.config/git"
    assert f"--volume={MOUNTPOINT}/{rt.GIT_DIR}:{git}" in started
    removed = [c[3:] for c in recorder.calls() if c[3] == "rm"]
    assert removed and removed[0][-1] == name  # one left from before goes first


def test_clearing_an_account_empties_its_file_in_place(tmp_path):
    recorder = account_recorder(tmp_path, volume_exists=0)
    podman_runtime(tmp_path, recorder).clear_account(2, 3)
    (cleared,) = runs(recorder)
    assert cleared[cleared.index("-c") + 1] == 'umask 077 && : > "$1"'
    assert "--volume=rcman-person-2:/account" in cleared


def test_removing_an_account_removes_its_volume(tmp_path):
    recorder = account_recorder(tmp_path, volume_exists=0)
    podman_runtime(tmp_path, recorder).remove_account(2)
    assert ["volume", "rm", "--force", "rcman-person-2"] in [c[3:] for c in recorder.calls()]


def test_the_host_runtime_shares_one_private_file_per_person(host):
    host.create(rt.Spec(name="rcman-ws-1", slot=1, workspace=1, person=4))
    found = host.root / ".accounts" / "4" / rt.CREDENTIALS
    assert found.exists() and (found.stat().st_mode & 0o777) == 0o600
    assert (found.parent.stat().st_mode & 0o777) == 0o700
    assert host.read_account(4, 1) == b""
    assert ".accounts" not in host.labelled()


# --- a person's git account -------------------------------------------------------------


def test_a_git_file_is_written_by_a_container_from_stdin_by_a_rename(tmp_path):
    recorder = account_recorder(tmp_path, volume_exists=0)
    secret = b"password=SECRET-TOKEN-1\n"
    podman_runtime(tmp_path, recorder).put_account(2, 3, "credential", secret)
    (written,) = runs(recorder)
    base = 3 * rt.RANGE
    assert "--rm" in written and "--network=none" in written
    assert "--interactive" in written or "-i" in written
    assert f"--uidmap=0:{base}:{rt.RANGE}" in written
    assert not [a for a in written if a.startswith("--label")]
    assert "--volume=rcman-person-2:/account" in written
    script = written[written.index("-c") + 1]
    assert script.startswith("umask 077") and ".new" in script and "mv -f" in script
    assert 'mkdir -p "$2"' in script  # the folder is made if it is not there
    assert written[-2:] == [f"{rt.ACCOUNT_MOUNT}/{rt.GIT_DIR}", "credential"]
    assert recorder.stdin() == secret  # the data went in on stdin
    assert "SECRET-TOKEN-1" not in " ".join(sum(recorder.calls(), []))  # and in no argv


def test_a_git_file_nobody_asked_for_is_refused_before_podman_is_run(tmp_path):
    recorder = account_recorder(tmp_path)
    for name in ("../credential", "x", ""):
        with pytest.raises(ValueError):
            podman_runtime(tmp_path, recorder).put_account(2, 3, name, b"x")
    assert recorder.calls() == []


def test_every_layout_has_the_timeout_tool(host):
    assert rt.PODMAN_LAYOUT.tool("timeout") == [f"{rt.TOOLS_MOUNT}/bin/busybox", "timeout"]
    assert host.layout("rcman-ws-1").tool("timeout")[0].endswith("/timeout")


def test_the_host_runtime_shares_one_private_git_directory_per_person(host):
    host.create(rt.Spec(name="rcman-ws-1", slot=1, workspace=1, person=4))
    shared = host.root / ".accounts" / "4" / rt.GIT_DIR
    assert shared.is_dir() and (shared.stat().st_mode & 0o777) == 0o700
    linked = Path(host.layout("rcman-ws-1").home) / ".config" / "git"
    assert linked.is_symlink() and linked.resolve() == shared.resolve()
    host.put_account(4, 1, "credential", b"password=x\n")
    assert (shared / "credential").read_bytes() == b"password=x\n"
    assert ((shared / "credential").stat().st_mode & 0o777) == 0o600
    # no .new left behind
    assert sorted(p.name for p in shared.iterdir()) == ["config.yml", "credential"]


# The programs an account container's scripts run, which its image has.
ACCOUNT_TOOLS = ("mkdir", "cat", "rm", "mv", "mktemp", "ln")


def account_tools(tmp_path: Path) -> Path:
    """A folder holding only ACCOUNT_TOOLS, for a PATH."""
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    for tool in ACCOUNT_TOOLS:
        (tools / tool).unlink(missing_ok=True)
        (tools / tool).symlink_to(shutil.which(tool))
    return tools


def ensure_argv(tmp_path: Path) -> list[str]:
    return ["/bin/sh", "-c", rt.ENSURE_SCRIPT, "sh", str(tmp_path / "cred"), str(tmp_path / "git")]


def run_ensure_script(tmp_path: Path) -> subprocess.CompletedProcess:
    """rt.ENSURE_SCRIPT run by a shell with only the tools an account container has; the
    credentials file is ``tmp_path/cred`` and the git folder ``tmp_path/git``."""
    tools = account_tools(tmp_path)
    return subprocess.run(ensure_argv(tmp_path), capture_output=True, env={"PATH": str(tools)})


def test_making_sure_of_an_account_makes_gh_s_configuration_if_it_is_not_there(tmp_path):
    done = run_ensure_script(tmp_path)
    assert done.returncode == 0, done.stderr
    folder = tmp_path / "git"
    assert (folder / "config.yml").read_text() == rt.GH_CONFIG
    assert ((folder / "config.yml").stat().st_mode & 0o777) == 0o600
    assert sorted(p.name for p in folder.iterdir()) == ["config.yml", "credential"]
    (folder / "config.yml").write_text("planted\n")
    (folder / "credential").write_text("password=kept\n")
    assert run_ensure_script(tmp_path).returncode == 0
    assert (folder / "config.yml").read_text() == "planted\n"  # never truncated or replaced
    assert (folder / "credential").read_text() == "password=kept\n"


def test_a_gh_configuration_that_is_a_link_it_cannot_follow_is_left_and_ensuring_succeeds(
    tmp_path,
):
    """A dotfile manager's link into the home folder, which an account container, mounting
    only the volume, cannot follow: the name is taken, so nothing is made, and every script
    that makes sure of the account still runs."""
    folder = tmp_path / "git"
    folder.mkdir()
    (folder / "config.yml").symlink_to("/nowhere/config.yml")
    done = run_ensure_script(tmp_path)
    assert done.returncode == 0, done.stderr
    assert os.readlink(folder / "config.yml") == "/nowhere/config.yml"  # left as it was
    assert sorted(p.name for p in folder.iterdir()) == ["config.yml", "credential"]


def test_the_host_runtime_leaves_a_gh_configuration_that_is_a_link_it_cannot_follow(host):
    folder = host.root / ".accounts" / "4" / rt.GIT_DIR
    folder.mkdir(parents=True)
    (folder / "config.yml").symlink_to("/nowhere/config.yml")
    host.ensure_account(4, 1)
    assert os.readlink(folder / "config.yml") == "/nowhere/config.yml"
    assert sorted(p.name for p in folder.iterdir()) == ["config.yml", "credential"]
    host.put_account(4, 1, "config.yml", rt.GH_CONFIG.encode())  # a put replaces it
    assert not (folder / "config.yml").is_symlink()
    assert (folder / "config.yml").read_text() == rt.GH_CONFIG


def test_the_host_runtime_makes_gh_s_configuration_if_it_is_not_there_and_never_replaces_it(
    host,
):
    host.ensure_account(4, 1)
    folder = host.root / ".accounts" / "4" / rt.GIT_DIR
    assert (folder / "config.yml").read_text() == rt.GH_CONFIG
    assert ((folder / "config.yml").stat().st_mode & 0o777) == 0o600
    (folder / "config.yml").write_text("planted\n")
    host.ensure_account(4, 1)
    assert (folder / "config.yml").read_text() == "planted\n"


def test_the_files_rcman_writes_in_the_git_folder_are_git_s_and_gh_s():
    assert rt.ACCOUNT_FILES == (*rt.GIT_FILES, *rt.GH_FILES)
    assert rt.GH_FILES == ("config.yml", "hosts.yml")


@pytest.mark.parametrize("name", ["config.yml", "hosts.yml"])
def test_gh_s_files_are_written_by_the_container_as_git_s_are(tmp_path, name):
    recorder = account_recorder(tmp_path, volume_exists=0)
    podman_runtime(tmp_path, recorder).put_account(2, 3, name, b"x: y\n")
    (written,) = runs(recorder)
    assert written[-2:] == [f"{rt.ACCOUNT_MOUNT}/{rt.GIT_DIR}", name]
    assert recorder.stdin() == b"x: y\n"


def run_put_script(tmp_path: Path, mv_fails: bool) -> subprocess.CompletedProcess:
    """rt.PUT_SCRIPT run by a shell, as the account container runs it, with a ``mv`` that
    fails if ``mv_fails``; the git folder is ``tmp_path/git``."""
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    for tool in ("mkdir", "cat", "rm", "mktemp", "ln"):
        (tools / tool).unlink(missing_ok=True)
        (tools / tool).symlink_to(shutil.which(tool))
    (tools / "mv").unlink(missing_ok=True)
    if mv_fails:
        (tools / "mv").write_text("#!/bin/sh\nexit 1\n")
        (tools / "mv").chmod(0o755)
    else:
        (tools / "mv").symlink_to(shutil.which("mv"))
    return subprocess.run(
        [
            "/bin/sh",
            "-c",
            rt.PUT_SCRIPT,
            "sh",
            str(tmp_path / "cred"),
            str(tmp_path / "git"),
            "credential",
        ],
        input=b"password=LEFT-BEHIND\n",
        capture_output=True,
        env={"PATH": str(tools)},
    )


def test_a_git_file_that_cannot_be_put_in_place_leaves_no_temporary_copy(tmp_path):
    done = run_put_script(tmp_path, mv_fails=False)
    assert done.returncode == 0, done.stderr
    made = sorted(p.name for p in (tmp_path / "git").iterdir())
    assert made == ["config.yml", "credential"]  # control
    (tmp_path / "second").mkdir()
    failed = run_put_script(tmp_path / "second", mv_fails=True)
    assert failed.returncode != 0
    left = [p.name for p in (tmp_path / "second" / "git").iterdir()]
    assert "credential.new" not in left, left
    assert b"LEFT-BEHIND" not in b"".join(
        p.read_bytes() for p in (tmp_path / "second" / "git").iterdir()
    )


@pytest.mark.parametrize("fault", ["write", "replace"])
def test_the_host_runtime_leaves_no_temporary_copy_of_a_git_file_that_failed(
    host, monkeypatch, fault
):
    host.put_account(4, 1, "credential", b"password=old\n")  # positive control: it works
    folder = host.root / ".accounts" / "4" / rt.GIT_DIR
    assert sorted(p.name for p in folder.iterdir()) == ["config.yml", "credential"]
    if fault == "replace":

        def refused(*args):
            raise OSError("refused")

        monkeypatch.setattr(rt.os, "replace", refused)
        data = b"password=LEFT-BEHIND\n"
    else:
        data = "password=LEFT-BEHIND\n"  # text, which the file refuses
    with pytest.raises((OSError, TypeError)):
        host.put_account(4, 1, "credential", data)
    assert sorted(p.name for p in folder.iterdir()) == ["config.yml", "credential"]
    assert (folder / "credential").read_bytes() == b"password=old\n"


# --- a container of the user's own: the uid map, the user, the image ----------------------


def ids_of(arguments: list[str], kind: str) -> list[tuple[int, int, int]]:
    """The (inside, outside, length) stretches of the ``--uidmap`` or ``--gidmap`` options."""
    found = []
    for argument in arguments:
        if argument.startswith(f"--{kind}map="):
            found.append(tuple(int(n) for n in argument.split("=", 1)[1].split(":")))
    return found


def mapped(stretches: list[tuple[int, int, int]]) -> dict[int, int]:
    """Container id -> host id, for the stretches; an id mapped twice fails the test."""
    result: dict[int, int] = {}
    for inside, outside, length in stretches:
        for offset in range(length):
            assert inside + offset not in result, f"{inside + offset} is mapped twice"
            result[inside + offset] = outside + offset
    return result


OWN_IDS = [1, 2, 1000, rt.RANGE - 2, rt.RANGE - 1]


def test_an_ordinary_map_is_the_one_line_it_always_was():
    base = 3 * rt.RANGE
    assert IDS(3) == [
        f"--uidmap=0:{base}:{rt.RANGE}",
        f"--gidmap=0:{base}:{rt.RANGE}",
    ]
    assert IDS(3, 0, 0) == IDS(3)


@pytest.mark.parametrize("own", OWN_IDS)
def test_a_swapped_map_swaps_the_users_id_with_roots_and_moves_nothing_else(own):
    base = 3 * rt.RANGE
    options = IDS(3, own, own)
    uid = mapped(ids_of(options, "uid"))
    assert sorted(uid) == list(range(rt.RANGE))  # every id of the container is mapped, once
    assert sorted(uid.values()) == list(range(base, base + rt.RANGE))  # onto every host id, once
    assert uid[own] == base  # the user is the host id root has in the ordinary map
    assert uid[0] == base + own  # and root is the user's
    assert all(uid[i] == base + i for i in range(1, rt.RANGE) if i != own)
    assert mapped(ids_of(options, "gid")) == uid  # the same for groups here


@pytest.mark.parametrize(("uid", "gid"), [(1000, 0), (0, 1000), (5, 1000)])
def test_the_user_and_the_group_are_swapped_each_on_its_own(uid, gid):
    options = IDS(2, uid, gid)
    base = 2 * rt.RANGE
    assert mapped(ids_of(options, "uid"))[uid] == base
    assert mapped(ids_of(options, "gid"))[gid] == base
    if uid == 0:
        assert ids_of(options, "uid") == [(0, base, rt.RANGE)]  # nothing swapped
    if gid == 0:
        assert ids_of(options, "gid") == [(0, base, rt.RANGE)]


def test_the_edges_leave_out_the_stretch_that_would_be_empty():
    base = rt.RANGE
    first = ids_of(IDS(1, 1, 1), "uid")
    assert first == [(0, base + 1, 1), (1, base, 1), (2, base + 2, rt.RANGE - 2)]
    last = ids_of(IDS(1, rt.RANGE - 1, 1), "uid")
    assert last == [
        (0, base + rt.RANGE - 1, 1),
        (1, base + 1, rt.RANGE - 2),
        (rt.RANGE - 1, base, 1),
    ]
    assert all(length > 0 for _, _, length in first + last)


@pytest.mark.parametrize("own", [rt.RANGE, rt.RANGE + 1, -1])
def test_an_id_beyond_the_range_is_refused_before_anything_is_made(tmp_path, own):
    with pytest.raises(rt.RuntimeFailed):
        IDS(3, own, 0)
    with pytest.raises(rt.RuntimeFailed):
        IDS(3, 0, own)
    recorder = account_recorder(tmp_path)
    with pytest.raises(rt.RuntimeFailed):
        podman_runtime(tmp_path, recorder).create(
            rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2, uid=own)
        )
    assert recorder.calls() == []  # nothing was asked of podman


def test_slot_zero_is_refused_for_a_swapped_map_too():
    with pytest.raises(rt.RuntimeFailed):
        IDS(0, 1000, 1000)


def owned_spec(**more) -> rt.Spec:
    return rt.Spec(
        "rcman-ws-7",
        slot=3,
        workspace=7,
        person=2,
        image="docker.io/library/node:20",
        user="dev",
        uid=1234,
        gid=1235,
        env={"A": "b", "C": "d e"},
        **more,
    )


def workspace_run(recorder: Recorder) -> list[str]:
    return next(r for r in runs(recorder) if "--name=rcman-ws-7" in r)


def test_a_container_of_the_users_own_is_made_from_the_image_as_the_user(tmp_path):
    recorder = account_recorder(tmp_path)
    podman_runtime(tmp_path, recorder).create(owned_spec(groups=(5000, 5001)))
    run = workspace_run(recorder)
    base = 3 * rt.RANGE
    assert mapped(ids_of(run, "uid"))[1234] == base and mapped(ids_of(run, "uid"))[0] == base + 1234
    assert mapped(ids_of(run, "gid"))[1235] == base and mapped(ids_of(run, "gid"))[0] == base + 1235
    for option in (
        "--user=dev",
        "--group-add=5000",
        "--group-add=5001",
        "--env=A=b",
        "--env=C=d e",
        "--tmpfs=/run/rcman:mode=0700,U",  # uid= and gid= are refused where this runs
        "--volume=rcman-ws-7:/rcman",
        "--label=rcman.workspace=7",
        "--init",
    ):
        assert option in run, option
    # The image's own entrypoint is not run, and nothing of rcman's is the image's.
    assert "--entrypoint=/opt/rcman/bin/busybox" in run
    after = run.index("docker.io/library/node:20")
    assert run[after + 1 :] == ["sleep", "2147483647"]
    assert "localhost/rcman-workspace:test" not in run
    assert not [a for a in run if a.startswith(("--privileged", "--cap", "-p"))]
    # the two every workspace container has, and no other
    assert [a for a in run if a.startswith("--security-opt")] == workspace_security(
        tmp_path / "tools"
    )


def test_the_default_container_is_made_as_before_with_no_user_and_the_workspace_image(tmp_path):
    recorder = account_recorder(tmp_path)
    podman_runtime(tmp_path, recorder).create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    run = workspace_run(recorder)
    assert "--tmpfs=/run/rcman:mode=0700" in run  # no ,U
    assert not [a for a in run if a.startswith(("--user", "--group-add", "--env"))]
    assert ids_of(run, "uid") == [(0, 3 * rt.RANGE, rt.RANGE)]
    assert "localhost/rcman-workspace:test" in run
    assert not [r for r in runs(recorder) if "--rm" in r and "--name=rcman-ws-7" in r]
    others = [
        r
        for r in runs(recorder)
        if "--volume=rcman-ws-7:/rcman" in r and "--name=rcman-ws-7" not in r
    ]
    assert others == []  # the folders are made by the ordinary map's root already


def test_the_folders_beside_the_mounts_are_made_by_the_ordinary_maps_root_first(tmp_path):
    recorder = account_recorder(tmp_path)
    podman_runtime(tmp_path, recorder).create(owned_spec())
    prepare = next(r for r in runs(recorder) if "--rm" in r and "--volume=rcman-ws-7:/rcman" in r)
    run = workspace_run(recorder)
    assert runs(recorder).index(prepare) < runs(recorder).index(run)
    assert ids_of(prepare, "uid") == [(0, 3 * rt.RANGE, rt.RANGE)]  # the ordinary map
    assert not [a for a in prepare if a.startswith("--user")]
    folders = prepare[prepare.index("sh", prepare.index("-c")) + 1 :]
    assert folders == [
        f"{rt.VOLUME_MOUNT}/{rt.WORK}",
        f"{rt.VOLUME_MOUNT}/{rt.CONFIG}",
        f"{rt.VOLUME_MOUNT}/{rt.HOME}/.config",  # where the git folder is bound
    ]
    assert (
        f"{rt.VOLUME_MOUNT}/{rt.HOME}/.config/git" == f"{rt.VOLUME_MOUNT}/{rt.HOME}/{rt.GIT_HOME}"
    )


def test_the_first_run_of_an_image_may_take_longer_than_any_other_call(tmp_path):
    recorder = account_recorder(tmp_path)
    runtime = podman_runtime(tmp_path, recorder)
    seen: dict[str, float] = {}
    real = runtime._podman

    def watched(*args, **kwargs):
        if args[0] == "run" and "--name=rcman-ws-7" in args:
            seen["timeout"] = kwargs.get("timeout", rt.PODMAN_TIMEOUT)
        return real(*args, **kwargs)

    runtime._podman = watched
    runtime.create(owned_spec())
    assert seen["timeout"] == rt.IMAGE_CREATE_TIMEOUT > rt.PODMAN_TIMEOUT
    runtime.create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    assert seen["timeout"] == rt.PODMAN_TIMEOUT


def test_discarding_removes_the_container_and_keeps_the_volume_and_removing_takes_both(tmp_path):
    recorder = Recorder(tmp_path, {"volume exists": (0, "", "")})
    runtime = podman_runtime(tmp_path, recorder)
    runtime.discard("rcman-ws-7")
    calls = [c[3:] for c in recorder.calls()]
    assert any(c[0] == "rm" and c[-1] == "rcman-ws-7" for c in calls)
    assert not [c for c in calls if c[:2] == ["volume", "rm"]]
    runtime.remove("rcman-ws-7")
    calls = [c[3:] for c in recorder.calls()]
    assert [c for c in calls if c[:2] == ["volume", "rm"]]  # the control: a remove does


def test_an_exec_in_a_folder_asks_podman_for_it(tmp_path):
    recorder = Recorder(tmp_path, {})
    runtime = podman_runtime(tmp_path, recorder)
    runtime.exec("rcman-ws-7", ["pwd"], cwd="/rcman/work")
    runtime.exec("rcman-ws-7", ["pwd"])
    first, second = [c[3:] for c in recorder.calls() if c[3] == "exec"]
    marked = rt.marked_exec(["pwd"])
    assert marked[:3] == [f"{rt.TOOLS_MOUNT}/bin/busybox", "sh", "-c"] and marked[-1] == "pwd"
    assert first == ["exec", "--workdir=/rcman/work", "rcman-ws-7", *marked]
    assert second == ["exec", "rcman-ws-7", *marked]  # no folder asked for


def fake_podman(tmp_path: Path, body: str, container_exists: bool = False) -> rt.PodmanRuntime:
    """A podman that is the Python ``body``, which has ``sys`` and ``args``, rcman's own
    arguments after ``--remote --url <socket>``; ``container exists`` answers
    ``container_exists``, as no workspace's container is there unless a test says so."""
    program = tmp_path / "podman"
    program.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys, time\n"
        "args = sys.argv[4:]\n"
        f"with open({str(tmp_path / 'calls')!r}, 'a') as log:\n"
        "    log.write(json.dumps(args) + '\\n')\n"
        "if args[:2] == ['container', 'exists']:\n"
        f"    sys.exit({0 if container_exists else rt.MISSING})\n" + body
    )
    program.chmod(0o755)
    return rt.PodmanRuntime(
        rt.PodmanConfig(
            socket=Path("/run/user/990/podman/podman.sock"),
            image="localhost/rcman-workspace:test",
            tools_dir=told(tmp_path / "tools"),
            claude_store=Path("/var/lib/rcman/claude"),
            podman=str(program),
        )
    )


def fake_calls(tmp_path: Path) -> list[list[str]]:
    return [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]


def test_a_pull_passes_each_line_it_prints_on_and_fails_with_the_last_words(tmp_path):
    ok = fake_podman(tmp_path, "print('Trying to pull x')\nprint('Copying blob a')\n")
    lines: list[str] = []
    ok.pull("docker.io/library/x", lines.append)
    assert lines == ["Trying to pull x", "Copying blob a"]
    assert fake_calls(tmp_path) == [["pull", "docker.io/library/x"]]
    other = tmp_path / "bad"
    other.mkdir()
    bad = fake_podman(
        other,
        "print('Trying to pull x')\n"
        "print('Error: manifest unknown', file=sys.stderr)\nsys.exit(125)\n",
    )
    with pytest.raises(rt.RuntimeFailed) as failed:
        bad.pull("x")
    assert "manifest unknown" in str(failed.value)


def test_a_pull_that_cannot_reach_podman_is_unavailable(tmp_path):
    runtime = fake_podman(
        tmp_path, "print('Cannot connect to Podman', file=sys.stderr)\nsys.exit(125)\n"
    )
    with pytest.raises(rt.RuntimeUnavailable):
        runtime.pull("x")


def test_a_registry_that_refuses_the_connection_fails_the_pull_and_is_not_an_unreachable_podman(
    tmp_path,
):
    """Podman exits with the same code for both, and says "connect" in both."""
    refused = fake_podman(
        tmp_path,
        "print('Trying to pull x...')\n"
        "print('Error: pinging container registry x: dial tcp: connect: connection refused',"
        " file=sys.stderr)\nsys.exit(125)\n"
        "",
    )
    with pytest.raises(rt.RuntimeFailed) as failed:
        refused.pull("x")
    assert not isinstance(failed.value, rt.RuntimeUnavailable)
    assert "connection refused" in str(failed.value)


def test_a_program_that_exits_as_podman_does_after_failing_to_connect_is_a_result(tmp_path):
    runtime = fake_podman(
        tmp_path,
        "print('curl: could not connect to the host', file=sys.stderr)\nsys.exit(125)\n",
    )
    assert runtime.exec("rcman-ws-7", ["curl", "x"]).code == 125
    (tmp_path / "gone").mkdir()
    unreachable = fake_podman(
        tmp_path / "gone",
        "print('Cannot connect to Podman. Please verify', file=sys.stderr)\nsys.exit(125)\n",
    )
    with pytest.raises(rt.RuntimeUnavailable):
        unreachable.exec("rcman-ws-7", ["true"])


def test_an_image_already_here_is_used_when_its_pull_fails_and_one_not_here_is_a_failure(tmp_path):
    body = (
        "if args[:2] == ['image', 'exists']:\n    sys.exit(0 if args[2] == 'here' else 1)\n"
        "print('Error: no registry', file=sys.stderr)\nsys.exit(125)\n"
    )
    runtime = fake_podman(tmp_path, body)
    lines: list[str] = []
    runtime.pull("here", lines.append)  # no failure: the copy that is here is the image
    assert lines and lines[-1] == rt.USING_LOCAL_IMAGE
    assert ["pull", "here"] in fake_calls(tmp_path)  # the registry was asked first
    with pytest.raises(rt.RuntimeFailed) as failed:
        runtime.pull("not-here")
    assert "no registry" in str(failed.value)


def test_a_cancelled_pull_does_not_fall_back_to_the_image_here(tmp_path):
    import threading

    runtime = fake_podman(
        tmp_path, "if args[:2] == ['image', 'exists']:\n    sys.exit(0)\ntime.sleep(60)\n"
    )
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(rt.RuntimeFailed):
        runtime.pull("here", cancel=cancel)


def test_a_container_made_from_an_image_never_pulls_one_itself(tmp_path):
    recorder = account_recorder(tmp_path)
    podman_runtime(tmp_path, recorder).create(owned_spec())
    assert "--pull=never" in workspace_run(recorder)
    (tmp_path / "default").mkdir()
    recorder = account_recorder(tmp_path / "default")
    podman_runtime(tmp_path / "default", recorder).create(
        rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2)
    )
    assert "--pull=never" not in workspace_run(recorder)


LOCAL_IMAGE = "if args[:2] == ['image', 'exists']:\n    sys.exit(0)\n"  # the image is here


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def waited_for(path: Path, seconds: float = 30.0) -> int:
    """The process id a fake podman wrote in ``path`` once it was running: waited for, up to a
    bound that only a fake that never ran can reach."""
    deadline = time.monotonic() + seconds
    while True:
        text = path.read_text() if path.exists() else ""
        if text.strip().isdigit():
            return int(text)
        assert time.monotonic() < deadline, f"{path} was never written"
        time.sleep(0.02)


def test_a_pull_is_stopped_when_it_is_cancelled_and_podman_ends_with_it(tmp_path, monkeypatch):
    import threading

    monkeypatch.setattr(rt, "PULL_POLL_SECONDS", 0.05)
    marker, pid_file = tmp_path / "ended", tmp_path / "pid"
    runtime = fake_podman(
        tmp_path,
        LOCAL_IMAGE  # one that is here is no reason to go on with a pull that was stopped
        + f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "print('Copying blob a', flush=True)\n"
        f"time.sleep(60)\nopen({str(marker)!r}, 'w').write('finished')\n",
    )
    cancel = threading.Event()
    seen = threading.Event()

    def progress(line: str) -> None:  # cancelled as soon as it is known to be running
        seen.set()
        cancel.set()

    began = time.monotonic()
    with pytest.raises(rt.RuntimeFailed):
        runtime.pull("x", progress, cancel)
    assert seen.is_set()  # positive control: it was running
    assert time.monotonic() - began < 30
    assert not alive(waited_for(pid_file))  # the child ended, not merely was left
    assert not marker.exists()
    assert [c for c in fake_calls(tmp_path) if c[:2] == ["image", "exists"]] == []


def test_a_pull_that_takes_too_long_is_stopped_and_does_not_fall_back_to_a_local_image(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(rt, "PULL_POLL_SECONDS", 0.05)
    monkeypatch.setattr(rt, "PULL_TIMEOUT", 3.0)
    pid_file = tmp_path / "pid"
    runtime = fake_podman(
        tmp_path,
        LOCAL_IMAGE + f"open({str(pid_file)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)\n",
    )
    with pytest.raises(rt.RuntimeFailed) as failed:
        runtime.pull("x")
    assert "in time" in str(failed.value)
    assert not alive(waited_for(pid_file))
    assert [c for c in fake_calls(tmp_path) if c[:2] == ["image", "exists"]] == []
    # The control: a pull that fails by itself does use the local image, so that the above is
    # the timeout's own doing.
    other = tmp_path / "failing"
    other.mkdir()
    falls_back = fake_podman(other, LOCAL_IMAGE + "if args[0] == 'pull':\n    sys.exit(1)\n")
    falls_back.pull("x")
    assert [c for c in fake_calls(other) if c[:2] == ["image", "exists"]]


def tar_of(files: dict[str, bytes]) -> bytes:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def image_podman(tmp_path: Path, cp_fails: bool = False, big: bool = False) -> rt.PodmanRuntime:
    passwd = b"root:x:0:0::/root:/bin/sh\ndev:x:1234:1234::/home/dev:/bin/sh\n"
    inspected = {
        "Labels": {"devcontainer.metadata": '[{"remoteUser": "dev"}]', "x": "y"},
        "User": "dev",
        "Config": {"Env": ["PATH=/a:/b", "EMPTY=", "NODE=20"]},
    }
    (tmp_path / "tar").write_bytes(
        tar_of({"passwd": b"x" * (rt.MAX_ID_FILE_BYTES + 1) if big else passwd})
    )
    return fake_podman(
        tmp_path,
        "if args[:2] == ['image', 'inspect']:\n"
        f"    sys.stdout.write({json.dumps(inspected)!r})\n"
        "elif args[0] == 'cp':\n"
        f"    if {cp_fails}:\n"
        "        print('Error: could not be found on container x: No such file or directory',\n"
        "              file=sys.stderr)\n"
        "        sys.exit(125)\n"
        f"    sys.stdout.buffer.write(open({str(tmp_path / 'tar')!r}, 'rb').read())\n",
    )


def test_an_image_is_read_without_being_run_or_mapped(tmp_path):
    facts = image_podman(tmp_path).image_facts("docker.io/library/node:20")
    assert facts.passwd.startswith("root:x:0:0") and "dev:x:1234" in facts.passwd
    assert facts.user == "dev"
    assert facts.env == {"PATH": "/a:/b", "EMPTY": "", "NODE": "20"}
    assert facts.labels["devcontainer.metadata"] == '[{"remoteUser": "dev"}]'
    calls = fake_calls(tmp_path)
    made = next(c for c in calls if c[0] == "create")
    assert not [a for a in made if "map" in a or a.startswith(("--user", "--userns"))]
    assert "--network=none" in made and "--pull=never" in made
    assert not [c for c in calls if c[0] in ("start", "run", "exec")]  # never run
    name = next(a for a in made if a.startswith("--name=")).split("=", 1)[1]
    assert name.startswith(rt.PROBE_PREFIX)
    assert any(c[0] == "rm" and c[-1] == name for c in calls)  # removed when done
    copied = [c[1] for c in calls if c[0] == "cp"]
    # and what /etc is, which is a file here, so that nothing below it is asked
    assert copied == [f"{name}:/etc/passwd", f"{name}:/etc/group", f"{name}:/etc"] and all(
        c[2] == "-" for c in calls if c[0] == "cp"
    )


def test_a_file_the_image_does_not_have_or_that_is_too_big_is_read_as_empty(tmp_path):
    none = image_podman(tmp_path, cp_fails=True).image_facts("x")
    assert (none.passwd, none.group) == ("", "")
    (tmp_path / "big").mkdir()
    big = image_podman(tmp_path / "big", big=True).image_facts("x")
    assert big.passwd == ""


def test_the_probe_container_is_removed_even_when_reading_fails(tmp_path):
    runtime = fake_podman(
        tmp_path,
        "if args[:2] == ['image', 'inspect']:\n    sys.stdout.write('{}')\n"
        "elif args[0] == 'cp':\n    print('Error: the storage broke', file=sys.stderr)\n"
        "    raise SystemExit(125)\n",
    )
    with pytest.raises(rt.RuntimeFailed) as failed:  # not the same as an image without the file
        runtime.image_facts("x")
    assert "the storage broke" in str(failed.value)
    assert any(c[0] == "rm" for c in fake_calls(tmp_path))
    (tmp_path / "second").mkdir()
    broken = fake_podman(tmp_path / "second", "if args[0] == 'image':\n    sys.exit(1)\n")
    with pytest.raises(rt.RuntimeFailed):
        broken.image_facts("x")


def test_only_podman_saying_the_file_is_not_there_means_the_image_has_none(tmp_path):
    for number, said in enumerate(
        ("No such file or directory", '"/etc/group" could not be found on container x', "boom")
    ):
        folder = tmp_path / str(number)
        folder.mkdir()
        runtime = fake_podman(
            folder,
            "if args[:2] == ['image', 'inspect']:\n    sys.stdout.write('{}')\n"
            f"elif args[0] == 'cp':\n    print({said!r}, file=sys.stderr)\n    sys.exit(125)\n",
        )
        if said == "boom":
            with pytest.raises(rt.RuntimeFailed):
                runtime.image_facts("x")
        else:
            assert runtime.image_facts("x").group == ""


def test_a_podman_cp_that_stalls_is_stopped_at_the_timeout_and_is_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(rt, "PULL_POLL_SECONDS", 0.05)
    monkeypatch.setattr(rt, "PODMAN_TIMEOUT", 3.0)
    pid_file = tmp_path / "pid"
    runtime = fake_podman(
        tmp_path,
        "if args[:2] == ['image', 'inspect']:\n    sys.stdout.write('{}')\n"
        "elif args[0] == 'cp':\n"
        f"    open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "    sys.stdout.buffer.write(b'partial')\n    sys.stdout.flush()\n"
        "    time.sleep(60)\n",
    )
    with pytest.raises(rt.RuntimeFailed) as failed:
        runtime.image_facts("x")
    assert "in time" in str(failed.value)
    assert not alive(waited_for(pid_file))
    assert any(c[0] == "rm" for c in fake_calls(tmp_path))  # and the probe container is gone


def test_a_podman_cp_is_stopped_when_the_workspace_is_cancelled(tmp_path, monkeypatch):
    import threading

    monkeypatch.setattr(rt, "PULL_POLL_SECONDS", 0.05)
    pid_file = tmp_path / "pid"
    runtime = fake_podman(
        tmp_path,
        "if args[:2] == ['image', 'inspect']:\n    sys.stdout.write('{}')\n"
        "elif args[0] == 'cp':\n"
        f"    open({str(pid_file)!r}, 'w').write(str(os.getpid()))\n"
        "    time.sleep(60)\n",
    )
    cancel = threading.Event()

    def cancel_once_it_runs():
        waited_for(pid_file)
        cancel.set()

    threading.Thread(target=cancel_once_it_runs, daemon=True).start()
    began = time.monotonic()
    with pytest.raises(rt.RuntimeFailed):
        runtime.image_facts("x", cancel)
    assert time.monotonic() - began < 30  # not the timeout
    assert not alive(waited_for(pid_file))
    assert any(c[0] == "rm" for c in fake_calls(tmp_path))


def test_leftover_probe_containers_are_removed(tmp_path):
    names = json.dumps([{"Names": ["rcman-probe-abc"]}, {"Names": ["rcman-probe-def"]}])
    runtime = fake_podman(tmp_path, f"if args[0] == 'ps':\n    sys.stdout.write({names!r})\n")
    runtime.clean_probes()
    calls = fake_calls(tmp_path)
    assert [c for c in calls if c[0] == "ps"][0].count(f"--filter=label={rt.LABEL_PROBE}") == 1
    assert sorted(c[-1] for c in calls if c[0] == "rm") == ["rcman-probe-abc", "rcman-probe-def"]


# --- the host runtime: what reaches create, images, discarding -----------------------------


def test_the_host_runtime_records_the_spec_each_workspace_was_made_from(host):
    spec_ = owned_spec()
    host.images[spec_.image] = rt.ImageFacts()
    host.create(spec_)
    assert host.specs[spec_.name] == spec_
    assert host.running(spec_.name)


@pytest.mark.parametrize("own", [rt.RANGE, -1])
def test_the_host_runtime_refuses_the_ids_podman_would(host, own):
    with pytest.raises(rt.RuntimeFailed):
        host.create(rt.Spec("rcman-ws-1", slot=1, workspace=1, person=4, uid=own))
    assert not host.exists("rcman-ws-1")


def test_the_host_runtime_pulls_and_reads_the_images_a_test_planted(host):
    facts = rt.ImageFacts(passwd="root:x:0:0::/root:/bin/sh\n", labels={"a": "b"})
    host.images["planted"] = facts
    lines: list[str] = []
    host.pull("planted", lines.append)
    assert host.pulled == ["planted"] and lines
    assert host.image_facts("planted") == facts
    with pytest.raises(rt.RuntimeFailed):
        host.pull("not-planted")
    with pytest.raises(rt.RuntimeFailed):
        host.image_facts("not-planted")
    assert host.pulled == ["planted"]


def test_the_host_runtime_stops_a_pull_that_was_cancelled(host):
    import threading

    host.images["planted"] = rt.ImageFacts()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(rt.RuntimeFailed):
        host.pull("planted", cancel=cancel)
    assert host.pulled == []


def test_a_layout_made_for_a_plan_runs_as_its_user_with_its_environment_and_path(host):
    base = rt.PODMAN_LAYOUT
    same = base.for_plan("dev", {"A": "b"}, None)
    assert (same.user, same.env, same.path) == ("dev", {"A": "b"}, base.path)
    changed = base.for_plan("dev", {}, "/x/bin:/usr/bin")
    assert changed.path == f"{rt.PODMAN_LAYOUT.path_prefix}:/x/bin:/usr/bin"  # rcman's first
    assert base.user == "root" and base.env == {}  # the layout itself is not changed
    on_host = host.layout("x")
    assert on_host.for_plan("u", {}, "/p").path == "/p"  # the host has no folder of its own


def test_a_plan_saved_with_gh_s_configuration_folder_cannot_move_it():
    """A plan saved before the variable was reserved may hold it: it is not taken."""
    made = rt.PODMAN_LAYOUT.for_plan("dev", {"GH_CONFIG_DIR": "/elsewhere", "A": "b"}, None)
    assert made.env == {"A": "b"}


def test_gh_is_pointed_at_the_persons_git_folder_wherever_claude_and_its_terminal_run(host):
    for layout in (rt.PODMAN_LAYOUT, host.layout("rcman-ws-7")):
        folder = f"{layout.home}/{rt.GIT_HOME}"
        assert rt.gh_config_dir(layout) == folder
        assert rt.child_env(layout)["GH_CONFIG_DIR"] == folder
        planned = dataclasses.replace(layout, env={"GH_CONFIG_DIR": "/elsewhere"})
        assert rt.child_env(planned)["GH_CONFIG_DIR"] == folder  # rcman's value wins
    launch = host.terminal_argv("rcman-ws-7", layout, "/x/0123456789abcdef.pid")
    assert f"GH_CONFIG_DIR={folder}" in launch.argv
    assert not [a for a in launch.argv if a.startswith(("GH_TOKEN=", "GITHUB_TOKEN="))]


def test_the_host_runtime_makes_no_container_from_an_image_it_does_not_have(host):
    spec_ = owned_spec()
    with pytest.raises(rt.RuntimeFailed):
        host.create(spec_)
    assert not host.exists(spec_.name) and spec_.name not in host.specs
    host.images[spec_.image] = rt.ImageFacts()  # the control: with the image it is made
    host.create(spec_)
    assert host.exists(spec_.name)


def test_a_container_starts_in_the_volumes_folder_which_always_exists(tmp_path):
    recorder = account_recorder(tmp_path)
    podman_runtime(tmp_path, recorder).create(owned_spec())
    run = workspace_run(recorder)
    assert f"--workdir={rt.VOLUME_MOUNT}" in run  # not the image's, which the user may not enter
    assert run.index(f"--workdir={rt.VOLUME_MOUNT}") < run.index("docker.io/library/node:20")
    other = tmp_path / "plain"
    other.mkdir()
    plain = account_recorder(other)
    podman_runtime(other, plain).create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    assert f"--workdir={rt.VOLUME_MOUNT}" in workspace_run(plain)


# A podman that runs what rcman's exec asks of it with the system's busybox, with the
# container's environment ``env`` (by default PATH the folder ``bin`` beside it); ``lose`` is how
# many execs, the first ones, end with the program's exit code and nothing it wrote, as Podman's
# remote exec sometimes does under load: the program ran, and its output never reached the
# client. ``warn`` is a line Podman's client writes on stderr itself, before the program's.
WRAPPED_EXEC = """
if args[0] == 'exec':
    import subprocess
    calls = os.path.join(os.path.dirname(sys.argv[0]), 'execs')
    n = len(open(calls).read()) if os.path.exists(calls) else 0
    open(calls, 'a').write('x')
    i = 1
    while args[i].startswith('--'):
        i += 1
    argv = args[i + 1:]
    assert argv[0] == '/opt/rcman/bin/busybox', argv
    sys.stderr.write({warn!r})
    sys.stderr.flush()
    if n < {lose}:
        code = subprocess.run(['/bin/busybox', *argv[1:]], capture_output=True,
                              env={env!r}).returncode
        sys.exit(code)
    os.execve('/bin/busybox', ['/bin/busybox', *argv[1:]], {env!r})
"""


def wrapped_podman(
    tmp_path: Path, lose: int = 0, env: dict | None = None, warn: str = ""
) -> rt.PodmanRuntime:
    path = tmp_path / "bin"
    path.mkdir(exist_ok=True)
    stat = path / "stat"  # a program of the image's, named as one of busybox's applets
    stat.write_text('#!/bin/busybox sh\necho "the image\'s stat $*"\n')
    stat.chmod(0o755)
    env = {"PATH": str(path)} if env is None else env
    return fake_podman(tmp_path, WRAPPED_EXEC.format(lose=lose, env=env, warn=warn))


def execs(tmp_path: Path) -> int:
    found = tmp_path / "execs"
    return len(found.read_text()) if found.exists() else 0


def test_a_podman_exec_runs_the_program_as_podman_would_behind_a_mark_it_takes_away(tmp_path):
    """What the program wrote, its exit code and its input are its own; the mark the exec
    writes first on stderr is not in the answer; a bare name is found on the container's PATH,
    as Podman finds it, and not taken for one of busybox's applets; a name found nowhere is
    127, as Podman's."""
    runtime = wrapped_podman(tmp_path)
    script = "cat; echo said >&2; exit 3"
    answer = runtime.exec("rcman-ws-7", ["/bin/busybox", "sh", "-c", script], stdin=b"in\n")
    assert answer == rt.Result(3, b"in\n", b"said\n")
    assert (
        runtime.exec("rcman-ws-7", ["stat", "-c", "%U", "x"]).out == b"the image's stat -c %U x\n"
    )
    absolute = runtime.exec("rcman-ws-7", [str(tmp_path / "bin" / "stat"), "y"])
    assert absolute.out == b"the image's stat y\n"
    missing = runtime.exec("rcman-ws-7", ["no-such-program"])
    assert missing.code == 127 and b"no-such-program" in missing.err
    assert rt.EXEC_MARK.encode() not in missing.err


ENV = shutil.which("env")


@pytest.mark.parametrize(
    "env",
    [
        {},  # env -i: nothing at all, not even PATH
        {"PATH": "/nowhere:/usr/bin", "FOO": "1"},
        # what the busybox shell itself sets, changes or exports, each with a value of its own
        {
            "SHLVL": "5",
            "PWD": "/elsewhere\n",
            "IFS": "ab\n",
            "PATH": "/a\n",
            "OPTIND": "4",
            "PPID": "7",
        },  # fmt: skip
    ],
    ids=["nothing", "plain", "the-shells-own"],
)
def test_a_podman_exec_gives_the_program_exactly_the_environment_podman_gives_it(tmp_path, env):
    """The program's environment is the one it is started with without the mark's shell: the
    shell's own variables (SHLVL, PWD, PATH, IFS) are put back as they were, or taken away."""
    direct = subprocess.run([ENV, "-0"], env=env, capture_output=True, check=True).stdout
    found = wrapped_podman(tmp_path, env=env).exec("rcman-ws-7", [ENV, "-0"])
    assert found.code == 0, found
    assert sorted(found.out.split(b"\0")) == sorted(direct.split(b"\0"))


def test_a_programs_own_copy_of_the_mark_is_kept_and_podmans_own_words_stay_first(tmp_path):
    """Only the mark the exec writes is taken out: one the program writes itself, on either
    stream, is its own; what Podman's client writes on stderr before the program is kept."""
    mark = rt.EXEC_MARK
    script = f"printf '%s' '{mark}'; printf '%s' '{mark}' >&2"
    plain = wrapped_podman(tmp_path).exec("rcman-ws-7", ["/bin/busybox", "sh", "-c", script])
    assert plain == rt.Result(0, mark.encode(), mark.encode())
    (tmp_path / "warns").mkdir()
    warns = wrapped_podman(tmp_path / "warns", warn="WARN[0000] podman says\n")
    said = warns.exec("rcman-ws-7", ["/bin/busybox", "sh", "-c", script])
    assert said == rt.Result(0, mark.encode(), b"WARN[0000] podman says\n" + mark.encode())


def test_a_podman_exec_whose_output_podman_lost_runs_again_only_for_a_program_that_changes_nothing(
    tmp_path,
):
    (tmp_path / "again").mkdir()
    again = wrapped_podman(tmp_path / "again", 1)
    found = again.exec("rcman-ws-7", ["stat", "x"], again=True)
    assert found == rt.Result(0, b"the image's stat x\n", b"") and execs(tmp_path / "again") == 2
    (tmp_path / "once").mkdir()
    once = wrapped_podman(tmp_path / "once", 1)
    # the control: a program that may change something is not run twice; only its code is known
    assert once.exec("rcman-ws-7", ["stat", "x"]) == rt.Result(0, b"", b"")
    assert execs(tmp_path / "once") == 1
    (tmp_path / "twice").mkdir()
    twice = wrapped_podman(tmp_path / "twice", 2)
    with pytest.raises(rt.RuntimeFailed) as failed:
        twice.exec("rcman-ws-7", ["stat", "x"], again=True)
    assert "lost" in str(failed.value) and execs(tmp_path / "twice") == 2
    (tmp_path / "refused").mkdir()
    refused = fake_podman(
        tmp_path / "refused",
        "print('Error: no container with name rcman-ws-7', file=sys.stderr)\nsys.exit(125)\n",
    )
    # Podman's own refusal says why, and no program ran: it is the answer, as it is
    said = refused.exec("rcman-ws-7", ["stat", "x"], again=True)
    assert said.code == 125 and b"no container" in said.err
    assert [c[0] for c in fake_calls(tmp_path / "refused")] == ["exec"]


def test_an_answer_lost_behind_a_warning_of_podmans_own_is_still_lost(tmp_path):
    """Podman's client may write a warning of its own on stderr whatever became of the
    program's streams: the answer is lost when the mark is missing, whatever else is there."""
    warning = "WARN[0000] podman says\n"
    (tmp_path / "again").mkdir()
    again = wrapped_podman(tmp_path / "again", 1, warn=warning)
    found = again.exec("rcman-ws-7", ["stat", "x"], again=True)
    assert found == rt.Result(0, b"the image's stat x\n", warning.encode())
    assert execs(tmp_path / "again") == 2
    (tmp_path / "twice").mkdir()
    twice = wrapped_podman(tmp_path / "twice", 2, warn=warning)
    with pytest.raises(rt.RuntimeFailed):
        twice.exec("rcman-ws-7", ["stat", "x"], again=True)


def test_a_podman_exec_keeps_only_the_tail_of_what_it_printed_when_asked(tmp_path):
    body = (
        "if args[0] == 'exec':\n"
        "    for i in range(30000):\n"
        "        print(f'line-{i}-xxxxxxxxxxxxxxxx')\n"
        "        print(f'err-{i}-xxxxxxxxxxxxxxxx', file=sys.stderr)\n"
        "    sys.exit(3)\n"
    )
    runtime = fake_podman(tmp_path, body)
    kept = runtime.exec("rcman-ws-7", ["x"], keep=1000)
    assert kept.code == 3
    assert len(kept.out) <= 1000 and len(kept.err) <= 1000
    assert kept.out.endswith(b"line-29999-xxxxxxxxxxxxxxxx\n") and b"line-0-" not in kept.out
    assert kept.err.endswith(b"err-29999-xxxxxxxxxxxxxxxx\n")
    whole = runtime.exec("rcman-ws-7", ["x"])  # the control: without keep, all of it
    assert len(whole.out) > 100_000


def test_a_kept_tail_cut_in_a_multi_byte_character_is_text_only_after_decoding(tmp_path):
    """The tail is bytes, cut wherever the limit falls: what is logged or shown is that tail
    decoded with replacement first, then cut to a text limit, so no byte that is not UTF-8
    reaches the journal or the database."""
    body = (
        "if args[0] == 'exec':\n"
        "    sys.stdout.buffer.write('\\u20ac'.encode() * 1000)\n"
        "    sys.stderr.buffer.write('\\u20ac'.encode() * 1000)\n"
        "    sys.exit(1)\n"
    )
    runtime = fake_podman(tmp_path, body)
    kept = runtime.exec("rcman-ws-7", ["x"], keep=1000)  # 1000 bytes: a euro sign is three
    assert len(kept.out) == 1000 and len(kept.err) == 1000
    with pytest.raises(UnicodeDecodeError):
        kept.out.decode("utf-8")  # the control: the cut is inside a character
    text = (kept.out + kept.err).decode("utf-8", "replace")
    assert text.startswith("�") and "�" in text[1:]  # one cut in each stream
    from rcman import workspaces as ws

    shown = ws.Workspaces._fit(text, 100)
    assert shown.encode("utf-8").decode("utf-8") == shown  # whole text, nothing else


def test_a_podman_exec_that_keeps_a_tail_still_times_out(tmp_path):
    runtime = fake_podman(tmp_path, "if args[0] == 'exec':\n    time.sleep(60)\n")
    began = time.monotonic()
    with pytest.raises(rt.RuntimeFailed):
        runtime.exec("rcman-ws-7", ["x"], timeout=0.5, keep=100)
    assert time.monotonic() - began < 20


def test_a_podman_exec_that_keeps_a_tail_passes_its_input_and_workdir_on(tmp_path):
    body = "if args[0] == 'exec':\n    sys.stdout.write(sys.stdin.read().upper())\n"
    runtime = fake_podman(tmp_path, body)
    answer = runtime.exec("rcman-ws-7", ["x"], stdin=b"abc", cwd="/rcman/work", keep=100)
    assert answer == rt.Result(0, b"ABC", b"")
    assert fake_calls(tmp_path)[0][:3] == ["exec", "--interactive", "--workdir=/rcman/work"]


def test_a_podman_cp_of_a_file_far_bigger_than_a_user_database_is_not_read_to_the_end(tmp_path):
    finished = tmp_path / "finished"
    body = (
        "if args[0] == 'cp':\n"
        "    chunk = b'x' * 65536\n"
        f"    for _ in range({4 * rt.MAX_ID_FILE_BYTES // 65536 + 8}):\n"
        "        sys.stdout.buffer.write(chunk)\n"
        "        sys.stdout.buffer.flush()\n"
        f"    open({str(finished)!r}, 'w').write('all of it was written')\n"
        "elif args[:2] == ['image', 'inspect']:\n"
        "    sys.stdout.write('{}')\n"
    )
    facts = fake_podman(tmp_path, body).image_facts("x")
    assert (facts.passwd, facts.group) == ("", "")
    assert not finished.exists()  # podman was stopped, not waited for
    assert any(c[0] == "rm" for c in fake_calls(tmp_path))


# --- the host runtime's builds ------------------------------------------------------------------


def a_build(host, **more):
    """``host.build_image`` for workspace 7 with a recording of what the vet was shown."""
    said: list[str] = []
    seen: list[dict] = []
    asked = rt.BuildRequest(7, 2, 1, rt.container_name(7), ".devcontainer/rcman-build.json", "{}")

    def vet(labels):
        seen.append(
            {"labels": dict(labels), "builders": host.builders(), "images": host.built_images()}
        )

    return (
        host.build_image(asked, said.append, more.get("cancel"), more.get("vet", vet)),
        said,
        seen,
    )


def test_the_names_of_what_is_built_are_derived_from_the_workspace():
    assert rt.LABEL_BUILDER == "rcman.builder" and rt.LABEL_BUILDER != rt.LABEL_WORKSPACE
    assert rt.built_name(7) == f"localhost/{rt.BUILT_REPOSITORY}/7:{rt.BUILT_TAG}"
    assert rt.built_name(7) != rt.built_name(70)
    assert rt.workspace_of_built_name(rt.built_name(70)) == 70
    for other in ("localhost/rcman-workspace:1", f"localhost/{rt.BUILT_REPOSITORY}/x:1", "ubuntu"):
        assert rt.workspace_of_built_name(other) is None


def test_the_host_runtime_builds_with_the_planted_builder_and_names_the_image_for_the_workspace(
    host,
):
    host.build_script = lambda request, progress, cancel: (
        progress("a step") or rt.ImageFacts(labels={"devcontainer.metadata": "[]"})
    )
    name, said, seen = a_build(host)
    assert name == rt.built_name(7) and host.images[name].labels == {"devcontainer.metadata": "[]"}
    assert said == ["a step"] and host.built_images() == {7: name}
    assert host.builders() == set()  # the builder is gone when the build is
    assert seen[0]["labels"] == {"devcontainer.metadata": "[]"}
    assert seen[0]["builders"] == {7} and seen[0]["images"] == {}  # vetted before the image is kept


def test_the_host_runtime_refuses_nothing_that_the_vet_does_not_and_leaves_nothing_of_a_refusal(
    host,
):
    from rcman import devcontainer

    def refuse(labels):
        raise devcontainer.Refused("refused_by_image", key="privileged", entry="./f", reason="no")

    with pytest.raises(devcontainer.Refused):
        a_build(host, vet=refuse)
    assert host.builders() == set() and host.built_images() == {}


def test_a_build_that_fails_leaves_neither_builder_nor_image(host):
    def fail(request, progress, cancel):
        raise rt.BuildFailed(tail="no good")

    host.build_script = fail
    with pytest.raises(rt.BuildFailed):
        a_build(host)
    assert host.builders() == set() and host.built_images() == {}


def test_a_build_begins_by_removing_a_builder_and_an_image_of_the_same_id_left_before(host):
    host.builder_ids.add(7)
    host.built[7] = "stale"
    host.images["stale"] = rt.ImageFacts()
    found: list = []

    def script(request, progress, cancel):
        found.append((host.builders(), host.built_images()))  # the builder of this build only
        return rt.ImageFacts()

    host.build_script = script
    a_build(host)
    assert found == [({7}, {})]


def test_a_stop_ends_a_build_whether_it_is_asked_by_the_event_or_by_removing_the_builder(host):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(rt.RuntimeFailed) as stopped:
        a_build(host, cancel=cancel)
    assert not isinstance(stopped.value, rt.BuildFailed)
    assert host.builders() == set() and host.built_images() == {}

    def removed(request, progress, cancel):
        host.remove_build(request.id)  # what a deletion does while the build runs
        return rt.ImageFacts()

    host.build_script = removed
    with pytest.raises(rt.RuntimeFailed):
        a_build(host)
    assert host.built_images() == {}


def test_the_build_failure_is_a_runtime_failure_with_its_tail_and_the_kind_it_was_given():
    failed = rt.BuildFailed(tail="the end", kind="build_timeout", feature="x")
    assert isinstance(failed, rt.RuntimeFailed)
    assert failed.tail == "the end" and failed.kind == "build_timeout"
    assert failed.fields == {"feature": "x"} and "the end" in str(failed)
    assert rt.BuildFailed(tail="t").kind is None


def test_the_layouts_have_tar_for_the_copy_of_a_repository(host):
    assert rt.PODMAN_LAYOUT.tool("tar")[-1] == "tar"
    assert host.layout("x").tool("tar")


def test_the_settings_name_the_builder_image_and_the_runtime_is_given_it(monkeypatch):
    from rcman.settings import Settings

    monkeypatch.setenv("RCMAN_PUBLIC_ORIGIN", "https://x.example.com")
    monkeypatch.setenv("RCMAN_BUILDER_IMAGE", "localhost/rcman-builder:abc")
    assert Settings().builder_image == "localhost/rcman-builder:abc"
    monkeypatch.delenv("RCMAN_BUILDER_IMAGE")
    assert Settings().builder_image  # a default beside the workspace image's
    config = rt.PodmanConfig(Path("/s"), "i", Path("/t"), Path("/c"), builder_image="b")
    assert config.builder_image == "b"
    from rcman import main

    made = main.podman_runtime(Settings(builder_image="localhost/rcman-builder:abc"))
    assert made.config.builder_image == "localhost/rcman-builder:abc"
    assert made.config.scratch_dir == Settings().database.parent  # rcman's own state folder


# --- the version a server is pinned to ------------------------------------------------------


def test_a_layout_names_the_store_it_pins_versions_in(host):
    layout = host.layout(rt.container_name(1))
    assert layout.claude_root == str(host.claude)
    assert rt.PODMAN_LAYOUT.claude_root == rt.CLAUDE_MOUNT
    for each, root in ((layout, str(host.claude)), (rt.PODMAN_LAYOUT, rt.CLAUDE_MOUNT)):
        assert each.pinned("1.2.3").claude == f"{root}/versions/1.2.3"
        assert each.claude == f"{root}/bin/claude"  # unpinned, the link the store keeps
        assert each.pinned("1.2.3").path == each.path  # the rest is as it was


# --- the canary ---------------------------------------------------------------------------------

CANDIDATE_FOLDER = Path("/var/lib/rcman/claude-staging/0123abcd")
CANARY_ENV = ("HOME", "TMPDIR", "DISABLE_AUTOUPDATER", "PYTHONDONTWRITEBYTECODE", "PYTHONPATH")


def canary_recorder(tmp_path, answer=(0, "{}", "")):
    recorder = Recorder(tmp_path, {"run": answer})
    return recorder, podman_runtime(tmp_path, recorder)


def test_the_canary_is_a_throwaway_container_that_can_only_run_the_checks(tmp_path):
    from rcman import people

    recorder, runtime = canary_recorder(tmp_path)
    assert runtime.run_canary(CANDIDATE_FOLDER, rt.CANARY_NAME, "1.2.3") == {}
    run = next(c[3:] for c in recorder.calls() if c[3] == "run")
    base = people.BUILDER_SLOT * rt.RANGE
    for option in (
        "--rm",
        f"--name={rt.CANARY_NAME}",
        f"--timeout={int(rt.CANARY_SECONDS)}",
        f"--label={rt.LABEL_CANARY}=1",
        "--network=none",
        "--read-only",
        "--cap-drop=all",
        "--security-opt=no-new-privileges",
        f"--uidmap=0:{base}:{rt.RANGE}",
        f"--gidmap=0:{base}:{rt.RANGE}",
        f"--volume={CANDIDATE_FOLDER}:{rt.CANARY_CANDIDATE_MOUNT}:ro",
        f"--volume={rt.HELPERS_DIR}:{rt.CANARY_HELPERS_MOUNT}/helpers:ro",
        "--env=HOME=/tmp",
        "--env=TMPDIR=/tmp",
        "--env=DISABLE_AUTOUPDATER=1",
        "--env=PYTHONDONTWRITEBYTECODE=1",
    ):
        assert option in run, option
    assert any(a.startswith("--tmpfs=/tmp") for a in run)
    assert "localhost/rcman-workspace:test" in run  # the default image
    assert run[-5:] == [
        "python3",
        "-m",
        "helpers.claude_canary",
        f"{rt.CANARY_CANDIDATE_MOUNT}/{rt.CANDIDATE}",
        "1.2.3",
    ]


def test_the_canary_has_nothing_a_workspace_has_and_no_way_out(tmp_path):
    recorder, runtime = canary_recorder(tmp_path)
    runtime.run_canary(CANDIDATE_FOLDER, rt.CANARY_NAME, "1.2.3")
    run = next(c[3:] for c in recorder.calls() if c[3] == "run")
    volumes = [a for a in run if a.startswith(("--volume", "-v"))]
    assert len(volumes) == 2 and all(v.endswith(":ro") for v in volumes)  # the candidate, helpers
    assert not [a for a in run if a.startswith(("-p", "--publish", "--privileged", "--cap-add"))]
    assert not [a for a in run if a.startswith("--label=") and "workspace" in a]
    assert [a for a in run if a.startswith("--env=")]  # positive control for the allowlist below
    assert not [a for a in run if a.startswith("--env=") and a[6:].split("=")[0] not in CANARY_ENV]
    assert "--detach" not in run and "--interactive" not in run


def test_a_canary_left_from_before_is_removed_first_as_a_sign_in_is(tmp_path):
    recorder, runtime = canary_recorder(tmp_path)
    runtime.run_canary(CANDIDATE_FOLDER, rt.CANARY_NAME, "1.2.3")
    calls = [c[3:] for c in recorder.calls()]
    assert calls[0] == ["rm", "--force", "--ignore", "--time=0", "--volumes", rt.CANARY_NAME]
    assert [c[0] for c in calls].index("run") > 0
    recorder.log.unlink()
    runtime.end_canary(rt.CANARY_NAME)
    assert [c[3:] for c in recorder.calls()] == [calls[0]]


def test_what_the_canary_prints_is_the_checks_it_fails(tmp_path):
    report = {"version": {"lines": ["ran: claude --version"], "more": 0}}
    _, runtime = canary_recorder(tmp_path, (0, json.dumps(report) + "\n", ""))
    assert runtime.run_canary(CANDIDATE_FOLDER, rt.CANARY_NAME, "1.2.3") == report


@pytest.mark.parametrize(
    "answer",
    [
        (1, "", "Error: crun: the container could not start"),
        (0, "", ""),
        (0, "not json", ""),
        (0, "[]", ""),
        (0, '{"version": "no report"}', ""),
        (124, "", ""),
    ],
)
def test_a_canary_that_did_not_report_is_a_failure_of_the_runtime_not_of_the_candidate(
    tmp_path, answer
):
    _, runtime = canary_recorder(tmp_path, answer)
    with pytest.raises(rt.RuntimeFailed):
        runtime.run_canary(CANDIDATE_FOLDER, rt.CANARY_NAME, "1.2.3")


def test_a_candidate_folder_that_podman_would_read_as_more_than_a_path_is_refused(tmp_path):
    recorder, runtime = canary_recorder(tmp_path)
    for folder in (Path("/var/lib/rcman/a:b"), Path("/var/lib/rcman/a,b"), Path("relative")):
        with pytest.raises(rt.RuntimeFailed):
            runtime.run_canary(folder, rt.CANARY_NAME, "1.2.3")
    assert recorder.calls() == []  # before anything was asked of podman


def test_the_host_runtime_answers_with_the_canary_a_test_scripted(host, tmp_path):
    seen = []

    def script(folder, name, version):
        seen.append((folder, name, version))
        return {"x": {}}

    host.canary_script = script
    assert host.run_canary(tmp_path, rt.CANARY_NAME, "1.2.3") == {"x": {}}
    assert seen == [(tmp_path, rt.CANARY_NAME, "1.2.3")]
    host.end_canary(rt.CANARY_NAME)  # nothing to end


def test_a_terminal_is_an_interactive_remote_exec_on_a_tty_of_its_own_session(tmp_path):
    """The Terminal tab's shell, as rcman starts it in Podman: ``setsid --ctty`` makes the pty
    the podman client's controlling terminal, so that ^C and a resize reach the shell; the exec
    has a tty, no detach keys (^P^Q pass through), the work folder, and an environment of
    exactly the workspace's, with the launcher and its arguments fixed."""
    recorder = account_recorder(tmp_path)
    runtime = podman_runtime(tmp_path, recorder)
    layout = runtime.layout("rcman-ws-7")
    pidfile = f"{layout.run}/{rt.TERMINALS}/0123456789abcdef.pid"
    launch = runtime.terminal_argv("rcman-ws-7", layout, pidfile)
    setsid = shutil.which("setsid", path="/usr/bin:/bin")
    assert launch.argv[:2] == [setsid, "--ctty"]
    assert launch.argv[2:6] == [
        str(recorder.program),
        "--remote",
        "--url",
        "unix:///run/user/990/podman/podman.sock",
    ]
    rest = launch.argv[6:]
    assert rest[:6] == [
        "exec", "--interactive", "--tty", "--detach-keys=", f"--workdir={layout.work}", "rcman-ws-7"
    ]  # fmt: skip
    env = {**rt.child_env(layout), "TERMINFO": layout.terminfo}
    assert rest[6:] == layout.with_env(
        env,
        [layout.shell, "-c", rt.TERMINAL_LAUNCHER, "rcman-terminal", layout.work, pidfile,
         layout.shell],
    )  # fmt: skip
    assert launch.env == rt.podman_env()


def test_a_terminal_on_the_host_is_the_launcher_under_setsid(host):
    layout = host.layout("rcman-ws-7")
    launch = host.terminal_argv("rcman-ws-7", layout, "/x/0123456789abcdef.pid")
    assert launch.argv[:2] == [shutil.which("setsid", path="/usr/bin:/bin"), "--ctty"]
    assert launch.argv[2:] == layout.with_env(
        rt.child_env(layout),
        [layout.shell, "-c", rt.TERMINAL_LAUNCHER, "rcman-terminal", layout.work,
         "/x/0123456789abcdef.pid", layout.shell],
    )  # fmt: skip
    assert launch.env == {"PATH": "/usr/bin:/bin"}


@pytest.mark.parametrize("pidfile", ["relative.pid", "/x/../y.pid", "/x/y\n.pid", "/x/y.txt"])
def test_a_terminal_is_given_only_a_pidfile_of_its_own_shape(host, pidfile):
    with pytest.raises(ValueError):
        host.terminal_argv("rcman-ws-7", host.layout("rcman-ws-7"), pidfile)


# --- what is injected: the range, the network and the slices ----------------------------------


def workspace_security(tools: Path) -> list[str]:
    """What every workspace container is given, and nothing else is (see below), with the
    tools ``tools``."""
    return [rt.UNMASK_PROC, f"--security-opt=seccomp={tools}/seccomp.json"]


def test_the_injected_fields_are_keyword_only_with_the_products_values_as_defaults():
    config = rt.PodmanConfig(Path("/s"), "i", Path("/t"), Path("/c"))
    assert (config.range, config.network, config.slices) == (rt.RANGE, rt.NETWORK, True)
    assert rt.RANGE == 65536 and rt.NETWORK == "slirp4netns:allow_host_loopback=false"
    fields = {f.name: f for f in dataclasses.fields(rt.PodmanConfig)}
    assert all(fields[name].kw_only for name in rt.INJECTED)
    assert set(rt.INJECTED) == {"range", "network", "slices"}
    positional = [f.name for f in dataclasses.fields(rt.PodmanConfig) if not f.kw_only]
    with pytest.raises(TypeError):  # one more positional argument than there are
        rt.PodmanConfig(*([None] * (len(positional) + 1)))


def test_a_smaller_range_maps_each_slot_and_refuses_an_id_beyond_it():
    ids = rt.PodmanRuntime(
        rt.PodmanConfig(Path("/s"), "i", Path("/t"), Path("/c"), range=8192)
    )._ids
    assert ids(3) == ["--uidmap=0:24576:8192", "--gidmap=0:24576:8192"]
    swapped = mapped(ids_of(ids(3, 1000, 1000), "uid"))
    assert sorted(swapped) == list(range(8192))
    assert sorted(swapped.values()) == list(range(24576, 24576 + 8192))
    assert swapped[1000] == 24576
    with pytest.raises(rt.RuntimeFailed):
        ids(3, 8192, 0)
    assert IDS(3, 8192, 0)  # control: the product's range holds it


def test_a_workspace_takes_the_injected_range_and_network_and_no_slice_without_slices(tmp_path):
    recorder = account_recorder(tmp_path)
    runtime = podman_runtime(tmp_path, recorder, range=8192, network="host", slices=False)
    runtime.create(rt.Spec(name="rcman-ws-7", slot=3, workspace=7, person=2))
    run = workspace_run(recorder)
    assert "--network=host" in run and "--uidmap=0:24576:8192" in run
    assert not [a for a in run if a.startswith(("--network=slirp4netns", "--cgroup-parent"))]
    assert "--pids-limit=-1" in run  # the limit of Podman's own is still lifted
    (tmp_path / "s").mkdir()
    sign_in = account_recorder(tmp_path / "s")
    podman_runtime(tmp_path / "s", sign_in, range=8192, network="host").start_sign_in(2, 3, 60)
    started = next(r for r in runs(sign_in) if "--name=" + rt.sign_in_name(2) in r)
    assert "--network=host" in started and "--uidmap=0:24576:8192" in started
    accounts = [r for r in runs(sign_in) if "--name=" + rt.sign_in_name(2) not in r]
    assert accounts and all("--network=none" in r for r in accounts)  # these keep no network


def test_a_workspace_goes_under_its_slice_with_the_products_defaults(tmp_path):
    recorder = account_recorder(tmp_path)
    podman_runtime(tmp_path, recorder).create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    run = workspace_run(recorder)
    assert [a for a in run if a.startswith("--cgroup-parent")] == [
        "--cgroup-parent=rcman-p2-w7.slice"
    ]
    assert f"--network={rt.NETWORK}" in run


def test_every_workspace_container_unmasks_proc_and_takes_the_workspace_seccomp_profile(tmp_path):
    """So that a container nested in it has a fresh /proc of its own and may name its own UTS
    namespace; nothing else rcman starts has either."""
    recorder = account_recorder(tmp_path)
    runtime = podman_runtime(tmp_path, recorder)
    runtime.create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    security = workspace_security(runtime.config.tools_dir)
    assert [a for a in workspace_run(recorder) if a.startswith("--security-opt")] == security
    assert rt.UNMASK_PROC == "--security-opt=unmask=/proc/*"
    runtime.start_sign_in(2, 3, 60)
    others = [r for r in runs(recorder) if "--name=rcman-ws-7" not in r]
    assert any("--name=" + rt.sign_in_name(2) in r for r in others)  # positive control
    for other in others:
        assert not set(security) & set(other), other
    (tmp_path / "c").mkdir()
    canary, made = canary_recorder(tmp_path / "c")
    made.run_canary(CANDIDATE_FOLDER, rt.CANARY_NAME, "1.2.3")
    (ran,) = runs(canary)
    security = workspace_security(made.config.tools_dir)
    assert f"--name={rt.CANARY_NAME}" in ran and not set(security) & set(ran)


def test_every_workspace_has_a_tmpfs_of_its_roots_for_roots_podman(tmp_path):
    """Empty at every start of the container, and its root's even where the ids are swapped, so
    that the wrapper's record of root's nest is root's alone and never outlives the nest."""
    recorder = account_recorder(tmp_path)
    runtime = podman_runtime(tmp_path, recorder)
    runtime.create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    state = f"--tmpfs={rt.ROOT_PODMAN_STATE}:mode=0700"
    assert rt.ROOT_PODMAN_STATE == "/run/rcman-podman"
    assert state in workspace_run(recorder)
    (tmp_path / "o").mkdir()
    owned = account_recorder(tmp_path / "o")
    podman_runtime(tmp_path / "o", owned).create(owned_spec())
    run = workspace_run(owned)
    assert "--tmpfs=/run/rcman:mode=0700,U" in run  # the control: this one is the user's
    assert [a for a in run if a.startswith(f"--tmpfs={rt.ROOT_PODMAN_STATE}")] == [state]
    (tmp_path / "s").mkdir()
    sign_in = account_recorder(tmp_path / "s")
    podman_runtime(tmp_path / "s", sign_in).start_sign_in(2, 3, 60)
    assert not [a for r in runs(sign_in) for a in r if rt.ROOT_PODMAN_STATE in a]


def test_every_workspace_has_a_tmp_of_its_own_off_the_root(tmp_path):
    """An anonymous volume, so that /tmp is on rcman's disk and not on the container's overlay
    root, where a Podman nested in the workspace could not put overlay storage; the sign-in and
    the canary have none, and the canary keeps its tmpfs."""
    tmp = f"--volume={rt.TMP_MOUNT}"
    for folder, spec in (("d", rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2)),
                         ("o", owned_spec())):  # fmt: skip
        (tmp_path / folder).mkdir()
        recorder = account_recorder(tmp_path / folder)
        podman_runtime(tmp_path / folder, recorder).create(spec)
        run = workspace_run(recorder)
        assert "--name=rcman-ws-7" in run  # the control: this is the workspace's run
        assert run.count(tmp) == 1, run
    (tmp_path / "s").mkdir()
    sign_in = account_recorder(tmp_path / "s")
    podman_runtime(tmp_path / "s", sign_in).start_sign_in(2, 3, 60)
    started = next(r for r in runs(sign_in) if "--name=" + rt.sign_in_name(2) in r)
    assert tmp not in started
    assert not [a for r in runs(sign_in) for a in r if a == tmp]
    (tmp_path / "c").mkdir()
    canary, made = canary_recorder(tmp_path / "c")
    made.run_canary(CANDIDATE_FOLDER, rt.CANARY_NAME, "1.2.3")
    (ran,) = runs(canary)
    assert f"--name={rt.CANARY_NAME}" in ran and tmp not in ran
    assert f"--tmpfs={rt.CANARY_TMP}:rw,mode=1777" in ran


BUSYBOX = f"{rt.TOOLS_MOUNT}/bin/busybox"
OWN_TMP = [
    [BUSYBOX, "sh", "-c", '[ -d "$1" ] && [ ! -L "$1" ]', "sh", rt.TMP_MOUNT],
    [BUSYBOX, "chown", "0:0", rt.TMP_MOUNT],
    [BUSYBOX, "chmod", "1777", rt.TMP_MOUNT],
]


@pytest.mark.parametrize("swapped", [False, True])
def test_after_the_run_the_containers_root_makes_tmp_roots_and_everyones(tmp_path, swapped):
    """Whatever the image has at /tmp, and whoever Podman made the volume's, it is 0:0 and 1777,
    set by the container's root even where the ids are swapped and the workspace runs as its
    user, once it is found a folder and not a link; before the folders are made."""
    recorder = account_recorder(tmp_path)
    spec = owned_spec() if swapped else rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2)
    podman_runtime(tmp_path, recorder).create(spec)
    calls = [c[3:] for c in recorder.calls()]
    owning = [c for c in calls if c[:3] == ["exec", "--user=0", "rcman-ws-7"]]
    assert [c[3:] for c in owning] == OWN_TMP
    run = calls.index(workspace_run(recorder))
    folders = next(i for i, c in enumerate(calls) if c[0] == "exec" and "mkdir" in c)
    assert all(run < calls.index(c) < folders for c in owning)
    if swapped:
        assert "--user=dev" in workspace_run(recorder)  # the control: the workspace's is not root
    assert not [c for c in calls if c[0] == "exec" and c not in owning and "--user=0" in c]


def failing_podman(tmp_path, step: str, said: str = "", code: int = 1):
    """A podman whose root exec of ``step`` (sh, chown or chmod) fails with ``code``, saying
    ``said``."""
    return fake_podman(
        tmp_path,
        "if args[0] == 'volume' and args[1] == 'inspect':\n"
        "    print('/vol' if '{{.Mountpoint}}' in args else '{\"rcman.person\": \"2\"}')\n"
        "elif args[0] == 'volume' and args[1] == 'exists':\n"
        "    sys.exit(1)\n"
        f"elif args[:2] == ['exec', '--user=0'] and args[4:5] == [{step!r}]:\n"
        f"    sys.stderr.write({said!r})\n"
        f"    sys.exit({code})\n",
    )


def own_tmp_steps(tmp_path) -> list[str]:
    calls = fake_calls(tmp_path)
    assert any(c[0] == "run" and "--name=rcman-ws-7" in c for c in calls)  # it ran
    return [c[4] for c in calls if c[:2] == ["exec", "--user=0"]]


def failing_after_the_run(tmp_path, step: str):
    """A podman whose exec of ``step`` after the run fails, saying so: the root exec of ``sh``
    (the check of /tmp), ``chown`` or ``chmod``, or the folders' ``mkdir``."""
    if step != "mkdir":
        return failing_podman(tmp_path, step, f"{step}: it failed\n", code=1)
    return fake_podman(
        tmp_path,
        "if args[0] == 'volume' and args[1] == 'inspect':\n"
        "    print('/vol' if '{{.Mountpoint}}' in args else '{\"rcman.person\": \"2\"}')\n"
        "elif args[0] == 'volume' and args[1] == 'exists':\n"
        "    sys.exit(1)\n"
        "elif args[0] == 'exec' and 'mkdir' in args:\n"
        "    sys.stderr.write('mkdir: it failed\\n')\n"
        "    sys.exit(1)\n",
    )


@pytest.mark.parametrize("step", ["sh", "chown", "chmod", "mkdir"])
def test_a_container_whose_making_fails_after_the_run_is_removed_and_its_volume_kept(
    tmp_path, step
):
    """So that no container is left running with a /tmp or folders that were not made so: a
    restore that finds it would take it for the workspace's. The error is the step's own."""
    runtime = failing_after_the_run(tmp_path, step)
    with pytest.raises(rt.RuntimeFailed) as failed:
        runtime.create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    assert (
        "link or not a folder" in str(failed.value)
        if step == "sh"
        else (f"{step}: it failed" in str(failed.value))
    ), failed.value
    calls = fake_calls(tmp_path)
    run = next(i for i, c in enumerate(calls) if c[0] == "run" and "--name=rcman-ws-7" in c)
    removed = [i for i, c in enumerate(calls) if c[0] == "rm" and c[-1] == "rcman-ws-7"]
    assert removed and removed[0] > run, calls
    assert {"--force", "--volumes"} <= set(calls[removed[0]])  # with its anonymous /tmp
    assert not [c for c in calls if c[:2] == ["volume", "rm"]]  # and not the workspace's volume


VOLUMES_ANSWERED = (
    "if args[0] == 'volume' and args[1] == 'inspect':\n"
    "    print('/vol' if '{{.Mountpoint}}' in args else '{\"rcman.person\": \"2\"}')\n"
    "elif args[0] == 'volume' and args[1] == 'exists':\n"
    "    sys.exit(1)\n"
)


def test_a_container_whose_run_fails_after_podman_made_it_is_removed_and_its_volume_kept(
    tmp_path,
):
    """Podman makes the container before it starts it, and a start that fails leaves it made: a
    restore would start it, its /tmp and folders never made so."""
    runtime = fake_podman(
        tmp_path,
        VOLUMES_ANSWERED + "elif args[0] == 'run' and '--name=rcman-ws-7' in args:\n"
        "    sys.stderr.write('Error: crun: it did not start\\n')\n"
        "    sys.exit(126)\n",
    )
    with pytest.raises(rt.RuntimeFailed, match="it did not start"):
        runtime.create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    calls = fake_calls(tmp_path)
    run = next(i for i, c in enumerate(calls) if c[0] == "run" and "--name=rcman-ws-7" in c)
    removed = [i for i, c in enumerate(calls) if c[0] == "rm" and c[-1] == "rcman-ws-7"]
    assert removed and removed[0] > run and {"--force", "--volumes"} <= set(calls[removed[0]])
    assert not [c for c in calls if c[:2] == ["volume", "rm"]]
    assert not [c for c in calls if c[0] == "exec"]  # nothing went on in it


def test_a_name_a_container_has_already_is_refused_and_that_container_left_alone(tmp_path):
    """So that what ``create`` removes when it fails is only ever what it made itself."""
    runtime = fake_podman(tmp_path, VOLUMES_ANSWERED, container_exists=True)
    with pytest.raises(rt.RuntimeFailed, match="rcman-ws-7"):
        runtime.create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    calls = fake_calls(tmp_path)
    assert ["container", "exists", "rcman-ws-7"] in calls  # it was asked
    assert not [c for c in calls if c[0] in ("run", "rm")]
    (tmp_path / "t").mkdir()  # the twin: with no container of the name, it is made
    fake_podman(tmp_path / "t", VOLUMES_ANSWERED).create(
        rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2)
    )
    assert any(c[0] == "run" for c in fake_calls(tmp_path / "t"))


def test_a_removal_after_a_failure_that_itself_fails_is_logged_with_what_podman_said(
    tmp_path, caplog
):
    runtime = fake_podman(
        tmp_path,
        VOLUMES_ANSWERED + "elif args[0] == 'run' and '--name=rcman-ws-7' in args:\n"
        "    sys.stderr.write('Error: it did not start\\n')\n"
        "    sys.exit(126)\n"
        "elif args[0] == 'rm':\n"
        "    sys.stderr.write('Error: the container is busy\\n')\n"
        "    sys.exit(125)\n",
    )
    with caplog.at_level("WARNING", logger="rcman.runtime"):
        with pytest.raises(rt.RuntimeFailed, match="it did not start"):  # the first error
            runtime.create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    assert any(c[0] == "rm" for c in fake_calls(tmp_path))  # the removal was tried
    assert any(
        "rcman-ws-7" in r.getMessage() and "the container is busy" in r.getMessage()
        for r in caplog.records
    ), caplog.records


def test_a_container_made_whole_is_not_removed(tmp_path):
    """The twin: a create whose steps all pass removes nothing."""
    runtime = failing_after_the_run(tmp_path, "none")
    runtime.create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    assert not [c for c in fake_calls(tmp_path) if c[0] == "rm"]


def test_an_image_whose_tmp_is_a_link_or_not_a_folder_is_refused_before_anything_is_changed(
    tmp_path,
):
    """Checked after the run, as the container's root: nothing is chowned or chmodded through a
    link, and the folders are not made."""
    runtime = failing_podman(tmp_path, "sh")
    with pytest.raises(rt.RuntimeFailed, match="is a link or not a folder"):
        runtime.create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    assert own_tmp_steps(tmp_path) == ["sh"]
    assert not [c for c in fake_calls(tmp_path) if c[0] == "exec" and "mkdir" in c]
    (tmp_path / "t").mkdir()  # the twin: where the check passes, chown and chmod follow
    failing_podman(tmp_path / "t", "none").create(rt.Spec("rcman-ws-7", slot=3, workspace=7,
                                                          person=2))  # fmt: skip
    assert own_tmp_steps(tmp_path / "t") == ["sh", "chown", "chmod"]


def test_the_check_of_tmp_failing_in_podman_itself_is_not_taken_for_a_link(tmp_path):
    """Only the check's own answer (1) refuses the image; Podman's exec failing (125: the container
    is gone, say) fails ``create`` with what it said."""
    said = "Error: no container with name or ID rcman-ws-7 found\n"
    runtime = failing_podman(tmp_path, "sh", said, code=125)
    with pytest.raises(rt.RuntimeFailed) as failed:
        runtime.create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    assert str(failed.value) == said.strip()
    assert "link or not a folder" not in str(failed.value)
    assert own_tmp_steps(tmp_path) == ["sh"]
    (tmp_path / "q").mkdir()  # and one that says nothing still says it was podman's exec
    with pytest.raises(rt.RuntimeFailed, match="podman exec failed"):
        failing_podman(tmp_path / "q", "sh", code=126).create(
            rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2)
        )


def test_the_refusal_of_a_tmp_that_is_a_link_reads_as_a_sentence():
    assert rt.TMP_REFUSED.format(path=rt.TMP_MOUNT) == (
        f"the image's {rt.TMP_MOUNT} is a link or not a folder."
    )


@pytest.mark.parametrize(("step", "before"), [("chown", ["sh"]), ("chmod", ["sh", "chown"])])
def test_a_tmp_that_cannot_be_made_roots_fails_the_workspace_with_what_was_said(
    tmp_path, step, before
):
    runtime = failing_podman(tmp_path, step, f"{step}: /tmp: Read-only file system\n")
    with pytest.raises(rt.RuntimeFailed, match=f"{step}: .*Read-only file system"):
        runtime.create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    assert own_tmp_steps(tmp_path) == [*before, step]
    assert not [c for c in fake_calls(tmp_path) if c[0] == "exec" and "mkdir" in c]


def test_a_workspace_whose_seccomp_profile_is_missing_is_not_made(tmp_path):
    """Podman refuses a profile that is not there (its words, recorded), so no workspace is made
    until the installer writes it again."""
    tools = tmp_path / "tools"
    tools.mkdir()
    runtime = fake_podman(
        tmp_path,
        "import os\n"
        "opt = [a for a in args if a.startswith('--security-opt=seccomp=')]\n"
        "path = opt[0].split('=', 2)[2] if opt else None\n"
        "if args[0] == 'volume' and args[1] == 'inspect':\n"
        "    print('/vol' if '{{.Mountpoint}}' in args else '{\"rcman.person\": \"2\"}')\n"
        "elif args[0] == 'volume' and args[1] == 'exists':\n"
        "    sys.exit(1)\n"
        "elif path and not os.path.exists(path):\n"
        "    sys.stderr.write(f'Error: opening seccomp profile failed: open {path}: '\n"
        "                     'no such file or directory\\n')\n"
        "    sys.exit(125)\n",
    )
    runtime = rt.PodmanRuntime(dataclasses.replace(runtime.config, tools_dir=tools))
    with pytest.raises(rt.RuntimeFailed, match="seccomp profile"):
        runtime.create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    (tools / rt.SECCOMP).write_text("{}")  # the control: with a profile there, it is made
    runtime.create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))


WRAPPER_SOURCE = REPO / "backend" / "rcman" / "podman" / "podman"


def wrapper_state(text: str) -> list[str]:
    """Every value the wrapper's text gives STATE, a line of its own at the top level."""
    import re

    return re.findall(r"^STATE=(\S+)$", text, re.MULTILINE)


def test_the_wrappers_record_folder_is_the_tmpfs_every_workspace_gets():
    assert wrapper_state(WRAPPER_SOURCE.read_text()) == [rt.ROOT_PODMAN_STATE]


def test_the_record_folder_check_finds_a_wrapper_that_keeps_its_record_elsewhere():
    text = WRAPPER_SOURCE.read_text()
    planted = text.replace(f"STATE={rt.ROOT_PODMAN_STATE}\n", "STATE=/run/elsewhere\n")
    assert planted != text  # the plant took effect
    assert wrapper_state(planted) == ["/run/elsewhere"] != [rt.ROOT_PODMAN_STATE]
    assert wrapper_state(text + "STATE=/run/second\n") != [rt.ROOT_PODMAN_STATE]  # and twice


# --- the podman and docker wrappers are on every workspace's PATH ------------------------------

PODMAN_BIN = f"{rt.TOOLS_MOUNT}/podman/bin"


def test_the_wrappers_come_right_after_claude_on_a_workspaces_path():
    layout = rt.PODMAN_LAYOUT
    assert layout.path.split(":")[:2] == [f"{rt.CLAUDE_MOUNT}/bin", PODMAN_BIN]
    assert layout.path_prefix == f"{rt.CLAUDE_MOUNT}/bin:{PODMAN_BIN}"
    assert rt.PODMAN_BIN == PODMAN_BIN
    # a plan without a PATH keeps the layout's, one with a PATH has rcman's folders first
    assert layout.for_plan("dev", {}, None).path == layout.path
    planned = layout.for_plan("dev", {}, "/usr/bin:/bin").path
    assert planned == f"{rt.CLAUDE_MOUNT}/bin:{PODMAN_BIN}:/usr/bin:/bin"


def test_the_host_runtime_puts_no_wrapper_on_its_path(host):
    """The control: the host runtime runs the host's own programs, and has no tools mounted."""
    layout = host.layout(rt.container_name(1))
    assert PODMAN_BIN not in layout.path.split(":")
    assert "podman/bin" not in layout.path


# --- the runtime's range, and the slot the canary runs in, as given --------------------------


def test_each_runtime_says_its_range_the_products_by_default(tmp_path, host):
    assert host.id_range == rt.RANGE
    small = rt.HostRuntime(host.root, host.sockets, host.claude, host.tmux_conf, range=8192)
    assert small.id_range == 8192
    config = rt.PodmanConfig(Path("/s"), "i", Path("/t"), Path("/c"))
    assert rt.PodmanRuntime(config).id_range == rt.RANGE
    assert rt.PodmanRuntime(dataclasses.replace(config, range=8192)).id_range == 8192


def test_a_host_runtime_of_a_smaller_range_refuses_an_id_beyond_it(host):
    small = rt.HostRuntime(host.root, host.sockets, host.claude, host.tmux_conf, range=8192)
    with pytest.raises(rt.RuntimeFailed, match="beyond the range"):
        small.create(rt.Spec(rt.container_name(41), slot=1, workspace=41, person=1, uid=8192))
    assert not small.exists(rt.container_name(41))
    small.create(rt.Spec(rt.container_name(42), slot=1, workspace=42, person=1, uid=8191))
    host.create(rt.Spec(rt.container_name(43), slot=1, workspace=43, person=1, uid=8192))  # control


def test_the_canary_runs_in_the_slot_it_is_given_and_the_builders_by_default(tmp_path):
    from rcman import people

    recorder, runtime = canary_recorder(tmp_path)
    runtime.run_canary(CANDIDATE_FOLDER, rt.CANARY_NAME, "1.2.3")
    runtime.run_canary(CANDIDATE_FOLDER, rt.CANARY_NAME, "1.2.3", slot=6)
    default, given = [c[3:] for c in recorder.calls() if c[3] == "run"]
    assert f"--uidmap=0:{people.BUILDER_SLOT * rt.RANGE}:{rt.RANGE}" in default
    assert f"--uidmap=0:{6 * rt.RANGE}:{rt.RANGE}" in given


# --- what Claude is told: a file of the tools bound read-only at Claude Code's managed path ----

LABELLED = json.dumps({instructions.TOOLS_LABEL: "1", "org.example.other": "x"})


def bound_instructions(run: list[str]) -> list[str]:
    """What a run binds at Claude Code's managed folder, or anywhere in it."""
    return [a for a in run if "/etc/claude-code" in a]


def labels_recorder(tmp_path: Path, answer: tuple[int, str, str]) -> Recorder:
    """account_recorder's answers, with the image's labels answered ``answer``."""
    return Recorder(
        tmp_path,
        {
            "volume exists": (1, "", ""),
            "volume inspect": (0, MOUNTPOINT + "\n", ""),
            "volume inspect labels": (0, '{"rcman.person": "2"}', ""),
            "image inspect": (0, "", ""),  # so that the labels are told apart by the verb
            "image inspect labels": answer,
        },
    )


def labels_asked(recorder: Recorder) -> list[list[str]]:
    return [c[3:] for c in recorder.calls() if c[3:5] == ["image", "inspect"]]


@pytest.mark.parametrize(
    ("image", "labels", "named"),
    [
        (None, LABELLED, instructions.DEFAULT_TOOLS),
        (None, json.dumps({"org.example.other": "x"}), instructions.DEFAULT_IMAGE),
        (None, "null", instructions.DEFAULT_IMAGE),  # an image with no labels at all
        # one built from the default image, which carries its label: told as the default is
        ("docker.io/library/node:20", LABELLED, instructions.DEFAULT_TOOLS),
        ("docker.io/library/node:20", json.dumps({"org.x": "y"}), instructions.OWN_IMAGE),
        ("docker.io/library/node:20", "null", instructions.OWN_IMAGE),
    ],
)
def test_the_instructions_bound_follow_the_spec_and_the_images_label(
    tmp_path, image, labels, named
):
    """A workspace whose image carries the default image's label is told of gh and of the
    default image's tools, whatever image it is; one of an old default image without it, of
    gh; one of an image of its own without it, neither. The labels are those of the image the
    workspace is made from."""
    recorder = labels_recorder(tmp_path, (0, labels + "\n", ""))
    runtime = podman_runtime(tmp_path, recorder)
    runtime.create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2, image=image))
    told_file = runtime.config.tools_dir / instructions.FOLDER / named
    assert told_file.is_file()
    assert bound_instructions(workspace_run(recorder)) == [
        f"--volume={told_file}:{instructions.MANAGED}:ro"
    ]
    shown = image or runtime.config.image
    assert labels_asked(recorder) == [["image", "inspect", "--format", "{{json .Labels}}", shown]]


@pytest.mark.parametrize("image", [None, "docker.io/library/node:20"])
def test_a_container_made_again_from_an_images_id_reads_that_images_labels(tmp_path, image):
    """Not those of whichever image the name names now: the default image's or the plan's."""
    recorder = labels_recorder(tmp_path, (0, LABELLED, ""))
    runtime = podman_runtime(tmp_path, recorder)
    runtime.create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2, image=image, image_id=ID_A))
    assert labels_asked(recorder) == [["image", "inspect", "--format", "{{json .Labels}}", ID_A]]
    told_file = runtime.config.tools_dir / instructions.FOLDER / instructions.DEFAULT_TOOLS
    assert bound_instructions(workspace_run(recorder)) == [
        f"--volume={told_file}:{instructions.MANAGED}:ro"
    ]


@pytest.mark.parametrize(
    "answer",
    [(125, "", "Error: the image is not there\n"), (0, "not json\n", ""), (0, "[1]\n", "")],
)
@pytest.mark.parametrize("image", [None, "docker.io/library/node:20"])
def test_a_failed_label_inspect_makes_no_volume_and_no_container(tmp_path, answer, image):
    recorder = labels_recorder(tmp_path, answer)
    with pytest.raises(rt.RuntimeFailed) as failed:
        podman_runtime(tmp_path, recorder).create(
            rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2, image=image)
        )
    if answer[0]:
        assert "the image is not there" in str(failed.value)
    assert labels_asked(recorder)  # it was asked
    calls = [c[3:] for c in recorder.calls()]
    assert not [c for c in calls if c[0] == "run" or c[:2] == ["volume", "create"]], calls
    (tmp_path / "control").mkdir()  # the control: with the labels read, it is made
    made = labels_recorder(tmp_path / "control", (0, LABELLED, ""))
    podman_runtime(tmp_path / "control", made).create(
        rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2)
    )
    assert workspace_run(made)


@pytest.mark.parametrize("name", sorted(instructions.FILES))
@pytest.mark.parametrize("planted", ["gone", "a folder"])
@pytest.mark.parametrize("image", [None, "docker.io/library/node:20"])
def test_a_missing_instructions_file_fails_create_before_podman_runs_and_names_the_installer(
    tmp_path, name, planted, image
):
    recorder = account_recorder(tmp_path)
    runtime = podman_runtime(tmp_path, recorder)
    missing = runtime.config.tools_dir / instructions.FOLDER / name
    missing.unlink()
    if planted == "a folder":
        missing.mkdir()
    assert not missing.is_file()  # the plant took effect
    spec_ = rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2, image=image)
    with pytest.raises(rt.InstructionsMissing) as failed:
        runtime.create(spec_)
    assert isinstance(failed.value, rt.RuntimeFailed)  # a failure of the runtime's, as any
    assert str(failed.value) == rt.INSTRUCTIONS_MISSING
    assert "run the installer again" in rt.INSTRUCTIONS_MISSING
    assert not [c for c in recorder.calls() if c[3] in ("run", "volume", "image")]
    if planted == "a folder":
        missing.rmdir()
    told(runtime.config.tools_dir)  # the control: with the file there again, it is made
    runtime.create(spec_)
    assert workspace_run(recorder)


def test_instructions_ready_names_a_missing_file_and_asks_podman_nothing(tmp_path):
    recorder = Recorder(tmp_path, {})
    runtime = podman_runtime(tmp_path, recorder)
    assert runtime.instructions_ready() is None  # the control: every file is there
    for name in instructions.FILES:
        told(runtime.config.tools_dir)
        (runtime.config.tools_dir / instructions.FOLDER / name).unlink()
        assert runtime.instructions_ready() == rt.INSTRUCTIONS_MISSING, name
    assert recorder.calls() == []


def test_only_a_workspace_container_binds_the_instructions(tmp_path):
    """Not the account's container, the one that prepares a swapped map's folders, the sign-in
    container, the canary, the probe of an image or the builder: none of them runs Claude for a
    person's work."""
    recorder = account_recorder(tmp_path)
    runtime = podman_runtime(tmp_path, recorder)
    runtime.create(owned_spec())
    runtime.start_sign_in(2, 3, 60)
    assert bound_instructions(workspace_run(recorder))  # the positive
    others = [r for r in runs(recorder) if "--name=rcman-ws-7" not in r]
    assert any("--name=" + rt.sign_in_name(2) in r for r in others)
    assert any("--rm" in r and "--volume=rcman-ws-7:/rcman" in r for r in others)  # folders
    assert any(rt.account_volume(2) in " ".join(r) for r in others if "--rm" in r)  # account
    for other in others:
        assert not bound_instructions(other), other
    (tmp_path / "c").mkdir()
    canary, made = canary_recorder(tmp_path / "c")
    made.run_canary(CANDIDATE_FOLDER, rt.CANARY_NAME, "1.2.3")
    (ran,) = runs(canary)
    assert not bound_instructions(ran)
    (tmp_path / "p").mkdir()  # the probe of an image, made and never started
    probing = Recorder(tmp_path / "p", {"image inspect": (0, "{}", "")})
    podman_runtime(tmp_path / "p", probing).image_facts("x/y:1")
    (probe,) = [c[3:] for c in probing.calls() if c[3] == "create"]
    assert any(a.startswith(f"--name={rt.PROBE_PREFIX}") for a in probe)
    assert not bound_instructions(probe)
    from tests.test_runtime_build import SUCCESS, StandIn, building, runtime_with

    (tmp_path / "b").mkdir()
    stand_in = StandIn(tmp_path / "b", **SUCCESS)
    building(runtime_with(stand_in))[0]()
    builder = next(c for c in stand_in.calls() if c[0] == "run")
    assert "--name=rcman-build-7" in builder
    assert not bound_instructions(builder)


def test_the_host_runtime_gives_no_instructions_file(host):
    """It runs on the host, where Claude Code's managed folder is the machine's own."""
    assert host.instructions_ready() is None
    with runtime_contract.made(host, spec(30)) as name:
        assert host.exists(name)


def header(kind: str) -> bytes:
    """A tar of one entry of ``kind``, as podman cp prints what is at a path."""
    import io
    import tarfile

    types = {"folder": tarfile.DIRTYPE, "file": tarfile.REGTYPE, "link": tarfile.SYMTYPE,
             "fifo": tarfile.FIFOTYPE}  # fmt: skip
    entry = tarfile.TarInfo("claude-code")
    entry.type = types[kind]
    if kind == "link":
        entry.linkname = "/elsewhere"
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.addfile(entry, io.BytesIO(b"") if kind == "file" else None)
    return buffer.getvalue()


def negative_extension() -> bytes:
    """A PAX extension header whose size, in the base-256 form, is negative, then a folder: a
    reader that took the size as it is would stand where it is for ever."""
    import tarfile

    data = bytearray(extended("folder", "pax"))
    size = -tarfile.BLOCKSIZE  # what would hold the reader where it is
    data[124:136] = b"\xff" + (256**11 + size).to_bytes(11, "big")
    data[148:156] = b" " * 8
    data[148:156] = b"%06o\0 " % sum(data[:512])
    assert tarfile.TarInfo.frombuf(bytes(data[:512]), "utf-8", "replace").size == size  # plant
    return bytes(data)


def extended(kind: str, form: str) -> bytes:
    """A tar of one entry of ``kind`` behind an extension header of ``form``: a PAX one that
    spans blocks, or a GNU long name, as podman cp may print them before the entry itself."""
    import io
    import tarfile

    types = {"folder": tarfile.DIRTYPE, "file": tarfile.REGTYPE}
    entry = tarfile.TarInfo("claude-code" if form == "pax" else "claude-code-" + "x" * 120)
    entry.type = types[kind]
    if form == "pax":
        entry.pax_headers = {"comment": "y" * 1500}
    formats = {"pax": tarfile.PAX_FORMAT, "gnu": tarfile.GNU_FORMAT}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=formats[form]) as archive:
        archive.addfile(entry, io.BytesIO(b"") if kind == "file" else None)
    data = buffer.getvalue()
    marker = {"pax": tarfile.XHDTYPE, "gnu": tarfile.GNUTYPE_LONGNAME}[form]
    assert data[156:157] == marker  # the plant: the first header is the extension's
    return data


def probed_podman(tmp_path: Path, found: dict[str, bytes], linger: bool = False):
    """A podman whose image has, at each path of ``found``, what podman cp prints for it, and
    nothing anywhere else; with ``linger`` cp waits, once it has printed, as a big copy does."""
    for number, data in enumerate(found.values()):
        (tmp_path / f"cp{number}").write_bytes(data)
    tars = {path: str(tmp_path / f"cp{number}") for number, path in enumerate(found)}
    return fake_podman(
        tmp_path,
        "if args[:2] == ['image', 'inspect']:\n    sys.stdout.write('{}')\n"
        "elif args[0] == 'cp':\n"
        f"    tars = {tars!r}\n"
        "    path = args[1].split(':', 1)[1]\n"
        "    if path not in tars:\n"
        "        print('Error: No such file or directory', file=sys.stderr)\n"
        "        sys.exit(125)\n"
        "    sys.stdout.buffer.write(open(tars[path], 'rb').read())\n"
        "    sys.stdout.buffer.flush()\n"
        f"    if {linger}:\n        time.sleep(60)\n",
    )


ETC, FOLDER_OF, MANAGED = instructions.PATHS
FOLDER_IS, FILE_IS = instructions.IS_FOLDER, instructions.IS_FILE


@pytest.mark.parametrize(
    ("found", "read", "said"),
    [
        (  # a folder with a file in it
            {ETC: header("folder"), FOLDER_OF: header("folder"), MANAGED: header("file")},
            [ETC, FOLDER_OF, MANAGED],
            {ETC: FOLDER_IS, FOLDER_OF: FOLDER_IS, MANAGED: FILE_IS},
        ),
        (  # a file where the folder would be: nothing below it is asked
            {ETC: header("folder"), FOLDER_OF: header("file")},
            [ETC, FOLDER_OF],
            {ETC: FOLDER_IS, FOLDER_OF: FILE_IS},
        ),
        ({ETC: header("folder")}, [ETC, FOLDER_OF], {ETC: FOLDER_IS}),  # absent
        ({}, [ETC], {}),  # no /etc at all
        ({ETC: header("file")}, [ETC], {ETC: FILE_IS}),
        (  # a folder where the file would be
            {ETC: header("folder"), FOLDER_OF: header("folder"), MANAGED: header("folder")},
            [ETC, FOLDER_OF, MANAGED],
            {ETC: FOLDER_IS, FOLDER_OF: FOLDER_IS, MANAGED: FOLDER_IS},
        ),
        (  # what podman would print of a link if it did not follow it, as it does
            {ETC: header("folder"), FOLDER_OF: header("link")},
            [ETC, FOLDER_OF],
            {ETC: FOLDER_IS, FOLDER_OF: instructions.IS_LINK},
        ),
        (
            {ETC: header("folder"), FOLDER_OF: header("folder"), MANAGED: header("fifo")},
            [ETC, FOLDER_OF, MANAGED],
            {ETC: FOLDER_IS, FOLDER_OF: FOLDER_IS, MANAGED: instructions.IS_OTHER},
        ),
        (  # an extension header before the entry: what is judged is the entry
            {
                ETC: extended("folder", "pax"),
                FOLDER_OF: extended("folder", "gnu"),
                MANAGED: extended("file", "pax"),
            },  # fmt: skip
            [ETC, FOLDER_OF, MANAGED],
            {ETC: FOLDER_IS, FOLDER_OF: FOLDER_IS, MANAGED: FILE_IS},
        ),
        (
            {ETC: header("folder"), FOLDER_OF: extended("file", "gnu")},
            [ETC, FOLDER_OF],
            {ETC: FOLDER_IS, FOLDER_OF: FILE_IS},
        ),
        (  # a header that is not one
            {ETC: b"x" * 1024},
            [ETC],
            {ETC: instructions.IS_OTHER},
        ),
    ],
)
def test_the_image_probe_says_what_the_image_has_at_the_managed_path(tmp_path, found, read, said):
    facts = probed_podman(tmp_path, found).image_facts("x")
    assert facts.managed == said
    copied = [c[1].split(":", 1)[1] for c in fake_calls(tmp_path) if c[0] == "cp"]
    assert copied == ["/etc/passwd", "/etc/group", *read]


def test_an_extension_header_of_a_negative_size_is_not_told_and_ends_the_reading(tmp_path):
    import threading

    runtime = probed_podman(tmp_path, {ETC: negative_extension()})
    found: list = []
    reading = threading.Thread(target=lambda: found.append(runtime.image_facts("x")), daemon=True)
    reading.start()
    reading.join(30)
    assert not reading.is_alive(), "the probe went round the header for ever"
    assert found[0].managed == {ETC: instructions.IS_OTHER}


def test_the_probe_reads_the_first_header_alone_and_does_not_wait_for_the_rest(tmp_path):
    """What is at /etc is a folder, whose copy is the whole of /etc."""
    found = {ETC: header("folder"), FOLDER_OF: header("folder"), MANAGED: header("file")}
    began = time.monotonic()
    facts = probed_podman(tmp_path, found, linger=True).image_facts("x")
    assert time.monotonic() - began < 30  # not the minute each copy waits
    assert facts.managed == {ETC: FOLDER_IS, FOLDER_OF: FOLDER_IS, MANAGED: FILE_IS}
