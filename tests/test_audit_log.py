"""Reading the audit trail."""

from tests.conftest import ALICE, BOB, as_user, changing


def test_admins_read_the_newest_first_with_paging(client):
    for prefix in ("a1", "a2", "a3"):
        client.put("/api/me/settings", json={"prefix": prefix}, headers=changing(BOB))
    page = client.get("/api/audit", params={"limit": 2}, headers=as_user(ALICE)).json()
    assert [e["action"] for e in page["events"]] == ["settings.update", "settings.update"]
    assert page["events"][0]["id"] > page["events"][1]["id"]
    rest = client.get(
        "/api/audit",
        params={"limit": 2, "before": page["events"][-1]["id"]},
        headers=as_user(ALICE),
    )
    assert len(rest.json()["events"]) == 1


def test_limit_is_bounded(client):
    assert client.get("/api/audit", params={"limit": 0}, headers=as_user(ALICE)).status_code == 422
    assert (
        client.get("/api/audit", params={"limit": 100000}, headers=as_user(ALICE)).status_code
        == 422
    )
