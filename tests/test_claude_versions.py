"""Claude Code versions: the approved one, rolling out another through the canary, and what
is pruned (backend/rcman/claude_versions.py), on the host runtime. The routes are
tests/test_claude.py's, a server's pinning tests/test_workspaces.py's."""

import hashlib
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import pytest
from rcman import claude_versions as cv
from rcman import db
from rcman import workspaces as ws
from rcman.problems import CLAUDE_INSTALL_MESSAGES, MESSAGES, Problem

from helpers import claude_checks, claude_release
from tests import claude_download
from tests.conftest import ALICE, BOB, FAKE_VERSION, fake_claude

NEW = "2.0.0"


@dataclass
class World:
    versions: cv.ClaudeVersions
    service: ws.Workspaces
    runtime: object
    settings: object
    releases: Path
    store: Path


@pytest.fixture
def world(tmp_path, settings, runtime, monkeypatch):
    monkeypatch.setattr(cv, "PRUNE_GRACE_SECONDS", 0.0)  # tests that prune age nothing
    releases = tmp_path / "releases"
    releases.mkdir()
    with claude_download.served(releases) as url:
        settings.claude_download = url
        service = ws.Workspaces(settings, runtime)
        service.versions.reconcile()
        try:
            yield World(
                service.versions, service, runtime, settings, releases, settings.claude_store
            )
        finally:
            service.close()


def approvals(world) -> list[tuple[str, str]]:
    con = db.connect(world.settings.database)
    try:
        rows = con.execute("select version, by from claude_approvals order by id").fetchall()
    finally:
        con.close()
    return [(row[0], row[1]) for row in rows]


def approve_rows(world, *versions, by="someone") -> None:
    """Approvals as earlier rollouts made them."""
    con = db.connect(world.settings.database)
    try:
        for version in versions:
            con.execute(
                "insert into claude_approvals (version, at, by) values (?, ?, ?)",
                (version, "2026-01-01T00:00:00+00:00", by),
            )
        con.commit()
    finally:
        con.close()


def snapshot(root: Path) -> dict:
    """Everything under ``root`` - its paths, kinds, modes, links and content hashes."""
    found = {}
    for path in sorted([root, *root.rglob("*")]):
        name = str(path.relative_to(root))
        status = path.lstat()
        if path.is_symlink():
            found[name] = ("link", os.readlink(path))
        elif path.is_dir():
            found[name] = ("dir", status.st_mode & 0o7777)
        else:
            found[name] = (
                "file",
                status.st_mode & 0o7777,
                hashlib.sha256(path.read_bytes()).hexdigest(),
            )
    return found


def staging(world) -> Path:
    return world.settings.database.parent / cv.STAGING_FOLDER


def staged(world) -> list[str]:
    folder = staging(world)
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def link(world) -> str:
    return os.readlink(world.store / "current")


def install(world, *versions) -> None:
    """Versions in the store, as earlier rollouts left them."""
    for version in versions:
        fake_claude(world.store, version=version)
    fake_claude(world.store, version=FAKE_VERSION)  # current is the adopted one again


def ready_workspace(world, person=1, name="mine"):
    """A ready workspace of the person's, made and signed in to Claude, with no server."""
    from rcman import runtime as rt

    from devtools import fake_claude as fake
    from tests.e2e_server import record_sign_in

    con = db.connect(world.settings.database)
    try:
        found = con.execute(
            "insert into workspaces (person, name, state) values (?, ?, 'ready')", (person, name)
        )
        con.commit()
        workspace_id = found.lastrowid
    finally:
        con.close()
    world.runtime.create(rt.Spec(rt.container_name(workspace_id), person, workspace_id, person))
    layout = world.runtime.layout(rt.container_name(workspace_id))
    fake.write_state(Path(layout.config), logged_in=True, trusted=[layout.work])
    record_sign_in(world.settings.database, person)
    return world.service.get(workspace_id)


def refusal(call, *args, **kwargs) -> Problem:
    with pytest.raises(Problem) as raised:
        call(*args, **kwargs)
    return raised.value


def canary_says(world, failures=None):
    """The runtime's canary says ``failures`` and records what it was given and what the
    staging folder held while it ran."""
    seen = []

    def script(directory, name, version):
        seen.append(
            {
                "name": name,
                "version": version,
                "folder": directory,
                "files": sorted(p.name for p in directory.iterdir()),
                "staged": staged(world),
            }
        )
        return failures or {}

    world.runtime.canary_script = script
    return seen


# --- adopting what the installer left, and repairing the link ----------------------------


def test_the_first_approval_is_the_version_current_names(world):
    assert approvals(world) == [(FAKE_VERSION, cv.ADOPTED_BY)]
    assert world.versions.approved() == FAKE_VERSION
    assert world.versions.previous() is None


def test_reconciling_again_adds_nothing(world):
    world.versions.reconcile()
    world.versions.reconcile()
    assert approvals(world) == [(FAKE_VERSION, cv.ADOPTED_BY)]


def test_the_database_wins_over_the_link_current(world):
    install(world, "1.5.0")
    approve_rows(world, "1.5.0")
    assert link(world) == f"versions/{FAKE_VERSION}"  # positive control: it names the old one
    world.versions.reconcile()
    assert world.versions.approved() == "1.5.0"
    assert link(world) == "versions/1.5.0"
    assert (world.store / "current").resolve() == world.store / "versions" / "1.5.0"


def test_a_link_that_dangles_is_repaired_to_the_approved_version(world):
    (world.store / "current").unlink()
    (world.store / "current").symlink_to("versions/9.9.9")
    world.versions.reconcile()
    assert link(world) == f"versions/{FAKE_VERSION}"


def test_an_approved_version_that_is_not_in_the_store_leaves_the_link_alone(world):
    approve_rows(world, "8.8.8")
    world.versions.reconcile()
    assert link(world) == f"versions/{FAKE_VERSION}"
    assert refusal(world.versions.resolve).code == "claude_not_installed"


def test_nothing_to_adopt_is_not_installed(tmp_path, settings, runtime):
    (settings.claude_store / "current").unlink()
    service = ws.Workspaces(settings, runtime)
    try:
        service.versions.reconcile()
        assert service.versions.approved() is None
        assert refusal(service.versions.resolve).code == "claude_not_installed"
    finally:
        service.close()


def test_resolving_without_a_reconcile_adopts_current(tmp_path, settings, runtime):
    service = ws.Workspaces(settings, runtime)  # no reconcile
    try:
        assert service.versions.resolve() == FAKE_VERSION
        assert service.versions.approved() == FAKE_VERSION
    finally:
        service.close()


def test_a_version_resolves_only_while_its_file_is_in_the_store(world):
    assert world.versions.resolve() == FAKE_VERSION
    (world.store / "versions" / FAKE_VERSION).unlink()
    assert refusal(world.versions.resolve).code == "claude_not_installed"


def test_the_previous_version_is_the_one_approved_before_this_one(world):
    approve_rows(world, "1.1.0", "1.2.0")
    assert (world.versions.approved(), world.versions.previous()) == ("1.2.0", "1.1.0")
    approve_rows(world, "1.1.0")  # a rollback is a new row
    assert (world.versions.approved(), world.versions.previous()) == ("1.1.0", "1.2.0")


def test_approvals_are_never_changed_or_deleted(world):
    con = db.connect(world.settings.database)
    try:
        for statement in (
            "update claude_approvals set version = '0.0.0'",
            "delete from claude_approvals",
        ):
            with pytest.raises(Exception, match="append-only"):
                con.execute(statement)
    finally:
        con.close()
    assert approvals(world) == [(FAKE_VERSION, cv.ADOPTED_BY)]


# --- rolling out a version -----------------------------------------------------------------


def test_a_good_version_is_installed_approved_and_switched_to(world):
    claude_download.release(world.releases, NEW)
    before = snapshot(world.store)
    report = world.versions.upgrade(NEW, ALICE)
    after = snapshot(world.store)
    assert after != before  # positive control for the unchanged stores below
    installed = world.store / "versions" / NEW
    assert installed.is_file() and installed.stat().st_mode & 0o777 == 0o755
    assert (
        installed.read_bytes()
        == (world.releases / NEW / claude_release.platform_key() / "claude").read_bytes()
    )
    assert link(world) == f"versions/{NEW}"
    assert approvals(world) == [(FAKE_VERSION, cv.ADOPTED_BY), (NEW, ALICE)]
    assert (world.versions.approved(), world.versions.previous()) == (NEW, FAKE_VERSION)
    assert (report["approved"], report["previous"]) == (NEW, FAKE_VERSION)
    assert staged(world) == []
    assert sorted(set(after) - set(before)) == [f"versions/{NEW}"]  # nothing else, no partial file


def test_a_broken_version_changes_nothing_and_names_the_checks_it_fails(world):
    claude_download.release(world.releases, NEW, kind="broken")
    before = snapshot(world.store)
    problem = refusal(world.versions.upgrade, NEW, ALICE)
    assert (problem.code, problem.status) == ("claude_checks_failed", 409)
    assert problem.extra["failed"] == sorted(name for name, _ in claude_checks.CHECKS)
    assert set(problem.extra["report"]) == set(problem.extra["failed"])
    assert problem.title == MESSAGES["claude_checks_failed"]
    assert snapshot(world.store) == before
    assert approvals(world) == [(FAKE_VERSION, cv.ADOPTED_BY)]
    assert staged(world) == []


def test_a_release_whose_checksum_fails_is_not_run_and_changes_nothing(world):
    claude_download.release(world.releases, NEW, kind="failing")
    seen = canary_says(world)
    before = snapshot(world.store)
    problem = refusal(world.versions.upgrade, NEW, ALICE)
    assert (problem.code, problem.extra["kind"]) == ("claude_install_failed", "checksum")
    assert problem.title == CLAUDE_INSTALL_MESSAGES["checksum"]
    assert seen == []  # the canary never saw it
    assert snapshot(world.store) == before
    assert staged(world) == []


def test_a_failure_to_get_a_version_says_why_in_plain_words(world, tmp_path):
    claude_download.release(world.releases, "3.0.0", platform="plan9-mips")
    cases = {"no_such_version": "9.9.9", "no_build": "3.0.0"}
    before = snapshot(world.store)
    for kind, version in cases.items():
        problem = refusal(world.versions.upgrade, version, ALICE)
        assert (problem.code, problem.extra["kind"]) == ("claude_install_failed", kind)
        assert problem.title == CLAUDE_INSTALL_MESSAGES[kind]
    with claude_download.served(world.releases) as gone:
        pass
    world.settings.claude_download = gone  # nothing answers there any more
    problem = refusal(world.versions.upgrade, NEW, ALICE)
    assert (problem.code, problem.extra["kind"]) == ("claude_install_failed", "network")
    assert snapshot(world.store) == before
    assert staged(world) == []
    assert len(set(CLAUDE_INSTALL_MESSAGES.values())) == len(CLAUDE_INSTALL_MESSAGES)


def test_the_canary_is_given_the_candidate_alone_in_a_staging_folder_outside_the_store(world):
    claude_download.release(world.releases, NEW)
    seen = canary_says(world)
    world.versions.upgrade(NEW, ALICE)
    [one] = seen
    assert one["name"] == cv.CANARY_NAME and one["version"] == NEW
    assert one["files"] == [cv.CANDIDATE]  # the candidate, and nothing else
    assert one["folder"].parent == staging(world)
    assert world.store not in one["folder"].parents
    assert one["staged"] == [one["folder"].name]  # positive control: it was there meanwhile
    assert staged(world) == []
    assert one["folder"].parent.stat().st_mode & 0o005 == 0o005  # readable and enterable by any uid


def test_a_failing_canary_leaves_the_staging_folder_removed_and_the_store_alone(world):
    claude_download.release(world.releases, NEW)
    seen = canary_says(world, {"version": {"lines": ["it said nothing"], "more": 0}})
    before = snapshot(world.store)
    problem = refusal(world.versions.upgrade, NEW, ALICE)
    assert (problem.code, problem.extra["failed"]) == ("claude_checks_failed", ["version"])
    assert problem.extra["report"] == {"version": {"lines": ["it said nothing"], "more": 0}}
    assert seen and seen[0]["staged"]  # it ran, with the folder there
    assert snapshot(world.store) == before
    assert staged(world) == []


def test_a_canary_that_cannot_run_leaves_everything_as_it_was(world):
    from rcman.runtime import RuntimeFailed

    claude_download.release(world.releases, NEW)

    def broken(directory, name, version):
        raise RuntimeFailed("podman is not there")

    world.runtime.canary_script = broken
    before = snapshot(world.store)
    with pytest.raises(RuntimeFailed):
        world.versions.upgrade(NEW, ALICE)
    assert snapshot(world.store) == before
    assert staged(world) == []
    assert world.versions._upgrading.acquire(blocking=False)  # the next upgrade is not refused
    world.versions._upgrading.release()


def test_a_second_upgrade_while_one_runs_is_refused_busy_and_the_first_goes_on(world):
    claude_download.release(world.releases, NEW)
    claude_download.release(world.releases, "2.1.0")
    entered, release = threading.Event(), threading.Event()

    def slow(directory, name, version):
        entered.set()
        release.wait(30)
        return {}

    world.runtime.canary_script = slow
    results = []
    first = threading.Thread(target=lambda: results.append(world.versions.upgrade(NEW, ALICE)))
    first.start()
    try:
        assert entered.wait(30)
        problem = refusal(world.versions.upgrade, "2.1.0", BOB)
        assert (problem.code, problem.status) == ("claude_upgrade_busy", 409)
        assert world.versions.report()["upgrading"] is True
    finally:
        release.set()
        first.join(60)
    assert [r["approved"] for r in results] == [NEW]
    assert approvals(world)[-1] == (NEW, ALICE)
    assert world.versions.report()["upgrading"] is False
    # positive control: once the first is done, another goes through
    world.runtime.canary_script = lambda *a: {}
    assert world.versions.upgrade("2.1.0", BOB)["approved"] == "2.1.0"


def test_a_rollback_is_a_new_approval_that_needs_neither_a_download_nor_a_canary(world):
    claude_download.release(world.releases, NEW)
    world.versions.upgrade(NEW, ALICE)
    seen = canary_says(world)
    for child in world.releases.rglob("*"):  # the release site has nothing any more
        if child.is_file():
            child.unlink()
    report = world.versions.upgrade(FAKE_VERSION, BOB)
    assert approvals(world)[-3:] == [
        (FAKE_VERSION, cv.ADOPTED_BY),
        (NEW, ALICE),
        (FAKE_VERSION, BOB),
    ]
    assert (report["approved"], report["previous"]) == (FAKE_VERSION, NEW)
    assert link(world) == f"versions/{FAKE_VERSION}"
    assert seen == []  # it passed the checks when it was approved
    assert staged(world) == []


def test_a_rollback_works_when_the_runtime_cannot_run_a_container(world):
    from rcman.runtime import RuntimeUnavailable

    claude_download.release(world.releases, NEW)
    world.versions.upgrade(NEW, ALICE)

    def unavailable(directory, name, version):
        raise RuntimeUnavailable("podman is not there")

    world.runtime.canary_script = unavailable
    assert world.versions.upgrade(FAKE_VERSION, BOB)["approved"] == FAKE_VERSION
    # positive control: a version that was never approved does need the canary
    fake_claude(world.store, version="1.9.0")
    fake_claude(world.store, version=FAKE_VERSION)
    with pytest.raises(RuntimeUnavailable):
        world.versions.upgrade("1.9.0", BOB)
    assert world.versions.approved() == FAKE_VERSION


def test_an_earlier_version_that_is_no_longer_in_the_store_is_fetched_and_checked_again(world):
    claude_download.release(world.releases, NEW)
    world.versions.upgrade(NEW, ALICE)
    (world.store / "versions" / FAKE_VERSION).unlink()
    claude_download.release(world.releases, FAKE_VERSION)
    seen = canary_says(world)
    world.versions.upgrade(FAKE_VERSION, BOB)
    assert [s["version"] for s in seen] == [FAKE_VERSION]
    assert (world.store / "versions" / FAKE_VERSION).is_file()


def test_approving_the_version_already_approved_changes_nothing(world):
    seen = canary_says(world)
    before = snapshot(world.store)
    report = world.versions.upgrade(FAKE_VERSION, ALICE)
    assert report["approved"] == FAKE_VERSION
    assert approvals(world) == [(FAKE_VERSION, cv.ADOPTED_BY)]
    assert seen == [] and snapshot(world.store) == before


def test_a_version_in_the_store_that_was_never_approved_is_approved_after_the_canary(world):
    """The tested version the installer put in the store beside the approved one."""
    fake_claude(world.store, version="1.9.0")
    fake_claude(world.store, version=FAKE_VERSION)
    seen = canary_says(world)
    world.versions.upgrade("1.9.0", ALICE)
    assert [s["version"] for s in seen] == ["1.9.0"]
    assert world.versions.approved() == "1.9.0"
    assert link(world) == "versions/1.9.0"


def test_a_stale_partial_file_in_the_store_does_not_stop_a_rollout(world):
    claude_download.release(world.releases, NEW)
    (world.store / "versions" / f".{NEW}{claude_release.PARTIAL_SUFFIX}").write_bytes(b"half")
    canary_says(world)
    world.versions.upgrade(NEW, ALICE)
    assert (world.store / "versions" / NEW).read_bytes().startswith(b"#!/bin/sh")
    assert [p.name for p in (world.store / "versions").iterdir() if p.name.startswith(".")] == []


# --- the sweep at start ------------------------------------------------------------------


def test_starting_sweeps_what_a_rollout_that_was_cut_short_left(world):
    folder = staging(world)
    (folder / "abc").mkdir(parents=True)
    (folder / "abc" / cv.CANDIDATE).write_bytes(b"half")
    partial = world.store / "versions" / f".{NEW}{claude_release.PARTIAL_SUFFIX}"
    partial.write_bytes(b"half")
    kept = world.store / "versions" / FAKE_VERSION
    ended = []
    world.runtime.end_canary = ended.append
    world.versions.sweep()
    assert staged(world) == [] and not partial.exists()
    assert kept.exists()  # positive control: only what is left over goes
    assert ended == [cv.CANARY_NAME]


def test_restore_sweeps_before_it_brings_anything_back(world):
    (staging(world) / "left").mkdir(parents=True)
    world.service.restore()
    assert staged(world) == []


def test_a_sweep_leaves_a_rollout_that_runs_alone(world):
    claude_download.release(world.releases, NEW)
    entered, release = threading.Event(), threading.Event()

    def slow(directory, name, version):
        entered.set()
        release.wait(30)
        return {}

    world.runtime.canary_script = slow
    upgrading = threading.Thread(target=lambda: world.versions.upgrade(NEW, ALICE))
    upgrading.start()
    try:
        assert entered.wait(30)
        world.versions.sweep()
        assert len(staged(world)) == 1  # the rollout's own folder is still there
    finally:
        release.set()
        upgrading.join(60)
    assert staged(world) == []


# --- pruning ---------------------------------------------------------------------------------


def insert_workspace(world, person, name, version, server=1):
    con = db.connect(world.settings.database)
    try:
        cursor = con.execute(
            "insert into workspaces (person, name, state, server, claude_version) "
            "values (?, ?, 'ready', ?, ?)",
            (person, name, server, version),
        )
        con.commit()
        return cursor.lastrowid
    finally:
        con.close()


def statuses(world, by_workspace):
    """What the servers read as: a running one, an ended one, or one that cannot be read."""
    shown = {
        "running": {"state": "ready", "exit_code": None},
        "ended": {"state": "exited", "exit_code": 1},
    }

    def status(workspace, probe=False):
        what = by_workspace[workspace.id]
        if what == "unreadable":
            raise Problem("timeout")
        return shown[what]

    world.service.server_status = status


def present(world) -> list[str]:
    return sorted(p.name for p in (world.store / "versions").iterdir())


def test_prune_keeps_the_approved_the_previous_and_what_running_or_unreadable_servers_use(world):
    install(world, "1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0", "1.6.0")
    approve_rows(world, "1.4.0", "1.5.0")
    world.versions.reconcile()
    running = insert_workspace(world, 1, "running", "1.3.0")
    unreadable = insert_workspace(world, 1, "unreadable", "1.2.0")
    ended = insert_workspace(world, 2, "ended", "1.1.0")
    nothing = insert_workspace(world, 2, "nothing", None, server=0)
    statuses(
        world, {running: "running", unreadable: "unreadable", ended: "ended", nothing: "ended"}
    )
    assert present(world) == sorted(["1.0.0", "1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0", "1.6.0"])
    world.versions.prune()
    # the approved 1.5.0 and the previous 1.4.0; 1.3.0 runs; 1.2.0 cannot be read
    assert present(world) == ["1.2.0", "1.3.0", "1.4.0", "1.5.0"]
    assert link(world) == "versions/1.5.0"


def test_prune_leaves_a_version_put_in_the_store_a_moment_ago_by_an_upgrade_beside_it(
    world, monkeypatch
):
    """An upgrade of rcman puts its own tested version in the store while the service of the
    release before it runs; that service does not know the version, which no approval names."""
    monkeypatch.setattr(cv, "PRUNE_GRACE_SECONDS", 3600.0)
    install(world, "2.5.0", "1.1.0")
    approve_rows(world, "1.1.0")
    world.versions.reconcile()
    long_ago = time.time() - 2 * cv.PRUNE_GRACE_SECONDS
    os.utime(world.store / "versions" / FAKE_VERSION, (long_ago, long_ago))
    os.utime(world.store / "versions" / "1.1.0", (long_ago, long_ago))
    assert world.versions.tested() not in ("2.5.0",)  # this service's tested version is another
    world.versions.prune()
    assert "2.5.0" in present(world)  # new
    os.utime(world.store / "versions" / "2.5.0", (long_ago, long_ago))
    world.versions.prune()
    assert "2.5.0" not in present(world)  # the control: once old it is an unapproved version


def test_prune_keeps_the_tested_version_whatever_is_approved(world):
    tested = world.versions.tested()
    install(world, tested, "1.1.0", "1.2.0")
    approve_rows(world, "1.1.0", "1.2.0")
    world.versions.reconcile()
    world.versions.prune()
    assert tested in present(world)  # approved 1.2.0, previous 1.1.0; the tested one stays
    assert present(world) == sorted({tested, "1.1.0", "1.2.0"})


def test_a_recorded_version_counts_as_in_use_whatever_the_server_column_says(world, monkeypatch):
    """The record of a version and the flag of a server are two writes: one that lands without
    the other must not let the store lose a version a server may run."""
    from types import SimpleNamespace

    install(world, "1.1.0", "1.2.0")
    approve_rows(world, "1.2.0")
    world.versions.reconcile()
    flagless = insert_workspace(world, 1, "flagless", "1.1.0", server=0)
    served = {"state": "ready", "exit_code": None}
    monkeypatch.setattr(
        world.service,
        "server",
        lambda workspace, version=None: SimpleNamespace(status=lambda: served),
    )
    world.versions.prune()
    assert "1.1.0" in present(world)  # it runs, by tmux's own word, whatever the column says
    served = None  # the control: tmux says there is no server
    monkeypatch.setattr(
        world.service,
        "server",
        lambda workspace, version=None: SimpleNamespace(status=lambda: None),
    )
    world.versions.prune()
    assert "1.1.0" not in present(world)
    assert flagless  # (the row is still there: only the version went)


def test_prune_leaves_a_version_a_server_that_is_starting_uses(world, monkeypatch):
    install(world, "1.1.0", "1.2.0")
    approve_rows(world, "1.2.0")
    world.versions.reconcile()
    starting = insert_workspace(world, 1, "starting", "1.1.0", server=0)
    statuses(world, {starting: "ended"})
    with world.service._lock:
        world.service._starting.add(starting)
    world.versions.prune()
    assert "1.1.0" in present(world)
    with world.service._lock:
        world.service._starting.discard(starting)
    world.versions.prune()
    assert "1.1.0" not in present(world)  # positive control: it was only the start that kept it


def test_prune_does_nothing_while_an_upgrade_runs(world):
    install(world, "1.1.0")
    approve_rows(world, "1.2.0")
    fake_claude(world.store, version="1.2.0")
    claude_download.release(world.releases, NEW)
    entered, release = threading.Event(), threading.Event()

    def slow(directory, name, version):
        entered.set()
        release.wait(30)
        return {}

    world.runtime.canary_script = slow
    upgrading = threading.Thread(target=lambda: world.versions.upgrade(NEW, ALICE))
    upgrading.start()
    try:
        assert entered.wait(30)
        world.versions.prune()
        assert "1.1.0" in present(world)
    finally:
        release.set()
        upgrading.join(60)
    world.versions.prune()
    assert "1.1.0" not in present(world)  # positive control: afterwards it goes


def test_an_approval_is_followed_by_a_prune(world):
    install(world, "1.1.0")
    claude_download.release(world.releases, NEW)
    canary_says(world)
    world.versions.upgrade(NEW, ALICE)
    # approved NEW, previous FAKE_VERSION; the unapproved 1.1.0 is gone
    assert present(world) == sorted([FAKE_VERSION, NEW])


def test_a_stop_triggers_a_prune(world):
    install(world, "1.1.0")
    world.service._stopped(1)
    assert present(world) == [FAKE_VERSION]


def test_restore_prunes(world):
    install(world, "1.1.0")
    world.service.restore()
    assert present(world) == [FAKE_VERSION]


def test_a_prune_that_fails_does_not_fail_what_triggered_it(world, caplog):
    def broken():
        raise OSError("the disk is gone")

    world.versions.prune = broken
    world.service._stopped(1)  # does not raise
    assert "prune" in caplog.text.lower()


def test_no_server_status_is_read_when_nothing_could_be_pruned(world):
    asked = []
    world.service.server_status = lambda workspace, probe=False: asked.append(workspace.id)
    insert_workspace(world, 1, "running", FAKE_VERSION)
    world.versions.prune()
    assert asked == []  # only the approved version is installed: nothing to decide


# --- the report -------------------------------------------------------------------------------


def test_the_report_names_the_approved_the_previous_the_tested_and_what_is_installed(world):
    install(world, "1.1.0")
    approve_rows(world, "1.1.0")
    running = insert_workspace(world, 1, "mine", FAKE_VERSION)
    statuses(world, {running: "running"})
    report = world.versions.report()
    assert (report["approved"], report["previous"]) == ("1.1.0", FAKE_VERSION)
    tested = (
        (Path(cv.__file__).resolve().parents[2] / "helpers" / "claude-version").read_text().strip()
    )
    assert report["tested"] == tested
    by_version = {entry["version"]: entry for entry in report["installed"]}
    assert set(by_version) == {FAKE_VERSION, "1.1.0"}
    assert by_version["1.1.0"]["approved"] is True
    assert by_version[FAKE_VERSION]["in_use"] == 1 and by_version["1.1.0"]["in_use"] == 0
    assert report["upgrading"] is False


def test_drift_lists_the_running_servers_that_run_another_version_than_the_approved(world):
    install(world, "1.1.0")
    approve_rows(world, "1.1.0")
    behind = insert_workspace(world, 1, "behind", FAKE_VERSION)
    current = insert_workspace(world, 2, "current", "1.1.0")
    ended = insert_workspace(world, 1, "ended", FAKE_VERSION)
    statuses(world, {behind: "running", current: "running", ended: "ended"})
    drift = world.versions.report()["drift"]
    assert drift == [
        {
            "workspace": behind,
            "name": "behind",
            "email": ALICE,
            "version": FAKE_VERSION,
            "approved": "1.1.0",
        }
    ]


def test_drift_lists_a_server_that_cannot_be_read_with_the_version_it_started_with(world):
    install(world, "1.1.0")
    approve_rows(world, "1.1.0")
    lost = insert_workspace(world, 1, "lost", FAKE_VERSION)
    statuses(world, {lost: "unreadable"})
    assert [d["workspace"] for d in world.versions.report()["drift"]] == [lost]


# --- the order of things ------------------------------------------------------------------------


class Watched:
    """The versions' lock, telling when a thread is about to wait for it."""

    def __init__(self, inner):
        self.inner = inner
        self.waiting = threading.Event()

    def __enter__(self):
        self.waiting.set()
        self.inner.acquire()
        return self

    def __exit__(self, *exc):
        self.inner.release()

    def acquire(self, *args, **kwargs):
        self.waiting.set()
        return self.inner.acquire(*args, **kwargs)

    def release(self):
        self.inner.release()


def watched(world) -> Watched:
    world.versions.lock = Watched(world.versions.lock)
    return world.versions.lock


def done_within(call, seconds=30):
    """Whether ``call`` finishes in ``seconds`` in a thread of its own."""
    worker = threading.Thread(target=call, daemon=True)
    worker.start()
    worker.join(seconds)
    return not worker.is_alive()


def test_a_server_that_hangs_does_not_hold_up_the_store_while_a_prune_asks_about_it(world):
    install(world, "1.1.0")
    insert_workspace(world, 1, "hung", "1.1.0")
    other = ready_workspace(world, 2, "other")
    entered, release = threading.Event(), threading.Event()

    def hangs(workspace, probe=False):
        entered.set()
        release.wait(60)
        return {"state": "ready", "exit_code": None}

    world.service.server_status = hangs
    pruning = threading.Thread(target=world.versions.prune)
    pruning.start()
    try:
        assert entered.wait(30)
        assert done_within(world.versions.resolve)  # the lock is free
        assert done_within(world.versions.reconcile)
        assert done_within(lambda: world.service.start_server(other, "pfx"))
        assert world.service.get(other.id).claude_version == FAKE_VERSION
    finally:
        release.set()
        pruning.join(60)
    assert "1.1.0" in present(world)  # the hung server was running it, as far as anyone knew


def test_a_prune_holds_up_the_version_a_start_pins_while_it_decides_what_to_delete(world):
    """The deciding and the deleting are under the lock a start pins under, so the version a
    start records is never one that has just been decided on for deletion."""
    install(world, "1.1.0")
    lock = watched(world)
    entered, release = threading.Event(), threading.Event()
    real = world.service.recorded_versions
    calls = []

    def gated():
        calls.append(1)
        if len(calls) > 1:  # the second reading is the one under the lock
            entered.set()
            release.wait(60)
        return real()

    world.service.recorded_versions = gated
    workspace = ready_workspace(world)
    pruning = threading.Thread(target=world.versions.prune)
    pruning.start()
    assert entered.wait(30)
    lock.waiting.clear()
    starting = threading.Thread(target=lambda: world.service.start_server(workspace, "pfx"))
    starting.start()
    try:
        assert lock.waiting.wait(30)  # the start is at the lock the prune holds
        assert world.service.get(workspace.id).claude_version is None
    finally:
        release.set()
        pruning.join(60)
        starting.join(60)
    assert world.service.get(workspace.id).claude_version == FAKE_VERSION
    assert present(world) == [FAKE_VERSION]  # the approved one was never a candidate


def test_a_start_that_pins_a_version_while_a_prune_reads_who_uses_what_is_not_lost(world):
    install(world, "1.1.0")
    entered, release = threading.Event(), threading.Event()
    workspace = ready_workspace(world)

    def reads(candidates):
        entered.set()
        release.wait(60)
        return set()  # nobody ran 1.1.0 when it looked

    world.service.versions_in_use = reads
    pruning = threading.Thread(target=world.versions.prune)
    pruning.start()
    try:
        assert entered.wait(30)
        approve_rows(world, "1.1.0")
        fake_claude(world.store, version="1.1.0")
        world.service.start_server(workspace, "pfx")  # pins 1.1.0, the approved version now
        assert world.service.get(workspace.id).claude_version == "1.1.0"
        install(world, "1.2.0", "1.3.0")
        approve_rows(world, "1.2.0", "1.3.0")  # and 1.1.0 is neither approved nor previous
        fake_claude(world.store, version="1.3.0")
    finally:
        release.set()
        pruning.join(60)
    assert present(world) == ["1.1.0", "1.2.0", "1.3.0"]  # kept: the record changed meanwhile
    assert FAKE_VERSION not in present(world)  # positive control: the rest went
    world.service.stop_server(world.service.get(workspace.id))
    assert "1.1.0" not in present(world)  # stopped, it goes


def test_a_prune_parked_while_a_rollout_completes_removes_nothing_the_rollout_needs(world):
    install(world, "1.9.0")
    entered, release = threading.Event(), threading.Event()
    errors = []

    def prune():
        try:
            world.versions.prune()
        except Exception as failed:  # noqa: BLE001 - reported below
            errors.append(failed)

    pruning = threading.Thread(target=prune)

    def reads(candidates):
        if threading.current_thread() is pruning:
            entered.set()
            release.wait(60)
        return set()

    world.service.versions_in_use = reads
    pruning.start()
    try:
        assert entered.wait(30)
        canary_says(world)
        world.versions.upgrade("1.9.0", ALICE)
    finally:
        release.set()
        pruning.join(60)
    assert errors == []
    assert present(world) == [FAKE_VERSION, "1.9.0"]
    assert world.versions.approved() == "1.9.0"


def test_a_rollout_stages_from_the_store_under_the_lock_a_prune_deletes_under(
    world,
):
    install(world, "1.9.0")
    stored = (world.store / "versions" / "1.9.0").read_bytes()
    claude_download.release(world.releases, "1.9.0")
    canary_says(world)
    lock = watched(world)
    lock.inner.acquire()  # a prune is deleting
    rolling = threading.Thread(target=lambda: world.versions.upgrade("1.9.0", ALICE))
    rolling.start()
    try:
        assert lock.waiting.wait(30)  # the rollout is at the lock before it reads the store
        (world.store / "versions" / "1.9.0").unlink()  # the deletion
    finally:
        lock.inner.release()
        rolling.join(60)
    assert not rolling.is_alive() and world.versions.approved() == "1.9.0"
    installed = (world.store / "versions" / "1.9.0").read_bytes()
    assert installed != stored  # it was fetched, not copied from a file that was gone
    assert installed.startswith(b"#!/bin/sh\n# The fake claude of rcman's tests")


def test_a_start_that_pinned_a_version_keeps_it_from_a_prune_while_the_server_starts(world):
    from rcman import claude_server as cs

    install(world, "1.1.0", "1.2.0", "1.3.0")
    world.versions.reconcile()
    workspace = ready_workspace(world)
    entered, release = threading.Event(), threading.Event()
    real = cs.Server.start

    def slow(self, prefix):
        entered.set()
        release.wait(30)
        return real(self, prefix)

    cs.Server.start = slow
    starting = threading.Thread(target=lambda: world.service.start_server(workspace, "pfx"))
    try:
        starting.start()
        assert entered.wait(30)
        assert world.service.get(workspace.id).claude_version == FAKE_VERSION
        approve_rows(world, "1.2.0", "1.3.0")  # two approvals later, FAKE_VERSION is neither
        fake_claude(world.store, version="1.3.0")
        world.versions.prune()
        assert FAKE_VERSION in present(world)
    finally:
        cs.Server.start = real
        release.set()
        starting.join(60)
    world.service.stop_server(world.service.get(workspace.id))
    world.versions.prune()
    assert FAKE_VERSION not in present(world)  # positive control: stopped, it goes


# --- a version that is not a version, and a store that was cut short --------------------------


@pytest.mark.parametrize(
    "version", ["../x", "1.2", "", "latest", "1.2.3/../../x", "1.2.3 ", "1.2.3\n", "a.b.c"]
)
def test_a_rollout_validates_the_version_before_it_touches_anything(world, version):
    before = snapshot(world.store)
    problem = refusal(world.versions.upgrade, version, ALICE)
    assert (problem.code, problem.extra["kind"]) == ("claude_install_failed", "no_such_version")
    assert snapshot(world.store) == before
    assert not staging(world).exists()
    assert approvals(world) == [(FAKE_VERSION, cv.ADOPTED_BY)]
    assert world.versions._upgrading.acquire(blocking=False)  # and nothing is left held
    world.versions._upgrading.release()


def test_a_version_that_is_a_path_does_not_copy_that_file_into_the_staging_folder(world, tmp_path):
    secret = tmp_path / "claude" / "versions" / ".." / ".." / "secret"
    secret.write_text("not a version")
    seen = canary_says(world)
    refusal(world.versions.upgrade, "../../secret", ALICE)
    assert seen == [] and not staging(world).exists()


def test_a_stored_file_with_nothing_in_it_is_not_a_version(world):
    """What a power loss between a rename and the write behind it leaves."""
    (world.store / "versions" / "1.5.0").write_bytes(b"")
    assert world.versions.installed() == [FAKE_VERSION]
    (world.store / "versions" / FAKE_VERSION).write_bytes(b"")
    assert world.versions.installed() == []
    assert refusal(world.versions.resolve).code == "claude_not_installed"


def test_a_link_to_a_file_with_nothing_in_it_is_not_adopted(tmp_path, settings, runtime):
    (settings.claude_store / "versions" / FAKE_VERSION).write_bytes(b"")
    service = ws.Workspaces(settings, runtime)
    try:
        service.versions.reconcile()
        assert service.versions.approved() is None
    finally:
        service.close()


def test_a_rollout_replaces_a_stored_file_with_nothing_in_it_by_fetching_the_version(world):
    claude_download.release(world.releases, NEW)
    (world.store / "versions" / NEW).write_bytes(b"")
    canary_says(world)
    world.versions.upgrade(NEW, ALICE)
    assert (world.store / "versions" / NEW).read_bytes().startswith(b"#!/bin/sh")


def test_a_sweep_removes_a_stored_file_with_nothing_in_it(world):
    empty = world.store / "versions" / "1.5.0"
    empty.write_bytes(b"")
    world.versions.sweep()
    assert not empty.exists() and (world.store / "versions" / FAKE_VERSION).exists()


def test_a_version_is_on_disk_before_it_takes_its_name_and_the_name_after(world, monkeypatch):
    claude_download.release(world.releases, NEW)
    canary_says(world)
    events = []
    real_fsync, real_replace = os.fsync, os.replace

    def fsync(fd):
        events.append(("fsync", os.readlink(f"/proc/self/fd/{fd}")))
        real_fsync(fd)

    def replace(source, target, *args, **kwargs):
        events.append(("replace", str(target)))
        real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)
    world.versions.upgrade(NEW, ALICE)
    versions = str(world.store / "versions")
    partial = f"{versions}/.{NEW}{claude_release.PARTIAL_SUFFIX}"
    named = events.index(("replace", f"{versions}/{NEW}"))
    assert ("fsync", partial) in events[:named]  # the bytes, before the name
    assert ("fsync", versions) in events[named:]  # and the name, after


# --- a link that could not be switched ---------------------------------------------------


def test_a_rollout_whose_link_cannot_be_switched_fails_and_approves_nothing(world):
    claude_download.release(world.releases, NEW)
    canary_says(world)

    def broken(version):
        raise OSError("the store is read-only")

    world.versions._switch = broken
    with pytest.raises(OSError):
        world.versions.upgrade(NEW, ALICE)
    assert approvals(world) == [(FAKE_VERSION, cv.ADOPTED_BY)]  # as the link says
    assert link(world) == f"versions/{FAKE_VERSION}"
    assert staged(world) == []
    # what was stored is only unapproved: the next prune takes it, and the link's version stays
    assert NEW in present(world)
    del world.versions._switch
    world.versions.prune()
    assert present(world) == [FAKE_VERSION]


def test_a_rollout_whose_approval_cannot_be_recorded_puts_the_link_back(world):
    claude_download.release(world.releases, NEW)
    canary_says(world)

    def broken(version, by):
        raise OSError("the database is read-only")

    world.versions._approve = broken
    with pytest.raises(OSError):
        world.versions.upgrade(NEW, ALICE)
    assert link(world) == f"versions/{FAKE_VERSION}"
    assert approvals(world) == [(FAKE_VERSION, cv.ADOPTED_BY)]


def test_prune_keeps_the_version_the_link_names_even_when_it_is_not_the_approved_one(world):
    install(world, "1.4.0", "1.5.0")
    approve_rows(world, "1.4.0", "1.5.0")

    def broken(version):
        raise OSError("the store is read-only")

    world.versions._switch = broken
    world.versions.reconcile()  # cannot repoint the link; it names FAKE_VERSION still
    assert link(world) == f"versions/{FAKE_VERSION}"
    world.versions.prune()
    assert present(world) == [FAKE_VERSION, "1.4.0", "1.5.0"]
    del world.versions._switch
    world.versions.reconcile()  # the link follows the database, and the old one is then prunable
    world.versions.prune()
    assert present(world) == ["1.4.0", "1.5.0"]


def test_the_canary_runs_in_the_builder_slot_the_service_is_given(world, monkeypatch):
    claude_download.release(world.releases, NEW)
    slots = []
    real = world.runtime.run_canary

    def watched(*args, slot=None):
        slots.append(slot)
        return real(*args, slot=slot)

    monkeypatch.setattr(world.runtime, "run_canary", watched)
    monkeypatch.setattr(world.service, "builder_slot", 6)
    world.versions.upgrade(NEW, ALICE)
    assert slots == [6]
