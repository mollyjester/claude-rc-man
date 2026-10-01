"""The fake `claude`: it replays recorded screens, driven through tmux as the real one is."""

import json
import os
import socket

import pytest

from devtools import fake_claude as fake
from devtools import record_claude as rec
from devtools import tmuxctl

FAKE = rec.FAKE_CLAUDE
READY = r"Capacity: [1-9]"


@pytest.fixture
def pane(tmp_path):
    with tmuxctl.Pane(socket_dir=tmp_path) as p:
        yield p


def env_for(config_dir, **extra):
    return {**rec.claude_env(config_dir), **extra}


def logged_in(config_dir):
    fake.write_state(config_dir, logged_in=True)
    return config_dir


def run(pane, args, cwd, config_dir, **extra):
    pane.run([str(FAKE), *args], cwd=cwd, env=env_for(config_dir, **extra))


def screen_of(name, **values):
    return rec.fill("\n".join(rec.load_fixtures()[name].lines), values).splitlines()


# --- round trip: the recorder over the fake reproduces every fixture ---------


def test_fake_reproduces_every_recorded_screen(tmp_path):
    account = logged_in(tmp_path / "account")
    recorded = rec.record(
        claude_bin=FAKE,
        sequences=rec.SEQUENCES,
        account_config_dir=account,
        workdir=tmp_path / "work",
    )
    expected = rec.load_fixtures()
    assert set(recorded) == set(expected)  # every capture was produced
    assert rec.compare(recorded, expected) == []


def test_round_trip_fails_when_a_fixture_differs(tmp_path):
    """Planted twin: a fixture the fake does not reproduce is reported."""
    recorded = rec.record(
        claude_bin=FAKE,
        sequences=[s for s in rec.SEQUENCES if s.name == "version"],
        account_config_dir=None,
        workdir=tmp_path / "work",
    )
    planted = {"version": rec.Recorded(lines=["{version} (Not Claude)"], exit_code=0)}
    assert rec.compare(recorded, planted) != []


# --- behaviour --------------------------------------------------------------------


def test_logged_out_remote_control_refuses_before_any_trust_prompt(pane, tmp_path):
    run(pane, ["remote-control"], tmp_path, tmp_path / "cfg")
    assert pane.wait_exit(timeout=10) == rec.load_fixtures()["rc_logged_out"].exit_code
    assert "[y/N]" not in pane.text()


def test_trust_is_remembered_and_the_session_resumes(pane, tmp_path):
    cfg = logged_in(tmp_path / "cfg")
    folder = tmp_path / "proj"
    folder.mkdir()
    args = ["remote-control", "--remote-control-session-name-prefix", "pfx"]

    run(pane, args, folder, cfg)
    pane.wait_for(r"\[y/N\]", timeout=10)
    pane.wait_stable(quiet=fake.MIN_ANSWER_SECONDS * 2)
    pane.type_line("y")
    pane.wait_for(READY, timeout=10)
    first = pane.text()
    pane.interrupt()
    assert pane.wait_exit(timeout=10) == 0

    pane.rerun([str(FAKE), *args], cwd=folder, env=env_for(cfg))
    pane.wait_for(READY, timeout=10)
    second = pane.text()

    assert "[y/N]" not in second  # trusted now
    name = next(line.strip() for line in first.splitlines() if line.strip().startswith("pfx-"))
    assert name in second  # same session resumed


def test_declining_trust_does_not_remember_it(pane, tmp_path):
    cfg = logged_in(tmp_path / "cfg")
    run(pane, ["remote-control"], tmp_path, cfg)
    pane.wait_for(r"\[y/N\]", timeout=10)
    pane.wait_stable(quiet=fake.MIN_ANSWER_SECONDS * 2)
    pane.type_line("n")
    assert pane.wait_exit(timeout=10) == rec.load_fixtures()["trust_declined"].exit_code
    assert str(tmp_path) not in json.loads((cfg / fake.STATE_FILE).read_text())["trusted"]


def test_session_name_uses_name_then_prefix_then_hostname(pane, tmp_path):
    cfg = logged_in(tmp_path / "cfg")
    fake.write_state(cfg, logged_in=True, trusted=[str(tmp_path)])
    cases = [
        (["--name", "exact"], "exact"),
        (["--remote-control-session-name-prefix", "pfx"], "pfx-"),
        ([], socket.gethostname() + "-"),
    ]
    for args, expected in cases:
        fake.write_state(cfg, logged_in=True, trusted=[str(tmp_path)])  # no prior session
        pane.close()
        pane.run([str(FAKE), "remote-control", *args], cwd=tmp_path, env=env_for(cfg))
        pane.wait_for(READY, timeout=10)
        lines = pane.screen()
        name = lines[lines.index(next(x for x in lines if "Capacity:" in x)) + 1].strip()
        assert name.startswith(expected), (args, name)


def test_auth_status_reflects_state(pane, tmp_path):
    cfg = tmp_path / "cfg"
    run(pane, ["auth", "status"], tmp_path, cfg)
    assert pane.wait_exit(timeout=10) == 1
    assert json.loads(pane.text())["loggedIn"] is False

    logged_in(cfg)
    pane.rerun([str(FAKE), "auth", "status"], cwd=tmp_path, env=env_for(cfg))
    assert pane.wait_exit(timeout=10) == 0
    assert json.loads(pane.text())["loggedIn"] is True


def test_login_does_not_echo_the_code(pane, tmp_path):
    run(
        pane,
        ["auth", "login", "--claudeai", "--email", "x@example.com"],
        tmp_path,
        tmp_path / "cfg",
    )
    pane.wait_for(r"Paste code here", timeout=10)
    pane.type_line("SECRET-CODE")
    pane.wait_exit(timeout=10)
    assert "SECRET-CODE" not in pane.text()


def test_unrecognised_scenario_matches_no_fixture(pane, tmp_path):
    cfg = logged_in(tmp_path / "cfg")
    run(pane, ["remote-control"], tmp_path, cfg, FAKE_CLAUDE_SCENARIO="unrecognised")
    pane.wait_for(r"\S", timeout=10)
    shown = pane.wait_stable(quiet=0.5)
    for name, fixture in rec.load_fixtures().items():
        assert shown != rec.fill("\n".join(fixture.lines), rec.ANY_VALUES).splitlines(), name


def test_unsupported_invocation_fails_loudly(pane, tmp_path):
    run(pane, ["mcp", "list"], tmp_path, tmp_path / "cfg")
    assert pane.wait_exit(timeout=10) == 2
    assert "unsupported" in pane.text()


def test_fake_never_touches_the_real_claude_directory(tmp_path):
    env = rec.claude_env(None)
    assert "CLAUDE_CONFIG_DIR" not in env  # real account runs use the default
    # ...but the fake's own default state lives elsewhere.
    assert fake.state_dir({"HOME": "/h"}) == os.path.join("/h", ".claude-fake")


def test_an_instant_trust_answer_is_refused_and_asked_again(pane, tmp_path):
    cfg = logged_in(tmp_path / "cfg")
    run(pane, ["remote-control"], tmp_path, cfg)
    pane.wait_for(r"\[y/N\]", timeout=10)
    pane.type_line("y")
    pane.wait_for(r"too quickly", timeout=10)
    assert pane.exit_status() is None  # still asking
    pane.wait_stable(quiet=fake.MIN_ANSWER_SECONDS * 2)
    pane.type_line("y")
    pane.wait_for(r"environment=", timeout=10)


def test_a_paced_trust_answer_is_accepted(pane, tmp_path):
    cfg = logged_in(tmp_path / "cfg")
    run(pane, ["remote-control"], tmp_path, cfg)
    pane.wait_for(r"\[y/N\]", timeout=10)
    pane.wait_stable(quiet=fake.MIN_ANSWER_SECONDS * 2)
    pane.type_line("y")
    pane.wait_for(r"environment=", timeout=10)
    assert "too quickly" not in pane.text()


def test_server_connects_first_and_becomes_ready_after(pane, tmp_path):
    cfg = logged_in(tmp_path / "cfg")
    fake.write_state(cfg, logged_in=True, trusted=[str(tmp_path)])
    run(pane, ["remote-control"], tmp_path, cfg)
    pane.wait_for(r"Capacity: 0/", timeout=10)
    connected = pane.text()
    pane.wait_for(READY, timeout=fake.READY_SECONDS + 10)
    assert "Capacity: 0/" in connected and READY not in connected
