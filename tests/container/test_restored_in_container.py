"""After every container has stopped and rcman starts again, as after a machine restarts, rcman
brings back its workspaces and their servers, and sets the limits again before it starts them.

On the tests' engine (tests/podman_service.py): rcman's app (backend/rcman/main.py) with the Podman
runtime at the suites' sizes (tests/container/sizes.py), the default image, and the fake `claude`
signed in, in its test client; and ``systemctl`` the stand-in (tests/container/stand_ins.py), which
keeps the slices' properties as the user manager does until a restart. The module makes a workspace
of alice's, starts its server and sets limits on her and on the workspace; then rcman stops, every
container on the engine is stopped, the stand-in forgets what a restart forgets, and rcman starts
again. What a restart of the machine itself does to systemd and the kernel is checked by hand
(project-knowledge/real-claude-checklist.md).
"""

from __future__ import annotations

import json
import re
import subprocess
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from rcman import db, limits, people
from rcman import runtime as rt
from rcman.main import create_app

from devtools import fake_claude as fake
from tests.conftest import ALICE, ORIGIN, as_user, changing, made_workspace
from tests.container import sizes
from tests.container import test_runtime_in_podman as base
from tests.container.conftest import engine_runtime, engine_settings, stand_in
from tests.e2e_server import record_sign_in

# How long the workspace and its server may take to be back.
BACK_WITHIN = 300
PERSON_LIMITS = {"memory": "4G", "cpu": "3", "pids": "6000"}
WORKSPACE_LIMITS = {"memory": "2G", "cpu": "1.5", "pids": "3000"}


def until(what, within: float = BACK_WITHIN):
    deadline = time.monotonic() + within
    while not (found := what()):
        assert time.monotonic() < deadline, f"never: {what.__doc__ or what}"
        time.sleep(1)
    return found


def claude_processes(podman, name: str) -> list[int]:
    """The ids, in the workspace, of every process of the fake claude there. The pattern is
    written so that it does not find the shell that looks for it."""
    busybox = f"{rt.TOOLS_MOUNT}/bin/busybox"
    script = (
        "for f in /proc/[0-9]*/cmdline; do "
        f'if {busybox} tr "\\0" " " < "$f" 2>/dev/null | '
        f"{busybox} grep -q 'fakes/clau[d]e'; then "
        'p="${f#/proc/}"; echo "${p%/cmdline}"; fi; done'
    )
    found = podman.exec(name, [podman.layout(name).shell, "-c", script])
    return sorted(int(pid) for pid in found.out.decode().split())


def server_state(client, workspace_id: int) -> str | None:
    listed = client.get("/api/workspaces", headers=as_user(ALICE)).json()["workspaces"]
    found = next(w for w in listed if w["id"] == workspace_id)
    return found["server"] and found["server"]["state"]


def started(podman_service, name: str) -> float:
    """When Podman says the container last started, in seconds since the epoch."""
    found = json.loads(podman_service.cli("container", "inspect", name).stdout)[0]
    text = re.sub(r"(\.\d{6})\d+", r"\1", found["State"]["StartedAt"])  # to microseconds
    return datetime.fromisoformat(text).timestamp()


def set_properties(state, after: float) -> dict[str, float]:
    """When, after ``after``, the stand-in was last asked to set properties on each slice."""
    found = {}
    for line in (state / "calls").read_text().splitlines():
        call = json.loads(line)
        if call["time"] > after and call["args"][:2] == ["--user", "set-property"]:
            found[call["args"][3]] = call["time"]
    return found


@pytest.fixture(scope="module")
def restarted(podman_service, default_image, engine_tools, fake_store, tmp_path_factory):
    folder = tmp_path_factory.mktemp("rcman")
    state = folder / "stand-in"
    systemctl = stand_in(folder, "systemctl", state)
    settings = engine_settings(
        folder / "rcman.db", fake_store, public_origin=ORIGIN, tools_dir=engine_tools,
        workspace_image=default_image,
    )  # fmt: skip
    db.migrate(settings.database)
    con = db.connect(settings.database)
    try:
        alice = people.add(con, ALICE, True)
    finally:
        con.close()
    podman = engine_runtime(
        podman_service, default_image, engine_tools, fake_store, folder, systemctl=str(systemctl)
    )

    def app():
        return TestClient(create_app(settings, runtime=podman, builder_slot=sizes.BUILDER_SLOT))

    with app() as client:
        assert client.app.state.workspaces.restored.wait(120)
        workspace = made_workspace(client, ALICE, "alpha")
        name = rt.container_name(workspace["id"])
        layout = podman.layout(name)
        trusted = json.dumps({"trusted": [layout.work], "servers": {}})
        for path, text in (
            (f"{layout.config}/{fake.STATE_FILE}", trusted),
            (f"{layout.config}/{rt.CREDENTIALS}", json.dumps(fake.credentials())),
        ):
            put = podman.exec(
                name, [layout.shell, "-c", 'cat > "$1"', "sh", path], stdin=text.encode()
            )
            assert put.code == 0, put
        record_sign_in(settings.database, alice.id)
        answer = client.post(
            "/api/servers", json={"workspace": workspace["id"]}, headers=changing(ALICE)
        )
        assert answer.status_code == 200, answer.text
        until(lambda: server_state(client, workspace["id"]) in ("ready", "connected"))
        for url, held in (
            (f"/api/people/{alice.id}/limits", PERSON_LIMITS),
            (f"/api/people/{alice.id}/workspaces/{workspace['id']}/limits", WORKSPACE_LIMITS),
        ):
            answer = client.put(url, json=held, headers=changing(ALICE))
            assert answer.status_code == 200, answer.text
        before = {
            "processes": claude_processes(podman, name),
            "started": started(podman_service, name),
        }

    # The machine restarts: every container stops, and what the user manager held is gone.
    stopped = podman_service.cli("stop", "--all", "--time=10", timeout=600)
    assert stopped.returncode == 0, stopped.stderr
    assert not podman.running(name)  # positive control: the workspace is down
    reboot = stand_in(folder, "reboot", state)
    assert subprocess.run([str(reboot)], capture_output=True).returncode == 0
    rebooted = time.time()

    with app() as client:
        assert client.app.state.workspaces.restored.wait(BACK_WITHIN)
        yield {
            "client": client,
            "podman": podman,
            "engine": podman_service,
            "name": name,
            "workspace": workspace["id"],
            "person": alice.id,
            "state": state,
            "rebooted": rebooted,
            "before": before,
        }
        for made in client.app.state.workspaces.everything():
            client.app.state.workspaces.delete(made)
        podman.remove_account(alice.id)


def test_the_workspace_runs_again(restarted):
    until(lambda: restarted["podman"].running(restarted["name"]))
    assert started(restarted["engine"], restarted["name"]) > restarted["rebooted"]


def test_its_server_is_ready_again(restarted):
    client, workspace = restarted["client"], restarted["workspace"]
    until(lambda: server_state(client, workspace) in ("ready", "connected"))
    found = claude_processes(restarted["podman"], restarted["name"])
    assert found and restarted["before"]["processes"]  # it ran before, and runs again


def test_the_limits_are_set_again_before_the_container_starts(restarted):
    """The stand-in forgot the limits with the restart; rcman set them again from its table
    before it started the workspace."""
    me, workspace = restarted["person"], restarted["workspace"]
    until(lambda: restarted["podman"].running(restarted["name"]))
    times = set_properties(restarted["state"], restarted["rebooted"])
    own, theirs = limits.workspace_slice(me, workspace), limits.person_slice(me)
    assert set(times) >= {own, theirs}, times
    assert max(times[own], times[theirs]) < started(restarted["engine"], restarted["name"])
    caps = restarted["podman"].caps(me, workspace)
    assert caps == limits.Caps(2048 * limits.MEBIBYTE, 3000)
    assert restarted["podman"].caps(me, None) == limits.Caps(4096 * limits.MEBIBYTE, 6000)


def test_the_slots_the_module_uses_are_the_suites(restarted):
    """The person rcman gave a slot is in the suites' people slots, so their range fits."""
    con = db.connect(restarted["client"].app.state.settings.database)
    try:
        person = people.by_email(con, ALICE)
    finally:
        con.close()
    assert person.slot in sizes.PEOPLE_SLOTS
    assert base.FIRST_WORKSPACE > restarted["workspace"]  # clear of the runtime module's ids
