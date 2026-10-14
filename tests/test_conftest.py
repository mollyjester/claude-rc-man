"""The fixtures in tests/conftest.py that hold shared resources: the host runtime's tmux
sockets, in a folder devtools/held_folders.py holds."""

import subprocess

from rcman import runtime as rt

from tests.conftest import host_runtime


def tmux(sock, *args):
    return subprocess.run(["tmux", "-S", str(sock), *args], capture_output=True, text=True)


def same_named(tmp_path):
    """Two tmp_paths with the same name under different base folders, as pytest-xdist's
    workers each number their own."""
    paths = [tmp_path / base / "test_a_name0" for base in ("gw0", "gw1")]
    for path in paths:
        path.mkdir(parents=True)
    return paths


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
        assert tmux(sock, "new-session", "-d", "-s", "kept", "sleep", "600").returncode == 0
        with host_runtime(two) as b:
            b.create(SPEC)
            assert tmux(sock, "has-session", "-t", "kept").returncode == 0  # positive control
        assert tmux(sock, "has-session", "-t", "kept").returncode == 0
    assert tmux(sock, "has-session", "-t", "kept").returncode != 0


def test_the_socket_folder_is_removed_after_the_test(tmp_path):
    with host_runtime(tmp_path) as runtime:
        sock = runtime.sockets / "probe.sock"
        assert tmux(sock, "new-session", "-d", "-s", "a", "sleep", "600").returncode == 0
        folder = runtime.sockets
        assert folder.is_dir() and sock.exists()  # positive control
    assert not folder.exists()
    assert tmux(sock, "has-session", "-t", "a").returncode != 0
