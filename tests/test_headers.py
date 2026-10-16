"""The headers every answer of rcman carries so that no page of it is framed or sniffed
(backend/rcman/headers.py), and the limit on a page's reports of failures
(backend/rcman/ratelimit.py, routers/client_errors.py)."""

import pytest
from fastapi import Response
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


def test_every_answer_forbids_framing_and_sniffing(served):
    seen = list(answers(served))
    assert len(seen) >= 10 and {r.status_code for _, r in seen} >= {200, 401, 403, 404, 422}
    for what, response in seen:
        for name, value in headers.SECURITY_HEADERS.items():
            assert response.headers.get(name) == value, (what, name)


def test_the_headers_are_the_ones_that_do_not_break_the_app_or_its_pages():
    found = headers.SECURITY_HEADERS
    assert found["Content-Security-Policy"] == "frame-ancestors 'none'"
    assert found["X-Frame-Options"] == "DENY"
    assert found["X-Content-Type-Options"] == "nosniff"
    assert found["Referrer-Policy"]
    # only framing is limited by the policy: a policy that named scripts or styles would
    # break the pages of the interactive description, which loads its own from elsewhere
    assert set(found["Content-Security-Policy"].split(";")) == {"frame-ancestors 'none'"}


def test_a_header_an_answer_sets_itself_is_not_replaced(settings, runtime, dist):
    app = create_app(settings.model_copy(update={"frontend_dir": dist}), runtime=runtime)

    @app.get("/api/own-policy")
    def own(response: Response):
        response.headers["Referrer-Policy"] = "no-referrer"
        return {}

    app.router.routes.insert(0, app.router.routes.pop())  # before the catch-all of the app
    with TestClient(app) as c:
        found = c.get("/api/own-policy", headers=as_user(ALICE))
    assert found.headers["Referrer-Policy"] == "no-referrer"
    assert found.headers["X-Frame-Options"] == "DENY"


def test_an_answer_to_an_error_nobody_expected_carries_them_too(settings, runtime, dist):
    """Starlette answers those outside the middleware, so the handler adds them itself."""
    app = create_app(settings.model_copy(update={"frontend_dir": dist}), runtime=runtime)

    @app.get("/api/breaks")
    def breaks():
        raise RuntimeError("planted")

    app.router.routes.insert(0, app.router.routes.pop())
    with TestClient(app, raise_server_exceptions=False) as c:
        found = c.get("/api/breaks", headers=as_user(ALICE))
    assert found.status_code == 500 and found.json()["code"] == "internal_error"
    for name, value in headers.SECURITY_HEADERS.items():
        assert found.headers.get(name) == value, name


# --- the limit on a page's reports -----------------------------------------------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_a_bucket_lets_a_burst_through_then_one_each_time_it_has_refilled():
    clock = Clock()
    bucket = ratelimit.TokenBuckets(burst=3, per_second=0.5, clock=clock)
    assert [bucket.take("a") for _ in range(4)] == [True, True, True, False]
    assert bucket.take("b") is True  # another's own
    clock.now += 1.0
    assert bucket.take("a") is False  # half a token
    clock.now += 1.0
    assert bucket.take("a") is True and bucket.take("a") is False
    clock.now += 1000
    assert [bucket.take("a") for _ in range(4)] == [True, True, True, False]  # never more


def test_a_bucket_forgets_those_who_have_been_quiet_and_holds_no_more_than_it_may():
    clock = Clock()
    bucket = ratelimit.TokenBuckets(burst=2, per_second=1.0, clock=clock, most_keys=3)
    for who in "abcd":
        assert bucket.take(who)
    assert len(bucket) <= 3  # a flood of names does not grow it
    clock.now += 100
    bucket.take("e")
    assert len(bucket) == 1  # the ones refilled are forgotten: they would start full


def test_reports_over_the_limit_are_refused_and_not_logged(served, caplog, monkeypatch):
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


def test_the_limit_has_named_constants_and_is_per_person():
    assert client_errors.REPORTS_BURST >= 1 and client_errors.REPORTS_PER_MINUTE >= 1
