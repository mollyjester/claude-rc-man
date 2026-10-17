"""Enumerating the application's routes."""

from rcman import routes


def test_included_routers_are_enumerated_with_their_prefixes(app):
    found = {(c.path, *sorted(c.methods)) for c in routes.api_routes(app)}
    assert ("/api/health", "GET") in found
    assert ("/api/servers/stop", "POST") in found  # from an included router
    assert ("/api/people/{person}", "DELETE") in found
    assert ("/api/people/{person}/limits", "PUT") in found


def test_every_route_is_under_api_except_the_frontend(app):
    from rcman import spa

    outside = {c.path for c in routes.api_routes(app) if not c.path.startswith("/api/")}
    assert outside == {spa.CATCH_ALL}
    assert routes.websocket_routes(app)  # the control for the next line
    assert all(r.path.startswith("/api/") for r in routes.websocket_routes(app))


def test_websocket_routes_are_enumerated_with_their_prefixes(app):
    assert {r.path for r in routes.websocket_routes(app)} >= {"/api/terminal"}
    assert not {r.path for r in routes.websocket_routes(app)} & {
        c.path for c in routes.api_routes(app)
    }


def test_websocket_routes_finds_one_through_a_router_and_one_direct_and_no_http_route():
    """Planted twin: a websocket route included through a router with a prefix, one on the
    app itself, and an HTTP route, which is not one."""
    from fastapi import APIRouter, FastAPI, WebSocket

    planted = FastAPI()
    router = APIRouter(prefix="/api/through")

    @router.websocket("/router")
    async def through_router(websocket: WebSocket):
        await websocket.close()

    @planted.websocket("/api/direct")
    async def direct(websocket: WebSocket):
        await websocket.close()

    @planted.get("/api/plain")
    def plain():
        return {}

    planted.include_router(router)
    found = {r.path for r in routes.websocket_routes(planted)}
    assert found == {"/api/through/router", "/api/direct"}


def test_admin_only_finds_exactly_the_routes_behind_admin_user():
    """Planted twin: one route behind admin_user, directly or through a router, one not."""
    from fastapi import APIRouter, Depends, FastAPI
    from rcman.identity import admin_user, current_user

    planted = FastAPI()
    router = APIRouter(dependencies=[Depends(admin_user)])

    @router.get("/through-router")
    def through_router():
        return {}

    @planted.get("/direct")
    def direct(identity=Depends(admin_user)):
        return {}

    @planted.get("/plain")
    def plain(identity=Depends(current_user)):
        return {}

    planted.include_router(router)
    found = {c.path for c in routes.api_routes(planted) if routes.admin_only(c.route)}
    assert found == {"/through-router", "/direct"}
