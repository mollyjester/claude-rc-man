"""What the runtimes do for limits (backend/rcman/runtime.py): the Podman runtime asks
systemctl, as rcman's own user, for properties of slices and puts workspace containers in
them; the host runtime records what it is asked. What the kernel then does with a slice is
checked by hand (project-knowledge/real-claude-checklist.md)."""

import json
from pathlib import Path

import pytest
from rcman import limits
from rcman import runtime as rt
from rcman.limits import Limits, Usage

from tests.test_runtime import account_recorder, podman_runtime, runs

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

# --- applying --------------------------------------------------------------------------


def test_limits_are_set_on_the_persons_slice_and_the_workspaces_beneath_it_for_the_runtime_only(
    tmp_path, monkeypatch
):
    """All four properties, for the user manager, never written to disk (/run goes with a
    restart), by an argv with the allowlisted environment only."""
    monkeypatch.setenv("RCMAN_SECRET_OF_THE_SERVICE", "x")
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/990")
    systemctl = Systemctl(tmp_path)
    runtime = runtime_with(tmp_path, systemctl)
    runtime.apply_limits(7, None, LIMITED)
    runtime.apply_limits(7, 12, Limits())
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
        ],
        [
            "--user",
            "set-property",
            "--runtime",
            "rcman-p7-w12.slice",
            "MemoryMax=infinity",
            "MemorySwapMax=infinity",
            "CPUQuota=",
            "TasksMax=infinity",
        ],
    ]
    for _, env in systemctl.entries():
        assert "RCMAN_SECRET_OF_THE_SERVICE" not in env
        assert (
            env["XDG_RUNTIME_DIR"] == "/run/user/990"
        )  # systemctl --user cannot find the bus without
        assert set(env) <= {"PATH", "LANG", "HOME", "XDG_RUNTIME_DIR"}


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


# --- the caps a slice has now ------------------------------------------------------------


def test_the_caps_are_what_systemctl_shows_of_the_slice_itself(tmp_path):
    shown = "MemoryMax=2147483648\nTasksMax=500\n"
    systemctl = Systemctl(tmp_path, {"show": (0, shown, "")})
    assert runtime_with(tmp_path, systemctl).caps(7, 12) == limits.Caps(2147483648, 500)
    assert systemctl.calls() == [
        ["--user", "show", "-p", "MemoryMax", "-p", "TasksMax", "rcman-p7-w12.slice"]
    ]


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


# --- containers are put in the slice -----------------------------------------------------


def test_a_workspace_container_is_made_in_its_slice_and_nothing_else_rcman_runs_is(tmp_path):
    """With no limit of its own on processes, else Podman gives the container one; what
    prepares a workspace, the person's account, is not the person's work and is in no slice."""
    recorder = account_recorder(tmp_path)
    podman_runtime(tmp_path, recorder).create(rt.Spec("rcman-ws-7", slot=3, workspace=7, person=2))
    workspace = next(r for r in runs(recorder) if "--name=rcman-ws-7" in r)
    assert "--pids-limit=-1" in workspace
    assert [a for a in workspace if a.startswith("--cgroup-parent")] == [
        f"--cgroup-parent={limits.workspace_slice(2, 7)}"
    ]
    others = [r for r in runs(recorder) if "--name=rcman-ws-7" not in r]
    assert others  # non-vacuity: the account's
    for other in others:
        assert not [a for a in other if a.startswith(("--cgroup-parent", "--pids-limit"))], other


# --- the host runtime ------------------------------------------------------------------


@pytest.fixture
def host(tmp_path):
    from devtools.held_folders import tmux_sockets

    root = tmp_path / "workspaces"
    root.mkdir()
    repo = Path(__file__).resolve().parent.parent
    with tmux_sockets("rcl-") as sockets:
        yield rt.HostRuntime(root, sockets, tmp_path / "claude", repo / "backend/rcman/tmux.conf")


def test_the_host_runtime_records_the_limits_it_applies_and_forgets_those_it_drops(host):
    host.apply_limits(7, None, LIMITED)
    host.apply_limits(7, 12, Limits(pids=100))
    assert host.limits == {"rcman-p7.slice": LIMITED, "rcman-p7-w12.slice": Limits(pids=100)}
    assert host.applied == [
        ("rcman-p7.slice", LIMITED),
        ("rcman-p7-w12.slice", Limits(pids=100)),
    ]
    host.drop_limits(7, 12)
    assert host.limits == {"rcman-p7.slice": LIMITED}
    assert host.dropped == ["rcman-p7-w12.slice"]
