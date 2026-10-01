"""Every error is RFC 9457 problem details."""

from rcman import problems, routes

from tests.conftest import ALICE, as_user, changing


def assert_problem(response, status, code):
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert body["status"] == status
    assert body["code"] == code
    assert body["type"] == f"urn:rcman:problem:{code}"
    assert body["title"]
    return body


def test_unknown_route_is_a_problem(client):
    assert_problem(client.get("/api/nope", headers=as_user(ALICE)), 404, "not_found")


def test_wrong_method_is_a_problem(client):
    assert_problem(client.delete("/api/me", headers=changing(ALICE)), 405, "method_not_allowed")


def test_validation_errors_are_problems_with_field_errors(client):
    body = assert_problem(
        client.post("/api/servers", json={}, headers=changing(ALICE)), 422, "validation_failed"
    )
    assert body["errors"] and body["errors"][0]["field"] == "path"
    assert body["errors"][0]["message"]


def test_every_route_answers_a_missing_identity_with_a_problem(app, client):
    found = [c for c in routes.api_routes(app) if c.path not in problems.PUBLIC_PATHS]
    assert found  # non-vacuity
    for route in found:
        method = sorted(route.methods)[0]
        path = route.path.replace("{linux}", "x").replace("{slug}", "x")
        response = client.request(method, path)
        assert_problem(response, 401, "not_authenticated")


def test_every_problem_code_has_a_status_and_a_message():
    assert set(problems.STATUS) == set(problems.MESSAGES)
