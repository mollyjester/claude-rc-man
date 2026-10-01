"""Enumerating the application's routes."""

from rcman import routes


def test_included_routers_are_enumerated_with_their_prefixes(app):
    found = {(c.path, *sorted(c.methods)) for c in routes.api_routes(app)}
    assert ("/api/health", "GET") in found
    assert ("/api/servers/stop", "POST") in found  # from an included router
    assert ("/api/users/{linux}", "DELETE") in found


def test_only_api_routes_are_enumerated(app):
    assert all(c.path.startswith("/api/") for c in routes.api_routes(app))
