"""The runtimes: the contract every runtime keeps, against the host runtime here, and what
the Podman runtime asks of podman, from a recording of its calls.

tests/container/test_runtime_in_podman.py runs the same contract against real Podman.
"""

import dataclasses
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from rcman import runtime as rt

from devtools.held_folders import tmux_sockets
from helpers import instructions
from tests import runtime_contract
from tests.hang_guard import HANG_GUARD

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


def test_a_workspace_container_is_made_as_the_design_says(tmp_path):
    """Every call remote through rcman's own socket; the person's account made sure of first;
    the workspace in its slice, with its credentials file and git folder bound from the
    account's volume, the security options, root's tmpfs for its Podman and a /tmp off the
    root that every workspace container has."""
    recorder = account_recorder(tmp_path)
    runtime = podman_runtime(tmp_path, recorder)
    runtime.create(rt.Spec(name="rcman-ws-7", slot=3, workspace=7, person=2))
    calls = recorder.calls()
    assert calls  # non-vacuity
    for call in calls:
        assert call[:3] == ["--remote", "--url", "unix:///run/user/990/podman/podman.sock"]
    run = workspace_run(recorder)
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
        f"--tmpfs={rt.ROOT_PODMAN_STATE}:mode=0700",
        "--label=rcman.workspace=7",
        "--label=rcman.person=2",
        "--name=rcman-ws-7",
        f"--workdir={rt.VOLUME_MOUNT}",
        f"--volume={MOUNTPOINT}/{rt.CREDENTIALS}:{rt.VOLUME_MOUNT}/{rt.CONFIG}/{rt.CREDENTIALS}",
        f"--volume={MOUNTPOINT}/{rt.GIT_DIR}:{rt.VOLUME_MOUNT}/{rt.HOME}/.config/git",
    ):
        assert option in run, option
    assert [a for a in run if a.startswith("--cgroup-parent")] == [
        "--cgroup-parent=rcman-p2-w7.slice"
    ]
    assert run.count(f"--volume={rt.TMP_MOUNT}") == 1
    assert [a for a in run if a.startswith("--security-opt")] == workspace_security(
        runtime.config.tools_dir
    )
    assert not [a for a in run if a.startswith(("-p", "--publish", "--privileged", "--cap"))]
    assert not [a for a in run if a.startswith(("--user", "--group-add", "--env"))]
    assert not [a for a in run if a.startswith("--volume=rcman-person-")]  # not by name
    assert "localhost/rcman-workspace:test" in run
    volume = next(c[3:] for c in calls if c[3:5] == ["volume", "create"])
    assert "--label=rcman.person=2" in volume
    made_first = [i for i, r in enumerate(runs(recorder)) if "--rm" in r]
    assert made_first and made_first[0] < runs(recorder).index(run)


def test_a_volume_is_labelled_with_its_person_and_install_and_adopted_again_by_them(tmp_path):
    """The install id is what the volumes of a database replaced from a backup do not have."""
    spec_ = rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2, install="abc123")
    labels = {"rcman.person": "2", "rcman.install": "abc123"}
    (tmp_path / "kept").mkdir()
    recorder = Recorder(
        tmp_path / "kept",
        {"volume exists": (0, "", ""), "volume inspect labels": (0, json.dumps(labels), ""),
         "volume inspect": (0, MOUNTPOINT + "\n", "")},
    )  # fmt: skip
    podman_runtime(tmp_path / "kept", recorder).create(spec_)
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


ID_A = "a" * 64


def test_a_container_made_again_is_made_from_the_image_id_and_pulls_nothing(tmp_path):
    recorder = account_recorder(tmp_path)
    runtime = podman_runtime(tmp_path, recorder)
    runtime.create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2, image_id=ID_A))
    run = next(c[3:] for c in recorder.calls() if c[3] == "run" and "--name=rcman-ws-7" in c)
    assert run[-3] == ID_A and "--pull=never" in run  # the default image's name is not used
    assert "localhost/rcman-workspace:test" not in run


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


# `podman images -a` of a plain two-step build, recorded from Podman on the tests' engine: the
# leaf, its nameless step image and the base.
RECORDED_STEPS = json.loads(
    (Path(__file__).parent / "fakes" / "podman-images" / "images-a.json").read_text()
)


def recorded_steps(step_age: int) -> tuple[dict, dict, dict]:
    """The recording's leaf, step and base, the step made ``step_age`` seconds ago."""
    import time

    leaf, step, base = (dict(entry) for entry in RECORDED_STEPS)
    assert leaf["ParentId"] == step["Id"] and step["ParentId"] == base["Id"] and not step["Names"]
    step["Created"] = int(time.time()) - step_age
    return leaf, step, base


def test_a_nameless_image_a_kept_image_is_built_on_stays_and_is_not_tried(tmp_path):
    leaf, step, base = recorded_steps(2 * int(rt.IMAGE_GRACE_SECONDS))
    loose = {**step, "Id": "e" * 64, "ParentId": base["Id"]}  # the control: built on by nothing
    recorder = Recorder(tmp_path, {"images": (0, json.dumps([leaf, step, base, loose]), "")})
    removed = podman_runtime(tmp_path, recorder).collect_images(set(), set(leaf["Names"]))
    assert removed == [loose["Id"]]  # a nameless image past the grace with no child goes
    asked = next(c for c in recorder.calls() if "images" in c)
    assert "-a" in asked  # the step images are listed too
    tried = [c[-1] for c in recorder.calls() if c[3:5] == ["image", "rm"]]
    # Podman removes an image with a child: neither the step nor the base, which images listed
    # are built on, is asked.
    assert tried == [loose["Id"]]


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


def test_a_sign_in_container_removes_itself_and_is_labelled_with_its_person_only(tmp_path):
    """And has nothing of a workspace's own: no security options, no root's tmpfs for Podman,
    no /tmp volume, no instructions for Claude and no slice."""
    recorder = account_recorder(tmp_path, volume_exists=0)
    runtime = podman_runtime(tmp_path, recorder)
    name = runtime.start_sign_in(2, 3, seconds=HANG_GUARD)
    assert name == rt.sign_in_name(2)
    started = next(r for r in runs(recorder) if f"--name={name}" in r)
    assert "--rm" in started and "--detach" in started and "--init" in started
    assert started[-2:] == ["sleep", str(int(HANG_GUARD))]
    labels = [a for a in started if a.startswith("--label")]
    assert labels == ["--label=rcman.person=2"]
    assert f"--uidmap=0:{3 * rt.RANGE}:{rt.RANGE}" in started
    target = f"{rt.VOLUME_MOUNT}/{rt.CONFIG}/{rt.CREDENTIALS}"
    assert f"--volume={MOUNTPOINT}/{rt.CREDENTIALS}:{target}" in started
    git = f"{rt.VOLUME_MOUNT}/{rt.HOME}/.config/git"
    assert f"--volume={MOUNTPOINT}/{rt.GIT_DIR}:{git}" in started
    removed = [c[3:] for c in recorder.calls() if c[3] == "rm"]
    assert removed and removed[0][-1] == name  # one left from before goes first
    assert not set(workspace_security(runtime.config.tools_dir)) & set(started)
    assert not [a for a in started if rt.ROOT_PODMAN_STATE in a or a == f"--volume={rt.TMP_MOUNT}"]
    assert not bound_instructions(started)
    assert not [a for a in started if a.startswith(("--cgroup-parent", "--pids-limit"))]


def test_clearing_an_account_empties_its_file_in_place_and_removing_it_removes_its_volume(
    tmp_path,
):
    recorder = account_recorder(tmp_path, volume_exists=0)
    runtime = podman_runtime(tmp_path, recorder)
    runtime.clear_account(2, 3)
    (cleared,) = runs(recorder)
    assert cleared[cleared.index("-c") + 1] == 'umask 077 && : > "$1"'
    assert "--volume=rcman-person-2:/account" in cleared
    runtime.remove_account(2)
    assert ["volume", "rm", "--force", "rcman-person-2"] in [c[3:] for c in recorder.calls()]


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


def run_ensure_script(tmp_path: Path) -> subprocess.CompletedProcess:
    """rt.ENSURE_SCRIPT run by a shell with only the tools an account container has; the
    credentials file is ``tmp_path/cred`` and the git folder ``tmp_path/git``."""
    tools = account_tools(tmp_path)
    argv = ["/bin/sh", "-c", rt.ENSURE_SCRIPT, "sh", str(tmp_path / "cred"), str(tmp_path / "git")]
    return subprocess.run(argv, capture_output=True, env={"PATH": str(tools)})


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


def test_a_swapped_map_swaps_the_users_id_with_roots_and_moves_nothing_else():
    own = 1000
    base = 3 * rt.RANGE
    options = IDS(3, own, own)
    uid = mapped(ids_of(options, "uid"))
    assert sorted(uid) == list(range(rt.RANGE))  # every id of the container is mapped, once
    assert sorted(uid.values()) == list(range(base, base + rt.RANGE))  # onto every host id, once
    assert uid[own] == base  # the user is the host id root has in the ordinary map
    assert uid[0] == base + own  # and root is the user's
    assert all(uid[i] == base + i for i in range(1, rt.RANGE) if i != own)
    assert mapped(ids_of(options, "gid")) == uid  # the same for groups here


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


BUSYBOX = f"{rt.TOOLS_MOUNT}/bin/busybox"
OWN_TMP = [
    [BUSYBOX, "sh", "-c", '[ -d "$1" ] && [ ! -L "$1" ]', "sh", rt.TMP_MOUNT],
    [BUSYBOX, "chown", "0:0", rt.TMP_MOUNT],
    [BUSYBOX, "chmod", "1777", rt.TMP_MOUNT],
]


def test_a_container_of_the_users_own_is_made_from_the_image_as_the_user(tmp_path):
    """The folders beside the mounts are made first by the ordinary map's root; after the run
    the container's root makes /tmp root's and everyone's, though the workspace runs as its
    user, before the folders are made."""
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
        "--pull=never",
    ):
        assert option in run, option
    # The image's own entrypoint is not run, and nothing of rcman's is the image's.
    assert "--entrypoint=/opt/rcman/bin/busybox" in run
    after = run.index("docker.io/library/node:20")
    assert run[after + 1 :] == ["sleep", "2147483647"]
    assert run.index(f"--workdir={rt.VOLUME_MOUNT}") < after  # not the image's folder
    assert "localhost/rcman-workspace:test" not in run
    assert not [a for a in run if a.startswith(("--privileged", "--cap", "-p"))]
    # the two every workspace container has, and no other
    assert [a for a in run if a.startswith("--security-opt")] == workspace_security(
        tmp_path / "tools"
    )
    prepare = next(r for r in runs(recorder) if "--rm" in r and "--volume=rcman-ws-7:/rcman" in r)
    assert runs(recorder).index(prepare) < runs(recorder).index(run)
    assert ids_of(prepare, "uid") == [(0, 3 * rt.RANGE, rt.RANGE)]  # the ordinary map
    assert not [a for a in prepare if a.startswith("--user")]
    assert prepare[prepare.index("sh", prepare.index("-c")) + 1 :] == [
        f"{rt.VOLUME_MOUNT}/{rt.WORK}",
        f"{rt.VOLUME_MOUNT}/{rt.CONFIG}",
        f"{rt.VOLUME_MOUNT}/{rt.HOME}/.config",  # where the git folder is bound
    ]
    calls = [c[3:] for c in recorder.calls()]
    owning = [c for c in calls if c[:3] == ["exec", "--user=0", "rcman-ws-7"]]
    assert [c[3:] for c in owning] == OWN_TMP
    ran = calls.index(run)
    folders = next(i for i, c in enumerate(calls) if c[0] == "exec" and "mkdir" in c)
    assert all(ran < calls.index(c) < folders for c in owning)
    assert not [c for c in calls if c[0] == "exec" and c not in owning and "--user=0" in c]


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


def test_a_pull_passes_each_line_it_prints_on(tmp_path):
    ok = fake_podman(tmp_path, "print('Trying to pull x')\nprint('Copying blob a')\n")
    lines: list[str] = []
    ok.pull("docker.io/library/x", lines.append)
    assert lines == ["Trying to pull x", "Copying blob a"]
    assert fake_calls(tmp_path) == [["pull", "docker.io/library/x"]]


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


def image_podman(tmp_path: Path) -> rt.PodmanRuntime:
    passwd = b"root:x:0:0::/root:/bin/sh\ndev:x:1234:1234::/home/dev:/bin/sh\n"
    inspected = {
        "Labels": {"devcontainer.metadata": '[{"remoteUser": "dev"}]', "x": "y"},
        "User": "dev",
        "Config": {"Env": ["PATH=/a:/b", "EMPTY=", "NODE=20"]},
    }
    (tmp_path / "tar").write_bytes(tar_of({"passwd": passwd}))
    return fake_podman(
        tmp_path,
        "if args[:2] == ['image', 'inspect']:\n"
        f"    sys.stdout.write({json.dumps(inspected)!r})\n"
        "elif args[0] == 'cp':\n"
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


# --- the host runtime: what reaches create, images, discarding -----------------------------


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


# A podman that runs what rcman's exec asks of it with the system's busybox, with the
# container's environment ``env`` (by default PATH the folder ``bin`` beside it).
WRAPPED_EXEC = """
if args[0] == 'exec':
    i = 1
    while args[i].startswith('--'):
        i += 1
    argv = args[i + 1:]
    assert argv[0] == '/opt/rcman/bin/busybox', argv
    os.execve('/bin/busybox', ['/bin/busybox', *argv[1:]], {env!r})
"""


def wrapped_podman(tmp_path: Path) -> rt.PodmanRuntime:
    path = tmp_path / "bin"
    path.mkdir(exist_ok=True)
    stat = path / "stat"  # a program of the image's, named as one of busybox's applets
    stat.write_text('#!/bin/busybox sh\necho "the image\'s stat $*"\n')
    stat.chmod(0o755)
    return fake_podman(tmp_path, WRAPPED_EXEC.format(env={"PATH": str(path)}))


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


def canary_recorder(tmp_path, answer=(0, "{}", "")):
    recorder = Recorder(tmp_path, {"run": answer})
    return recorder, podman_runtime(tmp_path, recorder)


def test_the_canary_is_a_throwaway_container_that_can_only_run_the_checks(tmp_path):
    """What it prints is the checks it fails."""
    from rcman import people

    report = {"version": {"lines": ["ran: claude --version"], "more": 0}}
    recorder, runtime = canary_recorder(tmp_path, (0, json.dumps(report) + "\n", ""))
    assert runtime.run_canary(CANDIDATE_FOLDER, rt.CANARY_NAME, "1.2.3") == report
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


# --- what is injected: the range, the network and the slices ----------------------------------


def workspace_security(tools: Path) -> list[str]:
    """What every workspace container is given, and nothing else is, with the tools
    ``tools``."""
    return [rt.UNMASK_PROC, f"--security-opt=seccomp={tools}/seccomp.json"]


def test_a_workspace_takes_the_injected_range_and_network_and_no_slice_without_slices(tmp_path):
    recorder = account_recorder(tmp_path)
    runtime = podman_runtime(tmp_path, recorder, range=8192, network="host", slices=False)
    runtime.create(rt.Spec(name="rcman-ws-7", slot=3, workspace=7, person=2))
    run = workspace_run(recorder)
    assert "--network=host" in run and "--uidmap=0:24576:8192" in run
    assert not [a for a in run if a.startswith(("--network=slirp4netns", "--cgroup-parent"))]
    assert "--pids-limit=-1" in run  # the limit of Podman's own is still lifted


def failing_podman(tmp_path, step: str, said: str):
    """A podman whose root exec of ``step`` (sh, chown or chmod) fails, saying ``said``."""
    return fake_podman(
        tmp_path,
        "if args[0] == 'volume' and args[1] == 'inspect':\n"
        "    print('/vol' if '{{.Mountpoint}}' in args else '{\"rcman.person\": \"2\"}')\n"
        "elif args[0] == 'volume' and args[1] == 'exists':\n"
        "    sys.exit(1)\n"
        f"elif args[:2] == ['exec', '--user=0'] and args[4:5] == [{step!r}]:\n"
        f"    sys.stderr.write({said!r})\n"
        "    sys.exit(1)\n",
    )


def test_a_container_whose_making_fails_after_the_run_is_removed_and_its_volume_kept(tmp_path):
    """So that no container is left running with a /tmp or folders that were not made so: a
    restore that finds it would take it for the workspace's. The error is the step's own."""
    runtime = failing_podman(tmp_path, "chown", "chown: it failed\n")
    with pytest.raises(rt.RuntimeFailed, match="chown: it failed"):
        runtime.create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    calls = fake_calls(tmp_path)
    run = next(i for i, c in enumerate(calls) if c[0] == "run" and "--name=rcman-ws-7" in c)
    removed = [i for i, c in enumerate(calls) if c[0] == "rm" and c[-1] == "rcman-ws-7"]
    assert removed and removed[0] > run, calls
    assert {"--force", "--volumes"} <= set(calls[removed[0]])  # with its anonymous /tmp
    assert not [c for c in calls if c[:2] == ["volume", "rm"]]  # and not the workspace's volume


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


# --- what Claude is told: a file of the tools bound read-only at Claude Code's managed path ----

LABELLED = json.dumps({instructions.TOOLS_LABEL: "1", "org.example.other": "x"})


def bound_instructions(run: list[str]) -> list[str]:
    """What a run binds at Claude Code's managed folder, or anywhere in it."""
    return [a for a in run if "/etc/claude-code" in a]


def test_the_instructions_bound_follow_the_images_label(tmp_path):
    """A workspace whose image carries the default image's label is told of gh and of the
    default image's tools; the labels are those of the image the workspace is made from."""
    recorder = Recorder(
        tmp_path,
        {
            "volume exists": (1, "", ""),
            "volume inspect": (0, MOUNTPOINT + "\n", ""),
            "volume inspect labels": (0, '{"rcman.person": "2"}', ""),
            "image inspect": (0, "", ""),  # so that the labels are told apart by the verb
            "image inspect labels": (0, LABELLED + "\n", ""),
        },
    )
    runtime = podman_runtime(tmp_path, recorder)
    runtime.create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    told_file = runtime.config.tools_dir / instructions.FOLDER / instructions.DEFAULT_TOOLS
    assert told_file.is_file()
    assert bound_instructions(workspace_run(recorder)) == [
        f"--volume={told_file}:{instructions.MANAGED}:ro"
    ]
    asked = [c[3:] for c in recorder.calls() if c[3:5] == ["image", "inspect"]]
    assert asked == [["image", "inspect", "--format", "{{json .Labels}}", runtime.config.image]]


def header(kind: str) -> bytes:
    """A tar of one entry of ``kind``, as podman cp prints what is at a path."""
    import io
    import tarfile

    entry = tarfile.TarInfo("claude-code")
    entry.type = {"folder": tarfile.DIRTYPE, "file": tarfile.REGTYPE}[kind]
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.addfile(entry, io.BytesIO(b"") if kind == "file" else None)
    return buffer.getvalue()


def probed_podman(tmp_path: Path, found: dict[str, bytes]):
    """A podman whose image has, at each path of ``found``, what podman cp prints for it, and
    nothing anywhere else."""
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
        "    sys.stdout.buffer.flush()\n",
    )


ETC, FOLDER_OF, MANAGED = instructions.PATHS
FOLDER_IS, FILE_IS = instructions.IS_FOLDER, instructions.IS_FILE


def test_the_image_probe_says_what_the_image_has_at_the_managed_path(tmp_path):
    found = {ETC: header("folder"), FOLDER_OF: header("folder"), MANAGED: header("file")}
    facts = probed_podman(tmp_path, found).image_facts("x")
    assert facts.managed == {ETC: FOLDER_IS, FOLDER_OF: FOLDER_IS, MANAGED: FILE_IS}
    copied = [c[1].split(":", 1)[1] for c in fake_calls(tmp_path) if c[0] == "cp"]
    assert copied == ["/etc/passwd", "/etc/group", ETC, FOLDER_OF, MANAGED]
