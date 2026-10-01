"""The application's API routes, as FastAPI resolves them after including routers."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.routing import APIRoute, RouteContext, iter_route_contexts


def api_routes(app: FastAPI) -> list[RouteContext]:
    """Every API route with its full path and methods."""
    return [c for c in iter_route_contexts(app.router.routes) if isinstance(c.route, APIRoute)]
