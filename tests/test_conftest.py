"""The fixtures in tests/conftest.py that hold shared resources: the host runtime's tmux
sockets, in a folder devtools/held_folders.py holds; and the SIGHUP the suite's children get."""

import subprocess
import sys
from pathlib import Path

import pytest
from rcman import runtime as rt

from tests.conftest import host_runtime
from tests.hang_guard import HANG_GUARD


def tmux(sock, *args):
    return subprocess.run(["tmux", "-S", str(sock), *args], capture_output=True, text=True)


def same_named(tmp_path):
    """Two tmp_paths with the same name under different base folders, as pytest-xdist's
    workers each number their own."""
    paths = [tmp_path / base / "test_a_name0" for base in ("gw0", "gw1")]
    for path in paths:
        path.mkdir(parents=True)
    return paths


# The length of a sleep that outlasts the test (rule (g) of "Time in tests").
OUTLASTING = f"{3 * HANG_GUARD:g}"
SPEC = rt.Spec(rt.container_name(1), slot=1, workspace=1, person=1)


def socket_of(runtime):
    return runtime.layout(SPEC.name).tmux_socket


def test_same_named_tmp_paths_get_socket_folders_of_their_own(tmp_path):
    one, two = same_named(tmp_path)
    with host_runtime(one) as a, host_runtime(two) as b:
        assert a.sockets.is_dir() and b.sockets.is_dir()
        assert a.sockets != b.sockets


def test_one_runtimes_teardown_leaves_anothers_tmux_alone(tmp_path):
    one, two = same_named(tmp_path)
    with host_runtime(one) as a:
        a.create(SPEC)
        sock = socket_of(a)
        assert tmux(sock, "new-session", "-d", "-s", "kept", "sleep", OUTLASTING).returncode == 0
        with host_runtime(two) as b:
            b.create(SPEC)
            assert tmux(sock, "has-session", "-t", "kept").returncode == 0  # positive control
        assert tmux(sock, "has-session", "-t", "kept").returncode == 0
    assert tmux(sock, "has-session", "-t", "kept").returncode != 0


def test_the_socket_folder_is_removed_after_the_test(tmp_path):
    with host_runtime(tmp_path) as runtime:
        sock = runtime.sockets / "probe.sock"
        assert tmux(sock, "new-session", "-d", "-s", "a", "sleep", OUTLASTING).returncode == 0
        folder = runtime.sockets
        assert folder.is_dir() and sock.exists()  # positive control
    assert not folder.exists()
    assert tmux(sock, "has-session", "-t", "a").returncode != 0


def off_the_engine(modules: set[str], tests: dict[str, dict]) -> set[str]:
    """The modules of tests/container/ that do not run on the tests' engine: one with no test,
    or with a test that does not use it."""
    on_the_engine = {
        name
        for name in modules
        if (mine := [t for n, t in tests.items() if n.split("::")[0] == f"tests/{name}"])
        and all("podman_service" in t["fixtures"] for t in mine)
    }
    return modules - on_the_engine


def test_every_container_module_runs_on_the_engine_and_is_collected():
    from pathlib import Path

    from tests import conftest
    from tests.collected import collect

    assert not getattr(conftest, "collect_ignore", [])  # nothing is left out on the host
    folder = Path(__file__).parent / "container"
    modules = {f"container/{p.name}" for p in folder.glob("test_*.py")}
    assert "container/test_nest_in_podman.py" in modules  # non-vacuity
    assert off_the_engine(modules, collect(["tests/container"])) == set()


def test_a_module_off_the_engine_is_found():
    tests = {
        "tests/container/test_a.py::test_x": {"fixtures": ["podman_service", "tmp_path"]},
        "tests/container/test_b.py::test_y": {"fixtures": ["tmp_path"]},
        "tests/container/test_c.py::test_z": {"fixtures": ["podman_service"]},
        "tests/container/test_c.py::test_w": {"fixtures": []},
    }
    modules = {"container/test_a.py", "container/test_b.py", "container/test_c.py",
               "container/test_d.py"}  # fmt: skip
    assert off_the_engine(modules, tests) == {
        "container/test_b.py",  # no test of it uses the engine
        "container/test_c.py",  # one of its tests does not
        "container/test_d.py",  # it has no test that is collected
    }


# A child Python started with SIGHUP ignored, as under nohup, which configures the suite or not
# and says what a program it starts gets.
SIGHUP_CHILD = """
import signal, subprocess, sys
signal.signal(signal.SIGHUP, signal.SIG_IGN)
if sys.argv[1] == "configured":
    import tests.conftest
    tests.conftest.pytest_configure(None)
    assert signal.getsignal(signal.SIGHUP) not in (signal.SIG_IGN, signal.SIG_DFL)
status = subprocess.run(["cat", "/proc/self/status"], capture_output=True, text=True).stdout
ignored = int(next(f.split()[1] for f in status.splitlines() if f.startswith("SigIgn:")), 16)
print("ignored" if ignored & 1 << (signal.SIGHUP - 1) else "default")
"""


@pytest.mark.parametrize("configured", [False, True])
def test_a_suite_started_ignoring_sighup_gives_its_children_its_default(configured):
    """A terminal test's shell, and what it starts, end on a hangup only if SIGHUP is at its
    default in them, which an ignore the suite was started with would not be."""
    ran = subprocess.run(
        [sys.executable, "-c", SIGHUP_CHILD, "configured" if configured else "not"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parent.parent,
    )
    assert ran.returncode == 0, ran.stderr
    assert ran.stdout.strip() == ("default" if configured else "ignored")
