"""The rcman application. Run with ``uvicorn --factory rcman.main:create_app``."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt

from fastapi import FastAPI

from rcman import audit, db, journal, problems, spa, unrecognised
from rcman.helpers_client import AdminRunner, HelperRunner, sudo_admin, sudo_helper
from rcman.routers import (
    attention,
    audit_log,
    claude,
    client_errors,
    docs,
    folders,
    git,
    github,
    me,
    servers,
    users,
)
from rcman.settings import Settings

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


def create_app(
    settings: Settings | None = None,
    helper: HelperRunner | None = None,
    admin: AdminRunner | None = None,
) -> FastAPI:
    settings = settings or Settings()
    for log in (audit.log, client_errors.log, unrecognised.log):
        journal.install(log)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        db.migrate(settings.database)
        _retention(settings)

        async def daily():
            while True:
                await asyncio.sleep(RETENTION_INTERVAL_SECONDS)
                await asyncio.to_thread(_retention, settings)

        task = asyncio.create_task(daily())
        try:
            yield
        finally:
            task.cancel()

    app = FastAPI(
        title="rcman",
        lifespan=lifespan,
        docs_url="/api/openapi",
        openapi_url="/api/openapi.json",
        redoc_url=None,
        swagger_ui_oauth2_redirect_url=None,
    )
    app.state.settings = settings
    app.state.helper = unrecognised.watched(
        helper or sudo_helper(settings.helpers_dir), unrecognised.Watch(settings)
    )
    app.state.admin = admin or sudo_admin(settings.helpers_dir)
    problems.install(app)

    @app.get("/api/health", tags=["health"])
    def health() -> dict:
        return {"status": "ok"}

    for module in (
        me,
        folders,
        git,
        github,
        servers,
        claude,
        users,
        audit_log,
        attention,
        docs,
        client_errors,
    ):
        app.include_router(module.router)
    spa.install(app, settings.frontend_dir)  # last: it answers every path not matched above
    return app
