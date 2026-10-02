"""The backend for the browser tests: real rcman, real rc-helper, fake claude.

``uvicorn --factory tests.e2e_server:create_app`` builds a throwaway world in
``RCMAN_E2E_DIR``: a users file with alice (administrator, Claude connected)
and bob (Claude not connected; the code ``GOOD-CODE`` connects him), a home
and a tmux socket for each, and the fake claude. rc-helper runs for real as
the current user against those homes. rc-admin needs root, so a stand-in
manages the users file itself and reports versions; the container suite
tests the real one.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from rcman.main import create_app as rcman_app
from rcman.settings import Settings

from devtools import fake_claude as fake
from devtools import record_claude as rec
from helpers import users_file as uf

REPO = Path(__file__).resolve().parent.parent
PEOPLE = {"alice": ("alice@example.com", True), "bob": ("bob@example.com", False)}
GOOD_CODE = "GOOD-CODE"


def sockets(root: Path) -> Path:
    """Each person's tmux socket is here; inside the world, so every run uses the same ones."""
    return root / "tmux"


def _stop_tmux(root: Path) -> None:
    # tmux detaches from whatever started it, so ending the backend leaves it running.
    for sock in sockets(root).glob("*.sock"):
        tmux = ["tmux", "-S", str(sock)]
        pid = subprocess.run(
            [*tmux, "display-message", "-p", "#{pid}"], capture_output=True, text=True
        ).stdout.strip()
        subprocess.run([*tmux, "kill-server"], capture_output=True)
        # kill-server returns before the server has gone.
        deadline = time.monotonic() + 10
        while pid and Path(f"/proc/{pid}").exists() and time.monotonic() < deadline:
            time.sleep(0.05)


def _world(root: Path) -> dict[str, Path]:
    _stop_tmux(root)  # a run that was killed before it could shut down
    if root.exists():
        shutil.rmtree(root)
    homes = {}
    for linux in PEOPLE:
        home = root / "homes" / linux
        for folder in ("alpha", "beta"):
            (home / "projects" / f"{linux}-{folder}").mkdir(parents=True)
        homes[linux] = home
    fake.write_state(homes["alice"] / ".claude-fake", logged_in=True)
    fake.write_state(homes["bob"] / ".claude-fake", logged_in=False, accept_codes=[GOOD_CODE])
    uf.save(
        root / "users.toml",
        [uf.User(email, linux, admin) for linux, (email, admin) in PEOPLE.items()],
    )
    wrapper = root / "claude"
    wrapper.write_text(f'#!/bin/sh\nFAKE_CLAUDE_READY_SECONDS=0.5 exec {rec.FAKE_CLAUDE} "$@"\n')
    wrapper.chmod(0o755)
    return homes


def create_app():
    root = Path(os.environ["RCMAN_E2E_DIR"])
    homes = _world(root)
    sockets(root).mkdir()

    def helper(linux, args, stdin=None, timeout=60):
        env = {
            "PATH": "/usr/bin:/bin",
            "RC_HELPER_HOME": str(homes[linux]),
            "RC_HELPER_CLAUDE": str(root / "claude"),
            "RC_HELPER_TMUX_SOCKET": str(sockets(root) / f"{linux}.sock"),
        }
        proc = subprocess.run(
            [str(REPO / "helpers" / "rc-helper"), *args],
            env=env, input=stdin, capture_output=True, text=True, timeout=timeout,
        )  # fmt: skip
        return proc.returncode, json.loads(proc.stdout)

    def admin(args):
        users = uf.load(root / "users.toml")
        if args[0] == "list":
            return 0, {
                "users": [
                    {"linux": u.linux, "email": u.email, "admin": u.admin, "account": True}
                    for u in users
                ]
            }
        if args[0] == "add":
            users = [u for u in users if u.linux != args[1]] + [
                uf.User(args[2], args[1], "--admin" in args)
            ]
            uf.save(root / "users.toml", users)
            return 0, {"user": {"linux": args[1]}, "created": True}
        if args[0] == "del":
            uf.save(root / "users.toml", [u for u in users if u.linux != args[1]])
            return 0, {"deleted": args[1], "home_deleted": "--delete" in args}
        version = rec.load_manifest()["claude_version"]
        if args[0] == "claude-status":
            return 0, {
                "approved": version,
                "tested": version,
                "users": [{"linux": u.linux, "installed": version} for u in users],
            }
        return 1, {
            "error": {
                "code": "claude_checks_failed",
                "message": "That version failed rcman's checks.",
                "failed": ["version"],
            }
        }

    settings = Settings(
        public_origin=os.environ["RCMAN_PUBLIC_ORIGIN"],
        users_file=root / "users.toml",
        database=root / "rcman.db",
        frontend_dir=REPO / "frontend" / "dist",
    )
    app = rcman_app(settings, helper=helper, admin=admin)
    rcman_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with rcman_lifespan(app) as state:
            try:
                yield state
            finally:
                _stop_tmux(root)

    app.router.lifespan_context = lifespan
    return app
