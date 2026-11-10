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
from pathlib import Path

import pytest
from rcman import db
from rcman import rcman_upgrade as up

from tests.conftest import ALICE, as_user, changing

ROUTE = "/api/rcman/upgrade"
BOOT = "6f1c2a7e-0d4b-4b1e-9a51-3c2d1e0f9a88"
OLD = "1" * 40  # a commit of a release before
NEW = "2" * 40
OLD_RUN, NEW_RUN = "a" * 32, "b" * 32  # invocations of the service

# What `systemctl show rcman-upgrade.service rcman-upgrade.path -p
# Id,LoadState,ActiveState,InvocationID` prints: a block for each unit, in the order named, the
# properties in systemd's own order, the blocks parted by an empty line.


def shown(service_state="inactive", service_run=""):
    return (
        f"Id=rcman-upgrade.service\nLoadState=loaded\nActiveState={service_state}\n"
        f"InvocationID={service_run}\n\n"
        "Id=rcman-upgrade.path\nLoadState=loaded\nActiveState=active\n"
        f"InvocationID={'c' * 32}\n"
    )


IDLE = shown()  # nothing runs; the path unit watches
IDLE_AFTER_OLD = shown(service_run=OLD_RUN)  # the service ran once, and is done
RUNNING_NEW = shown("activating", NEW_RUN)  # a run of the service, its ExecStart
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
    """Stands in for what systemd shows: ``answer``, or the error it raises; counts the calls."""

    def __init__(self, answer: str | Exception = IDLE):
        self.answer = answer
        self.calls = 0

    def __call__(self) -> str:
        self.calls += 1
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.fixture
def show(paths, on, app):
    fake = Show()
    app.state.upgrade.show = fake
    return fake


def write_status(paths: Path, found: dict) -> None:
    (paths / "status" / "status.json").write_text(json.dumps(found), encoding="utf-8")


def get(client) -> dict:
    r = client.get(ROUTE, headers=as_user(ALICE))
    assert r.status_code == 200, r.text
    return r.json()


def post(client):
    return client.post(ROUTE, headers=changing(ALICE))


def reason(report: dict) -> str | None:
    return report["reason"]["reason"] if report["reason"] else None


def audit_rows(settings) -> list[dict]:
    con = db.connect(settings.database)
    try:
        return [dict(r) for r in con.execute("select * from audit order by id")]
    finally:
        con.close()


# --- the report, and the option ----------------------------------------------------------

KEYS = {
    "release", "enabled", "available", "reason", "state", "started", "finished",
    "from", "to", "error",
}  # fmt: skip


def test_with_the_option_off_the_report_says_so_and_asks_systemd_nothing(paths, client, app):
    fake = Show(up.ShowFailed("planted: never asked"))
    app.state.upgrade.show = fake
    report = get(client)
    assert (report["enabled"], report["available"], reason(report)) == (False, False, "disabled")
    assert report["reason"]["code"] == "rcman_upgrade_refused"
    assert report["reason"]["title"]
    assert fake.calls == 0


# --- the release rcman runs ------------------------------------------------------------------


def test_the_release_is_the_one_rcman_started_from_even_after_current_is_switched(paths, client):
    assert get(client)["release"] == OLD
    (paths / "current").unlink()
    (paths / "current").symlink_to(paths / "releases" / "new")  # an upgrade switched it
    assert up.running_release() == NEW  # positive control: read now, it is the new one
    assert get(client)["release"] == OLD


# --- the state ---------------------------------------------------------------------------------


def test_a_finished_upgrade_is_reported_from_its_status(paths, client, show):
    write_status(paths, status("succeeded"))
    report = get(client)
    assert report["state"] == "succeeded" and report["available"] is True
    assert (report["from"], report["to"]) == (OLD, NEW)
    write_status(paths, status("failed"))
    report = get(client)
    assert (report["state"], report["error"]) == ("failed", "install.sh: it went wrong")


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
    write_status(paths, status("running", invocation=NEW_RUN))  # the run's own status
    report = get(client)
    assert (report["state"], report["available"], reason(report)) == (
        "running", False, "under_way",
    )  # fmt: skip
    assert (report["from"], report["started"]) == (OLD, "2026-10-19T10:00:00+00:00")
    show.answer = DONE_NEW
    write_status(paths, status("succeeded", invocation=NEW_RUN))
    report = get(client)
    assert (report["state"], report["available"]) == ("succeeded", True)


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


# --- asking for an upgrade ----------------------------------------------------------------------


def test_a_request_is_admitted_written_and_recorded(paths, client, show, settings):
    r = post(client)
    assert r.status_code == 202, r.text
    assert set(r.json()) == KEYS
    request = up.REQUEST
    assert request.is_file() and not request.is_symlink()
    assert stat.S_IMODE(request.stat().st_mode) & 0o077 == 0
    last = audit_rows(settings)[-1]
    assert (last["action"], last["result"], last["email"]) == ("rcman.upgrade", "ok", ALICE)


# --- the freeze ---------------------------------------------------------------------------------


def test_while_an_upgrade_is_under_way_no_workspace_is_made_or_cloned_or_version_rolled_out(
    client, show, settings
):
    versions = client.get("/api/claude/versions", headers=as_user(ALICE)).json()
    assert versions["rcman_upgrading"] is False  # the positive: nothing under way yet
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
    r = client.post("/api/claude/upgrade", json={"version": "2.2.0"}, headers=changing(ALICE))
    assert (r.status_code, r.json()["code"]) == (409, "rcman_upgrading")
    report = client.get("/api/claude/versions", headers=as_user(ALICE)).json()
    assert report["rcman_upgrading"] is True and report["upgrading"] is False
