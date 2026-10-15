"""The runtimes: the contract every runtime keeps, against the host runtime here, and what
the Podman runtime asks of podman, from a recording of its calls.

tests/container/test_runtime_in_podman.py runs the same contract against real Podman.
"""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from rcman import runtime as rt

from devtools.held_folders import tmux_sockets
from tests import runtime_contract

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "workspaces"
    root.mkdir()
    with tmux_sockets("rcr-") as sockets:
        yield rt.HostRuntime(root, sockets, tmp_path / "claude", REPO / "backend/rcman/tmux.conf")


def spec(n: int) -> rt.Spec:
    return rt.Spec(name=rt.container_name(100 + n), slot=n, workspace=100 + n, person=n)


@pytest.mark.parametrize("case", runtime_contract.CASES, ids=lambda case: case.__name__)
def test_the_host_runtime_keeps_the_contract(host, case):
    case(host, spec)


def test_every_contract_case_is_run_here():
    from tests.collected import collect

    test = "tests/test_runtime.py::test_the_host_runtime_keeps_the_contract"
    cases = {n.removeprefix(test) for n in collect(["tests/test_runtime.py"]) if n.startswith(test)}
    assert len(runtime_contract.CASES) >= 5  # non-vacuity
    assert cases == {f"[{case.__name__}]" for case in runtime_contract.CASES}


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
        script = {verb: list(answer) for verb, answer in answers.items()}
        self.program.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f"open({str(self.log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
            f"open({str(self.log) + '.stdin'!r}, 'ab').write(sys.stdin.buffer.read())\n"
            f"answers = {json.dumps(script)}\n"
            "words = [a for a in sys.argv[1:] if not a.startswith(('-', 'unix:'))]\n"
            "verb = ' '.join(words[:2]) if ' '.join(words[:2]) in answers else words[0]\n"
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


def podman_runtime(tmp_path, recorder) -> rt.PodmanRuntime:
    return rt.PodmanRuntime(
        rt.PodmanConfig(
            socket=Path("/run/user/990/podman/podman.sock"),
            image="localhost/rcman-workspace:test",
            tools_dir=Path("/var/lib/rcman/tools"),
            claude_store=Path("/var/lib/rcman/claude"),
            podman=str(recorder.program),
        )
    )


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
        "--volume=/var/lib/rcman/tools:/opt/rcman:ro",
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


def test_only_exit_1_of_exists_means_missing(tmp_path):
    for code, expected in ((0, True), (1, False)):
        (tmp_path / str(code)).mkdir()
        recorder = Recorder(tmp_path / str(code), {"container exists": (code, "", "")})
        assert podman_runtime(tmp_path, recorder).exists("rcman-ws-1") is expected


@pytest.mark.parametrize("code", [125, 2])
def test_any_other_exit_of_exists_is_not_missing(tmp_path, code):
    said = "Cannot connect to Podman" if code == 125 else "something else"
    recorder = Recorder(tmp_path, {"container exists": (code, "", said)})
    with pytest.raises(rt.RuntimeUnavailable):
        podman_runtime(tmp_path, recorder).exists("rcman-ws-1")


def test_an_exec_is_tried_once_more_on_conmons_error_only(tmp_path):
    conmon = 'Error: container create failed (no logs from conmon): conmon bytes ""'
    recorder = Recorder(tmp_path, {"exec": (126, "", conmon)})
    answer = podman_runtime(tmp_path, recorder).exec("rcman-ws-1", ["true"])
    assert answer.code == 126
    assert len([c for c in recorder.calls() if "exec" in c]) == 2
    other = tmp_path / "other"
    other.mkdir()
    recorder = Recorder(other, {"exec": (126, "", "Error: something else")})
    podman_runtime(other, recorder).exec("rcman-ws-1", ["true"])
    assert len([c for c in recorder.calls() if "exec" in c]) == 1


def test_podman_runs_with_the_allowlisted_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("RCMAN_SECRET_OF_THE_SERVICE", "x")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/990")
    env = rt.podman_env()
    assert "RCMAN_SECRET_OF_THE_SERVICE" not in env
    assert env["XDG_RUNTIME_DIR"] == "/run/user/990"
    assert set(env) <= {"PATH", "LANG", "HOME", "XDG_RUNTIME_DIR"}


# --- a person's Claude sign-in --------------------------------------------------------

MOUNTPOINT = "/var/lib/rcman/.local/share/containers/storage/volumes/rcman-person-2/_data"


def account_recorder(tmp_path: Path, volume_exists: int = 1) -> Recorder:
    return Recorder(
        tmp_path,
        {
            "volume exists": (volume_exists, "", ""),
            "volume inspect": (0, MOUNTPOINT + "\n", ""),
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
    assert sorted(p.name for p in shared.iterdir()) == ["credential"]  # no .new left behind


def run_put_script(tmp_path: Path, mv_fails: bool) -> subprocess.CompletedProcess:
    """rt.PUT_SCRIPT run by a shell, as the account container runs it, with a ``mv`` that
    fails if ``mv_fails``; the git folder is ``tmp_path/git``."""
    tools = tmp_path / "tools"
    tools.mkdir(exist_ok=True)
    for tool in ("mkdir", "cat", "rm"):
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
    assert sorted(p.name for p in (tmp_path / "git").iterdir()) == ["credential"]  # control
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
    assert sorted(p.name for p in folder.iterdir()) == ["credential"]
    if fault == "replace":

        def refused(*args):
            raise OSError("refused")

        monkeypatch.setattr(rt.os, "replace", refused)
        data = b"password=LEFT-BEHIND\n"
    else:
        data = "password=LEFT-BEHIND\n"  # text, which the file refuses
    with pytest.raises((OSError, TypeError)):
        host.put_account(4, 1, "credential", data)
    assert sorted(p.name for p in folder.iterdir()) == ["credential"]
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
    assert rt.PodmanRuntime._ids(3) == [
        f"--uidmap=0:{base}:{rt.RANGE}",
        f"--gidmap=0:{base}:{rt.RANGE}",
    ]
    assert rt.PodmanRuntime._ids(3, 0, 0) == rt.PodmanRuntime._ids(3)


@pytest.mark.parametrize("own", OWN_IDS)
def test_a_swapped_map_swaps_the_users_id_with_roots_and_moves_nothing_else(own):
    base = 3 * rt.RANGE
    options = rt.PodmanRuntime._ids(3, own, own)
    uid = mapped(ids_of(options, "uid"))
    assert sorted(uid) == list(range(rt.RANGE))  # every id of the container is mapped, once
    assert sorted(uid.values()) == list(range(base, base + rt.RANGE))  # onto every host id, once
    assert uid[own] == base  # the user is the host id root has in the ordinary map
    assert uid[0] == base + own  # and root is the user's
    assert all(uid[i] == base + i for i in range(1, rt.RANGE) if i != own)
    assert mapped(ids_of(options, "gid")) == uid  # the same for groups here


@pytest.mark.parametrize(("uid", "gid"), [(1000, 0), (0, 1000), (5, 1000)])
def test_the_user_and_the_group_are_swapped_each_on_its_own(uid, gid):
    options = rt.PodmanRuntime._ids(2, uid, gid)
    base = 2 * rt.RANGE
    assert mapped(ids_of(options, "uid"))[uid] == base
    assert mapped(ids_of(options, "gid"))[gid] == base
    if uid == 0:
        assert ids_of(options, "uid") == [(0, base, rt.RANGE)]  # nothing swapped
    if gid == 0:
        assert ids_of(options, "gid") == [(0, base, rt.RANGE)]


def test_the_edges_leave_out_the_stretch_that_would_be_empty():
    base = rt.RANGE
    first = ids_of(rt.PodmanRuntime._ids(1, 1, 1), "uid")
    assert first == [(0, base + 1, 1), (1, base, 1), (2, base + 2, rt.RANGE - 2)]
    last = ids_of(rt.PodmanRuntime._ids(1, rt.RANGE - 1, 1), "uid")
    assert last == [
        (0, base + rt.RANGE - 1, 1),
        (1, base + 1, rt.RANGE - 2),
        (rt.RANGE - 1, base, 1),
    ]
    assert all(length > 0 for _, _, length in first + last)


@pytest.mark.parametrize("own", [rt.RANGE, rt.RANGE + 1, -1])
def test_an_id_beyond_the_range_is_refused_before_anything_is_made(tmp_path, own):
    with pytest.raises(rt.RuntimeFailed):
        rt.PodmanRuntime._ids(3, own, 0)
    with pytest.raises(rt.RuntimeFailed):
        rt.PodmanRuntime._ids(3, 0, own)
    recorder = account_recorder(tmp_path)
    with pytest.raises(rt.RuntimeFailed):
        podman_runtime(tmp_path, recorder).create(
            rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2, uid=own)
        )
    assert recorder.calls() == []  # nothing was asked of podman


def test_slot_zero_is_refused_for_a_swapped_map_too():
    with pytest.raises(rt.RuntimeFailed):
        rt.PodmanRuntime._ids(0, 1000, 1000)


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
    assert not [a for a in run if a.startswith(("--privileged", "--cap", "--security-opt", "-p"))]


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
    assert first == ["exec", "--workdir=/rcman/work", "rcman-ws-7", "pwd"]
    assert second == ["exec", "rcman-ws-7", "pwd"]  # no folder asked for


def fake_podman(tmp_path: Path, body: str) -> rt.PodmanRuntime:
    """A podman that is the Python ``body``, which has ``sys`` and ``args``, rcman's own
    arguments after ``--remote --url <socket>``."""
    program = tmp_path / "podman"
    program.write_text(
        "#!/usr/bin/env python3\nimport json, os, sys, time\n"
        "args = sys.argv[4:]\n"
        f"with open({str(tmp_path / 'calls')!r}, 'a') as log:\n"
        "    log.write(json.dumps(args) + '\\n')\n" + body
    )
    program.chmod(0o755)
    return rt.PodmanRuntime(
        rt.PodmanConfig(
            socket=Path("/run/user/990/podman/podman.sock"),
            image="localhost/rcman-workspace:test",
            tools_dir=Path("/var/lib/rcman/tools"),
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
    assert copied == [f"{name}:/etc/passwd", f"{name}:/etc/group"] and all(
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
    assert changed.path == f"{rt.CLAUDE_MOUNT}/bin:/x/bin:/usr/bin"  # rcman's folder stays first
    assert base.user == "root" and base.env == {}  # the layout itself is not changed
    on_host = host.layout("x")
    assert on_host.for_plan("u", {}, "/p").path == "/p"  # the host has no folder of its own


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
