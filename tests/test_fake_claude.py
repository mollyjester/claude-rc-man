"""The fake `claude`: it shows its screens, driven through tmux as the real one is, and the
screens it keeps give nothing away."""

import json
import os
import sys
from pathlib import Path

import pytest

from devtools import fake_claude as fake
from devtools import held_folders, tmuxctl
from tests import claude_watches as cw
from tests.hang_guard import HANG_GUARD

FAKE = fake.FAKE_CLAUDE
READY = r"Capacity: [1-9]"
# The observed screen that asks a question, and takes an answer.
PROMPT = "remote_control_prompt"


@pytest.fixture
def pane():
    with held_folders.tmux_sockets("rcp-") as sockets, tmuxctl.Pane(socket_dir=sockets) as p:
        yield p


def env_for(config_dir, **extra):
    """The fake's environment with ``config_dir``, and a home beside it of its own."""
    home = Path(config_dir).parent / "home"
    home.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "TERM": "xterm-256color",
        "LANG": "C.UTF-8",
        "CLAUDE_CONFIG_DIR": str(config_dir),
    }
    return {**env, **extra}


def logged_in(config_dir):
    fake.write_state(config_dir, logged_in=True)
    return config_dir


def run(pane, args, cwd, config_dir, **extra):
    pane.run([str(FAKE), *args], cwd=cwd, env=env_for(config_dir, **extra))


def traced(tmp_path, **extra):
    """The fake's trace, gate and threshold (none of its answers too quick) in a folder of the
    test's, as tests/claude_watches.py's traced_env gives them: a test waits on what the fake
    traced, never for its screen to stay still. ``extra`` overrides them."""
    return {**cw.traced_env(tmp_path / "watch"), **extra}


def trace_of(env):
    return Path(env[fake.TRACE])


def release_connected(tmp_path):
    """Lets a fake given traced(tmp_path) leave its connected stage."""
    cw.release(Path(traced(tmp_path)[fake.GATE]), fake.CONNECTED_STAGE)


def test_unrecognised_scenario_matches_no_fixture(pane, tmp_path):
    cfg = logged_in(tmp_path / "cfg")
    env = traced(tmp_path, FAKE_CLAUDE_SCENARIO="unrecognised")
    run(pane, ["remote-control"], tmp_path, cfg, **env)
    shown = cw.shown(pane, trace_of(env))
    assert shown == [fake.UNRECOGNISED_SCREEN]  # positive control: the screen it draws
    for name, fixture in fake.load_fixtures().items():
        assert shown != fake.fill("\n".join(fixture.lines), fake.ANY_VALUES).splitlines(), name


@pytest.mark.parametrize("scenario", sorted(fake.load_observed()))
def test_an_observed_scenario_shows_the_observed_screen(pane, tmp_path, scenario):
    cfg = logged_in(tmp_path / "cfg")
    env = traced(tmp_path, FAKE_CLAUDE_SCENARIO=scenario)
    run(pane, ["remote-control"], tmp_path, cfg, **env)
    ctx = fake.Context(str(tmp_path), str(cfg), fake.FAKE_VERSION)
    raw = "\n".join(cw.shown(pane, trace_of(env)))
    observed = fake.load_observed()[scenario].lines
    assert fake.sanitise(raw, ctx).split("\n") == observed
    assert pane.exit_status() is None  # still running, as it was seen
    # Planted twin: a screen differing from the one shown is not sanitised into the
    # observed one, so the comparison would see a fake showing another.
    changed = f"{raw} planted"
    assert fake.sanitise(changed, ctx).split("\n") != observed
    pane.interrupt()
    assert pane.wait_exit(timeout=HANG_GUARD) == 1  # what follows Ctrl+C was not observed


def test_every_observed_screen_is_a_scenario_of_the_fake(tmp_path, monkeypatch, capsys):
    """The scenarios are the observed screens themselves: one more is shown by its name."""
    planted = fake.Observed(lines=["", "PLANTED OBSERVED SCREEN"], exit_code=None)
    real = fake.load_observed
    monkeypatch.setattr(fake, "load_observed", lambda: {**real(), "planted_screen": planted})
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
        == fake.load_fixtures()["server_stopped_after_trust"].exit_code
    )


def test_the_default_threshold_refuses_an_answer_sooner_and_takes_one_at_it(monkeypatch, tmp_path):
    """MIN_ANSWER_SECONDS, which the fake keeps when no test sets it, against a clock only this
    test moves: an answer half of it after the question is refused with its screen,
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


def test_the_accepted_code_signs_in_with_its_screens(pane, tmp_path):
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / ".claude.json").write_text(json.dumps({"numStartups": 3}))
    run(pane, ["auth", "login", "--claudeai", "--email", "x@example.com"], tmp_path, cfg)
    pane.wait_for(r"Paste code here", timeout=HANG_GUARD)
    pane.type_line(fake.ACCEPTED_CODE)
    pane.wait_for(r"Login successful", timeout=HANG_GUARD)
    pane.type_line("")
    assert (
        pane.wait_exit(timeout=HANG_GUARD) == fake.load_fixtures()["login_succeeded_done"].exit_code
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
    assert pane.wait_exit(timeout=HANG_GUARD) == fake.load_fixtures()["auth_logout"].exit_code
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


# --- the screens the fake keeps give nothing away ------------------------------------

# What a screen can give away that should have been made a placeholder: each kind, with the
# marks, the text that gives it away.
LEAK_MARKS = {
    "an email address": ("@",),
    "a home or temporary path": ("/home/", "/tmp/"),
    "a sign-in URL": ("oauth/authorize?",),
    "an environment ID": ("environment=env_",),
    "a session ID": ("cse_",),
}
# Planted screen lines, each giving away what one mark finds and nothing else.
LEAK_SAMPLES = (
    "Logged in as alice@example.com",
    "cwd: /home/alice/project",
    f"cwd: {held_folders.TMP}/claude-1001/scratch",  # built: no fixed path here
    "Open https://claude.ai/oauth/authorize?code=true&client_id=x",
    "https://claude.ai/code?environment=env_01ABCdef",
    "[10:00:00] failed for session cse_01ABCdef: 404",
)


def sample_of(mark: str) -> str:
    """The one planted sample that holds ``mark``."""
    [sample] = [sample for sample in LEAK_SAMPLES if mark in sample]
    return sample


def leaks(path: Path) -> list[str]:
    """What the screen in ``path`` gives away that should have been made a placeholder, each
    found by the text that gives it away."""
    text = path.read_text(encoding="utf-8")
    return [leak for leak, found in LEAK_MARKS.items() if any(mark in text for mark in found)]


def test_no_fixture_or_observed_screen_contains_an_unsanitised_secret_or_path():
    for directory in (fake.FIXTURES, fake.OBSERVED):
        paths = sorted(directory.glob("*.txt"))
        assert paths, f"no screens in {directory}"
        assert {path.name: leaks(path) for path in paths if leaks(path)} == {}


def test_the_leak_check_finds_each_mark_as_its_kind(tmp_path):
    """Planted twins: a screen holding only one mark's sample is caught as that mark's
    kind, and as nothing else."""
    planted = tmp_path / "planted.txt"
    for kind, marks in LEAK_MARKS.items():
        for mark in marks:
            planted.write_text(sample_of(mark), encoding="utf-8")
            assert leaks(planted) == [kind], mark


def sample_problems(table: dict[str, tuple[str, ...]], samples: tuple[str, ...]) -> list[str]:
    """The marks no sample holds, and the samples that hold other than one mark."""
    marks = [mark for kinds in table.values() for mark in kinds]
    problems = [f"{mark!r} has no sample" for mark in marks if not any(mark in s for s in samples)]
    for sample in samples:
        if len(held := [mark for mark in marks if mark in sample]) != 1:
            problems.append(f"{sample!r} holds {held}")
    return problems


def test_every_leak_mark_has_a_planted_sample_of_its_own():
    assert sample_problems(LEAK_MARKS, LEAK_SAMPLES) == []


def test_a_leak_mark_with_no_planted_sample_is_found():
    """Planted twin: a mark no sample holds."""
    planted = {**LEAK_MARKS, "a planted kind": ("PLANTED-MARK",)}
    assert sample_problems(planted, LEAK_SAMPLES) == ["'PLANTED-MARK' has no sample"]
