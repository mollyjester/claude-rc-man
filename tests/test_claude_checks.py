"""The checks a candidate Claude Code version must pass before it is rolled out."""

import os
import shutil
import sys

import pytest

from devtools import record_claude as rec
from helpers import claude_checks as cc

REAL = shutil.which("claude")


def runner_for(binary, tmp_path):
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "LANG": "C.UTF-8",
        "TERM": "xterm-256color",
    }
    return cc.local_runner(str(binary), env=env, cwd=tmp_path, config_dir=tmp_path / "cfg")


def test_the_fake_passes(tmp_path):
    version = rec.load_manifest()["claude_version"]
    assert cc.candidate_checks(runner_for(rec.FAKE_CLAUDE, tmp_path), version) == []


def test_every_check_runs(tmp_path):
    version = rec.load_manifest()["claude_version"]
    names = [name for name, _ in cc.CHECKS]
    assert len(names) == len(set(names)) and names  # non-vacuity
    seen = []
    cc.candidate_checks(runner_for(rec.FAKE_CLAUDE, tmp_path), version, on_check=seen.append)
    assert seen == names


def test_a_wrong_version_fails(tmp_path):
    assert "version" in cc.candidate_checks(runner_for(rec.FAKE_CLAUDE, tmp_path), "0.0.1")


def test_a_broken_binary_fails_every_check(tmp_path):
    broken = tmp_path / "claude"
    broken.write_text(f"#!{sys.executable}\nprint('something else entirely')\n")
    broken.chmod(0o755)
    failures = cc.candidate_checks(runner_for(broken, tmp_path), "1.2.3")
    assert set(failures) == {name for name, _ in cc.CHECKS}


def test_the_pty_runner_stops_at_its_pattern_and_kills_the_rest(tmp_path):
    script = tmp_path / "slow"
    script.write_text(
        f"#!{sys.executable}\nimport time\nprint('ready', flush=True)\ntime.sleep(60)\n"
    )
    script.chmod(0o755)
    run = cc.local_runner(str(script), env={}, cwd=tmp_path, config_dir=tmp_path / "cfg")
    exit_code, output = run([], until="ready", timeout=10)
    assert exit_code is None and "ready" in output


@pytest.mark.skipif(
    REAL is None and os.environ.get("RCMAN_REQUIRE_REAL_CLAUDE") != "1",
    reason="claude not installed",
)
def test_the_installed_real_claude_passes(tmp_path):
    version = rec.installed_version(REAL)
    assert cc.candidate_checks(runner_for(REAL, tmp_path), version) == []


def test_terminal_control_sequences_are_stripped():
    raw = (
        '\x1b7\x1b8{\n"loggedIn":false\n}\n\x1b(B\x0f\x1b[>4m\x1b[<u\x1b7\x1b8'
        "\x1b]0;title\x07\x1b[1;32mok\x1b[0m"
    )
    assert cc.strip_terminal(raw) == '{\n"loggedIn":false\n}\nok'


def test_the_logout_marker_is_in_the_recorded_logout():
    assert cc.LOGGED_OUT_MESSAGE in "\n".join(rec.load_fixtures()["auth_logout"].lines).lower()


def test_cursor_forward_becomes_spaces():
    assert (
        cc.strip_terminal("Successfully\x1b[1Clogged\x1b[Cout\x1b[3Cnow")
        == "Successfully logged out   now"
    )


def test_absolute_column_moves_are_rendered():
    raw = (
        "\x1b7\x1b[r\x1b8\x1b[?25hSuccessfully\x1b[14Glogged\x1b[21Gout\x1b[25Gfrom\x1b[30Gyour"
        "\x1b[35GAnthropic\x1b[45Gaccount.\r\r\n\x1b[?25h\x1b(B\x0f\x1b[?1016l\x1b[>4m\x1b[<u"
    )
    assert cc.strip_terminal(raw) == "Successfully logged out from your Anthropic account.\n"


def test_carriage_return_overwrites_and_erase_clears():
    assert cc.strip_terminal("loading...\rdone\x1b[K\n") == "done\n"
    assert cc.strip_terminal("abc\x1b[2Dz\n") == "azc\n"
