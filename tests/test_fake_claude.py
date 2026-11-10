"""The fake `claude`: it replays recorded screens, driven through tmux as the real one is."""

import json
import os
import sys
from pathlib import Path

import pytest

from devtools import fake_claude as fake
from devtools import held_folders, tmuxctl
from devtools import record_claude as rec
from tests import claude_watches as cw
from tests.hang_guard import HANG_GUARD

FAKE = rec.FAKE_CLAUDE
READY = r"Capacity: [1-9]"
# The observed screen that asks a question, and takes an answer.
PROMPT = "remote_control_prompt"


@pytest.fixture
def pane():
    with held_folders.tmux_sockets("rcp-") as sockets, tmuxctl.Pane(socket_dir=sockets) as p:
        yield p


def env_for(config_dir, **extra):
    """The fake's environment with ``config_dir``, and a home beside it of its own."""
    return {**rec.claude_env(config_dir, Path(config_dir).parent / "home"), **extra}


def logged_in(config_dir):
    fake.write_state(config_dir, logged_in=True)
    return config_dir


def run(pane, args, cwd, config_dir, **extra):
    pane.run([str(FAKE), *args], cwd=cwd, env=env_for(config_dir, **extra))


def traced(tmp_path, **extra):
    """The fake's trace, gate and threshold (none of its answers too quick) in a folder of the
    test's, as tests/claude_watches.py's TraceWatch gives them: a test waits on what the fake
    traced, never for its screen to stay still. ``extra`` overrides them."""
    return {**cw.traced_env(tmp_path / "watch"), **extra}


def trace_of(env):
    return Path(env[fake.TRACE])


def release_connected(tmp_path):
    """Lets a fake given traced(tmp_path) leave its connected stage."""
    cw.release(Path(traced(tmp_path)[fake.GATE]), fake.CONNECTED_STAGE)


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
        watch=cw.TraceWatch(),
    )
    expected = {name: f for name, f in rec.load_fixtures().items() if name in sequence.captures}
    assert sequence.captures and set(expected) == set(sequence.captures)  # each has a fixture
    assert set(recorded) == set(expected)  # every capture was produced
    assert rec.compare(recorded, expected) == []


ROUND_TRIP = "tests/test_fake_claude.py::test_fake_reproduces_the_recorded_screens_of"


def missing_cases(collected: list[str]) -> list[str]:
    """The automatic sequences with no round-trip case among ``collected``, and the cases of
    no automatic sequence."""
    cases = {n.removeprefix(ROUND_TRIP) for n in collected if n.startswith(ROUND_TRIP)}
    return sorted(cases ^ {f"[{s.name}]" for s in AUTOMATIC})


def test_the_round_trip_has_a_case_for_every_automatic_sequence_and_fixture():
    """Each sequence the recorder runs unattended is its own case, and together their
    captures are every fixture but those of the sequences that need a person."""
    from tests.collected import collect

    assert AUTOMATIC and len(AUTOMATIC) < len(rec.SEQUENCES)  # both kinds exist
    assert missing_cases(collect(["tests/test_fake_claude.py"])) == []
    names = {s.name for s in AUTOMATIC}
    fixtures = {c for c, e in rec.load_manifest()["captures"].items() if e["sequence"] in names}
    assert {c for s in AUTOMATIC for c in s.captures} == fixtures


def test_a_sequence_with_no_round_trip_case_is_found():
    """Planted twin: the cases collected but for the first sequence's."""
    collected = [f"{ROUND_TRIP}[{s.name}]" for s in AUTOMATIC[1:]]
    assert missing_cases(collected) == [f"[{AUTOMATIC[0].name}]"]


def test_round_trip_fails_when_a_fixture_differs(tmp_path):
    """Planted twin: a fixture the fake does not reproduce is reported."""
    recorded = rec.record(
        claude_bin=FAKE,
        sequences=[s for s in rec.SEQUENCES if s.name == "version"],
        account_config_dir=None,
        workdir=tmp_path / "work",
        watch=cw.TraceWatch(),
    )
    planted = {"version": rec.Recorded(lines=["{version} (Not Claude)"], exit_code=0)}
    assert rec.compare(recorded, planted) != []


def test_unrecognised_scenario_matches_no_fixture(pane, tmp_path):
    cfg = logged_in(tmp_path / "cfg")
    env = traced(tmp_path, FAKE_CLAUDE_SCENARIO="unrecognised")
    run(pane, ["remote-control"], tmp_path, cfg, **env)
    shown = cw.shown(pane, trace_of(env))
    assert shown == [fake.UNRECOGNISED_SCREEN]  # positive control: the screen it draws
    for name, fixture in rec.load_fixtures().items():
        assert shown != rec.fill("\n".join(fixture.lines), rec.ANY_VALUES).splitlines(), name


@pytest.mark.parametrize("scenario", sorted(rec.load_observed()))
def test_an_observed_scenario_shows_the_observed_screen(pane, tmp_path, scenario):
    cfg = logged_in(tmp_path / "cfg")
    env = traced(tmp_path, FAKE_CLAUDE_SCENARIO=scenario)
    run(pane, ["remote-control"], tmp_path, cfg, **env)
    ctx = rec.Context(str(tmp_path), str(cfg), rec.load_manifest()["claude_version"])
    raw = "\n".join(cw.shown(pane, trace_of(env)))
    observed = rec.load_observed()[scenario].lines
    assert rec.sanitise(raw, ctx).split("\n") == observed
    assert pane.exit_status() is None  # still running, as it was seen
    # Planted twin: a screen differing from the one shown is not sanitised into the
    # observed one, so the comparison would see a fake showing another.
    changed = f"{raw} planted"
    assert rec.sanitise(changed, ctx).split("\n") != observed
    pane.interrupt()
    assert pane.wait_exit(timeout=HANG_GUARD) == 1  # what follows Ctrl+C was not observed


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


def test_yes_at_the_remote_control_prompt_goes_on_to_the_server_screens(pane, tmp_path):
    """Not observed: what follows `y`. The fake goes on as after the trust question."""
    cfg = logged_in(tmp_path / "cfg")
    run(pane, ["remote-control"], tmp_path, cfg, **traced(tmp_path, FAKE_CLAUDE_SCENARIO=PROMPT))
    pane.wait_for(r"Enable Remote Control", timeout=HANG_GUARD)
    pane.type_line("y")
    pane.wait_for(r"Capacity: 0/", timeout=HANG_GUARD)  # held there until released
    assert "Enable Remote Control" not in pane.text()
    release_connected(tmp_path)
    pane.wait_for(READY, timeout=HANG_GUARD)
    assert pane.exit_status() is None
    pane.interrupt()
    assert (
        pane.wait_exit(timeout=HANG_GUARD)
        == rec.load_fixtures()["server_stopped_after_trust"].exit_code
    )


def test_the_default_threshold_refuses_an_answer_sooner_and_takes_one_at_it(monkeypatch, tmp_path):
    """MIN_ANSWER_SECONDS, which the fake keeps when no test sets it, against a clock only this
    test moves: an answer half of it after the question is refused with the recorded screen,
    and the next, a whole of it after being asked again, is taken."""
    from tests.time_doubles import FakeTime

    clock = FakeTime()
    monkeypatch.setattr(fake, "time", clock)
    monkeypatch.chdir(tmp_path)
    shown = []
    replying = fake.Fake(["remote-control"], env_for(tmp_path / "cfg"))
    answers = iter([(fake.MIN_ANSWER_SECONDS / 2, "n\n"), (fake.MIN_ANSWER_SECONDS, "y\n")])

    def read_line():
        seconds, line = next(answers)
        clock.advance(seconds)
        return line

    monkeypatch.setattr(replying, "read_line", read_line)
    monkeypatch.setattr(replying, "show", lambda name, prompt=False: shown.append(name))
    assert replying.paced_answer("trust_answer_too_fast") == "y\n"
    assert shown == ["trust_answer_too_fast"]


def test_the_accepted_code_signs_in_with_the_recorded_screens(pane, tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / ".claude.json").write_text(json.dumps({"numStartups": 3}))
    run(pane, ["auth", "login", "--claudeai", "--email", "x@example.com"], tmp_path, cfg)
    pane.wait_for(r"Paste code here", timeout=HANG_GUARD)
    pane.type_line(fake.ACCEPTED_CODE)
    pane.wait_for(r"Login successful", timeout=HANG_GUARD)
    pane.type_line("")
    assert (
        pane.wait_exit(timeout=HANG_GUARD) == rec.load_fixtures()["login_succeeded_done"].exit_code
    )
    credentials = cfg / fake.CREDENTIALS
    assert json.loads(credentials.read_text())["claudeAiOauth"]["refreshToken"]
    assert (credentials.stat().st_mode & 0o777) == 0o600
    config = json.loads((cfg / ".claude.json").read_text())
    assert config["numStartups"] == 3  # every other key kept
    assert config["oauthAccount"]["emailAddress"] == "x@example.com"
    assert fake.ACCEPTED_CODE not in pane.text()


def test_logout_removes_the_credentials_file_as_the_real_one_does(pane, tmp_path):
    """tests/test_claude_contract.py holds the real binary to the same."""
    cfg = logged_in(tmp_path / "cfg")
    assert (cfg / fake.CREDENTIALS).exists()  # positive control
    run(pane, ["auth", "logout"], tmp_path, cfg)
    assert pane.wait_exit(timeout=HANG_GUARD) == rec.load_fixtures()["auth_logout"].exit_code
    assert not (cfg / fake.CREDENTIALS).exists()


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
    what a command run by Claude would see: no more, and no less. A release's launcher
    exports ``FAKE_CLAUDE_VERSION`` for the fake alone, and the real Claude's children never
    see a variable of that kind, so the command does not either. Its input, output and exit
    status pass through."""
    # LANG as a workspace's Claude has it: without one, Python itself adds LC_CTYPE.
    given = {
        "HOME": str(tmp_path),
        "PATH": os.environ["PATH"],
        "LANG": "C.UTF-8",
        "RCMAN_PLANTED": "kept as given",
    }
    code = (
        "import os, sys\n"
        "print('\\n'.join(f'{k}={v}' for k, v in os.environ.items()))\n"
        "sys.stderr.write(sys.stdin.read())\n"
        "sys.exit(7)\n"
    )
    proc = fake_exec(
        tmp_path,
        sys.executable,
        "-c",
        code,
        stdin="from stdin\n",
        FAKE_CLAUDE_VERSION="9.9.9",
        **given,
    )
    assert (proc.stderr, proc.returncode) == ("from stdin\n", 7)
    seen = dict(line.split("=", 1) for line in proc.stdout.splitlines())
    assert {k: v for k, v in seen.items() if k != "PWD"} == given
