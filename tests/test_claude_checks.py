"""The checks a candidate Claude Code version must pass before it is rolled out."""

import json
import os
import shutil
import sys
import threading
import time

import pytest

from devtools import record_claude as rec
from helpers import claude_checks as cc
from helpers import screens
from tests.hang_guard import HANG_GUARD

REAL = shutil.which("claude")


def runner_for(binary, tmp_path):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
        "TERM": "xterm-256color",
    }
    return cc.local_runner(str(binary), env=env, cwd=tmp_path, config_dir=tmp_path / "cfg")


def render(text):
    """What the terminal shows for ``text``, fed whole."""
    screen = cc.Screen()
    screen.feed(text, final=True)
    return screen.text()


def said(entry):
    """A check's report as one text."""
    return "\n".join(entry["lines"])


def test_the_fake_passes_every_check_each_run_once(tmp_path):
    version = rec.load_manifest()["claude_version"]
    names = [name for name, _ in cc.CHECKS]
    assert len(names) == len(set(names)) and names  # non-vacuity
    seen = []
    run = runner_for(rec.FAKE_CLAUDE, tmp_path)
    assert cc.candidate_failures(run, version, on_check=seen.append) == {}
    assert seen == names


def script(tmp_path, body):
    path = tmp_path / "claude"
    path.write_text(f"#!{sys.executable}\nimport sys, time\n{body}\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_a_failing_check_reports_what_was_run_and_what_came_back(tmp_path):
    broken = script(
        tmp_path,
        "print('something else entirely')\nprint('a complaint', file=sys.stderr)\nsys.exit(3)",
    )
    failures = cc.candidate_failures(runner_for(broken, tmp_path), "1.2.3")
    assert set(failures) == {name for name, _ in cc.CHECKS}
    detail = said(failures["auth_status"])
    assert "auth status" in detail
    assert "exit code 3" in detail
    assert "something else entirely" in detail
    assert "a complaint" in detail  # stderr, which the check does not judge
    assert "something else entirely" in said(failures["version"])


SIGN_IN_URL = "https://claude.com/cai/oauth/authorize?code=true&state=SECRETSTATE"
TOKEN = "sk-ant-oat01-SECRETTOKEN_x-y"


def test_the_report_carries_no_sign_in_url_or_token(tmp_path):
    leaky = script(
        tmp_path,
        f"print('visit {SIGN_IN_URL} now')\n"
        f"print('key {TOKEN} here', file=sys.stderr)\n"
        "print('the rest survives')",
    )
    run = runner_for(leaky, tmp_path)
    _code, output = run(["auth", "status"])
    assert SIGN_IN_URL in output and TOKEN in output  # the plant took effect
    detail = said(cc.candidate_failures(run, "1.2.3")["auth_status"])
    assert "SECRETSTATE" not in detail and "SECRETTOKEN" not in detail
    assert "visit {login_url} now" in detail and "key {token} here" in detail
    assert "the rest survives" in detail


# Each kind of secret a candidate could print, and the part of it that must not reach a report.
PLANTS = {
    "a sign-in URL": (
        "https://claude.ai/oauth/authorize?code=true&state=PLANTSTATE",
        "PLANTSTATE",
    ),
    "a URL with a query": ("https://platform.example.com/callback?code=PLANTCODE", "PLANTCODE"),
    "an environment URL": ("https://claude.ai/code?environment=env_01PLANTENV", "PLANTENV"),
    "an environment id": ("environment env_01PLANTENV is ready", "PLANTENV"),
    "a session id": ("session cse_01PLANTSESSION started", "PLANTSESSION"),
    "an email address": ("signed in as plant.person@example.org", "plant.person"),
    "an access token": ('{"accessToken": "PLANTACCESS"}', "PLANTACCESS"),
    "a refresh token": ('{"refreshToken":"PLANTREFRESH"}', "PLANTREFRESH"),
    "a bearer token": ("Authorization: Bearer PLANTBEARER.x-y", "PLANTBEARER"),
    "an organisation id": ('"orgId": "PLANTORG"', "PLANTORG"),
    "an organisation name": ('"orgName": "Plant Org Ltd"', "Plant Org"),
    "an organisation uuid": (
        '"organizationUuid": "1b4e28ba-2fa1-11d2-883f-0016d3cca427"',
        "1b4e28ba",
    ),
    "tokens as environment variables": ("ACCESS_TOKEN=PLANTENVA refresh_token=PLANTENVR", "PLANT"),
}


def test_redaction_blanks_each_kind_of_secret_and_leaves_ordinary_text_and_a_plain_url():
    text = (
        "Error: something went wrong at line 3\n"
        "See https://docs.anthropic.com/en/docs/claude-code for help.\n"
        "env and session are words; so is environment_name, and a@b is not an address\n"
        '{"loggedIn": false, "authMethod": "none"}'
    )
    assert cc.redact(text) == text
    for planted, secret in PLANTS.values():  # and each plant on its own is changed
        assert secret not in cc.redact(planted), planted


# Screens and written values whose redaction must take time in proportion to their size: a
# pattern that backtracks would take time exponential in it, and never end in a test's time.
MANY_EMAILS = " ".join(f"a{i}@b.io" for i in range(22000))
TOO_MANY_VALUES = " ".join(f"token={i:08d}abcdefghijkl" for i in range(cc.MAX_CREDENTIALS + 1))
FEW_VALUES = " ".join(f"token={i:08d}abcdefghijkl" for i in range(cc.MAX_CREDENTIALS))
MANY_LINES = "\n" * (cc.MAX_CAPTURE_BYTES * 4)


def redacted(screen: str, written: str) -> str:
    """What cc.redact_screen gives for ``screen`` and ``written``, run in a thread joined
    within the hang guard: one that has not ended by then fails the test, with its teardown
    run, rather than hanging until the hang guard ends the run."""
    result: list[str] = []
    worker = threading.Thread(
        target=lambda: result.append(cc.redact_screen(screen, written)), daemon=True
    )
    worker.start()
    worker.join(HANG_GUARD)
    assert not worker.is_alive(), f"the redaction did not end within {HANG_GUARD}s"
    return result[0]


def test_a_screen_with_many_secrets_written_or_of_many_lines_is_redacted():
    assert len(MANY_EMAILS) <= cc.MAX_CAPTURE_BYTES
    assert redacted("on the screen", MANY_EMAILS) == "on the screen"
    assert redacted("on the screen", TOO_MANY_VALUES) == cc.WITHHELD
    assert redacted("00000001abcdefghijkl shown", FEW_VALUES) == f"{cc.CREDENTIAL} shown"
    assert redacted(MANY_LINES, "") == MANY_LINES


@pytest.mark.benchmark
def test_how_long_redacting_a_screen_takes_by_its_size(record_property):
    """Reports how long each of the redactions above takes against the size of what it reads:
    measures and reports, and decides nothing (make benchmark)."""
    for name, screen, written in (
        ("many secrets written", "on the screen", MANY_EMAILS),
        ("too many values written", "on the screen", TOO_MANY_VALUES),
        ("many lines", MANY_LINES, ""),
    ):
        size = len(screen) + len(written)
        started = time.perf_counter()
        cc.redact_screen(screen, written)
        took = time.perf_counter() - started
        record_property(name, {"bytes": size, "seconds": took})
        print(f"redacting {name}: {size} bytes in {took:.3f}s, {took / size * 1e6:.3f} s per MB")


def test_the_recorded_logouts_terminal_output_read_in_pieces_renders_as_it_does_whole():
    raw = "\x1b7\x1b[r\x1b8\x1b[?25hSuccessfully\x1b[14Glogged\x1b[21Gout\r\r\n"
    screen = cc.Screen()
    for ch in raw:  # every escape split at every point
        screen.feed(ch)
    screen.feed("", final=True)
    assert screen.text() == render(raw) == "Successfully logged out\n"


def test_the_logout_marker_is_in_the_recorded_logout():
    assert cc.LOGGED_OUT_MESSAGE in "\n".join(rec.load_fixtures()["auth_logout"].lines).lower()


def test_the_pty_runner_stops_at_its_pattern_with_no_exit_code(tmp_path):
    script = tmp_path / "slow"
    script.write_text(
        f"#!{sys.executable}\nimport time\nprint('ready', flush=True)\n"
        f"time.sleep({3 * HANG_GUARD:g})\n"
    )
    script.chmod(0o755)
    run = cc.local_runner(str(script), env={}, cwd=tmp_path, config_dir=tmp_path / "cfg")
    exit_code, output = run([], until="ready", timeout=HANG_GUARD)
    assert exit_code is None and "ready" in output


def test_the_terminal_is_the_size_rcman_gives_claude(tmp_path):
    """The screens are read on a terminal of the size rcman runs Claude's in."""
    run = cc.local_runner("/bin/sh", env={}, cwd=tmp_path, config_dir=tmp_path / "cfg")
    _code, output = run(["-c", "stty size"])
    assert output.split() == [str(screens.HEIGHT), str(screens.WIDTH)], output


TERMINAL_PROBE = f"""#!{sys.executable}
import json, os, sys
if sys.argv[1:] == ["auth", "status"]:
    print(json.dumps({{"loggedIn": False, "terminal": os.isatty(0) or os.isatty(1)}}))
"""


def test_auth_status_is_read_through_pipes_as_rcman_reads_it(tmp_path):
    """rcman runs auth status with no terminal; the check runs it the same way."""
    probe = tmp_path / "claude"
    probe.write_text(TERMINAL_PROBE, encoding="utf-8")
    probe.chmod(0o755)
    run = runner_for(probe, tmp_path)
    _code, output = run(["auth", "status"], terminal=False)
    assert json.loads(output) == {"loggedIn": False, "terminal": False}
    _code, output = run(["auth", "status"])  # positive control: the default is a terminal
    assert json.loads(output) == {"loggedIn": False, "terminal": True}
    seen = []

    def recording(args, until=None, timeout=HANG_GUARD, terminal=True):
        seen.append((args, terminal))
        return run(args, until=until, timeout=timeout, terminal=terminal)

    assert "auth_status" not in cc.candidate_failures(recording, "1.2.3")
    assert (["auth", "status"], False) in seen
    assert all(terminal for args, terminal in seen if args != ["auth", "status"])


@pytest.mark.skipif(
    REAL is None and os.environ.get("RCMAN_REQUIRE_REAL_CLAUDE") != "1",
    reason="claude not installed",
)
def test_the_installed_real_claude_passes(tmp_path):
    version = rec.installed_version(REAL)
    assert cc.candidate_failures(runner_for(REAL, tmp_path), version) == {}
