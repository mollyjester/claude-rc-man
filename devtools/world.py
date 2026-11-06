"""The world make screenshots shows: real rcman on the host runtime, with the fake claude.

``create_app`` makes the world in a held folder of its own under /tmp
(devtools/held_folders.py ``held``), holds it for the app's life and removes it, with
the tmux servers in it, when the app shuts down; one a killed run left is cleared by the
next. The world holds rcman's database with alice (administrator) and bob, the host
runtime's workspaces (backend/rcman/runtime.py) with their tmux sockets, and the fake
claude's store. alice's Claude account is signed in, as a sign-in leaves it, and bob's is
not. ``RCMAN_PUBLIC_ORIGIN`` names the origin rcman is served at.

Run as ``python -m devtools.world`` (``serve``), it serves the app on a port of
127.0.0.1 the system gives, prints its origin, and stops when its standard input closes
or on SIGTERM; devtools/screenshots.py runs it so.
"""

from __future__ import annotations

import contextlib
import os
import socket
import sys
import threading
from pathlib import Path

from rcman import db, people
from rcman.main import create_app as rcman_app
from rcman.runtime import HostRuntime
from rcman.settings import Settings

from devtools import fake_claude as fake
from devtools import held_folders
from devtools.fake_store import fake_claude

REPO = Path(__file__).resolve().parent.parent
# What the world's folder under /tmp is named with.
PREFIX = "rcworld-"
# Who the world lets in, and whether each is an administrator.
PEOPLE = {"alice@example.com": True, "bob@example.com": False}
# Whose Claude account is signed in.
SIGNED_IN = "alice@example.com"


def sockets(root: Path) -> Path:
    """Each workspace's tmux socket is here: inside the world, so each run has its own."""
    return root / "tmux"


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
    store = root / "claude"  # the Claude Code store, as settings.claude_store names it
    fake_claude(store, env="FAKE_CLAUDE_READY_SECONDS=0.5")
    (root / "workspaces").mkdir()
    sockets(root).mkdir()
    runtime = HostRuntime(
        root / "workspaces", sockets(root), store, REPO / "backend" / "rcman" / "tmux.conf"
    )
    # SIGNED_IN's Claude account, as a sign-in leaves it (backend/rcman/accounts.py).
    runtime.ensure_account(signed_in.id, signed_in.slot)
    fake.write_credentials(runtime._account_file(signed_in.id), fake.credentials())
    record_sign_in(settings.database, signed_in.id)
    return runtime


def create_app():
    """rcman's app over a world of its own, which it removes when it shuts down; the
    world's folder is ``app.state.world``."""
    holding = contextlib.ExitStack()
    try:
        root = holding.enter_context(held_folders.held(PREFIX, remover=held_folders.remove_tree))
        settings = Settings(
            public_origin=os.environ["RCMAN_PUBLIC_ORIGIN"],
            database=root / "rcman.db",
            allowed_emails=root / "allowed-emails",
            claude_store=root / "claude",
            frontend_dir=REPO / "frontend" / "dist",
        )
        app = rcman_app(settings, runtime=_world(root, settings))
    except BaseException:
        holding.close()
        raise
    app.state.world = root
    rcman_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(app):
        # uvicorn shuts the app down when it cannot bind, too, so this runs then.
        with holding:
            async with rcman_lifespan(app) as state:
                yield state

    app.router.lifespan_context = lifespan
    return app


def serve() -> None:
    """The app on a port of 127.0.0.1 the system gives, its origin printed on a line of its
    own once it listens; it shuts down when standard input closes, or on SIGTERM or SIGINT."""
    import uvicorn
    from rcman.routers.terminal import WEBSOCKETS

    listening = socket.socket()
    listening.bind(("127.0.0.1", 0))
    listening.listen()
    origin = f"http://127.0.0.1:{listening.getsockname()[1]}"
    os.environ["RCMAN_PUBLIC_ORIGIN"] = origin
    server = uvicorn.Server(uvicorn.Config(create_app(), ws=WEBSOCKETS, log_level="warning"))

    def until_stdin_closes() -> None:
        sys.stdin.buffer.read()
        server.should_exit = True

    threading.Thread(target=until_stdin_closes, daemon=True).start()
    # A request made from now on waits in the socket's queue until the app has started.
    print(origin, flush=True)
    server.run(sockets=[listening])


if __name__ == "__main__":
    serve()
