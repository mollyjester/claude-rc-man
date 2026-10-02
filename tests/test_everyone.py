"""Everyone's servers, for administrators: one rc-helper call per person, side by side."""

import threading
import time
from types import SimpleNamespace

import pytest
from rcman import everyone

from tests.conftest import ALICE, BOB, CAROL, changing


def request_for(app):
    return SimpleNamespace(app=app)


def by_linux(entries):
    return {e["linux"]: e for e in entries}


def test_every_person_is_listed_with_their_servers(app, client):
    r = client.post(
        "/api/servers", json={"path": "projects/alice-project"}, headers=changing(ALICE)
    )
    assert r.status_code == 200, r.text
    found = by_linux(everyone.everyone(request_for(app)))
    assert set(found) == {"alice", "bob", "carol"}
    assert found["alice"]["email"] == ALICE
    assert [s["path"] for s in found["alice"]["servers"]] == ["projects/alice-project"]
    assert found["bob"] == {"linux": "bob", "email": BOB, "servers": []}
    assert found["carol"] == {"linux": "carol", "email": CAROL, "servers": []}


def make_app(settings, runner, admin_runner):
    from rcman.main import create_app

    return create_app(settings, helper=runner, admin=admin_runner)


def test_one_failing_person_does_not_hide_the_others(settings, admin_runner):
    def runner(linux, args, stdin=None, timeout=None):
        if linux == "bob":
            return 1, {"error": {"code": "claude_not_installed", "message": "No claude here."}}
        return 0, {"servers": [{"path": f"projects/{linux}-project", "state": "ready"}]}

    found = by_linux(everyone.everyone(request_for(make_app(settings, runner, admin_runner))))
    assert found["bob"] == {
        "linux": "bob",
        "email": BOB,
        "error": {"code": "claude_not_installed", "title": "No claude here."},
    }
    assert found["alice"]["servers"][0]["path"] == "projects/alice-project"
    assert found["carol"]["servers"][0]["path"] == "projects/carol-project"


def test_one_slow_person_does_not_stall_the_rest(settings, admin_runner, monkeypatch):
    monkeypatch.setattr(everyone, "EVERYONE_TIMEOUT_SECONDS", 1.0)
    times, lock, release = {}, threading.Lock(), threading.Event()

    def runner(linux, args, stdin=None, timeout=None):
        assert timeout == everyone.EVERYONE_TIMEOUT_SECONDS  # so the call itself ends too
        with lock:
            times[linux] = [time.monotonic(), None]
        release.wait(5 if linux == "bob" else 0.3)
        with lock:
            times[linux][1] = time.monotonic()
        return 0, {"servers": []}

    app = make_app(settings, runner, admin_runner)
    started = time.monotonic()
    try:
        found = by_linux(everyone.everyone(request_for(app)))
        elapsed = time.monotonic() - started
    finally:
        release.set()
    assert elapsed < everyone.EVERYONE_TIMEOUT_SECONDS + 0.5
    assert found["bob"]["error"]["code"] == "helper_timeout"
    assert found["alice"]["servers"] == [] and found["carol"]["servers"] == []
    with lock:
        starts = [start for start, _ in times.values()]
        ends = [end for _, end in times.values() if end is not None]
    assert len(starts) == 3 and ends
    assert max(starts) < min(ends)  # every call started before any finished


@pytest.mark.parametrize("people", [1, 3])
def test_the_pool_is_no_larger_than_needed(people, settings, admin_runner, monkeypatch):
    from helpers import users_file as uf

    users = uf.load(settings.users_file)[:people]
    uf.save(settings.users_file, users)
    seen = {}
    real = everyone.ThreadPoolExecutor

    def pool(max_workers):
        seen["workers"] = max_workers
        return real(max_workers=max_workers)

    monkeypatch.setattr(everyone, "ThreadPoolExecutor", pool)
    everyone.everyone(
        request_for(make_app(settings, lambda *a, **k: (0, {"servers": []}), admin_runner))
    )
    assert seen["workers"] == min(people, everyone.EVERYONE_WORKERS)
