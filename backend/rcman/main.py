"""The rcman application. Run with ``uvicorn --factory rcman.main:create_app``."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import os
from pathlib import Path

from fastapi import FastAPI

from rcman import audit, claude_versions, db, journal, people, problems, spa, unrecognised
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
    workspaces,
)
from rcman.routers import people as people_router
from rcman.runtime import PodmanConfig, PodmanRuntime, Runtime
from rcman.settings import Settings
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


def create_app(settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    settings = settings or Settings()
    for log in (
        audit.log,
        client_errors.log,
        unrecognised.log,
        workspaces_log,
        claude_versions.log,
    ):
        journal.install(log)
    service = Workspaces(settings, runtime or podman_runtime(settings))
    sign_ins = Accounts(service)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        _start(settings)
        service.versions.reconcile()  # the approved version is current, before any server starts
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
    app.state.git_account = GitAccount(service)
    app.state.watch = unrecognised.Watch()
    problems.install(app)

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
        people_router,
        audit_log,
        attention,
        docs,
        client_errors,
    ):
        app.include_router(module.router)
    spa.install(app, settings.frontend_dir)  # last: it answers every path not matched above
    return app
