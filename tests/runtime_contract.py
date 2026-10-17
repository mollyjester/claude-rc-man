"""The cases every runtime (backend/rcman/runtime.py) must pass, whatever it is.

Not a test module. tests/test_runtime.py runs them against the host runtime, and
tests/container/test_runtime_in_podman.py against Podman in the podman host, so
that the host runtime the other tests use behaves as Podman does where it matters.
The ``CANARY_CASES`` need python3 in the runtime's image: tests/test_runtime.py runs them too,
and tests/container/test_claude_versions_in_podman.py against Podman with the default image.
Each case takes a runtime and a ``spec(n)``, the n-th workspace a case may make,
and removes what it made. Python standard library and the backend only.
"""

from __future__ import annotations

import contextlib
import dataclasses
import fcntl
import os
import re
import select
import shutil
import struct
import subprocess
import tempfile
import termios
import time
from pathlib import Path

import pytest
from rcman import runtime as rt
from rcman.runtime import TIMED_OUT

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


def _credentials(layout: rt.Layout) -> str:
    return f"{layout.config}/{rt.CREDENTIALS}"


def _read(runtime, name: str) -> bytes:
    layout = runtime.layout(name)
    found = runtime.exec(name, [*layout.tool("cat"), _credentials(layout)])
    assert found.code == 0, found
    return found.out


def _write_in_place(runtime, name: str, text: str) -> None:
    layout = runtime.layout(name)
    written = runtime.exec(name, _sh(layout, f"printf %s {text} > {_credentials(layout)}"))
    assert written.code == 0, written


def a_persons_workspaces_share_one_credentials_file_written_in_place(runtime, spec):
    first = spec(8)
    second = dataclasses.replace(spec(9), slot=first.slot, person=first.person)
    other = spec(10)
    try:
        with made(runtime, first) as a:
            assert runtime.read_account(first.person, first.slot) == b""  # made empty
            _write_in_place(runtime, a, "signed-in")
            with made(runtime, second) as b, made(runtime, other) as c:
                assert _read(runtime, b) == b"signed-in"  # made after it was written
                assert _read(runtime, c) == b""  # another person's is their own
                _write_in_place(runtime, b, "re")  # shorter: nothing of the old is left
                assert _read(runtime, a) == b"re"  # made before, it sees the change
                assert runtime.read_account(first.person, first.slot) == b"re"
                assert runtime.read_account(other.person, other.slot) == b""
                runtime.clear_account(first.person, first.slot)  # emptied in place
                assert _read(runtime, a) == _read(runtime, b) == b""
    finally:
        runtime.remove_account(first.person)
        runtime.remove_account(other.person)


def a_sign_in_container_has_the_persons_file_and_is_no_workspace(runtime, spec):
    who = spec(11)
    name = runtime.start_sign_in(who.person, who.slot, seconds=600)
    try:
        assert runtime.exists(name) and runtime.running(name)
        assert name not in runtime.labelled()  # restore never takes it for an orphan
        layout = runtime.layout(name)
        for folder in (layout.work, layout.config, layout.home):
            assert runtime.exec(name, _sh(layout, f"test -d {folder}")).code == 0, folder
        assert _read(runtime, name) == b""
        _write_in_place(runtime, name, "signed-in")
        assert runtime.read_account(who.person, who.slot) == b"signed-in"
        channel = runtime.channel(name)
        try:
            found = channel.run([*layout.tool("cat"), _credentials(layout)])
            assert found == rt.Result(0, b"signed-in", b"")
        finally:
            channel.close()
    finally:
        runtime.end_sign_in(name)
    assert not runtime.exists(name)
    runtime.end_sign_in(name)  # ending one that is gone is nothing
    assert runtime.read_account(who.person, who.slot) == b"signed-in"  # it outlives it
    runtime.remove_account(who.person)
    runtime.remove_account(who.person)  # removing one that is gone is nothing
    assert runtime.read_account(who.person, who.slot) == b""  # never the old one again
    runtime.remove_account(who.person)


def _git_dir(layout: rt.Layout) -> str:
    return f"{layout.home}/.config/git"


def _git_file(runtime, name: str, file: str) -> rt.Result:
    layout = runtime.layout(name)
    return runtime.exec(name, [*layout.tool("cat"), f"{_git_dir(layout)}/{file}"])


def a_persons_workspaces_share_one_git_directory_written_by_rename(runtime, spec):
    first = spec(12)
    second = dataclasses.replace(spec(13), slot=first.slot, person=first.person)
    other = spec(14)
    try:
        # With no workspace at all, and so before the account's folder is bound anywhere.
        runtime.put_account(first.person, first.slot, "config", b"before\n")
        with made(runtime, first) as a:
            assert _git_file(runtime, a, "config") == rt.Result(0, b"before\n", b"")
            runtime.put_account(first.person, first.slot, "config", b"after, and longer\n")
            runtime.put_account(first.person, first.slot, "credential", b"password=x\n")
            assert _git_file(runtime, a, "config").out == b"after, and longer\n"  # a running one
            runtime.put_account(first.person, first.slot, "credential", b"")  # emptied
            assert _git_file(runtime, a, "credential") == rt.Result(0, b"", b"")
            layout = runtime.layout(a)
            mode = runtime.exec(a, _sh(layout, f"stat -c %a {_git_dir(layout)}/config"))
            assert mode.out.strip() == b"600", mode
            with made(runtime, second) as b, made(runtime, other) as c:
                assert _git_file(runtime, b, "config").out == b"after, and longer\n"
                assert _git_file(runtime, c, "config").code != 0  # another person's is their own
                # A workspace replaces a file in the directory by a rename, which a bound
                # file does not allow.
                swap = f"cd {_git_dir(layout)} && echo mine > t.new && mv -f t.new t"
                assert runtime.exec(b, _sh(runtime.layout(b), swap)).code == 0
                assert _git_file(runtime, a, "t").out == b"mine\n"
    finally:
        runtime.remove_account(first.person)
        runtime.remove_account(other.person)


def a_git_file_is_a_name_in_the_directory_and_nothing_else(runtime, spec):
    who = spec(12)
    try:
        for name in ("../config", "a/b", "", ".", "credential.new", "other"):
            try:
                runtime.put_account(who.person, who.slot, name, b"x")
            except ValueError:
                continue
            raise AssertionError(f"{name!r} was written")
    finally:
        runtime.remove_account(who.person)


def an_exec_that_outlasts_its_timeout_fails(runtime, spec):
    with made(runtime, spec(13)) as name:
        layout = runtime.layout(name)
        began = time.monotonic()
        try:
            runtime.exec(name, _sh(layout, "exec sleep 30"), timeout=1.0)
        except rt.RuntimeFailed:
            pass
        else:
            raise AssertionError("an exec past its timeout answered")
        assert time.monotonic() - began < 10
        assert runtime.exec(name, _sh(layout, "echo after")).out == b"after\n"  # still usable


def the_timeout_tool_ends_a_program_that_outlasts_it_and_one_that_ignores_term(runtime, spec):
    with made(runtime, spec(13)) as name:
        layout = runtime.layout(name)
        timeout = [*layout.tool("timeout"), "-s", "TERM", "-k", "1"]
        # exec, so that nothing is left behind holding the exec's streams open
        ignoring = _sh(layout, "trap '' TERM; exec sleep 30")
        for argv in (_sh(layout, "exec sleep 30"), ignoring):
            began = time.monotonic()
            ended = runtime.exec(name, [*timeout, "1", *argv])
            assert time.monotonic() - began < 10, ended
            assert ended.code in TIMED_OUT, ended  # GNU timeout's and busybox's differ
        done = runtime.exec(name, [*timeout, "5", *_sh(layout, "exit 7")])
        assert done.code == 7  # a program that ends in time keeps its status
        missing = runtime.exec(name, [*timeout, "5", "no-such-program"])
        assert missing.code == 127, missing
        # A program named without a path is found on the PATH of the environment it is
        # given, as git is when a repository's verbs run it.
        listing = layout.with_env({"PATH": layout.path}, [*timeout, "5", "ls", "/"])
        assert runtime.exec(name, listing).code == 0


def an_exec_runs_in_the_folder_it_is_given(runtime, spec):
    with made(runtime, spec(11)) as name:
        layout = runtime.layout(name)
        pwd = _sh(layout, "pwd")
        asked = runtime.exec(name, pwd, cwd=layout.home)
        assert asked.out.decode().strip() == layout.home and asked.code == 0
        missing = runtime.exec(name, pwd, cwd=f"{layout.work}/no/such/folder")
        assert missing.code != 0


def an_exec_that_prints_a_great_deal_keeps_only_the_tail_it_is_asked_to(runtime, spec):
    with made(runtime, spec(14)) as name:
        layout = runtime.layout(name)
        script = "i=0; while [ $i -lt 3000 ]; do echo line-$i-xxxxxxxxxxxxxxxx; i=$((i+1)); done"
        both = _sh(layout, f"{script}; {script} >&2; exit 3")
        kept = runtime.exec(name, both, keep=500)
        assert kept.code == 3
        for stream in (kept.out, kept.err):
            assert len(stream) <= 500
            assert stream.endswith(b"line-2999-xxxxxxxxxxxxxxxx\n") and b"line-0-" not in stream
        whole = runtime.exec(name, both)  # the control: without keep, all of it
        assert len(whole.out) > 50_000 and whole.code == 3
        short = runtime.exec(name, _sh(layout, "echo small"), keep=500)
        assert short == rt.Result(0, b"small\n", b"")


def a_discarded_workspace_keeps_its_volume_and_is_made_again_on_it(runtime, spec):
    workspace = spec(12)
    try:
        runtime.create(workspace)
        layout = runtime.layout(workspace.name)
        written = runtime.exec(workspace.name, _sh(layout, f"echo kept > {layout.work}/file"))
        assert written.code == 0, written
        runtime.discard(workspace.name)
        assert not runtime.exists(workspace.name)
        assert workspace.name not in runtime.labelled()
        runtime.discard(workspace.name)  # nothing more to do the second time
        runtime.create(workspace)
        assert runtime.exists(workspace.name) and runtime.running(workspace.name)
        assert workspace.name in runtime.labelled()
        read = runtime.exec(workspace.name, [*layout.tool("cat"), f"{layout.work}/file"])
        assert read == rt.Result(0, b"kept\n", b"")
        runtime.remove(workspace.name)  # a remove takes the volume with it
        runtime.create(workspace)
        gone = runtime.exec(workspace.name, [*layout.tool("cat"), f"{layout.work}/file"])
        assert gone.code != 0
    finally:
        runtime.remove(workspace.name)
    assert not runtime.exists(workspace.name)


def a_volume_left_for_one_person_is_not_adopted_for_another(runtime, spec):
    workspace = spec(14)
    other = dataclasses.replace(workspace, person=workspace.person + 100, slot=workspace.slot)
    try:
        runtime.create(workspace)
        runtime.discard(workspace.name)  # its volume stays, labelled for its person
        with pytest.raises(rt.RuntimeFailed) as refused:
            runtime.create(other)
        assert "another person" in str(refused.value)
        assert not runtime.exists(workspace.name)  # nothing was made on it
        runtime.create(workspace)  # the person it belongs to gets it back
        assert runtime.exists(workspace.name)
    finally:
        runtime.remove(workspace.name)
    assert not runtime.exists(workspace.name)


def a_volume_of_another_install_is_not_adopted_by_the_same_person(runtime, spec):
    workspace = dataclasses.replace(spec(14), install="a" * 32)
    other = dataclasses.replace(workspace, install="b" * 32)
    try:
        runtime.create(workspace)
        runtime.discard(workspace.name)
        with pytest.raises(rt.RuntimeFailed):
            runtime.create(other)  # the same person, from a database that is not this one
        assert not runtime.exists(workspace.name)
        runtime.create(workspace)
        assert runtime.exists(workspace.name)
    finally:
        runtime.remove(workspace.name)


def collecting_images_never_takes_the_one_a_container_is_made_from(runtime, spec):
    workspace = spec(15)
    with made(runtime, workspace) as name:
        before = runtime.default_image_id()
        runtime.collect_images(set(), set())  # nothing is kept by anyone's word
        assert runtime.exists(name) and runtime.running(name)
        if before:  # a runtime with images: the one the container is made from is still there
            assert runtime.has_image(before)
    assert not runtime.exists(workspace.name)


def discarding_what_is_not_there_is_nothing(runtime, spec):
    workspace = spec(13)
    assert not runtime.exists(workspace.name)  # positive control for the absence below
    runtime.discard(workspace.name)
    assert not runtime.exists(workspace.name)


def _plant_build(runtime: rt.Runtime, workspace: int) -> None:
    """What a build in progress that rcman then lost leaves: a builder's volume carrying the
    label, and an image named for the workspace. A runtime is planted by its own means: the host
    runtime by what it keeps in memory, Podman by a labelled volume and a tag."""
    if isinstance(runtime, rt.HostRuntime):
        runtime.builder_ids.add(workspace)
        runtime.built[workspace] = rt.built_name(workspace)
        runtime.images[rt.built_name(workspace)] = rt.ImageFacts()
    else:
        runtime._podman(
            "volume",
            "create",
            f"--label={rt.LABEL_BUILDER}={workspace}",
            f"rcman-build-{workspace}",
        )
        runtime._podman("tag", runtime.config.image, rt.built_name(workspace))


def removing_a_build_that_is_not_there_is_nothing(runtime, spec):
    workspace = 9100
    assert workspace not in runtime.builders() and workspace not in runtime.built_images()
    runtime.remove_build(workspace)
    runtime.remove_build(workspace, image=True)
    assert workspace not in runtime.builders() and workspace not in runtime.built_images()


def a_build_that_is_there_is_listed_and_goes_with_or_without_its_image(runtime, spec):
    workspace = 9101
    _plant_build(runtime, workspace)
    try:
        assert workspace in runtime.builders()  # the positive control for the absences below
        assert runtime.built_images()[workspace] == rt.built_name(workspace)
        runtime.remove_build(workspace)  # the builder and its volume, not the image
        assert workspace not in runtime.builders()
        assert runtime.built_images()[workspace] == rt.built_name(workspace)
        runtime.remove_build(workspace)  # nothing more to do the second time
        assert workspace in runtime.built_images()
        runtime.remove_build(workspace, image=True)
        assert workspace not in runtime.built_images()
        runtime.remove_build(workspace, image=True)
        assert workspace not in runtime.builders() and workspace not in runtime.built_images()
    finally:
        runtime.remove_build(workspace, image=True)


def a_builder_and_an_image_are_removed_in_one_call_when_asked(runtime, spec):
    workspace = 9102
    _plant_build(runtime, workspace)
    try:
        assert workspace in runtime.builders() and workspace in runtime.built_images()
        runtime.remove_build(workspace, image=True)
        assert workspace not in runtime.builders() and workspace not in runtime.built_images()
    finally:
        runtime.remove_build(workspace, image=True)


def a_good_candidate_passes_the_canary(runtime, spec):
    """The candidate is a folder with the one file, which anyone may read and run; a good one
    fails no check. The canary container is no workspace, and nothing of it is left."""
    from helpers import claude_checks
    from tests import claude_download

    folder = Path(tempfile.mkdtemp(prefix="canary-candidate-"))
    try:
        claude_download.candidate(folder, "7.6.5")
        assert runtime.run_canary(folder, rt.CANARY_NAME, "7.6.5") == {}
        # positive control: the same call names the checks a candidate fails
        claude_download.candidate(folder, "7.6.5", kind="broken")
        failed = runtime.run_canary(folder, rt.CANARY_NAME, "7.6.5")
        assert set(failed) == {name for name, _ in claude_checks.CHECKS}
        assert rt.CANARY_NAME not in runtime.labelled()
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def a_broken_candidate_names_the_checks_it_fails_with_their_reports(runtime, spec):
    from tests import claude_download

    folder = Path(tempfile.mkdtemp(prefix="canary-candidate-"))
    try:
        claude_download.candidate(folder, "7.6.5", kind="broken")
        failed = runtime.run_canary(folder, rt.CANARY_NAME, "7.6.5")
        assert failed and all(set(entry) == {"lines", "more"} for entry in failed.values())
        assert all(entry["lines"] for entry in failed.values())
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def ending_a_canary_that_is_not_there_is_nothing(runtime, spec):
    runtime.end_canary(rt.CANARY_NAME)
    runtime.end_canary(rt.CANARY_NAME)


def _read_until(master: int, pattern: bytes, timeout: float = SETTLES_WITHIN) -> re.Match:
    """Read the terminal ``master`` until ``pattern`` is in what it printed."""
    seen, deadline = b"", time.monotonic() + timeout
    while not (found := re.search(pattern, seen)):
        left = deadline - time.monotonic()
        assert left > 0, seen[-2000:]
        if select.select([master], [], [], left)[0]:
            try:
                seen += os.read(master, 65536)
            except OSError:  # every writer has closed it
                break
    assert found, seen[-2000:]
    return found


def a_terminal_runs_a_shell_on_its_pty_as_the_user_and_writes_its_pidfile(runtime, spec):
    """``terminal_argv``, run on a pseudo-terminal as the Terminal tab runs it: an interactive
    shell in the work folder, with the size of the pty, the workspace's environment, SHELL the
    shell that runs, and the pidfile naming that shell, the leader of its own session, by its
    id and start time as the workspace sees them."""
    with made(runtime, spec(5)) as name:
        layout = runtime.layout(name)
        folder = f"{layout.run}/{rt.TERMINALS}"
        assert runtime.exec(name, [*layout.tool("mkdir"), "-p", folder]).code == 0
        pidfile = f"{folder}/0123456789abcdef.pid"
        launch = runtime.terminal_argv(name, layout, pidfile)
        master, slave = os.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 33, 111, 0, 0))
        proc = subprocess.Popen(
            launch.argv, stdin=slave, stdout=slave, stderr=slave, env=launch.env
        )
        os.close(slave)
        try:
            os.write(master, b"echo at-$((40+2))-$$-$(stty size)-$(pwd)\r")
            found = _read_until(master, rb"at-42-(\d+)-(\d+ \d+)-(\S+)\r\n")
            shell, size, folder_ = int(found[1]), found[2].decode(), found[3].decode()
            assert (size, folder_) == ("33 111", layout.work)
            os.write(
                master, b'echo "home=$HOME" "term=$TERM" "same=$([ "$0" = "$SHELL" ] && echo y)"\r'
            )
            _read_until(
                master, rf"home={re.escape(layout.home)} term=xterm-256color same=y".encode()
            )
            written = runtime.exec(name, [*layout.tool("cat"), pidfile]).out.decode()
            pid, start = written.split()
            assert written == f"{pid} {start}\n" and int(pid) == shell
            stat = runtime.exec(name, [*layout.tool("cat"), f"/proc/{shell}/stat"]).out.decode()
            fields = stat.rpartition(")")[2].split()
            assert (int(fields[3]), fields[19]) == (shell, start)  # its session's leader
            os.write(master, b"exit 5\r")
            assert proc.wait(timeout=SETTLES_WITHIN) == 5
        finally:
            os.close(master)
            if proc.poll() is None:
                proc.kill()
            proc.wait()


CASES = [
    a_workspace_that_is_made_exists_runs_and_is_labelled,
    removing_what_is_not_there_is_nothing,
    an_exec_gets_its_input_and_answers_its_streams_and_code,
    a_channel_runs_in_the_workspace,
    a_tmux_server_started_through_a_channel_outlives_it_and_not_a_stop,
    a_workspaces_files_outlive_a_stop,
    a_whole_process_group_is_ended_by_kill_group_of,
    a_persons_workspaces_share_one_credentials_file_written_in_place,
    a_sign_in_container_has_the_persons_file_and_is_no_workspace,
    a_persons_workspaces_share_one_git_directory_written_by_rename,
    a_git_file_is_a_name_in_the_directory_and_nothing_else,
    an_exec_that_outlasts_its_timeout_fails,
    the_timeout_tool_ends_a_program_that_outlasts_it_and_one_that_ignores_term,
    an_exec_runs_in_the_folder_it_is_given,
    a_discarded_workspace_keeps_its_volume_and_is_made_again_on_it,
    a_volume_left_for_one_person_is_not_adopted_for_another,
    a_volume_of_another_install_is_not_adopted_by_the_same_person,
    collecting_images_never_takes_the_one_a_container_is_made_from,
    discarding_what_is_not_there_is_nothing,
    an_exec_that_prints_a_great_deal_keeps_only_the_tail_it_is_asked_to,
    removing_a_build_that_is_not_there_is_nothing,
    a_build_that_is_there_is_listed_and_goes_with_or_without_its_image,
    a_builder_and_an_image_are_removed_in_one_call_when_asked,
    ending_a_canary_that_is_not_there_is_nothing,
    a_terminal_runs_a_shell_on_its_pty_as_the_user_and_writes_its_pidfile,
]
# The cases that run the canary's program in the runtime's own image, which therefore needs
# python3, as the default workspace image has it: the podman host's base image has none.
CANARY_CASES = [
    a_good_candidate_passes_the_canary,
    a_broken_candidate_names_the_checks_it_fails_with_their_reports,
]
