"""The documentation served to signed-in users."""

from pathlib import Path

from tests.conftest import BOB, as_user

REPO = Path(__file__).resolve().parent.parent


def test_lists_every_page_in_docs(client):
    pages = client.get("/api/docs", headers=as_user(BOB)).json()["pages"]
    slugs = {p["slug"] for p in pages}
    assert slugs == {p.stem for p in (REPO / "docs").glob("*.md")}
    assert all(p["title"] for p in pages)


def test_serves_a_page_as_markdown(client):
    page = client.get("/api/docs/servers", headers=as_user(BOB)).json()
    assert page["title"] == "Servers"
    assert page["markdown"] == (REPO / "docs" / "servers.md").read_text(encoding="utf-8")


def test_unknown_and_escaping_slugs_are_404(client):
    for slug in ("nope", "..%2FREADME", "..", "servers.md"):
        r = client.get(f"/api/docs/{slug}", headers=as_user(BOB))
        assert r.status_code == 404, slug
