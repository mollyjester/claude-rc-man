"""Enumerating the application's routes."""

from rcman import routes


def test_included_routers_are_enumerated_with_their_prefixes(app):
    found = {(c.path, *sorted(c.methods)) for c in routes.api_routes(app)}
    assert ("/api/health", "GET") in found
    assert ("/api/servers/stop", "POST") in found  # from an included router
    assert ("/api/users/{linux}", "DELETE") in found


def test_every_route_is_under_api_except_the_frontend(app):
    from rcman import spa

    outside = {c.path for c in routes.api_routes(app) if not c.path.startswith("/api/")}
    assert outside == {spa.CATCH_ALL}
