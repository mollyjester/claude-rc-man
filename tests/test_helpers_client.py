"""How the backend calls rc-helper and rc-admin, and maps their errors."""

from pathlib import Path

import pytest
from rcman import helpers_client as hc

from helpers import rc_admin, rc_helper


def test_production_helper_runs_through_sudo_as_the_user(tmp_path):
    seen = {}

    def fake_run(argv, **kw):
        import subprocess

        seen["argv"], seen["input"] = argv, kw.get("input")
        return subprocess.CompletedProcess(argv, 0, stdout='{"servers": []}', stderr="")

    run = hc.sudo_helper(Path("/opt/rcman/helpers"), runner=fake_run)
    assert run("alice", ["list"], stdin="secret") == (0, {"servers": []})
    assert seen["argv"] == ["sudo", "-n", "-u", "alice", "/opt/rcman/helpers/rc-helper", "list"]
    assert seen["input"] == "secret"


def test_production_admin_runs_through_sudo_as_root():
    seen = {}

    def fake_run(argv, **kw):
        import subprocess

        seen["argv"] = argv
        return subprocess.CompletedProcess(argv, 0, stdout='{"users": []}', stderr="")

    run = hc.sudo_admin(Path("/opt/rcman/helpers"), runner=fake_run)
    assert run(["list"]) == (0, {"users": []})
    assert seen["argv"] == ["sudo", "-n", "/opt/rcman/helpers/rc-admin", "list"]


def test_unparseable_output_is_helper_unavailable():
    def fake_run(argv, **kw):
        import subprocess

        return subprocess.CompletedProcess(
            argv, 1, stdout="sudo: a password is required", stderr=""
        )

    code, out = hc.sudo_helper(Path("/x"), runner=fake_run)("alice", ["list"])
    assert out["error"]["code"] == "helper_unavailable"


def test_every_helper_error_code_has_a_status():
    assert set(hc.HELPER_STATUS) == set(rc_helper.MESSAGES)


def test_every_admin_error_code_has_a_status():
    assert set(hc.ADMIN_STATUS) == set(rc_admin.MESSAGES)


@pytest.mark.parametrize("status", [*hc.HELPER_STATUS.values(), *hc.ADMIN_STATUS.values()])
def test_statuses_are_client_or_server_errors(status):
    assert 400 <= status < 600


def test_a_helper_that_times_out_is_a_problem():
    import subprocess

    from rcman.identity import Identity
    from rcman.problems import Problem

    seen = {}

    def fake_run(argv, **kw):
        seen["timeout"] = kw["timeout"]
        raise subprocess.TimeoutExpired(argv, kw["timeout"])

    run = hc.sudo_helper(Path("/x"), runner=fake_run)
    with pytest.raises(Problem) as raised:
        hc.call_helper(run, Identity("a@example.com", "alice", False), ["list"], timeout=7)
    assert (raised.value.code, raised.value.status) == ("helper_timeout", 504)
    assert seen["timeout"] == 7
    hc.call_helper(
        hc.sudo_helper(Path("/x"), runner=lambda argv, **kw: seen.update(kw) or ok(argv)),
        Identity("a@example.com", "alice", False),
        ["list"],
    )
    assert seen["timeout"] == hc.HELPER_TIMEOUT  # the default when none is given


def ok(argv):
    import subprocess

    return subprocess.CompletedProcess(argv, 0, stdout='{"servers": []}', stderr="")
