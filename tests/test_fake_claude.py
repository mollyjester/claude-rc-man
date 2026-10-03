"""The fake `claude`: it replays recorded screens, driven through tmux as the real one is."""

import json
import os
import re
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
    automatic = [s for s in rec.SEQUENCES if not s.needs_human]
    recorded = rec.record(
        claude_bin=FAKE,
        sequences=automatic,
        account_config_dir=account,
        workdir=tmp_path / "work",
    )
    names = {c for s in automatic for c in s.captures}
    expected = {name: f for name, f in rec.load_fixtures().items() if name in names}
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


def test_output_to_a_pipe_is_plain_text(tmp_path):
    """Like the real binary, nothing but the text when stdout is not a terminal."""
    import subprocess

    proc = subprocess.run(
        [str(FAKE), "auth", "status"], env=env_for(tmp_path / "cfg"), capture_output=True, text=True
    )
    assert json.loads(proc.stdout)["loggedIn"] is False


def test_logout_disconnects_the_account(pane, tmp_path):
    cfg = logged_in(tmp_path / "cfg")
    run(pane, ["auth", "logout"], tmp_path, cfg)
    assert pane.wait_exit(timeout=10) == rec.load_fixtures()["auth_logout"].exit_code
    assert json.loads((cfg / fake.STATE_FILE).read_text())["logged_in"] is False


def test_an_accepted_code_signs_in_with_the_recorded_screens(pane, tmp_path):
    cfg = tmp_path / "cfg"
    fake.write_state(cfg, logged_in=False, accept_codes=["GOOD-CODE"])
    run(pane, ["auth", "login", "--claudeai", "--email", "x@example.com"], tmp_path, cfg)
    pane.wait_for(r"Paste code here", timeout=10)
    pane.type_line("GOOD-CODE")
    pane.wait_for(r"Login successful", timeout=10)
    pane.type_line("")
    assert pane.wait_exit(timeout=10) == rec.load_fixtures()["login_succeeded_done"].exit_code
    assert json.loads((cfg / fake.STATE_FILE).read_text())["logged_in"] is True
    assert "GOOD-CODE" not in pane.text()


def test_a_code_not_accepted_still_fails(pane, tmp_path):
    cfg = tmp_path / "cfg"
    fake.write_state(cfg, logged_in=False, accept_codes=["GOOD-CODE"])
    run(pane, ["auth", "login", "--claudeai", "--email", "x@example.com"], tmp_path, cfg)
    pane.wait_for(r"Paste code here", timeout=10)
    pane.type_line("BAD-CODE")
    assert pane.wait_exit(timeout=10) == rec.load_fixtures()["login_failed"].exit_code


def test_login_help_is_the_recorded_help(pane, tmp_path):
    run(pane, ["auth", "login", "--help"], tmp_path, tmp_path / "cfg")
    assert pane.wait_exit(timeout=10) == rec.load_fixtures()["auth_login_help"].exit_code
    assert "--claudeai" in pane.text() and "--email" in pane.text()


def test_the_reported_version_can_be_overridden(pane, tmp_path):
    run(pane, ["--version"], tmp_path, tmp_path / "cfg", FAKE_CLAUDE_VERSION="7.7.7")
    pane.wait_exit(timeout=10)
    assert pane.text() == "7.7.7 (Claude Code)"


# --- spawn mode, in a git repository ------------------------------------------------

SPAWN_QUESTION = r"Choose \[1/2\]"


def repository(path, branch="main"):
    from tests.git_fixtures import fixture_git, make_repo

    make_repo(path)
    if branch != "main":
        fixture_git(path, "switch", "-q", "-c", branch)
    return path


def answer_trust(pane):
    pane.wait_for(r"\[y/N\]", timeout=10)
    pane.wait_stable(quiet=fake.MIN_ANSWER_SECONDS * 2)
    pane.type_line("y")


def test_in_a_repository_without_spawn_it_asks_the_spawn_mode_question(pane, tmp_path):
    import time

    cfg = logged_in(tmp_path / "cfg")
    folder = repository(tmp_path / "proj")
    run(pane, ["remote-control"], folder, cfg)
    answer_trust(pane)
    pane.wait_for(SPAWN_QUESTION, timeout=10)
    assert "Capacity:" not in pane.text()
    pane.interrupt()  # the question ignores Ctrl+C, as recorded
    time.sleep(1)
    assert pane.exit_status() is None
    assert re.search(SPAWN_QUESTION, pane.text())
    pane.wait_stable(quiet=fake.MIN_ANSWER_SECONDS * 2)
    pane.type_line("1")
    pane.wait_for(READY, timeout=10)
    pane.interrupt()
    assert pane.wait_exit(timeout=10) == rec.load_fixtures()["spawn_stopped_same_dir"].exit_code


def test_the_spawn_mode_question_ends_on_sigterm(pane, tmp_path):
    import signal

    cfg = logged_in(tmp_path / "cfg")
    folder = repository(tmp_path / "proj")
    run(pane, ["remote-control"], folder, cfg)
    answer_trust(pane)
    pane.wait_for(SPAWN_QUESTION, timeout=10)
    pid = int(pane._tmux("display-message", "-p", "-t", "p", "#{pane_pid}").strip())
    os.kill(pid, signal.SIGTERM)
    assert pane.wait_exit(timeout=10) == 128 + signal.SIGTERM


def test_in_a_repository_with_spawn_same_dir_it_connects_and_shows_the_branch(pane, tmp_path):
    cfg = logged_in(tmp_path / "cfg")
    folder = repository(tmp_path / "proj", branch="feature-x")
    run(pane, ["remote-control", "--spawn", "same-dir"], folder, cfg)
    answer_trust(pane)
    pane.wait_for(READY, timeout=10)
    text = pane.text()
    assert "Spawn mode" not in text
    assert re.search(r"Connected · proj · feature-x$", text, re.MULTILINE)


def test_in_a_folder_of_a_repository_it_is_in_the_repository(pane, tmp_path):
    cfg = logged_in(tmp_path / "cfg")
    folder = repository(tmp_path / "proj") / "sub"
    folder.mkdir()
    run(pane, ["remote-control"], folder, cfg)
    answer_trust(pane)
    pane.wait_for(SPAWN_QUESTION, timeout=10)


def test_outside_a_repository_spawn_same_dir_changes_nothing(pane, tmp_path):
    cfg = logged_in(tmp_path / "cfg")
    fake.write_state(cfg, logged_in=True, trusted=[str(tmp_path)])
    run(pane, ["remote-control", "--spawn", "same-dir"], tmp_path, cfg)
    pane.wait_for(READY, timeout=10)
    assert re.search(r"Connected · \S+ · HEAD$", pane.text(), re.MULTILINE)


def test_a_spawn_mode_it_has_no_recording_of_fails_loudly(pane, tmp_path):
    cfg = logged_in(tmp_path / "cfg")
    run(pane, ["remote-control", "--spawn", "worktree"], tmp_path, cfg)
    assert pane.wait_exit(timeout=10) == 2
    assert "unsupported" in pane.text()


def test_a_trusted_repository_without_spawn_fails_loudly(pane, tmp_path):
    """Whether Claude asks again once a folder is trusted has no recording."""
    folder = repository(tmp_path / "proj")
    cfg = logged_in(tmp_path / "cfg")
    fake.write_state(cfg, logged_in=True, trusted=[str(folder)])
    run(pane, ["remote-control"], folder, cfg)
    assert pane.wait_exit(timeout=10) == 2
    assert "unsupported" in pane.text()
