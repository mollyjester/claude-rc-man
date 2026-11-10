"""Claude Code versions: the approved one, rolling out another through the canary, and what
is pruned (backend/rcman/claude_versions.py), on the host runtime. The routes are
tests/test_claude.py's, a server's pinning tests/test_workspaces.py's."""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import pytest
from rcman import claude_versions as cv
from rcman import db
from rcman import workspaces as ws
from rcman.problems import MESSAGES, Problem

from helpers import claude_release
from tests import claude_download
from tests.conftest import ALICE, BOB, FAKE_VERSION, fake_claude
from tests.hang_guard import runs_out

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
    runs_out(monkeypatch, cv, "PRUNE_GRACE_SECONDS")  # what the tests prune ages nothing
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


def approve_rows(world, *versions) -> None:
    """Approvals as earlier rollouts made them."""
    con = db.connect(world.settings.database)
    try:
        for version in versions:
            con.execute(
                "insert into claude_approvals (version, at, by) values (?, ?, ?)",
                (version, "2026-01-01T00:00:00+00:00", "someone"),
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


def refusal(call, *args, **kwargs) -> Problem:
    with pytest.raises(Problem) as raised:
        call(*args, **kwargs)
    return raised.value


def canary_says(world):
    """The runtime's canary passes every candidate and records what it was given and what
    the staging folder held while it ran."""
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
        return {}

    world.runtime.canary_script = script
    return seen


# --- adopting what the installer left, and following the database -----------------------


def test_the_first_approval_is_the_version_current_names(world):
    assert approvals(world) == [(FAKE_VERSION, cv.ADOPTED_BY)]
    assert world.versions.approved() == FAKE_VERSION
    assert world.versions.previous() is None
    world.versions.reconcile()  # again: it adds nothing
    assert approvals(world) == [(FAKE_VERSION, cv.ADOPTED_BY)]


def test_the_database_wins_over_the_link_current(world):
    install(world, "1.5.0")
    approve_rows(world, "1.5.0")
    assert link(world) == f"versions/{FAKE_VERSION}"  # positive control: it names the old one
    world.versions.reconcile()
    assert world.versions.approved() == "1.5.0"
    assert link(world) == "versions/1.5.0"
    assert (world.store / "current").resolve() == world.store / "versions" / "1.5.0"


# --- rolling out a version -----------------------------------------------------------------


def test_a_good_version_is_checked_alone_installed_approved_switched_to_and_pruned_after(world):
    install(world, "1.1.0")
    claude_download.release(world.releases, NEW)
    seen = canary_says(world)
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
    assert sorted(set(after) - set(before)) == [f"versions/{NEW}"]  # nothing else, no partial file
    assert present(world) == sorted([FAKE_VERSION, NEW])  # the unapproved 1.1.0 is pruned
    [one] = seen
    assert one["name"] == cv.CANARY_NAME and one["version"] == NEW
    assert one["files"] == [cv.CANDIDATE]  # the candidate, and nothing else
    assert one["folder"].parent == staging(world)
    assert world.store not in one["folder"].parents
    assert one["staged"] == [one["folder"].name]  # positive control: it was there meanwhile
    assert staged(world) == []
    assert one["folder"].parent.stat().st_mode & 0o005 == 0o005  # readable and enterable by any uid


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


def test_a_version_in_the_store_that_was_never_approved_is_approved_after_the_canary(world):
    """The tested version the installer put in the store beside the approved one."""
    fake_claude(world.store, version="1.9.0")
    fake_claude(world.store, version=FAKE_VERSION)
    seen = canary_says(world)
    world.versions.upgrade("1.9.0", ALICE)
    assert [s["version"] for s in seen] == ["1.9.0"]
    assert world.versions.approved() == "1.9.0"
    assert link(world) == "versions/1.9.0"


# --- the sweep at start, and pruning ---------------------------------------------------------


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


def test_restore_sweeps_what_a_rollout_that_was_cut_short_left_and_prunes(world):
    install(world, "1.1.0")
    folder = staging(world)
    (folder / "abc").mkdir(parents=True)
    (folder / "abc" / cv.CANDIDATE).write_bytes(b"half")
    partial = world.store / "versions" / f".{NEW}{claude_release.PARTIAL_SUFFIX}"
    partial.write_bytes(b"half")
    ended = []
    world.runtime.end_canary = ended.append
    world.service.restore()
    assert staged(world) == [] and not partial.exists()
    assert ended == [cv.CANARY_NAME]
    assert present(world) == [FAKE_VERSION]  # the unapproved 1.1.0 is pruned


def test_prune_keeps_the_approved_the_previous_the_tested_and_what_servers_may_run(world):
    tested = world.versions.tested()
    install(world, tested, "1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0", "1.6.0")
    approve_rows(world, "1.4.0", "1.5.0")
    world.versions.reconcile()
    running = insert_workspace(world, 1, "running", "1.3.0")
    unreadable = insert_workspace(world, 1, "unreadable", "1.2.0")
    ended = insert_workspace(world, 2, "ended", "1.1.0")
    nothing = insert_workspace(world, 2, "nothing", None, server=0)
    statuses(
        world, {running: "running", unreadable: "unreadable", ended: "ended", nothing: "ended"}
    )
    assert present(world) == sorted(
        [FAKE_VERSION, tested, "1.1.0", "1.2.0", "1.3.0", "1.4.0", "1.5.0", "1.6.0"]
    )
    world.versions.prune()
    # the approved 1.5.0, the previous 1.4.0 and the tested one; 1.3.0 runs; 1.2.0 cannot be read
    assert present(world) == sorted([tested, "1.2.0", "1.3.0", "1.4.0", "1.5.0"])
    assert link(world) == "versions/1.5.0"


# --- the report -------------------------------------------------------------------------------


def test_the_report_names_the_approved_the_previous_the_tested_the_installed_and_drift(world):
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
    assert report["drift"] == [  # a running server on another version than the approved
        {
            "workspace": running,
            "name": "mine",
            "email": ALICE,
            "version": FAKE_VERSION,
            "approved": "1.1.0",
            "failed": False,
        }
    ]


# --- a version marked as failed -------------------------------------------------------------


def test_a_version_marked_failed_is_not_rolled_out_and_nothing_changes(world):
    claude_download.release(world.releases, NEW)
    world.versions.mark_failed(NEW, BOB, "users report it hangs")
    assert [f["version"] for f in world.versions.report()["failed"]] == [NEW]
    seen = canary_says(world)
    before, approved = snapshot(world.store), approvals(world)
    problem = refusal(world.versions.upgrade, NEW, ALICE)
    assert (problem.code, problem.status) == ("claude_version_failed", 409)
    assert problem.title == MESSAGES["claude_version_failed"]
    assert seen == []  # neither fetched nor checked
    assert snapshot(world.store) == before and approvals(world) == approved
    assert not staging(world).exists()
    # positive control: with the mark removed, the same rollout goes through the canary
    world.versions.clear_failed(NEW)
    assert world.versions.upgrade(NEW, ALICE)["approved"] == NEW
    assert [s["version"] for s in seen] == [NEW]


def test_the_previous_version_skips_a_version_marked_failed(world):
    approve_rows(world, "1.1.0", "1.2.0", "1.3.0")
    assert world.versions.previous() == "1.2.0"
    world.versions.mark_failed("1.2.0", ALICE, None)
    assert (world.versions.approved(), world.versions.previous()) == ("1.3.0", "1.1.0")
    world.versions.clear_failed("1.2.0")
    assert world.versions.previous() == "1.2.0"
