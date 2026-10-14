"""The backend for the browser tests: real rcman on the host runtime, fake claude.

``uvicorn --factory tests.e2e_server:create_app`` makes the world
``RCMAN_E2E_DIR`` names and removes it when it shuts down; how each run keeps a
world of its own is in project-knowledge/frontend.md. The world holds rcman's
database with alice (administrator) and bob, the host runtime's workspaces
(backend/rcman/runtime.py) with their tmux sockets, and the fake claude. Signing
in to Claude in a workspace is not there yet, so the world signs in the fake
claude of every workspace of alice's as it is made, and none of bob's.
"""

from __future__ import annotations

import contextlib
import os
import re
from pathlib import Path

from rcman import db, people
from rcman.main import create_app as rcman_app
from rcman.runtime import HostRuntime
from rcman.settings import Settings

from devtools import fake_claude as fake
from devtools import held_folders
from devtools import record_claude as rec

REPO = Path(__file__).resolve().parent.parent
# Who the world lets in, and whether each is an administrator.
PEOPLE = {"alice@example.com": True, "bob@example.com": False}
# Whose Claude account is signed in.
SIGNED_IN = "alice@example.com"
# Every world's name, as frontend/e2e/world.ts makes one: the prefix and a random
# UUID. Worlds a killed run left are cleared by it, so nothing else that begins the
# same way is taken for one.
WORLDS = re.compile(r"rcman-e2e-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def is_world(name: str) -> bool:
    """Whether ``name`` is shaped as a world's."""
    return WORLDS.fullmatch(name) is not None


def sockets(root: Path) -> Path:
    """Each workspace's tmux socket is here: inside the world, so each run has its own."""
    return root / "tmux"


# A world is held, and one a killed run left cleared, as devtools/held_folders.py holds
# and clears a folder; these call it at the time they are called, so a test can replace
# one of them.


def _stop_tmux(root: Path) -> None:
    """Stop the tmux servers on the sockets in the world's own tmux folder."""
    held_folders.stop_tmux(sockets(root))


def _remove(root: Path) -> None:
    """Stop the world's tmux servers and remove it, its lock file last."""
    held_folders.remove(root, stop=lambda world: _stop_tmux(world))


def _lock(root: Path):
    """The world's lock, held while the returned file is open."""
    return held_folders.lock(root)


def clear_abandoned(parent: Path, uid: int | None = None) -> None:
    """Stop and remove every world in ``parent`` that a killed run of ``uid``, by default
    the current user, left; leave everything else, and go on past what cannot be read or
    removed."""
    held_folders.clear_abandoned(
        parent,
        is_world,
        uid=uid,
        remover=lambda world: _remove(world),
        label="e2e_server",
    )


def record_sign_in(database: Path, person: int) -> None:
    """The person's account as a sign-in records it (backend/rcman/accounts.py), which
    a server's start needs besides the credentials file."""
    con = db.connect(database)
    try:
        account = {"oauthAccount": fake.oauth_account(fake.FAKE_ACCOUNT["email"])}
        people.set_claude_account(con, person, account)
    finally:
        con.close()


def _world(root: Path, settings: Settings) -> HostRuntime:
    db.migrate(settings.database)
    con = db.connect(settings.database)
    try:
        for email, admin in PEOPLE.items():
            people.add(con, email, admin)
        signed_in = people.by_email(con, SIGNED_IN)
    finally:
        con.close()
    wrapper = root / "claude"
    wrapper.write_text(f'#!/bin/sh\nFAKE_CLAUDE_READY_SECONDS=0.5 exec {rec.FAKE_CLAUDE} "$@"\n')
    wrapper.chmod(0o755)
    (root / "workspaces").mkdir()
    sockets(root).mkdir()
    runtime = HostRuntime(
        root / "workspaces", sockets(root), wrapper, REPO / "backend" / "rcman" / "tmux.conf"
    )
    # SIGNED_IN's Claude account, as a sign-in leaves it (backend/rcman/accounts.py).
    runtime.ensure_account(signed_in.id, signed_in.slot)
    fake.write_credentials(runtime._account_file(signed_in.id), fake.credentials())
    record_sign_in(settings.database, signed_in.id)
    return runtime


def create_app():
    root = Path(os.environ["RCMAN_E2E_DIR"])
    clear_abandoned(root.parent)
    root.mkdir(mode=0o700)  # refuses a folder that exists, whoever made it
    held = None
    try:
        held = _lock(root)
        return _app(root, held)
    except BaseException:
        try:
            _remove(root)
        finally:
            if held:
                held.close()
        raise


def _app(root: Path, held):
    settings = Settings(
        public_origin=os.environ["RCMAN_PUBLIC_ORIGIN"],
        database=root / "rcman.db",
        allowed_emails=root / "allowed-emails",
        frontend_dir=REPO / "frontend" / "dist",
    )
    app = rcman_app(settings, runtime=_world(root, settings))
    app.state.world_lock = held
    rcman_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(app):
        # uvicorn shuts the app down when it cannot bind, too, so this runs then.
        try:
            async with rcman_lifespan(app) as state:
                yield state
        finally:
            try:
                _remove(root)
            finally:
                held.close()

    app.router.lifespan_context = lifespan
    return app
