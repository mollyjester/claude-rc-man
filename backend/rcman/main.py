"""The rcman application. Run with ``uvicorn --factory rcman.main:create_app``, and with
``--ws`` naming routers/terminal.py's WEBSOCKETS, as its unit runs it."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import os
from pathlib import Path

from fastapi import FastAPI

from helpers import seccomp
from rcman import (
    audit,
    claude_versions,
    db,
    headers,
    journal,
    people,
    problems,
    rcman_upgrade,
    spa,
    unrecognised,
)
from rcman.accounts import Accounts
from rcman.gitaccount import GitAccount
from rcman.routers import (
    attention,
    audit_log,
    claude,
    client_errors,
    docs,
    git,
    github,
    me,
    servers,
    terminal,
    workspaces,
)
from rcman.routers import people as people_router
from rcman.routers import rcman_upgrade as rcman_upgrade_router
from rcman.runtime import PodmanConfig, PodmanRuntime, Runtime
from rcman.settings import Settings
from rcman.terminals import Terminals
from rcman.terminals import log as terminals_log
from rcman.workspaces import Workspaces
from rcman.workspaces import log as workspaces_log

RETENTION_INTERVAL_SECONDS = 24 * 60 * 60


def _retention(settings: Settings) -> None:
    con = db.connect(settings.database)
    try:
        audit.apply_retention(
            con,
            now=dt.datetime.now(dt.UTC),
            days=settings.audit_retention_days,
            max_rows=settings.audit_max_rows,
        )
    finally:
        con.close()


def podman_socket(settings: Settings) -> Path:
    """Podman's socket: the setting's, or rcman's own user manager's."""
    if settings.podman_socket is not None:
        return settings.podman_socket
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime_dir) / "podman" / "podman.sock"


def podman_runtime(settings: Settings) -> PodmanRuntime:
    return PodmanRuntime(
        PodmanConfig(
            socket=podman_socket(settings),
            image=settings.workspace_image,
            builder_image=settings.builder_image,
            scratch_dir=settings.database.parent,
            tools_dir=settings.tools_dir,
            claude_store=settings.claude_store,
        )
    )


def _start(settings: Settings) -> None:
    """Migrate the database, write the allowed list from it, and apply audit retention."""
    db.migrate(settings.database)
    con = db.connect(settings.database)
    try:
        people.write_allowed(con, settings.allowed_emails)
    finally:
        con.close()
    _retention(settings)


def check_seccomp(tools: Path, default: Path = seccomp.PODMAN_DEFAULT) -> None:
    """Say in the journal, loudly, when the workspaces' seccomp profile in the tools is missing or
    is not the one derived from Podman's current default, as after an upgrade of Podman's
    profile. Missing, no workspace can be made, since Podman refuses a profile that is not there;
    stale, workspaces are made with the profile that is there. Either way until the installer
    runs again."""
    why = seccomp.stale(tools, default)
    if why is not None:
        workspaces_log.error(
            "%s: run the installer again, which derives it from Podman's profile", why
        )


def check_instructions(runtime: Runtime) -> None:
    """Say in the journal when the instructions for Claude the runtime binds into every workspace
    are missing from the tools: no workspace can be made, nor a lost container made again, until
    the installer runs again."""
    why = runtime.instructions_ready()
    if why is not None:
        workspaces_log.error("%s", why)


def create_app(
    settings: Settings | None = None,
    runtime: Runtime | None = None,
    *,
    builder_slot: int = people.BUILDER_SLOT,
) -> FastAPI:
    settings = settings or Settings()
    for log in (
        audit.log,
        client_errors.log,
        unrecognised.log,
        workspaces_log,
        claude_versions.log,
        terminals_log,
    ):
        journal.install(log)
    if runtime is None:  # rcman's own Podman, whose workspaces take the profile
        check_seccomp(settings.tools_dir)
    service = Workspaces(settings, runtime or podman_runtime(settings), builder_slot=builder_slot)
    check_instructions(service.runtime)
    sign_ins = Accounts(service)
    terminals = Terminals(service)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        _start(settings)
        service.versions.reconcile()  # the approved version is current, before any server starts
        terminals.bind(asyncio.get_running_loop())  # before restore, which sweeps through it
        service.start_background()  # restore: HTTP answers meanwhile

        async def daily():
            while True:
                await asyncio.sleep(RETENTION_INTERVAL_SECONDS)
                await asyncio.to_thread(_retention, settings)

        task = asyncio.create_task(daily())
        try:
            yield
        finally:
            task.cancel()
            # uvicorn closes every WebSocket before this; whatever is left is closed here, and
            # nothing that goes wrong with it keeps the rest from closing
            try:
                await app.state.terminals.close_all(audit.SERVICE_CLOSING)
            except Exception:  # noqa: BLE001 - logged; the restore's sweep ends what is left
                workspaces_log.exception("the terminals did not all close")
            finally:
                await asyncio.to_thread(sign_ins.close)
                await asyncio.to_thread(service.close)

    app = FastAPI(
        title="rcman",
        lifespan=lifespan,
        docs_url="/api/openapi",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
    )
    app.state.settings = settings
    app.state.workspaces = service
    app.state.versions = service.versions
    app.state.accounts = sign_ins
    app.state.terminals = terminals
    app.state.git_account = GitAccount(service)
    service.restore_hooks.append(app.state.git_account.write_gh_for_everyone)
    app.state.watch = unrecognised.Watch()
    app.state.client_error_limit = client_errors.new_limit()
    # The release rcman runs is read now, once: an upgrade switches the release meanwhile.
    app.state.upgrade = rcman_upgrade.Upgrades(settings, service)
    service.upgrading = app.state.upgrade.under_way
    app.state.upgrade_limit = rcman_upgrade_router.new_limit()
    problems.install(app)
    headers.install(app)

    @app.get("/api/health", tags=["health"])
    def health() -> dict:
        return {"status": "ok"}

    for module in (
        me,
        workspaces,
        servers,
        git,
        github,
        claude,
        rcman_upgrade_router,
        people_router,
        audit_log,
        attention,
        docs,
        client_errors,
        terminal,
    ):
        app.include_router(module.router)
    spa.install(app, settings.frontend_dir)  # last: it answers every path not matched above
    return app
