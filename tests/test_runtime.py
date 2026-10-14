"""The runtimes: the contract every runtime keeps, against the host runtime here, and what
the Podman runtime asks of podman, from a recording of its calls.

tests/container/test_runtime_in_podman.py runs the same contract against real Podman.
"""

import json
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
            f"answers = {json.dumps(script)}\n"
            "words = [a for a in sys.argv[1:] if not a.startswith(('-', 'unix:'))]\n"
            "verb = ' '.join(words[:2]) if ' '.join(words[:2]) in answers else words[0]\n"
            "code, out, err = answers.get(verb, [0, '', ''])\n"
            "sys.stdout.write(out); sys.stderr.write(err); sys.exit(code)\n"
        )
        self.program.chmod(0o755)

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
    assert made[-1].endswith(f"/{rt.CREDENTIALS}")


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
