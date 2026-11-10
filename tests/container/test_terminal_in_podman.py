"""The Terminal tab (backend/rcman/terminals.py, routers/terminal.py) against real rootless
Podman: the tests' engine (tests/podman_service.py), with the suites' sizes
(tests/container/sizes.py).

The workspaces run the default workspace image, built on the engine from the installer's build
file, and are made through the app, in its test client, as a person makes them. What the host
runtime's tests cannot show is shown here: the bytes reaching the shell through
``podman --remote exec`` on its own tty, a resize reaching it there, ^P^Q passing through, the
shell running in the person's range as its root, and nothing of the terminal left in the
container or in Podman once it is closed.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from rcman import db, people
from rcman.main import create_app

from tests.conftest import ALICE, ORIGIN, made_workspace
from tests.container import sizes
from tests.container.conftest import engine_runtime, engine_settings
from tests.hang_guard import CONTAINER_HANG_GUARD
from tests.test_terminal import opened


@pytest.fixture(scope="module")
def client(podman_service, default_image, engine_tools, engine_store, tmp_path_factory):
    folder = tmp_path_factory.mktemp("rcman")
    settings = engine_settings(
        folder / "rcman.db", engine_store, public_origin=ORIGIN, tools_dir=engine_tools,
        workspace_image=default_image,
    )  # fmt: skip
    db.migrate(settings.database)
    con = db.connect(settings.database)
    try:
        alice = people.add(con, ALICE, True)
    finally:
        con.close()
    podman = engine_runtime(podman_service, default_image, engine_tools, engine_store, folder)
    app = create_app(settings, runtime=podman, builder_slot=sizes.BUILDER_SLOT)
    with TestClient(app) as made:
        assert made.app.state.workspaces.restored.wait(CONTAINER_HANG_GUARD)
        made.alice, made.engine = alice, podman_service
        try:
            yield made
        finally:
            for workspace in made.app.state.workspaces.everything():
                made.app.state.workspaces.delete(workspace)
            podman.remove_account(alice.id)


@pytest.fixture(scope="module")
def workspace(client):
    return made_workspace(client, ALICE, "terminal")


def test_what_is_typed_reaches_the_shell_and_comes_back(client, workspace):
    with opened(client, ALICE, workspace["id"]) as peer:
        peer.ask("echo $((6*7))", rb"[\r\n]42\r\n")
