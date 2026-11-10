"""After every container has stopped and rcman starts again, as after a machine restarts, rcman
brings back its workspaces and their servers, and sets the limits again before it starts them.

On the tests' engine (tests/podman_service.py): rcman's app (backend/rcman/main.py) with the Podman
runtime at the suites' sizes (tests/container/sizes.py), the default image, and the fake `claude`
signed in, in its test client; and ``systemctl`` the stand-in (tests/container/stand_ins.py), which
keeps the slices' properties as the user manager does until a restart. The module makes a workspace
of alice's, starts its server and sets limits on her and on the workspace; then rcman stops, every
container on the engine is stopped, the stand-in forgets what a restart forgets, and rcman starts
again. A second workspace is caught by the restart in the middle of a rebuild, its container
replaced by one of another image while its row says the container is being switched. What a
restart of the machine itself does to systemd and the kernel is checked by hand
(project-knowledge/real-claude-checklist.md).
"""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient
from rcman import db, people
from rcman import runtime as rt
from rcman.main import create_app

from devtools import fake_claude as fake
from devtools.world import record_sign_in
from tests.conftest import ALICE, ORIGIN, as_user, changing, made_workspace
from tests.container import sizes
from tests.container.conftest import engine_runtime, engine_settings, image_of, stand_in
from tests.hang_guard import CONTAINER_HANG_GUARD
from tests.until import SLOW, until

PERSON_LIMITS = {"memory": "4G", "cpu": "3", "pids": "6000"}
WORKSPACE_LIMITS = {"memory": "2G", "cpu": "1.5", "pids": "3000"}


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


@pytest.fixture(scope="module")
def restarted(
    podman_service, default_image, builder_image, engine_tools, fake_store, tmp_path_factory
):
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
        assert client.app.state.workspaces.restored.wait(CONTAINER_HANG_GUARD)
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
        until(lambda: server_state(client, workspace["id"]) in ("ready", "connected"), SLOW)
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
        # A rebuild cut short while it replaced the container: the old one discarded, a new one
        # made from another image, and the row in the phase that says so.
        service = client.app.state.workspaces
        beta = made_workspace(client, ALICE, "beta")
        beta_name = rt.container_name(beta["id"])
        old_image = image_of(podman, beta_name)
        podman.discard(beta_name)
        other = dataclasses.replace(
            service._spec(service.get(beta["id"]), alice.slot), image=builder_image, image_id=""
        )
        podman.create(other)
        assert image_of(podman, beta_name) not in (None, old_image)  # the plant took effect
        service._set(beta["id"], rebuild_state="switching")

    # The machine restarts: every container stops, and what the user manager held is gone.
    stopped = podman_service.cli("stop", "--all", "--time=10", timeout=CONTAINER_HANG_GUARD)
    assert stopped.returncode == 0, stopped.stderr
    assert not podman.running(name)  # positive control: the workspace is down
    reboot = stand_in(folder, "reboot", state)
    assert subprocess.run([str(reboot)], capture_output=True).returncode == 0
    rebooted = time.time()

    with app() as client:
        assert client.app.state.workspaces.restored.wait(CONTAINER_HANG_GUARD)
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
            "beta": beta["id"],
            "beta_image": old_image,
        }
        for made in client.app.state.workspaces.everything():
            client.app.state.workspaces.delete(made)
        podman.remove_account(alice.id)


def test_the_workspace_runs_again(restarted):
    until(lambda: restarted["podman"].running(restarted["name"]), SLOW)
    assert started(restarted["engine"], restarted["name"]) > restarted["rebooted"]
