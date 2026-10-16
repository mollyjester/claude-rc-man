"""What the runtimes do for limits (backend/rcman/runtime.py): the Podman runtime asks
systemctl, as rcman's own user, for properties of slices and puts workspace containers in
them; the host runtime records what it is asked. The real thing is the on-demand suite's
(tests/container/test_limits_in_podman.py)."""

import json
import subprocess
from pathlib import Path

import pytest
from rcman import limits, people
from rcman import runtime as rt
from rcman.limits import Limits, Usage

from tests.test_runtime import Recorder, account_recorder, podman_runtime, runs

# --- a systemctl that records what it is asked ----------------------------------------


class Systemctl:
    """Stands in for systemctl: records each argv with the environment it was run with, and
    answers from a script by the verb after ``--user``."""

    def __init__(self, tmp_path: Path, answers: dict[str, tuple[int, str, str]] | None = None):
        self.log = tmp_path / "systemctl-calls"
        self.program = tmp_path / "systemctl"
        self.program.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            f"line = json.dumps([sys.argv[1:], dict(os.environ)]) + '\\n'\n"
            f"open({str(self.log)!r}, 'a').write(line)\n"
            f"answers = {json.dumps(answers or {})}\n"
            "words = [a for a in sys.argv[1:] if not a.startswith('-')]\n"
            "code, out, err = answers.get(words[0], [0, '', ''])\n"
            "sys.stdout.write(out); sys.stderr.write(err); sys.exit(code)\n"
        )
        self.program.chmod(0o755)

    def entries(self) -> list[tuple[list[str], dict[str, str]]]:
        if not self.log.exists():
            return []
        return [tuple(json.loads(line)) for line in self.log.read_text().splitlines()]

    def calls(self) -> list[list[str]]:
        return [argv for argv, _ in self.entries()]


def runtime_with(tmp_path: Path, systemctl: Systemctl | str) -> rt.PodmanRuntime:
    program = systemctl if isinstance(systemctl, str) else str(systemctl.program)
    return rt.PodmanRuntime(
        rt.PodmanConfig(
            socket=Path("/run/user/990/podman/podman.sock"),
            image="localhost/rcman-workspace:test",
            tools_dir=Path("/opt/rcman/tools"),
            claude_store=Path("/var/lib/rcman/claude"),
            podman=str(tmp_path / "podman"),
            systemctl=program,
        )
    )


LIMITED = Limits(2048, 150, 500)
# What systemctl shows of an active slice with nothing limited, for both kinds of reading.
SHOWN = (
    "ActiveState=active\nMemoryCurrent=1\nMemorySwapCurrent=0\nTasksCurrent=1\n"
    "MemoryMax=infinity\nTasksMax=infinity\n"
)

# --- applying --------------------------------------------------------------------------


def test_systemctl_is_a_setting_of_the_runtime_beside_podman():
    config = rt.PodmanConfig(Path("/s"), "i", Path("/t"), Path("/c"))
    assert config.systemctl.endswith("systemctl") and Path(config.systemctl).is_absolute()
    assert (
        rt.PodmanConfig(Path("/s"), "i", Path("/t"), Path("/c"), systemctl="/x/ctl").systemctl
        == "/x/ctl"
    )


def test_a_persons_limits_are_all_four_properties_of_their_slice_set_for_the_runtime_only(tmp_path):
    systemctl = Systemctl(tmp_path)
    runtime_with(tmp_path, systemctl).apply_limits(7, None, LIMITED)
    assert systemctl.calls() == [
        [
            "--user",
            "set-property",
            "--runtime",
            "rcman-p7.slice",
            f"MemoryMax={2048 * 1024 * 1024}",
            "MemorySwapMax=0",
            "CPUQuota=150%",
            "TasksMax=500",
        ]
    ]


def test_a_workspaces_limits_are_set_on_its_own_slice_beneath_its_persons(tmp_path):
    systemctl = Systemctl(tmp_path)
    runtime_with(tmp_path, systemctl).apply_limits(7, 12, Limits())
    [(argv, _)] = systemctl.entries()
    assert argv[:4] == ["--user", "set-property", "--runtime", "rcman-p7-w12.slice"]
    assert argv[4:] == [
        "MemoryMax=infinity",
        "MemorySwapMax=infinity",
        "CPUQuota=",
        "TasksMax=infinity",
    ]


def test_every_systemctl_call_is_for_the_user_manager_and_runtime_properties_only(tmp_path):
    systemctl = Systemctl(tmp_path, {"show": (0, SHOWN, "")})
    runtime = runtime_with(tmp_path, systemctl)
    runtime.apply_limits(7, 12, LIMITED)
    runtime.usage(7, 12)
    runtime.drop_limits(7, 12)
    calls = systemctl.calls()
    assert [c[1] for c in calls] == ["set-property", "show", "revert"]  # non-vacuity
    for call in calls:
        assert call[0] == "--user"
        if call[1] == "set-property":
            assert call[2] == "--runtime"  # never written to disk: /run goes with a restart


def test_systemctl_is_run_with_an_argv_and_no_shell_and_with_the_allowlisted_environment(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RCMAN_SECRET_OF_THE_SERVICE", "x")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/990")
    systemctl = Systemctl(tmp_path)
    seen = []
    real = subprocess.run

    def recording(argv, *args, **kwargs):
        seen.append((argv, kwargs))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording)
    runtime_with(tmp_path, systemctl).apply_limits(7, None, LIMITED)
    [(argv, kwargs)] = seen
    assert isinstance(argv, list) and argv[0] == str(systemctl.program)
    assert not kwargs.get("shell")
    [(_, env)] = systemctl.entries()
    assert "RCMAN_SECRET_OF_THE_SERVICE" not in env
    assert env["XDG_RUNTIME_DIR"] == "/run/user/990"  # systemctl --user cannot find the bus without
    assert set(env) <= {"PATH", "LANG", "HOME", "XDG_RUNTIME_DIR"}


def test_a_systemctl_that_refuses_is_a_failure_with_what_it_said(tmp_path):
    systemctl = Systemctl(tmp_path, {"set-property": (1, "", "Unit rcman-p7.slice not found.\n")})
    with pytest.raises(rt.RuntimeFailed, match="not found"):
        runtime_with(tmp_path, systemctl).apply_limits(7, None, LIMITED)


def test_no_bus_to_the_user_manager_is_the_runtime_being_unavailable(tmp_path):
    said = "Failed to connect to bus: No medium found\n"
    systemctl = Systemctl(tmp_path, {"set-property": (1, "", said), "show": (1, "", said)})
    runtime = runtime_with(tmp_path, systemctl)
    with pytest.raises(rt.RuntimeUnavailable):
        runtime.apply_limits(7, None, LIMITED)
    with pytest.raises(rt.RuntimeUnavailable):
        runtime.usage(7, None)


def test_a_systemctl_that_is_not_there_is_the_runtime_being_unavailable(tmp_path):
    runtime = runtime_with(tmp_path, str(tmp_path / "no-such-systemctl"))
    with pytest.raises(rt.RuntimeUnavailable):
        runtime.apply_limits(7, None, LIMITED)


def test_a_systemctl_that_does_not_answer_in_time_is_a_failure(tmp_path, monkeypatch):
    systemctl = Systemctl(tmp_path)
    program = Path(systemctl.program)
    program.write_text("#!/bin/sh\nexec sleep 30\n")
    monkeypatch.setattr(rt, "SYSTEMCTL_TIMEOUT", 0.3)
    with pytest.raises(rt.RuntimeFailed, match="in time"):
        runtime_with(tmp_path, systemctl).apply_limits(7, None, LIMITED)


# --- what is in use --------------------------------------------------------------------


def test_usage_is_what_systemctl_shows_of_the_slice_and_what_is_beneath_it(tmp_path):
    shown = "ActiveState=active\nMemoryCurrent=3221225472\nMemorySwapCurrent=0\nTasksCurrent=77\n"
    systemctl = Systemctl(tmp_path, {"show": (0, shown, "")})
    used = runtime_with(tmp_path, systemctl).usage(7, None)
    assert used == Usage(memory_bytes=3 * 1024**3, tasks=77)
    assert systemctl.calls() == [
        [
            "--user", "show", "-p", "ActiveState", "-p", "MemoryCurrent",
            "-p", "MemorySwapCurrent", "-p", "TasksCurrent", "rcman-p7.slice",
        ]
    ]  # fmt: skip
    runtime_with(tmp_path, systemctl).usage(7, 12)
    assert systemctl.calls()[-1][-1] == "rcman-p7-w12.slice"


def test_a_slice_that_never_ran_anything_uses_nothing(tmp_path):
    shown = (
        "ActiveState=inactive\nMemoryCurrent=[not set]\nMemorySwapCurrent=[not set]\n"
        "TasksCurrent=[not set]\n"
    )
    systemctl = Systemctl(tmp_path, {"show": (0, shown, "")})
    assert runtime_with(tmp_path, systemctl).usage(7, 12) == Usage(0, 0)


def test_an_active_slice_whose_use_is_not_counted_is_unknown_not_zero(tmp_path):
    shown = (
        "ActiveState=active\nMemoryCurrent=[not set]\nMemorySwapCurrent=[not set]\nTasksCurrent=5\n"
    )
    used = runtime_with(tmp_path, Systemctl(tmp_path, {"show": (0, shown, "")})).usage(7, 12)
    assert not used.memory_known and used.tasks_known and used.tasks == 5
    assert used != Usage(0, 5)


def test_usage_that_cannot_be_read_is_a_failure_and_never_zero(tmp_path):
    for number, (out, code) in enumerate((("garbage\n", 0), ("MemoryCurrent=1\n", 0), ("", 1))):
        folder = tmp_path / str(number)
        folder.mkdir()
        systemctl = Systemctl(folder, {"show": (code, out, "no")})
        with pytest.raises(rt.RuntimeFailed):
            runtime_with(folder, systemctl).usage(7, None)


# --- the caps a slice has now ------------------------------------------------------------


def test_the_caps_are_what_systemctl_shows_of_the_slice_itself(tmp_path):
    shown = "MemoryMax=2147483648\nTasksMax=500\n"
    systemctl = Systemctl(tmp_path, {"show": (0, shown, "")})
    assert runtime_with(tmp_path, systemctl).caps(7, 12) == limits.Caps(2147483648, 500)
    assert systemctl.calls() == [
        ["--user", "show", "-p", "MemoryMax", "-p", "TasksMax", "rcman-p7-w12.slice"]
    ]


def test_infinity_is_no_cap_and_caps_that_cannot_be_read_are_a_failure(tmp_path):
    shown = "MemoryMax=infinity\nTasksMax=infinity\n"
    systemctl = Systemctl(tmp_path, {"show": (0, shown, "")})
    assert runtime_with(tmp_path, systemctl).caps(7, None) == limits.Caps(None, None)
    (tmp_path / "bad").mkdir()
    bad = Systemctl(tmp_path / "bad", {"show": (0, "garbage\n", "")})
    with pytest.raises(rt.RuntimeFailed):
        runtime_with(tmp_path / "bad", bad).caps(7, None)


# --- dropping --------------------------------------------------------------------------


def test_dropping_limits_reverts_the_slice_and_stops_nothing(tmp_path):
    systemctl = Systemctl(tmp_path)
    runtime = runtime_with(tmp_path, systemctl)
    runtime.drop_limits(7, 12)
    runtime.drop_limits(7, None)
    assert systemctl.calls() == [
        ["--user", "revert", "rcman-p7-w12.slice"],
        ["--user", "revert", "rcman-p7.slice"],
    ]


def test_a_revert_that_fails_is_a_failure_for_the_caller_to_decide_on(tmp_path):
    systemctl = Systemctl(tmp_path, {"revert": (1, "", "Failed to revert\n")})
    with pytest.raises(rt.RuntimeFailed):
        runtime_with(tmp_path, systemctl).drop_limits(7, 12)


# --- containers are put in the slice -----------------------------------------------------


def test_a_workspace_container_is_made_in_its_slice_with_no_limit_of_its_own_on_processes(tmp_path):
    recorder = account_recorder(tmp_path)
    podman_runtime(tmp_path, recorder).create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    workspace = next(r for r in runs(recorder) if "--name=rcman-ws-7" in r)
    assert "--cgroup-parent=rcman-p2-w7.slice" in workspace
    assert "--pids-limit=-1" in workspace  # else Podman gives the container a limit of its own
    assert [a for a in workspace if a.startswith("--cgroup-parent")] == [
        f"--cgroup-parent={limits.workspace_slice(2, 7)}"
    ]


def test_a_container_of_the_users_own_is_in_the_slice_too(tmp_path):
    recorder = account_recorder(tmp_path)
    spec = rt.Spec(
        "rcman-ws-7", slot=3, workspace=7, person=2, image="x/y:1", user="dev", uid=1234, gid=1234
    )
    podman_runtime(tmp_path, recorder).create(spec)
    [workspace] = [r for r in runs(recorder) if "--name=rcman-ws-7" in r]
    assert "--cgroup-parent=rcman-p2-w7.slice" in workspace and "--pids-limit=-1" in workspace


def only_the_workspace_has_a_slice(recorder: Recorder) -> None:
    """Every container run other than the workspace's own is in no slice, and has no limit of
    its own on processes set."""
    others = [r for r in runs(recorder) if "--name=rcman-ws-7" not in r]
    assert others  # non-vacuity: there are some
    for other in others:
        assert not [a for a in other if a.startswith(("--cgroup-parent", "--pids-limit"))], other


def test_what_prepares_a_workspace_is_in_no_slice(tmp_path):
    """The folders of a swapped map are made by a container of their own, and the person's
    account by another: neither is held to a limit, as neither is the person's work."""
    recorder = account_recorder(tmp_path, volume_exists=0)
    spec = rt.Spec(
        "rcman-ws-7", slot=3, workspace=7, person=2, image="x/y:1", user="dev", uid=1234, gid=1234
    )
    podman_runtime(tmp_path, recorder).create(spec)
    only_the_workspace_has_a_slice(recorder)
    assert len(runs(recorder)) >= 3  # the account's, the folders', the workspace's


def test_signing_in_and_the_account_are_in_no_slice_so_signing_out_is_never_held_up(tmp_path):
    recorder = account_recorder(tmp_path, volume_exists=0)
    runtime = podman_runtime(tmp_path, recorder)
    runtime.ensure_account(2, 3)
    runtime.start_sign_in(2, 3, seconds=900)
    runtime.clear_account(2, 3)
    runtime.read_account(2, 3)
    runtime.put_account(2, 3, "config", b"x")
    made = runs(recorder)
    assert len(made) >= 5  # non-vacuity
    for run in made:
        assert not [a for a in run if a.startswith(("--cgroup-parent", "--pids-limit"))], run


def test_the_canary_is_rcmans_own_and_in_no_slice(tmp_path):
    recorder = Recorder(tmp_path, {"run": (0, "{}", "")})
    runtime = podman_runtime(tmp_path, recorder)
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    runtime.run_canary(candidate, rt.CANARY_NAME, "1.2.3")
    [run] = runs(recorder)
    assert not [a for a in run if a.startswith(("--cgroup-parent", "--pids-limit"))]


def test_the_probe_that_reads_an_image_is_in_no_slice(tmp_path):
    from tests.test_runtime import fake_calls, image_podman

    image_podman(tmp_path).image_facts("docker.io/library/node:20")
    made = [c for c in fake_calls(tmp_path) if c[0] == "create"]
    assert made  # non-vacuity: a probe container is made
    for create in made:
        assert not [a for a in create if a.startswith(("--cgroup-parent", "--pids-limit"))]


# --- the host runtime ------------------------------------------------------------------


@pytest.fixture
def host(tmp_path):
    from devtools.held_folders import tmux_sockets

    root = tmp_path / "workspaces"
    root.mkdir()
    repo = Path(__file__).resolve().parent.parent
    with tmux_sockets("rcl-") as sockets:
        yield rt.HostRuntime(root, sockets, tmp_path / "claude", repo / "backend/rcman/tmux.conf")


def test_the_host_runtime_records_the_limits_it_is_asked_to_apply_by_slice(host):
    host.apply_limits(7, None, LIMITED)
    host.apply_limits(7, 12, Limits(pids=100))
    assert host.limits == {"rcman-p7.slice": LIMITED, "rcman-p7-w12.slice": Limits(pids=100)}
    assert host.applied == [
        ("rcman-p7.slice", LIMITED),
        ("rcman-p7-w12.slice", Limits(pids=100)),
    ]


def test_the_host_runtime_forgets_the_limits_of_a_slice_it_is_asked_to_drop(host):
    host.apply_limits(7, 12, LIMITED)
    host.apply_limits(7, None, LIMITED)
    host.drop_limits(7, 12)
    assert host.limits == {"rcman-p7.slice": LIMITED}
    assert host.dropped == ["rcman-p7-w12.slice"]
    host.drop_limits(7, 12)  # nothing there is nothing to drop
    assert host.limits == {"rcman-p7.slice": LIMITED}


def test_the_host_runtime_has_the_usage_a_test_planted_and_none_otherwise(host):
    assert host.usage(7, 12) == Usage(0, 0)
    host.used[limits.workspace_slice(7, 12)] = Usage(10, 3)
    assert host.usage(7, 12) == Usage(10, 3)
    assert host.usage(7, None) == Usage(0, 0)


def test_the_host_runtime_has_the_caps_it_was_actually_given_and_none_otherwise(host):
    """The caps are the actual ones, as the slice has them: what was applied last, and
    nothing once dropped or never applied, whatever a table says."""
    assert host.caps(7, 12) == limits.Caps()
    host.apply_limits(7, 12, Limits(2048, 150, 500))
    assert host.caps(7, 12) == limits.Caps(2048 * 1024 * 1024, 500)
    assert host.caps(7, None) == limits.Caps()  # another slice
    host.apply_limits(7, 12, Limits(cpu_percent=50))
    assert host.caps(7, 12) == limits.Caps()  # applied again with none: reset
    host.apply_limits(7, 12, Limits(pids=100))
    host.drop_limits(7, 12)
    assert host.caps(7, 12) == limits.Caps()


def test_the_host_runtime_fails_as_a_test_makes_it(host):
    host.limits_error = rt.RuntimeFailed("no")
    with pytest.raises(rt.RuntimeFailed):
        host.apply_limits(7, None, LIMITED)
    assert host.limits == {}  # a failed apply applied nothing
    with pytest.raises(rt.RuntimeFailed):
        host.usage(7, None)
    with pytest.raises(rt.RuntimeFailed):
        host.caps(7, None)
    with pytest.raises(rt.RuntimeFailed):
        host.drop_limits(7, None)


def test_a_host_runtime_container_is_made_with_the_spec_that_names_its_slice(host):
    host.create(rt.Spec(rt.container_name(7), slot=1, workspace=7, person=2))
    assert limits.workspace_slice(
        host.specs["rcman-ws-7"].person, host.specs["rcman-ws-7"].workspace
    ) == ("rcman-p2-w7.slice")


def test_people_are_not_given_a_slot_by_the_slice_so_a_reused_slot_is_not_a_reused_slice():
    assert people.MAX_PEOPLE > 1
    assert limits.person_slice(1) != limits.person_slice(2)
