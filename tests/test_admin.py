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


def run(env_file, *argv, runtime=None, capsys=None) -> tuple[int, str, str]:
    code = admin.main(["--env-file", str(env_file), *argv], runtime=runtime)
    out = capsys.readouterr() if capsys else None
    return code, out.out if out else "", out.err if out else ""


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


def test_an_address_taken_or_invalid_is_refused_in_words(env_file, capsys):
    run(env_file, "add", ALICE, capsys=capsys)
    code, _, err = run(env_file, "add", ALICE.upper(), capsys=capsys)
    assert code == 1 and "Someone with that address is there already." in err
    code, _, err = run(env_file, "add", "--", "-x@example.com", capsys=capsys)
    assert code == 1 and "That is not a valid email address." in err


def test_delete_removes_the_person_and_their_workspaces(env_file, tmp_path, capsys):
    from tests.conftest import host_runtime

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
    with host_runtime(tmp_path) as runtime:
        runtime.create(rt.Spec(rt.container_name(workspace), bob.slot, workspace, bob.id))
        code, out, _ = run(env_file, "delete", BOB, runtime=runtime, capsys=capsys)
        assert code == 0 and BOB in out
        assert runtime.labelled() == {}
    con = database(env_file)
    assert people.by_email(con, BOB) is None and people.by_email(con, ALICE) is not None
    con.close()
    assert (env_file.parent / "allowed-emails").read_text() == f"{ALICE}\n"


def test_deleting_someone_who_is_not_there_is_refused(env_file, capsys):
    code, _, err = run(env_file, "delete", BOB, capsys=capsys)
    assert code == 1 and "There is no such person." in err


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
    for verb in ("add", "delete", "list"):
        assert verb in found.stdout
