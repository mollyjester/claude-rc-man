"""The cases every runtime (backend/rcman/runtime.py) must pass, whatever it is.

Not a test module. tests/test_runtime.py runs them against the host runtime, and
tests/container/test_runtime_in_podman.py against Podman in the podman host, so
that the host runtime the other tests use behaves as Podman does where it matters.
Each case takes a runtime and a ``spec(n)``, the n-th workspace a case may make,
and removes what it made. Python standard library and the backend only.
"""

from __future__ import annotations

import contextlib
import time

from rcman import runtime as rt

# How long a tmux server started through a channel is waited for.
SETTLES_WITHIN = 10.0


@contextlib.contextmanager
def made(runtime: rt.Runtime, spec: rt.Spec):
    runtime.create(spec)
    try:
        yield spec.name
    finally:
        runtime.remove(spec.name)


def _sh(layout: rt.Layout, script: str) -> list[str]:
    return [layout.shell, "-c", script]


def a_workspace_that_is_made_exists_runs_and_is_labelled(runtime, spec):
    workspace = spec(1)
    assert not runtime.exists(workspace.name)  # positive control for the absence below
    with made(runtime, workspace) as name:
        assert runtime.exists(name) and runtime.running(name)
        labels = runtime.labelled()[name]
        assert labels[rt.LABEL_WORKSPACE] == str(workspace.workspace)
        assert labels[rt.LABEL_PERSON] == str(workspace.person)
        layout = runtime.layout(name)
        for folder in (layout.work, layout.config, layout.home):
            assert runtime.exec(name, _sh(layout, f"test -d {folder}")).code == 0, folder
        assert runtime.exec(name, _sh(layout, "test -d /no/such/folder")).code != 0
    assert not runtime.exists(workspace.name)
    assert workspace.name not in runtime.labelled()


def removing_what_is_not_there_is_nothing(runtime, spec):
    workspace = spec(2)
    assert not runtime.exists(workspace.name)
    runtime.remove(workspace.name)
    assert not runtime.exists(workspace.name)


def an_exec_gets_its_input_and_answers_its_streams_and_code(runtime, spec):
    with made(runtime, spec(3)) as name:
        layout = runtime.layout(name)
        answer = runtime.exec(name, [*layout.tool("cat")], stdin=b"secret\n")
        assert answer == rt.Result(0, b"secret\n", b"")
        answer = runtime.exec(name, _sh(layout, "echo out; echo err >&2; exit 3"))
        assert answer == rt.Result(3, b"out\n", b"err\n")


def a_channel_runs_in_the_workspace(runtime, spec):
    with made(runtime, spec(4)) as name:
        layout = runtime.layout(name)
        runtime.exec(name, _sh(layout, f"echo here > {layout.work}/marker"))
        channel = runtime.channel(name)
        try:
            found = channel.run([*layout.tool("cat"), f"{layout.work}/marker"])
            assert found == rt.Result(0, b"here\n", b"")
        finally:
            channel.close()


def tmux(layout: rt.Layout, *args: str) -> list[str]:
    env = {"SHELL": layout.shell, "PATH": layout.path, "TERM": "xterm-256color"}
    if layout.terminfo:
        env["TERMINFO"] = layout.terminfo
    base = [layout.tmux, "-S", layout.tmux_socket, "-f", layout.tmux_conf]
    return layout.with_env(env, [*base, *args])


def _sessions(runtime, name, layout) -> bytes:
    channel = runtime.channel(name)
    try:
        return channel.run(tmux(layout, "list-sessions", "-F", "#{session_name}")).out
    finally:
        channel.close()


def a_tmux_server_started_through_a_channel_outlives_it_and_not_a_stop(runtime, spec):
    with made(runtime, spec(5)) as name:
        layout = runtime.layout(name)
        channel = runtime.channel(name)
        try:
            started = channel.run(tmux(layout, "new-session", "-d", "-s", "probe", "sleep 600"))
            assert started.code == 0, started
        finally:
            channel.close()
        deadline = time.monotonic() + SETTLES_WITHIN
        while _sessions(runtime, name, layout) != b"probe\n":
            assert time.monotonic() < deadline
            time.sleep(0.2)
        runtime.stop(name)
        assert not runtime.running(name)
        runtime.start(name)
        assert runtime.running(name)
        assert _sessions(runtime, name, layout) == b""


def a_workspaces_files_outlive_a_stop(runtime, spec):
    with made(runtime, spec(6)) as name:
        layout = runtime.layout(name)
        runtime.exec(name, _sh(layout, f"echo kept > {layout.work}/file"))
        runtime.stop(name)
        runtime.start(name)
        assert runtime.exec(name, [*layout.tool("cat"), f"{layout.work}/file"]).out == b"kept\n"


def a_whole_process_group_is_ended_by_kill_group_of(runtime, spec):
    with made(runtime, spec(7)) as name:
        layout = runtime.layout(name)
        pids = f"{layout.work}/pids"
        group = (
            f"setsid {layout.shell} -c 'sleep 600 & echo $$ $! > {pids}; wait' "
            "</dev/null >/dev/null 2>&1 &"
        )
        assert runtime.exec(name, _sh(layout, group)).code == 0
        deadline = time.monotonic() + SETTLES_WITHIN
        while True:
            found = runtime.exec(name, [*layout.tool("cat"), pids]).out.split()
            if len(found) == 2:
                break
            assert time.monotonic() < deadline
            time.sleep(0.1)
        leader, child = (int(pid) for pid in found)
        alive = [*layout.tool("kill"), "-0"]
        assert runtime.exec(name, [*alive, str(leader)]).code == 0  # positive control
        assert runtime.exec(name, [*alive, str(child)]).code == 0
        assert runtime.exec(name, layout.kill_group_of(leader)).code == 0
        deadline = time.monotonic() + SETTLES_WITHIN
        while runtime.exec(name, [*alive, str(child)]).code == 0:
            assert time.monotonic() < deadline
            time.sleep(0.1)


CASES = [
    a_workspace_that_is_made_exists_runs_and_is_labelled,
    removing_what_is_not_there_is_nothing,
    an_exec_gets_its_input_and_answers_its_streams_and_code,
    a_channel_runs_in_the_workspace,
    a_tmux_server_started_through_a_channel_outlives_it_and_not_a_stop,
    a_workspaces_files_outlive_a_stop,
    a_whole_process_group_is_ended_by_kill_group_of,
]
