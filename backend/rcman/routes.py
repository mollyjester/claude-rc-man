"""The application's API routes, as FastAPI resolves them after including routers."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute, RouteContext, iter_route_contexts

from rcman.identity import admin_user


def api_routes(app: FastAPI) -> list[RouteContext]:
    """Every API route with its full path and methods."""
    return [c for c in iter_route_contexts(app.router.routes) if isinstance(c.route, APIRoute)]


def admin_only(route: APIRoute) -> bool:
    """Whether ``admin_user`` is anywhere in the route's dependency tree."""
    pending = list(route.dependant.dependencies)
    while pending:
        dependant = pending.pop()
        if dependant.call is admin_user:
            return True
        pending.extend(dependant.dependencies)
    return False
