"""The fake `claude`: it replays recorded screens, driven through tmux as the real one is."""

import json
import os
import re
import socket

import pytest

from devtools import fake_claude as fake
from devtools import held_folders, tmuxctl
from devtools import record_claude as rec

FAKE = rec.FAKE_CLAUDE
READY = r"Capacity: [1-9]"


@pytest.fixture
def pane():
    with held_folders.tmux_sockets("rcp-") as sockets, tmuxctl.Pane(socket_dir=sockets) as p:
        yield p


def test_the_panes_tmux_server_is_in_a_held_folder_of_its_own(pane, tmp_path):
    """So a run killed by a signal leaves it for the next run to stop."""
    pane.run(["sleep", "60"], cwd=tmp_path, env={})
    folder = pane.socket.parent
    assert held_folders.made_with("rcp-")(folder.name) and folder.parent == held_folders.TMP
    assert not held_folders.unheld(folder)


def env_for(config_dir, **extra):
    return {**rec.claude_env(config_dir), **extra}


def logged_in(config_dir):
    fake.write_state(config_dir, logged_in=True)
    return config_dir


def run(pane, args, cwd, config_dir, **extra):
    pane.run([str(FAKE), *args], cwd=cwd, env=env_for(config_dir, **extra))


# --- round trip: the recorder over the fake reproduces every fixture ---------


AUTOMATIC = [s for s in rec.SEQUENCES if not s.needs_human]


@pytest.mark.parametrize("sequence", AUTOMATIC, ids=lambda s: s.name)
def test_fake_reproduces_the_recorded_screens_of(sequence, tmp_path):
    """The sequence, recorded over the fake with an account of its own, gives its
    fixtures."""
    account = logged_in(tmp_path / "account")
    recorded = rec.record(
        claude_bin=FAKE,
        sequences=[sequence],
        account_config_dir=account,
        workdir=tmp_path / "work",
    )
    expected = {name: f for name, f in rec.load_fixtures().items() if name in sequence.captures}
    assert sequence.captures and set(expected) == set(sequence.captures)  # each has a fixture
    assert set(recorded) == set(expected)  # every capture was produced
    assert rec.compare(recorded, expected) == []


def test_the_round_trip_has_a_case_for_every_automatic_sequence_and_fixture():
    """Each sequence the recorder runs unattended is its own case, and together their
    captures are every fixture but those of the sequences that need a person."""
    from tests.collected import collect

    test = "tests/test_fake_claude.py::test_fake_reproduces_the_recorded_screens_of"
    found = collect(["tests/test_fake_claude.py"])
    cases = {n.removeprefix(test) for n in found if n.startswith(test)}
    assert AUTOMATIC and len(AUTOMATIC) < len(rec.SEQUENCES)  # both kinds exist
    assert cases == {f"[{s.name}]" for s in AUTOMATIC}
    names = {s.name for s in AUTOMATIC}
    fixtures = {c for c, e in rec.load_manifest()["captures"].items() if e["sequence"] in names}
    assert {c for s in AUTOMATIC for c in s.captures} == fixtures


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


RECONNECTED_SCENARIO = "reconnected_without_status"


def test_the_reconnected_scenario_shows_the_observed_screen(pane, tmp_path):
    cfg = logged_in(tmp_path / "cfg")
    run(pane, ["remote-control"], tmp_path, cfg, FAKE_CLAUDE_SCENARIO=RECONNECTED_SCENARIO)
    pane.wait_for(r"Reconnected", timeout=10)
    ctx = rec.Context(str(tmp_path), str(cfg), rec.load_manifest()["claude_version"])
    raw = "\n".join(pane.wait_stable(quiet=0.5))
    observed = rec.load_observed()[RECONNECTED_SCENARIO].lines
    assert rec.sanitise(raw, ctx).split("\n") == observed
    assert pane.exit_status() is None  # still running, as it was seen
    # Planted twin: a screen differing from the one shown is not sanitised into the
    # observed one, so the comparison would see a fake showing another.
    changed = raw.replace("Reconnected after 3s", "Reconnected after 4s")
    assert changed != raw  # the plant took effect
    assert rec.sanitise(changed, ctx).split("\n") != observed
    pane.interrupt()
    assert pane.wait_exit(timeout=10) == 1  # what follows Ctrl+C was not observed


# Unlike the other observed screens, this scenario is interactive. What was seen is the
# screen; the answers, the screens after `y`, the exit status after `n`, Ctrl+C at the
# prompt and how fast an answer may come are not.
PROMPT_SCENARIO = "remote_control_prompt"
PROMPT = "Enable Remote Control"


def at_the_prompt(pane, tmp_path):
    cfg = logged_in(tmp_path / "cfg")
    run(pane, ["remote-control"], tmp_path, cfg, FAKE_CLAUDE_SCENARIO=PROMPT_SCENARIO)
    pane.wait_for(PROMPT, timeout=10)
    return cfg


def test_the_remote_control_prompt_scenario_shows_the_observed_screen(pane, tmp_path):
    cfg = at_the_prompt(pane, tmp_path)
    ctx = rec.Context(str(tmp_path), str(cfg), rec.load_manifest()["claude_version"])
    raw = "\n".join(pane.wait_stable(quiet=0.5))
    observed = rec.load_observed()[PROMPT_SCENARIO].lines
    assert rec.sanitise(raw, ctx).split("\n") == observed
    assert pane.exit_status() is None  # still asking, as it was seen
    # Planted twin: a screen differing from the one shown is not sanitised into the
    # observed one, so the comparison would see a fake showing another.
    changed = raw.replace("Enable Remote Control?", "Enable Remote Control!")
    assert changed != raw  # the plant took effect
    assert rec.sanitise(changed, ctx).split("\n") != observed


def test_yes_at_the_remote_control_prompt_goes_on_to_the_server_screens(pane, tmp_path):
    """Not observed: what follows `y`. The fake goes on as after the trust question."""
    at_the_prompt(pane, tmp_path)
    pane.type_line("y")
    pane.wait_for(r"Capacity: 0/", timeout=10)
    assert PROMPT not in pane.text()
    pane.wait_for(READY, timeout=fake.READY_SECONDS + 10)
    assert pane.exit_status() is None
    pane.interrupt()
    assert pane.wait_exit(timeout=10) == rec.load_fixtures()["server_stopped_after_trust"].exit_code


def test_no_at_the_remote_control_prompt_ends_the_fake_with_status_one(pane, tmp_path):
    """Not observed: the exit status after `n`. Claude Code is documented to exit."""
    at_the_prompt(pane, tmp_path)
    assert pane.exit_status() is None  # positive control: it was running until the answer
    pane.type_line("n")
    assert pane.wait_exit(timeout=10) == 1
    assert "Capacity:" not in pane.text()


def test_ctrl_c_at_the_remote_control_prompt_ends_the_fake_with_status_one(pane, tmp_path):
    """Not observed: what Ctrl+C does at the prompt."""
    at_the_prompt(pane, tmp_path)
    assert pane.exit_status() is None
    pane.interrupt()
    assert pane.wait_exit(timeout=10) == 1


def test_an_answer_other_than_y_or_n_at_the_remote_control_prompt_asks_nothing_more(pane, tmp_path):
    """Not observed. Anything but `y` is taken as a no, as the trust question takes it."""
    at_the_prompt(pane, tmp_path)
    pane.type_line("maybe")
    assert pane.wait_exit(timeout=10) == 1


def test_the_default_scenario_never_shows_the_remote_control_prompt(pane, tmp_path):
    cfg = at_the_prompt(pane, tmp_path)  # positive control: the scenario shows it
    pane.interrupt()
    pane.wait_exit(timeout=10)
    pane.rerun([str(FAKE), "remote-control"], cwd=tmp_path, env=env_for(cfg))
    answer_trust(pane)
    pane.wait_for(READY, timeout=fake.READY_SECONDS + 10)
    assert PROMPT not in pane.text()


def test_every_observed_screen_is_a_scenario_of_the_fake(tmp_path, monkeypatch, capsys):
    """The scenarios are the observed screens themselves: one more is shown by its name."""
    planted = rec.Observed(
        lines=["", "PLANTED OBSERVED SCREEN"], exit_code=None, seen_on="", kept_for=[]
    )
    real = rec.load_observed
    monkeypatch.setattr(rec, "load_observed", lambda: {**real(), "planted_screen": planted})
    monkeypatch.setattr(fake.Fake, "wait_for_interrupt", lambda self, interrupted=None: None)
    monkeypatch.chdir(tmp_path)
    cfg = tmp_path / "cfg"
    fake.write_state(cfg, logged_in=True, trusted=[os.getcwd()])
    env = env_for(cfg, FAKE_CLAUDE_SCENARIO="planted_screen", FAKE_CLAUDE_READY_SECONDS="0")
    assert fake.Fake(["remote-control"], env).run() == 1
    assert "PLANTED OBSERVED SCREEN" in capsys.readouterr().out


def test_unsupported_invocation_fails_loudly(pane, tmp_path):
    run(pane, ["mcp", "list"], tmp_path, tmp_path / "cfg")
    assert pane.wait_exit(timeout=10) == 2
    assert "unsupported" in pane.text()


def fake_exec(tmp_path, *command, stdin=None, **env):
    """The fake's ``fake-exec`` verb run as a workspace's Claude is: through ``env -i``
    with exactly the variables given, signed out and with no state of its own."""
    import subprocess

    variables = [f"{name}={value}" for name, value in env.items()]
    return subprocess.run(
        ["/usr/bin/env", "-i", *variables, str(FAKE), "fake-exec", *command],
        capture_output=True,
        text=True,
        input=stdin,
        cwd=tmp_path,
    )


def test_fake_exec_runs_the_command_with_the_environment_the_fake_was_started_with(tmp_path):
    """That is the environment a Claude child gets, so what a command run by it sees is
    what a command run by Claude would see: no more, and no less."""
    # LANG as a workspace's Claude has it: without one, Python itself adds LC_CTYPE.
    given = {
        "HOME": str(tmp_path),
        "PATH": os.environ["PATH"],
        "LANG": "C.UTF-8",
        "RCMAN_PLANTED": "kept as given",
    }
    proc = fake_exec(tmp_path, "env", **given)
    assert proc.returncode == 0, proc.stderr
    seen = dict(line.split("=", 1) for line in proc.stdout.splitlines())
    assert {k: v for k, v in seen.items() if k != "PWD"} == given


def test_fake_exec_does_not_hand_on_the_version_the_program_was_baked_with(tmp_path):
    """A release's launcher exports ``FAKE_CLAUDE_VERSION`` for the fake alone: the real
    Claude's children never see a variable of that kind, so a command run through the
    verb must not either (a workspace test compares a child's environment with
    ``runtime.child_env`` exactly)."""
    given = {"HOME": str(tmp_path), "PATH": os.environ["PATH"], "LANG": "C.UTF-8"}
    proc = fake_exec(tmp_path, "env", FAKE_CLAUDE_VERSION="9.9.9", **given)
    assert proc.returncode == 0, proc.stderr
    seen = dict(line.split("=", 1) for line in proc.stdout.splitlines())
    assert {k: v for k, v in seen.items() if k != "PWD"} == given


def test_fake_exec_passes_the_commands_output_input_and_exit_status_through(tmp_path):
    proc = fake_exec(
        tmp_path,
        "sh",
        "-c",
        'cat; echo "to stderr" >&2; exit 7',
        stdin="from stdin\n",
        PATH=os.environ["PATH"],
    )
    assert (proc.stdout, proc.stderr, proc.returncode) == ("from stdin\n", "to stderr\n", 7)


def test_fake_exec_needs_no_sign_in_and_leaves_no_state(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    proc = fake_exec(tmp_path, "true", HOME=str(home), PATH=os.environ["PATH"])
    assert proc.returncode == 0
    assert list(home.iterdir()) == []


def test_fake_exec_with_no_command_or_a_missing_one_fails_loudly(tmp_path):
    bare = fake_exec(tmp_path, PATH=os.environ["PATH"])
    assert bare.returncode == 2 and "unsupported" in bare.stdout
    missing = fake_exec(tmp_path, "no-such-command-here", PATH=os.environ["PATH"])
    assert missing.returncode == 127 and "no-such-command-here" in missing.stderr


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


def test_logout_removes_the_credentials_file_as_the_real_one_does(pane, tmp_path):
    """tests/test_claude_contract.py holds the real binary to the same."""
    cfg = logged_in(tmp_path / "cfg")
    assert (cfg / fake.CREDENTIALS).exists()  # positive control
    run(pane, ["auth", "logout"], tmp_path, cfg)
    assert pane.wait_exit(timeout=10) == rec.load_fixtures()["auth_logout"].exit_code
    assert not (cfg / fake.CREDENTIALS).exists()


def test_logout_leaves_a_file_it_cannot_remove(tmp_path, monkeypatch):
    """A bound file cannot be removed (EBUSY); rcman empties it in place afterwards."""
    import errno

    cfg = logged_in(tmp_path / "cfg")

    def busy(path):
        raise OSError(errno.EBUSY, "Device or resource busy", str(path))

    monkeypatch.setattr(fake.os, "unlink", busy)
    fake.remove_credentials(cfg / fake.CREDENTIALS)
    assert fake.signed_in(cfg)


@pytest.mark.parametrize("content", ["", "{}"])
def test_an_empty_credentials_file_is_signed_out(tmp_path, content):
    """As the real binary reads one (tests/test_claude_contract.py)."""
    import subprocess

    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / fake.CREDENTIALS).write_text(content)
    proc = subprocess.run(
        [str(FAKE), "auth", "status"], env=env_for(cfg), capture_output=True, text=True
    )
    assert json.loads(proc.stdout)["loggedIn"] is False
    assert proc.returncode == rec.load_fixtures()["auth_status_logged_out"].exit_code
    assert (cfg / fake.CREDENTIALS).read_text() == content  # left as it was


def test_the_accepted_code_signs_in_with_the_recorded_screens(pane, tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / ".claude.json").write_text(json.dumps({"numStartups": 3}))
    run(pane, ["auth", "login", "--claudeai", "--email", "x@example.com"], tmp_path, cfg)
    pane.wait_for(r"Paste code here", timeout=10)
    pane.type_line(fake.ACCEPTED_CODE)
    pane.wait_for(r"Login successful", timeout=10)
    pane.type_line("")
    assert pane.wait_exit(timeout=10) == rec.load_fixtures()["login_succeeded_done"].exit_code
    credentials = cfg / fake.CREDENTIALS
    assert json.loads(credentials.read_text())["claudeAiOauth"]["refreshToken"]
    assert (credentials.stat().st_mode & 0o777) == 0o600
    config = json.loads((cfg / ".claude.json").read_text())
    assert config["numStartups"] == 3  # every other key kept
    assert config["oauthAccount"]["emailAddress"] == "x@example.com"
    assert fake.ACCEPTED_CODE not in pane.text()


def test_a_sign_in_through_a_link_writes_the_file_linked_to(pane, tmp_path):
    """The host runtime links each workspace's file to the person's one."""
    shared = tmp_path / "account" / fake.CREDENTIALS
    shared.parent.mkdir()
    shared.write_text("")
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / fake.CREDENTIALS).symlink_to(shared)
    run(pane, ["auth", "login", "--claudeai", "--email", "x@example.com"], tmp_path, cfg)
    pane.wait_for(r"Paste code here", timeout=10)
    pane.type_line(fake.ACCEPTED_CODE)
    pane.wait_for(r"Login successful", timeout=10)
    pane.type_line("")
    pane.wait_exit(timeout=10)
    assert (cfg / fake.CREDENTIALS).is_symlink()
    assert "claudeAiOauth" in json.loads(shared.read_text())


def test_credentials_are_replaced_by_rename(tmp_path):
    path = tmp_path / fake.CREDENTIALS
    path.write_text("old")
    before = path.stat().st_ino
    fake.write_credentials(path, {"claudeAiOauth": {}})
    assert path.stat().st_ino != before
    assert json.loads(path.read_text()) == {"claudeAiOauth": {}}
    assert (path.stat().st_mode & 0o777) == 0o600


def test_a_rename_refused_busy_writes_in_place_and_leaves_nothing_of_the_old(tmp_path, monkeypatch):
    """A bound file cannot be renamed over (EBUSY), as in a workspace."""
    import errno

    path = tmp_path / fake.CREDENTIALS
    path.write_text("x" * 500)
    before = path.stat().st_ino

    def busy(src, dst):
        raise OSError(errno.EBUSY, "Device or resource busy", str(dst))

    monkeypatch.setattr(fake.os, "replace", busy)
    fake.write_credentials(path, {"claudeAiOauth": {}})
    assert path.stat().st_ino == before
    assert json.loads(path.read_text()) == {"claudeAiOauth": {}}
    assert [p.name for p in tmp_path.iterdir()] == [fake.CREDENTIALS]  # no temporary left


def test_any_other_failure_of_the_rename_is_not_hidden(tmp_path, monkeypatch):
    import errno

    path = tmp_path / fake.CREDENTIALS
    path.write_text("old")

    def refused(src, dst):
        raise OSError(errno.EACCES, "Permission denied", str(dst))

    monkeypatch.setattr(fake.os, "replace", refused)
    with pytest.raises(PermissionError):
        fake.write_credentials(path, {"claudeAiOauth": {}})
    assert path.read_text() == "old"


def test_a_code_not_accepted_still_fails(pane, tmp_path):
    cfg = tmp_path / "cfg"
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
    cfg = logged_in(tmp_path / "cfg")
    folder = repository(tmp_path / "proj")
    run(pane, ["remote-control"], folder, cfg)
    answer_trust(pane)
    pane.wait_for(SPAWN_QUESTION, timeout=10)
    assert "Capacity:" not in pane.text()
    pane.wait_stable(quiet=fake.MIN_ANSWER_SECONDS * 2)
    pane.type_line("1")
    pane.wait_for(READY, timeout=10)
    pane.interrupt()
    assert pane.wait_exit(timeout=10) == rec.load_fixtures()["spawn_stopped_same_dir"].exit_code


def test_after_ctrl_c_the_spawn_mode_question_takes_no_answer_or_sigterm_and_ends_on_sigkill(
    pane, tmp_path
):
    import signal
    import time

    cfg = logged_in(tmp_path / "cfg")
    folder = repository(tmp_path / "proj")
    run(pane, ["remote-control"], folder, cfg)
    answer_trust(pane)
    pane.wait_for(SPAWN_QUESTION, timeout=10)
    pane.interrupt()  # changes nothing, as recorded
    time.sleep(1)
    assert pane.exit_status() is None
    pane.type_line("1")  # taken by nothing, as recorded
    time.sleep(1)
    assert pane.exit_status() is None
    assert "Capacity:" not in pane.text()
    pane.signal(signal.SIGTERM)  # changes nothing, as recorded
    time.sleep(1)
    assert pane.exit_status() is None
    assert "Capacity:" not in pane.text()
    pane.signal(signal.SIGKILL)
    assert pane.wait_exit(timeout=10) == rec.load_fixtures()["spawn_prompt_killed"].exit_code


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


# The keys a real sign-in writes, as seen with a real account (real-claude-checklist.md,
# The shared sign-in).
REAL_CREDENTIAL_KEYS = {
    "accessToken", "expiresAt", "rateLimitTier", "refreshToken", "refreshTokenExpiresAt",
    "scopes", "subscriptionType",
}  # fmt: skip
REAL_OAUTH_ACCOUNT_KEYS = {
    "accountCreatedAt", "accountUuid", "billingType", "ccOnboardingFlags",
    "claudeCodeTrialDurationDays", "claudeCodeTrialEndsAt", "displayName", "emailAddress",
    "fullName", "hasExtraUsageEnabled", "organizationName", "organizationRateLimitTier",
    "organizationRole", "organizationType", "organizationUuid", "profileFetchedAt",
    "seatTier", "subscriptionCreatedAt", "userRateLimitTier", "workspaceRole",
}  # fmt: skip


def test_a_sign_in_writes_the_keys_the_real_one_writes():
    assert set(fake.credentials()) == {"claudeAiOauth"}
    assert set(fake.credentials()["claudeAiOauth"]) == REAL_CREDENTIAL_KEYS
    assert set(fake.oauth_account("x@example.com")) == REAL_OAUTH_ACCOUNT_KEYS


def test_remote_control_without_oauth_account_is_refused_as_the_real_one_is(tmp_path):
    import subprocess

    cfg = logged_in(tmp_path / "cfg")
    (cfg / fake.CONFIG_FILE).write_text(json.dumps({"numStartups": 1}))
    proc = subprocess.run(
        [str(FAKE), "remote-control"], env=env_for(cfg), cwd=tmp_path,
        capture_output=True, text=True, timeout=30,
    )  # fmt: skip
    recorded = rec.load_fixtures()["rc_no_oauth_account"]
    assert (proc.returncode, proc.stdout) == (recorded.exit_code, "")
    assert proc.stderr.rstrip("\n").split("\n") == recorded.lines
