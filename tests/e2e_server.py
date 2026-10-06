"""The backend for the browser tests: real rcman, real rc-helper, fake claude.

``uvicorn --factory tests.e2e_server:create_app`` makes the world
``RCMAN_E2E_DIR`` names and removes it when it shuts down; how each run keeps a
world of its own is in project-knowledge/frontend.md. The world holds a
users file with alice (administrator, Claude connected) and bob (Claude not
connected; the code ``GOOD-CODE`` connects him), a home and a tmux socket for
each, and the fake claude. alice also has a git
repository, projects/alice-repo, whose origin is
https://github.com/alice/alice-repo.git: it holds main and feature, and
later, which alice's repository has not fetched; https://github.com/alice/demo,
which holds main and feature, is in no home, to be cloned. While the backend runs, the
world's GitHub (tests/github_server.py) serves the world's origins/ and takes
a push with the token in ``RCMAN_E2E_GITHUB_TOKEN``; rc-helper's
``RC_HELPER_GITHUB`` points https://github.com/ at it. rc-helper runs for
real as the current user against those homes. rc-admin needs root, so a
stand-in manages the users file itself and reports versions; the container
suite tests the real one.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import subprocess
from pathlib import Path

from rcman.main import create_app as rcman_app
from rcman.settings import Settings

from devtools import fake_claude as fake
from devtools import held_folders
from devtools import record_claude as rec
from helpers import rc_admin as ra
from helpers import users_file as uf
from tests.git_fixtures import make_repo, push_branch_to_origin
from tests.github_server import UNREACHABLE, GitHubServer
from tests.helper_env import helper_env

REPO = Path(__file__).resolve().parent.parent
PEOPLE = {"alice": ("alice@example.com", True), "bob": ("bob@example.com", False)}
GOOD_CODE = "GOOD-CODE"
# Every world's name, as frontend/e2e/world.ts makes one: the prefix and a random
# UUID. Worlds a killed run left are cleared by it, so nothing else that begins the
# same way is taken for one.
WORLDS = re.compile(r"rcman-e2e-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def is_world(name: str) -> bool:
    """Whether ``name`` is shaped as a world's."""
    return WORLDS.fullmatch(name) is not None


def sockets(root: Path) -> Path:
    """Each person's tmux socket is here: inside the world, so each run has its own."""
    return root / "tmux"


def origins(root: Path) -> Path:
    """The bare repositories the world's GitHub serves, as <owner>/<name>.git."""
    return root / "origins"


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


def _world(root: Path) -> dict[str, Path]:
    homes = {}
    for linux in PEOPLE:
        home = root / "homes" / linux
        for folder in ("alpha", "beta"):
            (home / "projects" / f"{linux}-{folder}").mkdir(parents=True)
        homes[linux] = home
    repo = homes["alice"] / "projects" / "alice-repo"
    origin = origins(root) / "alice" / "alice-repo.git"
    make_repo(repo, origin=origin, url=GitHubServer.github_url("alice", "alice-repo"))
    push_branch_to_origin(repo, origin, "later")
    # A repository on the world's GitHub that no home holds, to clone.
    make_repo(root / "sources" / "demo", origin=origins(root) / "alice" / "demo.git")
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
    homes = _world(root)
    sockets(root).mkdir()
    github = GitHubServer()

    def helper(linux, args, stdin=None, timeout=60):
        sock = sockets(root) / f"{linux}.sock"
        env = helper_env(homes[linux], root / "claude", sock, github.url or UNREACHABLE)
        proc = subprocess.run(
            [str(REPO / "helpers" / "rc-helper"), *args],
            env=env, input=stdin, capture_output=True, text=True, timeout=timeout,
        )  # fmt: skip
        return proc.returncode, json.loads(proc.stdout)

    def admin(argv, timeout=None):
        # Read as rc-admin reads its command line, so the backend's argv is held to it.
        args = ra.parser("rc-admin", requester=True).parse_args(argv)
        users = uf.load(root / "users.toml")
        if args.verb == "list":
            return 0, {
                "users": [
                    {"linux": u.linux, "email": u.email, "admin": u.admin, "account": True}
                    for u in users
                ]
            }
        if args.verb == "add":
            users = [u for u in users if u.linux != args.linux_user] + [
                uf.User(args.email, args.linux_user, args.admin)
            ]
            uf.save(root / "users.toml", users)
            return 0, {"user": {"linux": args.linux_user}, "created": True}
        if args.verb == "del":
            uf.save(root / "users.toml", [u for u in users if u.linux != args.linux_user])
            return 0, {"deleted": args.linux_user, "home_deleted": args.delete}
        version = rec.load_manifest()["claude_version"]
        if args.verb == "claude-status":
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
                "report": {
                    "version": {
                        "lines": ["ran: claude --version (on a terminal); exit code 0", "0.0.0"],
                        "more": 0,
                    }
                },
            }
        }

    settings = Settings(
        public_origin=os.environ["RCMAN_PUBLIC_ORIGIN"],
        users_file=root / "users.toml",
        database=root / "rcman.db",
        frontend_dir=REPO / "frontend" / "dist",
    )
    app = rcman_app(settings, helper=helper, admin=admin)
    app.state.github = github
    app.state.world_lock = held
    rcman_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(app):
        # uvicorn shuts the app down when it cannot bind, too, so this runs then.
        try:
            github.start(origins(root), os.environ["RCMAN_E2E_GITHUB_TOKEN"])
            try:
                async with rcman_lifespan(app) as state:
                    yield state
            finally:
                github.stop()
        finally:
            try:
                _remove(root)
            finally:
                held.close()

    app.router.lifespan_context = lifespan
    return app
