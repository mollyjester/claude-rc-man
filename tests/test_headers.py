"""The headers every answer of rcman carries so that no page of it is framed or sniffed
(backend/rcman/headers.py), and the limit on a page's reports of failures
(backend/rcman/ratelimit.py, routers/client_errors.py)."""

import pytest
from fastapi.testclient import TestClient
from rcman import headers, ratelimit
from rcman.main import create_app
from rcman.routers import client_errors

from tests.conftest import ALICE, BOB, CALLS, as_user, changing

BODY = CALLS[("POST", "/api/client-errors")]["json"]


@pytest.fixture
def dist(tmp_path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><div id=root></div>")
    (root / "assets" / "app.js").write_text("console.log(1)")
    return root


@pytest.fixture
def served(settings, runtime, dist):
    app = create_app(settings.model_copy(update={"frontend_dir": dist}), runtime=runtime)
    # The limit on a page's reports on a clock that does not move: none refills meanwhile.
    app.state.client_error_limit = ratelimit.TokenBuckets(
        client_errors.REPORTS_BURST,
        client_errors.REPORTS_PER_MINUTE / client_errors.SECONDS_PER_MINUTE,
        clock=lambda: 1000.0,
    )
    with TestClient(app) as c:
        yield c


def answers(client):
    """Answers of every kind: the application, a file, an error, a refusal, a page of the API's
    own description."""
    yield "health", client.get("/api/health")
    yield "page", client.get("/")
    yield "page of the app", client.get("/help/workspaces")
    yield "file", client.get("/assets/app.js")
    yield "problem", client.get("/api/nope", headers=as_user(ALICE))
    yield "not signed in", client.get("/api/me")
    yield "refused origin", client.post("/api/client-errors", json=BODY, headers=as_user(ALICE))
    yield "invalid", client.post("/api/client-errors", json={}, headers=changing(ALICE))
    yield "description", client.get("/api/openapi.json")
    yield "interactive description", client.get("/api/openapi")
    yield "refused upgrade", refused_upgrade(client, {})
    yield "upgrade from elsewhere", refused_upgrade(client, as_user(ALICE))
    yield "invalid upgrade", refused_upgrade(client, changing(ALICE), "")


def refused_upgrade(client, headers, query="?workspace=1"):
    """The answer to an upgrade to a WebSocket refused before it is accepted."""
    from starlette.testclient import WebSocketDenialResponse

    try:
        with client.websocket_connect("/api/terminal" + query, headers=headers):
            pass
    except WebSocketDenialResponse as denied:
        return denied
    raise AssertionError("the upgrade was accepted")


def test_every_answer_forbids_framing_and_sniffing(served):
    seen = list(answers(served))
    assert len(seen) >= 13 and {r.status_code for _, r in seen} >= {200, 401, 403, 404, 422}
    for what, response in seen:
        for name, value in headers.SECURITY_HEADERS.items():
            assert response.headers.get(name) == value, (what, name)


# --- the limit on a page's reports -----------------------------------------------------------


def test_reports_over_the_limit_are_refused_and_not_logged(served, caplog):
    caplog.set_level("INFO", logger=client_errors.log.name)
    limit = served.app.state.client_error_limit
    for _ in range(limit.burst):
        assert (
            served.post("/api/client-errors", json=BODY, headers=changing(ALICE)).status_code == 200
        )
    logged = len([r for r in caplog.records if r.name == client_errors.log.name])
    assert logged == limit.burst  # the control: every report within the limit was logged
    over = served.post("/api/client-errors", json=BODY, headers=changing(ALICE))
    assert (over.status_code, over.json()["code"]) == (429, "too_many_reports")
    assert over.headers["Retry-After"].isdigit()
    assert len([r for r in caplog.records if r.name == client_errors.log.name]) == logged
    assert served.post("/api/client-errors", json=BODY, headers=changing(BOB)).status_code == 200
