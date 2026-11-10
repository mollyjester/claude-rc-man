"""Who is asking: the proxy's email header, the people table, the origin."""

from pathlib import Path

from rcman import db, identity, people, routes

from tests.conftest import ALICE, BOB, CALLS, ORIGIN, as_user, changing, concrete

REPO = Path(__file__).resolve().parent.parent


def test_only_an_active_person_is_let_in_by_their_address_in_any_case(client, settings):
    assert client.get("/api/me", headers=as_user("ALICE@Example.com")).json()["email"] == ALICE
    r = client.get("/api/me", headers=as_user("stranger@example.com"))
    assert (r.status_code, r.json()["code"]) == (403, "not_allowed")
    assert client.get("/api/me", headers=as_user(BOB)).status_code == 200
    con = db.connect(settings.database)
    try:
        people.remove(con, people.by_email(con, BOB))
    finally:
        con.close()
    assert client.get("/api/me", headers=as_user(BOB)).status_code == 403


KNOWN_ADMIN_ROUTES = {
    ("GET", "/api/audit"),
    ("GET", "/api/people"),
    ("POST", "/api/people"),
    ("DELETE", "/api/people/{person}"),
    ("GET", "/api/people/{person}/limits"),
    ("PUT", "/api/people/{person}/limits"),
    ("PUT", "/api/people/{person}/workspaces/{workspace}/limits"),
    ("GET", "/api/claude/versions"),
    ("GET", "/api/claude/latest"),
    ("POST", "/api/claude/upgrade"),
    ("PUT", "/api/claude/failures"),
    ("DELETE", "/api/claude/failures/{version}"),
    ("GET", "/api/servers/everyone"),
    ("GET", "/api/attention"),
    ("GET", "/api/monitor"),
    ("GET", "/api/monitor/settings"),
    ("PUT", "/api/monitor/settings"),
    ("GET", "/api/forwarding/settings"),
    ("PUT", "/api/forwarding/settings"),
}


def admin_routes(app):
    return {
        (method, c.path)
        for c in routes.api_routes(app)
        if routes.admin_only(c.route)
        for method in c.methods
    }


def admitting(client, found, calls) -> list[tuple[str, str]]:
    """The routes of ``found`` that answer someone who is not an administrator with anything
    but admin_only."""
    let_in = []
    for method, path in sorted(found):
        r = client.request(method, concrete(path), headers=changing(BOB), **calls[(method, path)])
        if (r.status_code, r.json().get("code")) != (403, "admin_only"):
            let_in.append((method, path))
    return let_in


def test_admin_routes_refuse_non_admins(client, app):
    found = admin_routes(app)
    assert KNOWN_ADMIN_ROUTES <= found  # non-vacuity: every known one is found
    assert ("GET", "/api/me") not in found and ("GET", "/api/workspaces") not in found
    assert found <= set(CALLS)  # every admin route has a valid request to drive it
    assert admitting(client, found, CALLS) == []
    assert client.get("/api/audit", headers=as_user(ALICE)).status_code == 200


def test_the_admin_check_finds_an_admin_route_that_lets_anyone_in(app):
    """Planted twin: a route that depends on admin_user, whose check is overridden to let
    anyone signed in through, is found."""
    from fastapi import Depends
    from fastapi.testclient import TestClient
    from rcman.identity import admin_user, current_user

    @app.post("/api/planted-admin")  # a GET would be the pages' catch-all's
    def planted(identity=Depends(admin_user)):
        return {}

    planted_route = ("POST", "/api/planted-admin")
    assert planted_route in admin_routes(app)
    with TestClient(app) as client:
        calls = {planted_route: {}}
        assert admitting(client, {planted_route}, calls) == []  # the control: it refuses
        app.dependency_overrides[admin_user] = current_user
        try:
            assert admitting(client, {planted_route}, calls) == [planted_route]
        finally:
            app.dependency_overrides.clear()


def test_changes_need_the_public_origin(client):
    body = {"prefix": "abc"}
    headers = as_user(ALICE)
    r = client.patch(
        "/api/me/settings", json=body, headers={**headers, "Origin": "https://evil.example.com"}
    )
    assert (r.status_code, r.json()["code"]) == (403, "origin_refused")
    assert (
        client.patch(
            "/api/me/settings", json=body, headers={**headers, "Origin": ORIGIN}
        ).status_code
        == 200
    )


def unnamed_navigations(app, named: dict) -> list[str]:
    """The routes that record an audit row, and so change something, although their method
    carries no Origin, that ``named`` does not name; and the names no such route has."""
    from rcman import audit

    recording_reads = {
        (method, c.path)
        for c in routes.api_routes(app)
        if audit.action_of(c.route) is not None
        for method in c.methods
        if method not in identity.CHANGING
    }
    unnamed = [f"{m} {p}: changes something with no Origin" for m, p in
               sorted(recording_reads - set(named))]  # fmt: skip
    stale = [f"{m} {p}: no such route" for m, p in sorted(set(named) - recording_reads)]
    return unnamed + stale


def test_every_change_without_an_origin_is_a_named_navigation(app):
    assert identity.NAVIGATIONS  # non-vacuity: the hand-off is one
    assert unnamed_navigations(app, identity.NAVIGATIONS) == []
    guide = (REPO / "project-knowledge" / "backend.md").read_text(encoding="utf-8")
    assert all(reason in guide for reason in identity.NAVIGATIONS.values())


def test_the_navigation_check_finds_an_unnamed_one_and_a_stale_name(app):
    """Planted twin: a GET that records, not named, is found; and so is a name of no route."""
    from fastapi import Depends
    from rcman import audit

    @app.get("/api/planted-recording-read")
    def planted(record=Depends(audit.Recorder("planted.read"))):
        return {}

    assert unnamed_navigations(app, identity.NAVIGATIONS) == [
        "GET /api/planted-recording-read: changes something with no Origin"
    ]
    stale = {**identity.NAVIGATIONS, ("GET", "/api/planted-gone"): "planted"}
    assert "GET /api/planted-gone: no such route" in unnamed_navigations(app, stale)


# --- the upgrade to a WebSocket ---------------------------------------------------------------


def upgrades_without_the_origin(client, paths) -> list[str]:
    """The WebSocket routes of ``paths`` that accept an upgrade from a signed-in person without
    the public origin, or with another: a WebSocket counts as a change, since a page of any
    site may open one and the browser sends its cookies with it."""
    from starlette.testclient import WebSocketDenialResponse

    found = []
    for path in paths:
        for origin in (None, "https://evil.example.com"):
            headers = as_user(ALICE) | ({"Origin": origin} if origin else {})
            try:
                with client.websocket_connect(f"{path}?workspace=1", headers=headers):
                    pass
            except WebSocketDenialResponse as denied:
                if (denied.status_code, denied.json()["code"]) == (403, "origin_refused"):
                    continue
            found.append(f"{path} {origin}")
    return found


def test_every_websocket_upgrade_needs_the_public_origin(app, client):
    paths = [r.path for r in routes.websocket_routes(app)]
    assert paths  # non-vacuity
    assert upgrades_without_the_origin(client, paths) == []
    # the control: with the public origin the upgrade is accepted
    with client.websocket_connect(f"{paths[0]}?workspace=1", headers=changing(ALICE)):
        pass


def test_the_origin_check_finds_a_websocket_route_that_takes_any_origin(app):
    """Planted twin: a route reading the identity header itself takes any origin, and is
    found; one depending on current_user is not."""
    from fastapi import Depends, WebSocket
    from fastapi.testclient import TestClient
    from rcman.identity import current_user

    @app.websocket("/api/planted-own-identity")
    async def own_identity(websocket: WebSocket):
        assert websocket.headers.get("x-forwarded-email")
        await websocket.accept()
        await websocket.close()

    @app.websocket("/api/planted-current-user")
    async def checked(websocket: WebSocket, identity=Depends(current_user)):
        await websocket.accept()
        await websocket.close()

    with TestClient(app) as client:
        found = upgrades_without_the_origin(
            client, ["/api/planted-own-identity", "/api/planted-current-user"]
        )
    assert found == [
        "/api/planted-own-identity None",
        "/api/planted-own-identity https://evil.example.com",
    ]
