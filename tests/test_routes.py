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
