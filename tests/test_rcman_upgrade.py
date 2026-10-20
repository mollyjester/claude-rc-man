"""Upgrading rcman from its web interface, rcman's side (backend/rcman/rcman_upgrade.py and
backend/rcman/routers/rcman_upgrade.py): the report GET /api/rcman/upgrade gives, what it reads
of the status the wrapper writes and of what systemd shows of the upgrade's units, the admission
of a request, and the freeze of workspace creation, clones and roll-outs while an upgrade is under
way. The wrapper and the units themselves are the installer's: tests/test_upgrade.py and
tests/test_install.py.

What systemd shows is given as recorded output, through the Upgrades' ``show``; one test runs
the real argv against a stand-in for systemctl."""

from __future__ import annotations

import json
import stat
import threading
from pathlib import Path

import pytest
from rcman import db, people
from rcman import rcman_upgrade as up

from tests.conftest import ALICE, BOB, as_user, changing

ROUTE = "/api/rcman/upgrade"
BOOT = "6f1c2a7e-0d4b-4b1e-9a51-3c2d1e0f9a88"
OTHER_BOOT = "0b9e8d7c-6a5f-4e3d-2c1b-0a9f8e7d6c5b"
OLD = "1" * 40  # a commit of a release before
NEW = "2" * 40
OLD_RUN, NEW_RUN = "a" * 32, "b" * 32  # invocations of the service

# What `systemctl show rcman-upgrade.service rcman-upgrade.path -p
# Id,LoadState,ActiveState,InvocationID` prints: a block for each unit, in the order named, the
# properties in systemd's own order, the blocks parted by an empty line.


def shown(service_state="inactive", service_run="", path_state="active", path_load="loaded"):
    return (
        f"Id=rcman-upgrade.service\nLoadState=loaded\nActiveState={service_state}\n"
        f"InvocationID={service_run}\n\n"
        f"Id=rcman-upgrade.path\nLoadState={path_load}\nActiveState={path_state}\n"
        f"InvocationID={'c' * 32}\n"
    )


IDLE = shown()  # nothing runs; the path unit watches
IDLE_AFTER_OLD = shown(service_run=OLD_RUN)  # the service ran once, and is done
RUNNING_NEW = shown("activating", NEW_RUN)  # a run of the service, its ExecStart
FINISHING_NEW = shown("deactivating", NEW_RUN)  # its ExecStopPost
DONE_NEW = shown(service_run=NEW_RUN)


def status(state="succeeded", boot=BOOT, invocation=OLD_RUN, **more) -> dict:
    found = {
        "state": state,
        "boot_id": boot,
        "invocation": invocation,
        "started": "2026-10-19T10:00:00+00:00",
        "finished": None if state == "running" else "2026-10-19T10:05:00+00:00",
        "from": OLD,
        "to": None if state == "running" else NEW,
        "notices": [],
        "error": "install.sh: it went wrong" if state == "failed" else None,
    }
    found.update(more)
    return found


@pytest.fixture
def paths(tmp_path, monkeypatch):
    """The installer's paths, each in the test's folder: the status, the request file, the boot's
    id and the release rcman runs from, a release folder ``current`` names with its mark. Patched
    before the app starts, which reads its release then."""
    folder = tmp_path / "rcman-upgrade"
    (folder / "status").mkdir(parents=True)
    (folder / "state").mkdir()
    releases = folder / "releases"
    for name, commit in (("old", OLD), ("new", NEW)):
        (releases / name / ".venv").mkdir(parents=True)
        (releases / name / ".complete").write_text(commit + "\n", encoding="utf-8")
    (folder / "current").symlink_to(releases / "old")
    (folder / "boot_id").write_text(BOOT + "\n", encoding="utf-8")
    monkeypatch.setattr(up, "STATUS", folder / "status" / "status.json")
    monkeypatch.setattr(up, "REQUEST", folder / "state" / "upgrade.request")
    monkeypatch.setattr(up, "BOOT_ID", folder / "boot_id")
    monkeypatch.setattr(up, "PREFIX", folder / "current" / ".venv")
    return folder


@pytest.fixture
def on(settings):
    settings.web_upgrade = True
    return settings


class Show:
    """Stands in for what systemd shows: ``answer``, or the error it raises; counts the calls,
    and, once ``hold`` is set, waits in the call for ``go`` after setting ``entered``."""

    def __init__(self, answer: str | Exception = IDLE):
        self.answer = answer
        self.calls = 0
        self.hold = False
        self.entered, self.go = threading.Event(), threading.Event()

    def __call__(self) -> str:
        self.calls += 1
        if self.hold:
            self.entered.set()
            assert self.go.wait(30)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def show(paths, on, app):
    fake = Show()
    app.state.upgrade.show = fake
    return fake


def write_status(paths: Path, found: dict | str) -> None:
    text = found if isinstance(found, str) else json.dumps(found)
    (paths / "status" / "status.json").write_text(text, encoding="utf-8")


def get(client) -> dict:
    r = client.get(ROUTE, headers=as_user(ALICE))
    assert r.status_code == 200, r.text
    return r.json()


def post(client, who=ALICE):
    return client.post(ROUTE, headers=changing(who))


def reason(report: dict) -> str | None:
    return report["reason"]["reason"] if report["reason"] else None


def audit_rows(settings) -> list[dict]:
    con = db.connect(settings.database)
    try:
        return [dict(r) for r in con.execute("select * from audit order by id")]
    finally:
        con.close()


def creating_row(settings, name="being-made") -> None:
    """A workspace of alice's recorded as being made, as one is while it is cloned."""
    con = db.connect(settings.database)
    try:
        con.execute(
            "insert into workspaces (person, name, state) values (1, ?, 'creating')", (name,)
        )
        con.commit()
    finally:
        con.close()


def alice(settings) -> people.Person:
    con = db.connect(settings.database)
    try:
        return people.by_id(con, 1)
    finally:
        con.close()


# --- the report's shape, and the option ---------------------------------------------------

KEYS = {
    "release", "enabled", "available", "reason", "state", "started", "finished",
    "from", "to", "notices", "error",
}  # fmt: skip


def test_the_report_has_its_stable_shape_whatever_the_state(paths, client, app, on):
    fake = Show()
    app.state.upgrade.show = fake
    assert set(get(client)) == KEYS
    write_status(paths, status("failed"))
    assert set(get(client)) == KEYS
    fake.answer = up.ShowFailed("planted: systemd does not answer")
    assert set(get(client)) == KEYS
    write_status(paths, "planted: not a status")
    assert set(get(client)) == KEYS


def test_with_the_option_off_the_report_says_so_and_asks_systemd_nothing(paths, client, app):
    fake = Show(up.ShowFailed("planted: never asked"))
    app.state.upgrade.show = fake
    report = get(client)
    assert (report["enabled"], report["available"], reason(report)) == (False, False, "disabled")
    assert report["reason"]["code"] == "rcman_upgrade_refused"
    assert report["reason"]["title"]
    assert fake.calls == 0


def test_with_the_option_off_a_request_is_refused_and_recorded(paths, client, settings):
    r = post(client)
    assert r.status_code == 409, r.text
    assert (r.json()["code"], r.json()["reason"]) == ("rcman_upgrade_refused", "disabled")
    assert not up.REQUEST.exists()
    last = audit_rows(settings)[-1]
    assert (last["action"], last["result"]) == ("rcman.upgrade", "rcman_upgrade_refused")


def test_the_report_and_the_request_are_for_administrators_only(paths, client, show):
    assert client.get(ROUTE, headers=as_user(BOB)).json()["code"] == "admin_only"
    assert post(client, BOB).json()["code"] == "admin_only"
    assert not up.REQUEST.exists()


# --- the release rcman runs ------------------------------------------------------------------


def test_the_release_is_the_one_rcman_started_from_even_after_current_is_switched(paths, client):
    assert get(client)["release"] == OLD
    (paths / "current").unlink()
    (paths / "current").symlink_to(paths / "releases" / "new")  # an upgrade switched it
    assert up.running_release() == NEW  # positive control: read now, it is the new one
    assert get(client)["release"] == OLD


def test_a_development_run_has_no_release(tmp_path):
    (tmp_path / ".venv").mkdir()
    assert up.running_release(tmp_path / ".venv") is None


@pytest.mark.parametrize("text", ["", "\n", "not a commit\n", "1" * 39 + "\n", "G" * 40])
def test_a_mark_that_names_no_commit_is_no_release(tmp_path, text):
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".complete").write_text(text, encoding="utf-8")
    assert up.running_release(tmp_path / ".venv") is None
    (tmp_path / ".complete").write_text(OLD + "\n", encoding="utf-8")
    assert up.running_release(tmp_path / ".venv") == OLD  # positive control


# --- the state ---------------------------------------------------------------------------------


def test_with_no_status_the_last_upgrade_is_unknown_and_one_may_be_asked_for(client, show):
    report = get(client)
    assert (report["state"], report["available"], report["reason"]) == ("unknown", True, None)
    assert (report["from"], report["to"], report["notices"], report["error"]) == (
        None, None, [], None,
    )  # fmt: skip


def test_a_finished_upgrade_is_reported_from_its_status(paths, client, show):
    write_status(paths, status("succeeded", notices=["Reload Caddy."]))
    report = get(client)
    assert report["state"] == "succeeded" and report["available"] is True
    assert (report["from"], report["to"], report["notices"]) == (OLD, NEW, ["Reload Caddy."])
    write_status(paths, status("failed"))
    report = get(client)
    assert (report["state"], report["error"]) == ("failed", "install.sh: it went wrong")


@pytest.mark.parametrize("unit", [RUNNING_NEW, FINISHING_NEW])
def test_a_run_of_the_service_is_running_with_its_own_status(paths, client, show, unit):
    show.answer = unit
    write_status(paths, status("running", invocation=NEW_RUN))
    report = get(client)
    assert (report["state"], report["available"], reason(report)) == (
        "running", False, "under_way",
    )  # fmt: skip
    assert (report["from"], report["started"]) == (OLD, "2026-10-19T10:00:00+00:00")


def test_a_run_that_has_not_written_its_status_yet_shows_none_of_the_last_one(paths, client, show):
    show.answer = RUNNING_NEW
    write_status(paths, status("succeeded", invocation=OLD_RUN))
    report = get(client)
    assert report["state"] == "running"
    assert (report["from"], report["to"], report["started"], report["finished"]) == (
        None, None, None, None,
    )  # fmt: skip


def test_a_running_status_from_another_boot_reads_as_failed(paths, client, show):
    write_status(paths, status("running", boot=OTHER_BOOT, invocation=NEW_RUN))
    report = get(client)
    assert (report["state"], report["available"]) == ("failed", True)
    write_status(paths, status("running", invocation=NEW_RUN))  # positive control: this boot
    show.answer = RUNNING_NEW
    assert get(client)["state"] == "running"


def test_a_running_status_the_service_no_longer_runs_reads_as_failed(paths, client, show):
    show.answer = DONE_NEW  # the run ended, and its end was never written
    write_status(paths, status("running", invocation=NEW_RUN))
    assert get(client)["state"] == "failed"


def test_a_running_status_of_this_boot_is_running_where_systemd_cannot_be_read(paths, client, show):
    show.answer = up.ShowFailed("planted: systemd does not answer")
    write_status(paths, status("running", invocation=NEW_RUN))
    report = get(client)
    assert (report["state"], reason(report)) == ("running", "units_unreadable")


def test_a_request_is_requested_until_its_run_starts(paths, client, show):
    show.answer = IDLE_AFTER_OLD
    write_status(paths, status("succeeded", invocation=OLD_RUN))
    assert post(client).status_code == 202
    report = get(client)
    assert (report["state"], report["available"], reason(report)) == (
        "requested", False, "under_way",
    )  # fmt: skip
    assert report["from"] is None and report["to"] is None  # not the last run's
    show.answer = RUNNING_NEW  # the path unit started it
    assert get(client)["state"] == "running"
    show.answer = DONE_NEW
    write_status(paths, status("succeeded", invocation=NEW_RUN))
    report = get(client)
    assert (report["state"], report["available"]) == ("succeeded", True)


def test_a_run_that_started_and_ended_between_two_reports_is_seen_by_its_status(
    paths, client, show
):
    """systemd may forget a finished unit's invocation; the status still names it."""
    write_status(paths, status("succeeded", invocation=OLD_RUN))
    assert post(client).status_code == 202
    assert get(client)["state"] == "requested"  # the last run's status is not this one's
    write_status(paths, status("failed", invocation=NEW_RUN))
    assert get(client)["state"] == "failed"


def test_a_request_that_never_started_is_not_started_once_the_grace_is_over(
    paths, client, show, monkeypatch
):
    assert post(client).status_code == 202
    assert get(client)["state"] == "requested"
    monkeypatch.setattr(up, "UPGRADE_START_GRACE", 0.0)
    report = get(client)
    assert (report["state"], report["available"], report["reason"]) == (
        "not_started", True, None,
    )  # fmt: skip


MALFORMED_STATUS = [
    "",
    "not json",
    "[]",
    json.dumps({k: v for k, v in status().items() if k != "error"}),
    json.dumps({**status(), "more": 1}),
    json.dumps(status(state="done")),
    json.dumps(status(notices="Reload Caddy.")),
    json.dumps(status(notices=[1])),
    json.dumps(status(error="x" * (up.ERROR_LENGTH + 1))),
    json.dumps(status(started=5)),
    json.dumps(status(boot=None)),
]


@pytest.mark.parametrize("text", MALFORMED_STATUS)
def test_a_malformed_status_reads_as_unknown_never_as_an_error(paths, client, show, text):
    write_status(paths, status())
    assert get(client)["state"] == "succeeded"  # positive control: a status is read
    write_status(paths, text)
    report = get(client)
    assert (report["state"], report["from"], report["error"]) == ("unknown", None, None)


MALFORMED_SHOW = [
    "",
    "garbage",
    IDLE.split("\n\n")[0],  # one unit's block only
    IDLE + "\nId=another.service\nLoadState=loaded\nActiveState=active\nInvocationID=\n",
    IDLE.replace("ActiveState=inactive\n", ""),
    IDLE.replace("rcman-upgrade.path", "something-else.path"),
    IDLE.replace("ActiveState=inactive", "ActiveState=sleeping"),
    IDLE.replace("\n\n", "\n"),  # no part between the blocks
]


@pytest.mark.parametrize("text", MALFORMED_SHOW)
def test_unexpected_show_output_is_an_error_never_idle(text):
    assert up.read_units(IDLE)["rcman-upgrade.path"].active == "active"  # positive control
    with pytest.raises(ValueError):
        up.read_units(text)


@pytest.mark.parametrize("text", MALFORMED_SHOW)
def test_when_systemd_cannot_be_read_no_upgrade_is_offered_or_admitted(client, show, text):
    show.answer = text
    report = get(client)
    assert (report["available"], reason(report)) == (False, "units_unreadable")
    r = post(client)
    assert (r.status_code, r.json()["reason"]) == (409, "units_unreadable")
    assert not up.REQUEST.exists()


@pytest.mark.parametrize("unit", [shown(path_state="inactive"), shown(path_load="not-found")])
def test_no_upgrade_is_offered_or_admitted_while_the_path_unit_does_not_watch(client, show, unit):
    show.answer = unit
    assert reason(get(client)) == "path_inactive"
    r = post(client)
    assert (r.status_code, r.json()["reason"]) == (409, "path_inactive")
    assert not up.REQUEST.exists()


def test_systemd_is_asked_with_a_fixed_argv_and_environment(tmp_path, monkeypatch, paths, on):
    record = tmp_path / "systemctl-asked"
    program = tmp_path / "systemctl"
    program.write_text(
        f'#!/bin/sh\nprintf "%s\\n" "$@" > {record}\nenv >> {record}\ncat <<\'EOF\'\n{IDLE}EOF\n',
        encoding="utf-8",
    )
    program.chmod(0o755)
    monkeypatch.setattr(up, "SYSTEMCTL", str(program))
    monkeypatch.setenv("RCMAN_PLANTED_SECRET", "never-passed-on")
    units = up.read_units(up.show_units())
    assert units["rcman-upgrade.service"].active == "inactive"
    said = record.read_text(encoding="utf-8").splitlines()
    assert said[: len(up.SHOW_ARGS)] == list(up.SHOW_ARGS)
    assert "RCMAN_PLANTED_SECRET=never-passed-on" not in said
    environment = dict(line.partition("=")[::2] for line in said[len(up.SHOW_ARGS) :])
    assert set(environment) - {"PWD", "SHLVL", "_"} == set(up.SHOW_ENVIRONMENT)


def test_systemctl_refusing_is_an_error(tmp_path, monkeypatch):
    program = tmp_path / "systemctl"
    program.write_text("#!/bin/sh\necho 'planted: no bus' >&2\nexit 1\n", encoding="utf-8")
    program.chmod(0o755)
    monkeypatch.setattr(up, "SYSTEMCTL", str(program))
    with pytest.raises(up.ShowFailed, match="planted: no bus"):
        up.show_units()
    monkeypatch.setattr(up, "SYSTEMCTL", str(tmp_path / "not-there"))
    with pytest.raises(up.ShowFailed):
        up.show_units()


# --- asking for an upgrade ----------------------------------------------------------------------


def test_a_request_is_admitted_written_and_recorded(paths, client, show, settings):
    r = post(client)
    assert r.status_code == 202, r.text
    assert set(r.json()) == KEYS and r.json()["state"] == "requested"
    request = up.REQUEST
    assert request.is_file() and not request.is_symlink()
    assert stat.S_IMODE(request.stat().st_mode) & 0o077 == 0
    before = request.stat().st_mtime_ns
    last = audit_rows(settings)[-1]
    assert (last["action"], last["result"], last["email"]) == ("rcman.upgrade", "ok", ALICE)
    # a second is refused while the first is under way, and the file is left as it is
    r = post(client)
    assert (r.status_code, r.json()["reason"]) == (409, "under_way")
    assert request.stat().st_mtime_ns == before


def test_a_request_after_one_that_never_started_is_admitted_again(paths, client, show, monkeypatch):
    assert post(client).status_code == 202
    monkeypatch.setattr(up, "UPGRADE_START_GRACE", 0.0)
    assert post(client).status_code == 202


def test_requests_are_limited_by_a_token_bucket(paths, client, show, monkeypatch):
    monkeypatch.setattr(up, "UPGRADE_START_GRACE", 0.0)  # each request may be admitted again
    answers = [post(client).status_code for _ in range(up.REQUESTS_BURST)]
    assert answers == [202] * up.REQUESTS_BURST
    r = post(client)
    assert (r.status_code, r.json()["code"]) == (429, "too_many_requests")
    assert int(r.headers["Retry-After"]) >= 1


def test_no_upgrade_is_admitted_while_a_workspace_is_being_made(client, show, settings):
    creating_row(settings)
    assert reason(get(client)) == "creating"
    r = post(client)
    assert (r.status_code, r.json()["reason"]) == (409, "creating")
    assert not up.REQUEST.exists()


def test_no_upgrade_is_admitted_while_a_workspace_is_being_rebuilt(client, show, settings):
    """The phase of a rebuild is in the row, so that one this process does not run, or one a
    restart left, is seen too."""
    creating_row(settings, "being-rebuilt")
    con = db.connect(settings.database)
    try:
        con.execute("update workspaces set state = 'ready', rebuild_state = 'preparing'")
        con.commit()
    finally:
        con.close()
    assert reason(get(client)) == "rebuilding"
    r = post(client)
    assert (r.status_code, r.json()["reason"]) == (409, "rebuilding")
    assert not up.REQUEST.exists()


def cloned_workspace(client, github_stand_in, tmp_path) -> dict:
    """A ready workspace of alice's, cloned from the stand-in for github.com, which a rebuild
    may be asked for."""
    from tests.git_fixtures import make_repo
    from tests.test_clone import cloned

    make_repo(tmp_path / "src", origin=github_stand_in / "octo" / "widget.git")
    found = cloned(client)
    assert found["state"] == "ready" and found["rebuildable"], found
    return found


def preparing_held(app, monkeypatch):
    """Every rebuild held in its first phase, preparing, until the event this gives is set."""
    from rcman import workspaces as ws

    held, entered = threading.Event(), threading.Event()
    real = ws.Workspaces._prepare

    def prepare(self, *args, **more):
        entered.set()
        assert held.wait(30)
        return real(self, *args, **more)

    monkeypatch.setattr(ws.Workspaces, "_prepare", prepare)
    return held, entered


def test_an_upgrade_asked_for_while_a_real_rebuild_prepares_is_refused(
    client, show, app, settings, github_stand_in, tmp_path, monkeypatch
):
    found = cloned_workspace(client, github_stand_in, tmp_path)
    held, entered = preparing_held(app, monkeypatch)
    r = client.post(
        f"/api/workspaces/{found['id']}/rebuild", json={"confirm": True}, headers=changing(ALICE)
    )
    assert r.status_code == 202, r.text
    try:
        assert entered.wait(30)  # the rebuild is preparing, in its own thread
        r = post(client)
        assert (r.status_code, r.json()["reason"]) == (409, "rebuilding")
        assert not up.REQUEST.exists()
    finally:
        held.set()
    service = app.state.workspaces
    with service._lock:
        thread = service._making[found["id"]]
    thread.join(30)
    assert not thread.is_alive()
    assert post(client).status_code == 202  # the positive: once it is over, it is admitted


def test_a_rebuild_asked_for_once_an_upgrade_was_admitted_is_refused(
    client, show, app, settings, github_stand_in, tmp_path
):
    found = cloned_workspace(client, github_stand_in, tmp_path)
    assert post(client).status_code == 202
    r = client.post(
        f"/api/workspaces/{found['id']}/rebuild", json={"confirm": True}, headers=changing(ALICE)
    )
    assert (r.status_code, r.json()["code"]) == (409, "rcman_upgrading")
    assert app.state.workspaces.get(found["id"]).rebuild_state is None


def held_images(app):
    """An image collection that holds the images' lock until ``go`` is set."""
    service = app.state.workspaces
    entered, go = threading.Event(), threading.Event()
    real = service._collect_unreferenced

    def collect():
        entered.set()
        assert go.wait(30)
        real()

    service._collect_unreferenced = collect
    thread = threading.Thread(target=service._collect_images)
    thread.start()
    assert entered.wait(10)
    return go, thread


def test_a_request_waits_only_so_long_for_the_images_to_be_collected(
    client, show, app, monkeypatch
):
    monkeypatch.setattr(up, "ADMISSION_WAIT", 0.2)
    go, thread = held_images(app)
    try:
        r = post(client)
        assert (r.status_code, r.json()["reason"]) == (409, "images")
        assert not up.REQUEST.exists()
    finally:
        go.set()
        thread.join(10)
    assert post(client).status_code == 202  # once they are collected


def test_a_request_waits_for_a_collection_within_the_admission_wait(client, show, app, settings):
    go, thread = held_images(app)
    answered: list = []
    asking = threading.Thread(target=lambda: answered.append(app.state.upgrade.request()))
    asking.start()
    asking.join(0.3)
    assert asking.is_alive() and not up.REQUEST.exists()  # it waits for the lock
    go.set()
    asking.join(10)
    thread.join(10)
    assert answered and answered[0]["state"] == "requested"
    assert up.REQUEST.exists()


def held_sweep(app):
    """The sweep of what a roll-out left, holding the roll-outs' lock until ``go`` is set."""
    runtime = app.state.workspaces.runtime
    entered, go = threading.Event(), threading.Event()
    real = runtime.end_canary

    def end_canary(name):
        entered.set()
        assert go.wait(30)
        real(name)

    runtime.end_canary = end_canary
    thread = threading.Thread(target=app.state.versions.sweep)
    thread.start()
    assert entered.wait(10)
    return go, thread


def test_no_upgrade_is_admitted_while_a_roll_out_is_swept_up(client, show, app):
    go, thread = held_sweep(app)
    try:
        assert reason(get(client)) == "rolling_out"
        r = post(client)
        assert (r.status_code, r.json()["reason"]) == (409, "rolling_out")
        assert not up.REQUEST.exists()
    finally:
        go.set()
        thread.join(10)
    assert post(client).status_code == 202


# --- the freeze ---------------------------------------------------------------------------------


def test_while_an_upgrade_is_under_way_no_workspace_is_made_or_cloned(client, show, settings):
    assert post(client).status_code == 202
    r = client.post("/api/workspaces", json={"name": "another"}, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (409, "rcman_upgrading")
    r = client.post(
        "/api/git/clone", json={"url": "https://github.com/alice/a-clone"}, headers=changing(ALICE)
    )
    assert (r.status_code, r.json()["code"]) == (409, "rcman_upgrading")
    listed = client.get("/api/workspaces", headers=as_user(ALICE)).json()
    assert listed["workspaces"] == [] and listed["rcman_upgrading"] is True
    rows = [(r["action"], r["result"]) for r in audit_rows(settings)[-2:]]
    assert rows == [("workspace.create", "rcman_upgrading"), ("git.clone", "rcman_upgrading")]


def test_while_an_upgrade_is_under_way_no_claude_code_version_is_rolled_out(client, show):
    assert (
        client.get("/api/claude/versions", headers=as_user(ALICE)).json()["rcman_upgrading"]
        is False
    )
    assert post(client).status_code == 202
    r = client.post("/api/claude/upgrade", json={"version": "2.2.0"}, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (409, "rcman_upgrading")
    report = client.get("/api/claude/versions", headers=as_user(ALICE)).json()
    assert report["rcman_upgrading"] is True and report["upgrading"] is False


def test_with_no_upgrade_under_way_workspaces_are_made_as_ever(client, show):
    listed = client.get("/api/workspaces", headers=as_user(ALICE)).json()
    assert listed["rcman_upgrading"] is False
    r = client.post("/api/workspaces", json={"name": "another"}, headers=changing(ALICE))
    assert r.status_code == 202, r.text


def test_a_running_upgrade_freezes_creation_in_a_service_that_never_saw_the_request(
    paths, client, show
):
    """As in the rcman an upgrade restarted, while the upgrade's own run goes on."""
    show.answer = RUNNING_NEW
    write_status(paths, status("running", invocation=NEW_RUN))
    r = client.post("/api/workspaces", json={"name": "another"}, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (409, "rcman_upgrading")
    show.answer = DONE_NEW
    write_status(paths, status("succeeded", invocation=NEW_RUN))
    r = client.post("/api/workspaces", json={"name": "another"}, headers=changing(ALICE))
    assert r.status_code == 202, r.text


def test_the_freeze_holds_while_a_started_run_has_not_written_its_status_yet(
    paths, client, show, monkeypatch
):
    """Between the service's start and the wrapper's first write the status is the last run's:
    a report read then must not let the request go, or creation would be let in meanwhile."""
    write_status(paths, status("succeeded", invocation=OLD_RUN))
    show.answer = IDLE_AFTER_OLD
    assert post(client).status_code == 202
    show.answer = RUNNING_NEW  # the path unit started it; the wrapper has written nothing yet
    assert get(client)["state"] == "running"
    r = client.post("/api/workspaces", json={"name": "another"}, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (409, "rcman_upgrading")
    # still so once the grace for a start is over: the service runs
    monkeypatch.setattr(up, "UPGRADE_START_GRACE", 0.0)
    assert get(client)["state"] == "running"
    r = client.post("/api/workspaces", json={"name": "another"}, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (409, "rcman_upgrading")
    write_status(paths, status("running", invocation=NEW_RUN))  # the wrapper's first write
    r = client.post("/api/workspaces", json={"name": "another"}, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (409, "rcman_upgrading")
    show.answer = DONE_NEW
    write_status(paths, status("succeeded", invocation=NEW_RUN))
    assert get(client)["state"] == "succeeded"
    r = client.post("/api/workspaces", json={"name": "another"}, headers=changing(ALICE))
    assert r.status_code == 202, r.text


def test_a_run_that_ended_without_writing_its_status_lets_the_request_go(paths, client, show):
    write_status(paths, status("succeeded", invocation=OLD_RUN))
    show.answer = IDLE_AFTER_OLD
    assert post(client).status_code == 202
    show.answer = DONE_NEW  # a new invocation, ended, its status never written
    report = get(client)
    assert report["state"] == "succeeded" and report["available"] is True  # the last run's
    r = client.post("/api/workspaces", json={"name": "another"}, headers=changing(ALICE))
    assert r.status_code == 202, r.text


def test_listing_workspaces_asks_systemd_nothing_while_no_upgrade_runs(paths, client, show):
    write_status(paths, status("succeeded"))
    for _ in range(3):
        client.get("/api/workspaces", headers=as_user(ALICE))
    assert show.calls == 0


def test_a_workspace_asked_for_during_the_admission_is_refused_not_slipped_in(
    client, show, app, settings
):
    """The admission holds the images' lock from its checks to its record; a creation that
    comes meanwhile waits for it, and then finds the upgrade under way."""
    show.hold = True
    admitted: list = []
    admitting = threading.Thread(target=lambda: admitted.append(app.state.upgrade.request()))
    admitting.start()
    assert show.entered.wait(10)  # the admission holds the locks, asking systemd
    refused: list = []

    def create():
        try:
            app.state.workspaces.create(alice(settings), "slipped-in")
        except Exception as error:  # noqa: BLE001 - the refusal is what is looked at
            refused.append(error)

    creating = threading.Thread(target=create)
    creating.start()
    creating.join(0.3)
    assert creating.is_alive()  # it waits for the admission
    show.go.set()
    admitting.join(10)
    creating.join(10)
    assert admitted and admitted[0]["state"] == "requested"
    assert [getattr(e, "code", e) for e in refused] == ["rcman_upgrading"]
    assert app.state.workspaces.everything() == []


def test_a_roll_out_asked_for_during_the_admission_is_refused(client, show, app):
    show.hold = True
    admitting = threading.Thread(target=app.state.upgrade.request)
    admitting.start()
    assert show.entered.wait(10)
    with pytest.raises(Exception) as busy:
        app.state.versions.upgrade("2.2.0", ALICE)
    assert busy.value.code == "claude_upgrade_busy"  # the admission holds the roll-outs' lock
    show.go.set()
    admitting.join(10)
    with pytest.raises(Exception) as frozen:
        app.state.versions.upgrade("2.2.0", ALICE)
    assert frozen.value.code == "rcman_upgrading"
