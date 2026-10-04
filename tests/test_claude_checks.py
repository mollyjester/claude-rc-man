"""The checks a candidate Claude Code version must pass before it is rolled out."""

import json
import os
import shutil
import sys
from pathlib import Path

import pytest

from devtools import record_claude as rec
from helpers import claude_checks as cc
from helpers import rc_helper

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


def script(tmp_path, body, name="claude"):
    path = tmp_path / name
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
    detail = failures["auth_status"]
    assert "auth status" in detail
    assert "exit code 3" in detail
    assert "something else entirely" in detail
    assert "a complaint" in detail  # stderr, which the check does not judge
    assert "something else entirely" in failures["version"]


def test_the_names_of_the_failures_are_the_failed_checks(tmp_path):
    """The fake passes every check but the version, which is not the one it reports."""
    run = runner_for(rec.FAKE_CLAUDE, tmp_path)
    assert list(cc.candidate_failures(run, "0.0.1")) == ["version"]
    assert cc.candidate_checks(run, "0.0.1") == ["version"]


def test_auth_status_is_judged_on_its_standard_output_only(tmp_path):
    """JSON on stderr does not pass, however it reads; noise on stderr does not fail."""
    on_stderr = script(tmp_path, "print('{\"loggedIn\": false}', file=sys.stderr)", "a")
    failures = cc.candidate_failures(runner_for(on_stderr, tmp_path), "1.2.3")
    assert '{"loggedIn": false}' in failures["auth_status"]
    on_stdout = script(
        tmp_path, "print('{\"loggedIn\": false}')\nprint('noise', file=sys.stderr)", "b"
    )
    assert "auth_status" not in cc.candidate_failures(runner_for(on_stdout, tmp_path), "1.2.3")


def test_auth_status_that_does_not_exit_fails_and_says_so(tmp_path):
    """Its JSON is complete before it hangs, and the check still fails it."""
    hangs = script(
        tmp_path,
        "if sys.argv[1:] == ['auth', 'status']:\n"
        "    print('{\"loggedIn\": false}', flush=True)\n"
        "    print('still going', file=sys.stderr, flush=True)\n"
        "    time.sleep(60)",
    )
    run = runner_for(hangs, tmp_path)

    def quick(args, until=None, timeout=cc.TIMEOUT, terminal=True):
        return run(args, until=until, timeout=2, terminal=terminal)

    detail = cc.candidate_failures(quick, "1.2.3")["auth_status"]
    assert "did not exit" in detail
    assert '{"loggedIn": false}' in detail and "still going" in detail


def test_a_check_that_raises_reports_the_exception(tmp_path):
    def raising(args, until=None, timeout=cc.TIMEOUT, terminal=True):
        raise RuntimeError("the runner broke")

    failures = cc.candidate_failures(raising, "1.2.3")
    assert set(failures) == {name for name, _ in cc.CHECKS}
    assert "RuntimeError" in failures["auth_status"]
    assert "the runner broke" in failures["auth_status"]


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
    detail = cc.candidate_failures(run, "1.2.3")["auth_status"]
    assert "SECRETSTATE" not in detail and "SECRETTOKEN" not in detail
    assert "visit {login_url} now" in detail and "key {token} here" in detail
    assert "the rest survives" in detail


def test_redaction_replaces_only_secrets():
    text = f"a {SIGN_IN_URL} b {TOKEN} c"
    assert cc.redact(text) == "a {login_url} b {token} c"
    assert cc.redact("nothing secret here") == "nothing secret here"


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
    assert cc.candidate_failures(runner_for(REAL, tmp_path), version) == {}


@pytest.mark.skipif(
    REAL is None and os.environ.get("RCMAN_REQUIRE_REAL_CLAUDE") != "1",
    reason="claude not installed",
)
def test_the_installed_real_claude_passes_with_a_long_configuration_path(tmp_path):
    """auth status prints the configuration's path in its JSON; on a terminal a path
    longer than a line wraps inside a string, and the JSON no longer reads. The path
    here is longer than rc-helper's terminal is wide."""
    version = rec.installed_version(REAL)
    deep = tmp_path / ("d" * rc_helper.WIDTH)
    deep.mkdir()
    assert cc.candidate_failures(runner_for(REAL, deep), version) == {}


def test_the_terminal_is_the_size_rc_helper_gives_claude(tmp_path):
    """The screens are read on a terminal of the size rc-helper runs Claude's in."""
    run = cc.local_runner("/bin/sh", env={}, cwd=tmp_path, config_dir=tmp_path / "cfg")
    _code, output = run(["-c", "stty size"])
    assert output.split() == [str(rc_helper.HEIGHT), str(rc_helper.WIDTH)], output


TERMINAL_PROBE = f"""#!{sys.executable}
import json, os, sys
if sys.argv[1:] == ["auth", "status"]:
    print(json.dumps({{"loggedIn": False, "terminal": os.isatty(0) or os.isatty(1)}}))
"""


def test_auth_status_is_read_through_pipes_as_rc_helper_reads_it(tmp_path):
    """rc-helper runs auth status with no terminal; the check runs it the same way."""
    probe = tmp_path / "claude"
    probe.write_text(TERMINAL_PROBE, encoding="utf-8")
    probe.chmod(0o755)
    run = runner_for(probe, tmp_path)
    _code, output = run(["auth", "status"], terminal=False)
    assert json.loads(output) == {"loggedIn": False, "terminal": False}
    _code, output = run(["auth", "status"])  # positive control: the default is a terminal
    assert json.loads(output) == {"loggedIn": False, "terminal": True}
    seen = []

    def recording(args, until=None, timeout=cc.TIMEOUT, terminal=True):
        seen.append((args, terminal))
        return run(args, until=until, timeout=timeout, terminal=terminal)

    assert "auth_status" not in cc.candidate_checks(recording, "1.2.3")
    assert (["auth", "status"], False) in seen
    assert all(terminal for args, terminal in seen if args != ["auth", "status"])


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


# --- the guide's table of checks ------------------------------------------------------

GUIDE = Path(__file__).resolve().parent.parent / "docs" / "claude-version.md"
HEADER = "| Check | What it checks |"


def guide_checks(markdown: str) -> list[str]:
    """The checks the guide's table names, in order: the first cell of each row, in code."""
    lines = markdown.splitlines()
    if HEADER not in lines:
        return []
    rows = []
    for line in lines[lines.index(HEADER) + 2 :]:
        if not line.startswith("|"):
            break
        rows.append(line.split("|")[1].strip().strip("`"))
    return rows


def test_the_guide_lists_every_check_in_order():
    names = [name for name, _ in cc.CHECKS]
    assert names  # non-vacuity
    assert guide_checks(GUIDE.read_text(encoding="utf-8")) == names


def test_the_guide_check_fails_on_a_drifted_table():
    names = [name for name, _ in cc.CHECKS]
    table = "\n".join([HEADER, "|---|---|", *(f"| `{n}` | what |" for n in names)])
    assert guide_checks(table) == names
    missing = table.replace(f"| `{names[-1]}` | what |", "")
    assert missing != table  # the plant took effect
    assert guide_checks(missing) != names
    assert guide_checks(table + "\n| `extra` | what |") != names
    assert guide_checks("") == []
