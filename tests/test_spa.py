"""The built frontend, served by the backend."""

import pytest
from fastapi.testclient import TestClient
from rcman.main import create_app


@pytest.fixture
def dist(tmp_path):
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text("<!doctype html><div id=root></div>")
    (root / "assets" / "app.js").write_text("console.log(1)")
    (tmp_path / "secret.txt").write_text("outside")
    return root


@pytest.fixture
def spa_client(settings, runtime, dist):
    app = create_app(settings.model_copy(update={"frontend_dir": dist}), runtime=runtime)
    with TestClient(app) as c:
        yield c


def test_root_serves_the_app(spa_client):
    r = spa_client.get("/")
    assert r.status_code == 200 and "id=root" in r.text
    assert r.headers["content-type"].startswith("text/html")


def test_app_routes_fall_back_to_the_app(spa_client):
    for path in ("/help/workspaces", "/admin/people", "/settings", "/?workspace=1"):
        r = spa_client.get(path)
        assert r.status_code == 200 and "id=root" in r.text, path


def test_built_files_are_served(spa_client):
    r = spa_client.get("/assets/app.js")
    assert r.status_code == 200 and r.text == "console.log(1)"


def test_unknown_api_paths_stay_problems(spa_client):
    r = spa_client.get("/api/nope", headers={"X-Forwarded-Email": "alice@example.com"})
    assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_nothing_outside_the_build_is_served(spa_client):
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/../../secret.txt"):
        r = spa_client.get(path)
        assert "outside" not in r.text, path


def test_without_a_build_the_app_is_a_problem(settings, runtime, tmp_path):
    app = create_app(
        settings.model_copy(update={"frontend_dir": tmp_path / "none"}), runtime=runtime
    )
    with TestClient(app) as c:
        r = c.get("/")
    assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_the_page_is_checked_again_each_time_and_built_files_are_kept(spa_client, dist):
    """index.html names the build's files by their hashes, so a browser must ask again for it,
    or it keeps naming files an upgrade removed; the files under assets/ never change."""
    (dist / "favicon.svg").write_text("<svg/>")
    for path in ("/", "/settings", "/?workspace=1", "/index.html", "/favicon.svg"):
        r = spa_client.get(path)
        assert r.status_code == 200, path
        assert r.headers["cache-control"] == "no-cache", path
    r = spa_client.get("/assets/app.js")
    assert r.status_code == 200 and r.text == "console.log(1)"  # the positive: it is served
    assert r.headers["cache-control"] == "public, max-age=31536000, immutable"
