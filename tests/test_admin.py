"""rcman-admin: people from a shell (backend/rcman/admin.py), the same code as the People
page, run as rcman."""

import os
import subprocess
import sys

import pytest
from rcman import admin, db, people
from rcman import runtime as rt

from helpers import rules
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


def test_a_deletion_from_the_shell_leaves_the_store_to_the_service_to_prune(
    env_file, tmp_path, capsys, monkeypatch
):
    """The shell's process has locks of its own: the service pins a version of Claude Code
    between its resolving and its recording under one, which a prune here would not wait for.
    The service prunes at its next stop, roll-out or start."""
    from rcman import claude_versions
    from rcman.claude_versions import ClaudeVersions

    from tests.conftest import fake_claude, host_runtime

    monkeypatch.setattr(claude_versions, "PRUNE_GRACE_SECONDS", 0.0)  # the files are new
    store = tmp_path / "claude-store"
    env_file.write_text(env_file.read_text() + f"RCMAN_CLAUDE_STORE={store}\n")
    fake_claude(store, version="1.0.0")
    fake_claude(store, version="1.1.0")
    fake_claude(store, version="1.0.0")  # the link names this one
    run(env_file, "add", ALICE, "--admin", capsys=capsys)
    run(env_file, "add", BOB, capsys=capsys)
    with host_runtime(tmp_path) as runtime:
        code, _, _ = run(env_file, "delete", BOB, runtime=runtime, capsys=capsys)
    assert code == 0
    assert sorted(p.name for p in (store / "versions").iterdir()) == ["1.0.0", "1.1.0"]
    # the control: the same store is pruned by a service that prunes
    from rcman.settings import Settings
    from rcman.workspaces import Workspaces

    (tmp_path / "again").mkdir()
    with host_runtime(tmp_path / "again") as runtime:
        service = Workspaces(Settings(_env_file=env_file), runtime)
        try:
            service.versions.prune()
        finally:
            service.close()
    assert sorted(p.name for p in (store / "versions").iterdir()) == ["1.0.0"]
    assert ClaudeVersions  # (the class the service prunes with)


def bobs_workspace(env_file, state: str) -> int:
    con = database(env_file)
    try:
        bob = people.by_email(con, BOB)
        cursor = con.execute(
            "insert into workspaces (person, name, state) values (?, 'his', ?)", (bob.id, state)
        )
        con.commit()
        return cursor.lastrowid
    finally:
        con.close()


def state_of(env_file, workspace: int) -> str | None:
    con = database(env_file)
    try:
        found = con.execute("select state from workspaces where id = ?", (workspace,)).fetchone()
        return found[0] if found else None
    finally:
        con.close()


def test_delete_waits_for_a_workspace_the_service_is_making_and_succeeds(
    env_file, tmp_path, capsys, monkeypatch
):
    """rcman-admin has none of the service's threads: the service, which makes the workspace,
    finds it deleting once the step it is on ends, and removes it."""
    import threading
    import time

    from rcman import workspaces as ws
    from rcman.settings import Settings

    from tests.conftest import MADE_WITHIN, host_runtime

    monkeypatch.setattr(ws, "WORKING_POLL_SECONDS", 0.02)
    run(env_file, "add", ALICE, "--admin", capsys=capsys)
    run(env_file, "add", BOB, capsys=capsys)
    con = database(env_file)
    bob = people.by_email(con, BOB)
    con.close()
    with host_runtime(tmp_path) as runtime:
        service = ws.Workspaces(Settings(_env_file=env_file), runtime)
        release, real = threading.Event(), runtime.create
        runtime.create = lambda spec: (release.wait(MADE_WITHIN), real(spec))
        try:
            made = service.create(bob, "his")

            def let_go():  # once rcman-admin has marked it deleting, the making goes on
                deadline = time.monotonic() + MADE_WITHIN
                while state_of(env_file, made.id) != "deleting":
                    if time.monotonic() > deadline:
                        return
                    time.sleep(0.01)
                release.set()

            threading.Thread(target=let_go, daemon=True).start()
            code, out, err = run(env_file, "delete", BOB, runtime=runtime, capsys=capsys)
        finally:
            release.set()
            service.close()
        assert (code, err) == (0, "") and BOB in out
        assert runtime.labelled() == {}
    assert state_of(env_file, made.id) is None
    con = database(env_file)
    assert people.by_email(con, BOB) is None and people.by_email(con, ALICE) is not None
    con.close()
    assert (env_file.parent / "allowed-emails").read_text() == f"{ALICE}\n"


def test_delete_with_the_service_down_leaves_a_workspace_being_made_deleting_for_restore(
    env_file, tmp_path, capsys, monkeypatch
):
    from rcman import problems
    from rcman import workspaces as ws
    from rcman.settings import Settings

    from tests.conftest import host_runtime

    monkeypatch.setattr(ws, "WORKING_POLL_SECONDS", 0.02)
    monkeypatch.setattr(ws, "MADE_WITHIN_SECONDS", 0.2)  # nothing makes it: the service is down
    run(env_file, "add", ALICE, "--admin", capsys=capsys)
    run(env_file, "add", BOB, capsys=capsys)
    workspace = bobs_workspace(env_file, "creating")
    con = database(env_file)
    bob = people.by_email(con, BOB)
    con.close()
    with host_runtime(tmp_path) as runtime:
        name = rt.container_name(workspace)
        runtime.create(rt.Spec(name, bob.slot, workspace, bob.id))  # what the service had made
        code, _, err = run(env_file, "delete", BOB, runtime=runtime, capsys=capsys)
        assert code == 1 and problems.PERSON_DELETION_LEFT in err
        assert state_of(env_file, workspace) == "deleting"  # left for restore to finish
        con = database(env_file)
        assert people.by_email(con, BOB).state == people.DELETING  # let in nowhere meanwhile
        con.close()
        assert BOB not in (env_file.parent / "allowed-emails").read_text()
        assert runtime.exists(name)  # positive control for what restore removes
        service = ws.Workspaces(Settings(_env_file=env_file), runtime)  # the service starts
        try:
            service.restore()
        finally:
            service.close()
        assert runtime.labelled() == {}
    assert state_of(env_file, workspace) is None
    con = database(env_file)
    assert people.by_email(con, BOB) is None
    con.close()


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
    for verb in ("add", "delete", "list", "limits"):
        assert verb in found.stdout


# --- limits ------------------------------------------------------------------------------


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


def test_none_takes_a_limit_away_and_the_others_stay(env_file, two, runtime, capsys):
    from rcman import limits

    run(env_file, "limits", BOB, "--memory", "2G", "--cpu", "2", runtime=runtime, capsys=capsys)
    code, out, _ = run(env_file, "limits", BOB, "--memory", "none", runtime=runtime, capsys=capsys)
    assert code == 0 and "memory=none cpu=2 pids=none" in out
    assert stored_limits(env_file) == limits.Limits(cpu_percent=200)
    assert next(iter(runtime.limits.values())) == limits.Limits(cpu_percent=200)


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


def test_limits_of_no_one_and_of_no_such_workspace_are_refused_in_words(
    env_file, two, runtime, capsys
):
    code, _, err = run(env_file, "limits", "nobody@example.com", runtime=runtime, capsys=capsys)
    assert code == 1 and "There is no such person." in err
    args = (env_file, "limits", BOB, "--workspace", "nothing", "--pids", "70")
    code, _, err = run(*args, runtime=runtime, capsys=capsys)
    assert code == 1 and "There is no such workspace." in err
    assert runtime.limits == {}


@pytest.mark.parametrize(
    ("option", "value", "rule"),
    [
        ("--memory", "lots", rules.MEMORY),
        ("--memory", "1M", rules.MEMORY),
        ("--cpu", "1.234", rules.CPU),
        ("--cpu", "0", rules.CPU),
        ("--pids", "x", rules.PIDS),
        ("--pids", "1", rules.PIDS),
    ],
)
def test_a_value_the_rules_refuse_is_refused_in_the_rules_words_and_nothing_is_set(
    env_file, two, runtime, capsys, option, value, rule
):
    code, _, err = run(env_file, "limits", BOB, option, value, runtime=runtime, capsys=capsys)
    assert code == 1 and rule.sentence in err
    assert stored_limits(env_file).limited is False and runtime.limits == {}


def test_a_limit_below_what_is_in_use_is_refused_in_words_and_nothing_is_set(
    env_file, two, runtime, capsys
):
    from rcman import limits

    bob, _ = two
    runtime.used[limits.person_slice(bob.id)] = limits.Usage(3000 * 1024**2, 7)
    code, _, err = run(env_file, "limits", BOB, "--memory", "2G", runtime=runtime, capsys=capsys)
    assert code == 1 and "3000M" in err and "in use" in err
    assert stored_limits(env_file) == limits.Limits() and runtime.limits == {}


def test_limits_that_cannot_be_applied_are_refused_in_words_and_nothing_is_stored(
    env_file, two, runtime, capsys
):
    runtime.limits_error = rt.RuntimeFailed("systemctl refused")
    code, _, err = run(env_file, "limits", BOB, "--memory", "2G", runtime=runtime, capsys=capsys)
    assert code == 1 and "could not apply" in err
    assert stored_limits(env_file).limited is False


def test_limits_leave_no_audit_row_as_adding_and_deleting_leave_none(
    env_file, two, runtime, capsys
):
    run(env_file, "limits", BOB, "--memory", "2G", runtime=runtime, capsys=capsys)
    con = database(env_file)
    try:
        assert con.execute("select count(*) from audit").fetchone()[0] == 0
    finally:
        con.close()


def test_list_shows_the_limits_of_those_who_have_some(env_file, two, runtime, capsys):
    run(env_file, "limits", BOB, "--memory", "2G", "--pids", "500", runtime=runtime, capsys=capsys)
    code, out, _ = run(env_file, "list", capsys=capsys)
    assert code == 0 and out.splitlines() == [
        f"{ALICE}  administrator  0 workspaces",
        f"{BOB}  1 workspaces  memory=2G cpu=none pids=500",
    ]


def test_every_option_of_limits_is_in_its_help(capsys):
    parser = admin._parser()
    limits_parser = next(
        action.choices["limits"]
        for action in parser._actions
        if isinstance(action.choices, dict) and "limits" in action.choices
    )
    options = {o for a in limits_parser._actions for o in a.option_strings if o.startswith("--")}
    assert {"--workspace", "--memory", "--cpu", "--pids"} <= options  # non-vacuity
    helped = limits_parser.format_help()
    for option in options:
        assert option in helped
    assert "none" in helped  # the word for no limit


def test_rcman_admin_runs_podman_with_the_products_range_network_and_slices(
    env_file, two, runtime, capsys, monkeypatch
):
    """rcman-admin makes its runtime as the service does (main.podman_runtime), with none of
    the sizes the tests inject; the host runtime stands in for it once it is made."""
    from rcman import main

    made = []

    def recorded(settings):
        made.append(main_podman_runtime(settings))
        return runtime

    main_podman_runtime = main.podman_runtime
    monkeypatch.setattr(main, "podman_runtime", recorded)
    code, _, _ = run(env_file, "limits", BOB, capsys=capsys)
    assert code == 0 and len(made) == 1  # it was asked for, once
    config = made[0].config
    assert (config.range, config.network, config.slices) == (rt.RANGE, rt.NETWORK, True)


def test_rcman_admin_makes_its_service_with_the_products_builder_slot(
    env_file, two, runtime, capsys, monkeypatch
):
    from rcman import workspaces

    made = []

    class Recorded(workspaces.Workspaces):
        def __init__(self, *args, **more):
            super().__init__(*args, **more)
            made.append(self)

    monkeypatch.setattr(workspaces, "Workspaces", Recorded)
    code, _, _ = run(env_file, "limits", BOB, runtime=runtime, capsys=capsys)
    assert code == 0 and len(made) == 1  # it was made, once
    assert made[0].builder_slot == people.BUILDER_SLOT
