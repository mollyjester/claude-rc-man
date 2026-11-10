"""rcman-admin: people from a shell (backend/rcman/admin.py), the same code as the People
page, run as rcman."""

import os
import subprocess
import sys

import pytest
from rcman import admin, db, people
from rcman import runtime as rt

from tests.conftest import ALICE, BOB, ORIGIN


@pytest.fixture
def env_file(tmp_path):
    """The settings rcman-admin reads, as the installer writes them for the service."""
    path = tmp_path / "rcman.env"
    path.write_text(
        f"RCMAN_PUBLIC_ORIGIN={ORIGIN}\n"
        f"RCMAN_DATABASE={tmp_path / 'rcman.db'}\n"
        f"RCMAN_ALLOWED_EMAILS={tmp_path / 'allowed-emails'}\n"
    )
    return path


def run(env_file, *argv, capsys, runtime=None) -> tuple[int, str, str]:
    code = admin.main(["--env-file", str(env_file), *argv], runtime=runtime)
    out = capsys.readouterr()
    return code, out.out, out.err


def database(env_file):
    return db.connect(env_file.parent / "rcman.db")


def test_add_list_and_the_allowed_list(env_file, capsys):
    code, out, _ = run(env_file, "add", ALICE, "--admin", capsys=capsys)
    assert code == 0 and ALICE in out
    assert run(env_file, "add", "Bob@Example.com", capsys=capsys)[0] == 0
    code, out, _ = run(env_file, "list", capsys=capsys)
    assert code == 0
    assert out.splitlines() == [f"{ALICE}  administrator  0 workspaces", f"{BOB}  0 workspaces"]
    assert (env_file.parent / "allowed-emails").read_text() == f"{ALICE}\n{BOB}\n"


@pytest.fixture
def runtime(tmp_path):
    from tests.conftest import host_runtime

    with host_runtime(tmp_path) as made:
        yield made


@pytest.fixture
def two(env_file, capsys):
    """Alice and Bob added, and a workspace of Bob's."""
    run(env_file, "add", ALICE, "--admin", capsys=capsys)
    run(env_file, "add", BOB, capsys=capsys)
    con = database(env_file)
    bob = people.by_email(con, BOB)
    con.execute(
        "insert into workspaces (person, name, state) values (?, 'his', 'ready')", (bob.id,)
    )
    con.commit()
    workspace = con.execute("select id from workspaces").fetchone()[0]
    con.close()
    return bob, workspace


def test_delete_removes_the_person_and_their_workspaces(env_file, two, runtime, capsys):
    bob, workspace = two
    runtime.create(rt.Spec(rt.container_name(workspace), bob.slot, workspace, bob.id))
    code, out, _ = run(env_file, "delete", BOB, runtime=runtime, capsys=capsys)
    assert code == 0 and BOB in out
    assert runtime.labelled() == {}
    con = database(env_file)
    assert people.by_email(con, BOB) is None and people.by_email(con, ALICE) is not None
    con.close()
    assert (env_file.parent / "allowed-emails").read_text() == f"{ALICE}\n"


def test_run_as_root_it_runs_again_as_rcman(env_file, monkeypatch):
    """With rcman's own runtime folder, whatever root's shell had, so that it reaches
    rcman's Podman socket."""
    import pwd
    from types import SimpleNamespace

    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/0")
    account = SimpleNamespace(pw_uid=990, pw_dir="/var/lib/rcman")
    monkeypatch.setattr(pwd, "getpwnam", lambda name: account)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    ran = []

    def execvp(program, argv):
        ran.append(argv)
        raise SystemExit(0)

    monkeypatch.setattr(os, "execvp", execvp)
    with pytest.raises(SystemExit):
        admin.main(["--env-file", str(env_file), "list"])
    assert ran == [
        ["runuser", "-u", admin.ACCOUNT, "--", "/usr/bin/env", "XDG_RUNTIME_DIR=/run/user/990",
         "HOME=/var/lib/rcman", sys.executable, "-m", "rcman.admin",
         "--env-file", str(env_file), "list"]
    ]  # fmt: skip


def test_the_command_is_installed_and_answers_help():
    found = subprocess.run(
        [os.path.join(os.path.dirname(sys.executable), "rcman-admin"), "--help"],
        capture_output=True,
        text=True,
    )
    assert found.returncode == 0, found.stderr
    for verb in ("add", "delete", "list", "limits"):
        assert verb in found.stdout


# --- limits ------------------------------------------------------------------------------


def stored_limits(env_file, email=BOB):
    con = database(env_file)
    try:
        return people.by_email(con, email).limits
    finally:
        con.close()


def test_limits_with_no_option_prints_what_is_set_and_what_is_in_use(
    env_file, two, runtime, capsys
):
    from rcman import limits

    bob, _ = two
    code, out, _ = run(env_file, "limits", BOB, runtime=runtime, capsys=capsys)
    assert code == 0
    assert out.splitlines() == [
        f"Limits of {BOB}: memory=none cpu=none pids=none",
        "In use: memory=0M tasks=0",
    ]
    runtime.used[limits.person_slice(bob.id)] = limits.Usage(1500 * 1024**2, 12)
    run(env_file, "limits", BOB, "--memory", "2G", "--cpu", "1.5", runtime=runtime, capsys=capsys)
    code, out, _ = run(env_file, "limits", BOB, runtime=runtime, capsys=capsys)
    assert out.splitlines() == [
        f"Limits of {BOB}: memory=2G cpu=1.5 pids=none",
        "In use: memory=1500M tasks=12",
    ]


def test_limits_sets_only_the_options_given_and_applies_them(env_file, two, runtime, capsys):
    from rcman import limits

    bob, _ = two
    code, out, _ = run(env_file, "limits", BOB, "--memory", "2G", runtime=runtime, capsys=capsys)
    assert code == 0 and out.splitlines() == [
        f"Set the limits of {BOB}: memory=2G cpu=none pids=none"
    ]
    code, out, _ = run(env_file, "limits", BOB, "--pids", "500", runtime=runtime, capsys=capsys)
    assert out.splitlines() == [f"Set the limits of {BOB}: memory=2G cpu=none pids=500"]
    assert stored_limits(env_file) == limits.Limits(memory_mib=2048, pids=500)
    assert runtime.limits == {limits.person_slice(bob.id): limits.Limits(memory_mib=2048, pids=500)}
    assert stored_limits(env_file, ALICE) == limits.Limits()


def test_a_workspaces_limits_are_set_and_shown_by_its_name(env_file, two, runtime, capsys):
    from rcman import limits

    bob, workspace = two
    code, out, _ = run(
        env_file,
        "limits",
        BOB,
        "--workspace",
        "his",
        "--pids",
        "70",
        runtime=runtime,
        capsys=capsys,
    )
    assert code == 0 and out.splitlines() == [
        f"Set the limits of {BOB}/his: memory=none cpu=none pids=70"
    ]
    assert runtime.limits == {limits.workspace_slice(bob.id, workspace): limits.Limits(pids=70)}
    assert stored_limits(env_file) == limits.Limits()  # the person's own are not touched
    code, out, _ = run(
        env_file, "limits", BOB, "--workspace", "his", runtime=runtime, capsys=capsys
    )
    assert out.splitlines()[0] == f"Limits of {BOB}/his: memory=none cpu=none pids=70"
