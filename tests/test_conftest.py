"""The fixtures in tests/conftest.py that hold shared resources: each test's tmux sockets."""

import subprocess

from tests.conftest import helper_runner, tmux_sockets


def tmux(sock, *args):
    return subprocess.run(["tmux", "-S", str(sock), *args], capture_output=True, text=True)


def same_named(tmp_path):
    """Two tmp_paths with the same name under different base folders, as pytest-xdist's
    workers each number their own."""
    paths = [tmp_path / base / "test_a_name0" for base in ("gw0", "gw1")]
    for path in paths:
        path.mkdir(parents=True)
    return paths


def test_same_named_tmp_paths_get_socket_folders_of_their_own(tmp_path, github):
    one, two = same_named(tmp_path)
    with helper_runner(one, github.url) as a, helper_runner(two, github.url) as b:
        assert a.socket("alice").parent.is_dir() and b.socket("alice").parent.is_dir()
        assert a.socket("alice").parent != b.socket("alice").parent


def test_one_runners_teardown_leaves_anothers_tmux_alone(tmp_path, github):
    one, two = same_named(tmp_path)
    with helper_runner(one, github.url) as a:
        assert a("alice", ["list"])[0] == 0
        sock = a.socket("alice")
        assert tmux(sock, "new-session", "-d", "-s", "kept", "sleep", "600").returncode == 0
        with helper_runner(two, github.url) as b:
            assert b("alice", ["list"])[0] == 0
            assert tmux(sock, "has-session", "-t", "kept").returncode == 0  # positive control
        assert tmux(sock, "has-session", "-t", "kept").returncode == 0
    assert tmux(sock, "has-session", "-t", "kept").returncode != 0


def test_the_socket_folder_is_removed_after_the_test(tmp_path, github):
    with helper_runner(tmp_path, github.url) as runner:
        assert runner("alice", ["list"])[0] == 0
        sock = runner.socket("alice")
        assert tmux(sock, "new-session", "-d", "-s", "a", "sleep", "600").returncode == 0
        folder = sock.parent
        assert folder.is_dir() and sock.exists()  # positive control
    assert not folder.exists()
    assert tmux(sock, "has-session", "-t", "a").returncode != 0


def test_a_sockets_folder_is_removed_with_its_tmux_servers_after_the_test():
    with tmux_sockets("rct-") as one, tmux_sockets("rct-") as two:
        assert one != two and one.name.startswith("rct-")
        sock = one / "x.sock"
        assert tmux(sock, "new-session", "-d", "-s", "a", "sleep", "600").returncode == 0
        assert one.is_dir() and sock.exists()  # positive control
    assert not one.exists() and not two.exists()
    assert tmux(sock, "has-session", "-t", "a").returncode != 0
