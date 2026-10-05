"""The checks a candidate Claude Code version must pass before it is rolled out."""

import json
import os
import re
import shutil
import sys
import time
from pathlib import Path

import pytest

from devtools import record_claude as rec
from helpers import claude_checks as cc
from helpers import rc_admin as ra
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


def render(text):
    """What the terminal shows for ``text``, fed whole."""
    screen = cc.Screen()
    screen.feed(text, final=True)
    return screen.text()


def said(entry):
    """A check's report as one text."""
    return "\n".join(entry["lines"])


def test_the_fake_passes(tmp_path):
    version = rec.load_manifest()["claude_version"]
    assert cc.candidate_failures(runner_for(rec.FAKE_CLAUDE, tmp_path), version) == {}


def test_every_check_runs(tmp_path):
    version = rec.load_manifest()["claude_version"]
    names = [name for name, _ in cc.CHECKS]
    assert len(names) == len(set(names)) and names  # non-vacuity
    seen = []
    cc.candidate_failures(runner_for(rec.FAKE_CLAUDE, tmp_path), version, on_check=seen.append)
    assert seen == names


def test_a_wrong_version_fails(tmp_path):
    assert "version" in cc.candidate_failures(runner_for(rec.FAKE_CLAUDE, tmp_path), "0.0.1")


def test_a_broken_binary_fails_every_check(tmp_path):
    broken = tmp_path / "claude"
    broken.write_text(f"#!{sys.executable}\nprint('something else entirely')\n")
    broken.chmod(0o755)
    failures = cc.candidate_failures(runner_for(broken, tmp_path), "1.2.3")
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
    detail = said(failures["auth_status"])
    assert "auth status" in detail
    assert "exit code 3" in detail
    assert "something else entirely" in detail
    assert "a complaint" in detail  # stderr, which the check does not judge
    assert "something else entirely" in said(failures["version"])


def test_the_names_of_the_failures_are_the_failed_checks(tmp_path):
    """The fake passes every check but the version, which is not the one it reports."""
    run = runner_for(rec.FAKE_CLAUDE, tmp_path)
    failures = cc.candidate_failures(run, "0.0.1")
    assert list(failures) == ["version"]
    assert failures["version"]["lines"][0] == "ran: claude --version (on a terminal); exit code 0"
    assert failures["version"]["more"] == 0


def test_auth_status_is_judged_on_its_standard_output_only(tmp_path):
    """JSON on stderr does not pass, however it reads; noise on stderr does not fail."""
    on_stderr = script(tmp_path, "print('{\"loggedIn\": false}', file=sys.stderr)", "a")
    failures = cc.candidate_failures(runner_for(on_stderr, tmp_path), "1.2.3")
    assert '{"loggedIn": false}' in said(failures["auth_status"])
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

    detail = said(cc.candidate_failures(quick, "1.2.3")["auth_status"])
    assert "did not exit" in detail
    assert '{"loggedIn": false}' in detail and "still going" in detail


def test_a_check_that_raises_reports_the_exception(tmp_path):
    def raising(args, until=None, timeout=cc.TIMEOUT, terminal=True):
        raise RuntimeError("the runner broke")

    failures = cc.candidate_failures(raising, "1.2.3")
    assert set(failures) == {name for name, _ in cc.CHECKS}
    assert "RuntimeError" in said(failures["auth_status"])
    assert "the runner broke" in said(failures["auth_status"])


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


def test_redaction_replaces_only_secrets():
    text = f"a {SIGN_IN_URL} b {TOKEN} c"
    assert cc.redact(text) == "a {login_url} b {token} c"
    assert cc.redact("nothing secret here") == "nothing secret here"


# Each kind of secret a candidate could print, and the part of it that must not reach a report.
PLANTS = {
    "sign-in URL on claude.ai": (
        "https://claude.ai/oauth/authorize?code=true&state=PLANTSTATE",
        "PLANTSTATE",
    ),
    "sign-in URL on console.anthropic.com": (
        "https://console.anthropic.com/oauth/authorize?client_id=c&state=PLANTSTATE",
        "PLANTSTATE",
    ),
    "sign-in URL on another path": (
        "https://claude.com/v2/authorize?code_challenge=PLANTCHALLENGE",
        "PLANTCHALLENGE",
    ),
    "a URL with a query": ("https://platform.example.com/callback?code=PLANTCODE", "PLANTCODE"),
    "a sign-in URL with its scheme written over": (
        "X" * 38 + "authorize?code=true&client_id=PLANTCLIENT&state=PLANTSTATE",
        "PLANT",
    ),
    "a sign-in URL in capitals": ("HTTPS://CLAUDE.AI/OAUTH/AUTHORIZE?STATE=PLANTSTATE", "PLANT"),
    "a sign-in URL with JSON-escaped slashes": (
        '{"url": "https:\\/\\/claude.ai\\/oauth\\/authorize?state=PLANTSTATE"}',
        "PLANT",
    ),
    "the query of a URL wrapped inside a box": ("│ te=PLANTBOX&code=x │", "PLANTBOX"),
    "an environment URL": ("https://claude.ai/code?environment=env_01PLANTENV", "PLANTENV"),
    "an environment id": ("environment env_01PLANTENV is ready", "PLANTENV"),
    "an environment id after an underscore": ("id=foo_env_01PLANTENV", "PLANTENV"),
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
    "a token as Node prints an object": ("{ accessToken: 'PLANTNODE', other: 1 }", "PLANTNODE"),
    "an organisation as Node prints an object": (
        "{ organizationName: 'Plant Org Ltd', orgId: 'PLANTNODEORG' }",
        "Plant Org",
    ),
    "an organisation id as Node prints an object": ("{ orgId: 'PLANTNODEORG' }", "PLANTNODEORG"),
    "tokens as environment variables": ("ACCESS_TOKEN=PLANTENVA refresh_token=PLANTENVR", "PLANT"),
}


@pytest.mark.parametrize("kind", PLANTS)
def test_no_secret_reaches_the_report(tmp_path, kind):
    """Planted on the terminal, in auth status's standard output, and on its standard error."""
    text, secret = PLANTS[kind]
    leaky = script(
        tmp_path,
        f"print({text!r})\nprint({text!r}, file=sys.stderr)\nprint('the rest survives')",
    )
    run = runner_for(leaky, tmp_path)
    _code, on_terminal = run(["--version"])
    piped = run(["auth", "status"], terminal=False)
    # the plant took effect, everywhere it is looked for below
    assert secret in on_terminal and secret in piped.stdout and secret in piped.stderr
    failures = cc.candidate_failures(run, "1.2.3")
    for name in ("version", "auth_status"):
        detail = said(failures[name])
        assert "the rest survives" in detail  # the report is there
        assert secret not in detail, (kind, name, detail)


def test_redaction_leaves_ordinary_text_and_a_plain_url():
    """The twin of the secrets above: what is not one survives as it was."""
    text = (
        "Error: something went wrong at line 3\n"
        "See https://docs.anthropic.com/en/docs/claude-code for help.\n"
        "env and session are words; so is environment_name, and a@b is not an address\n"
        '{"loggedIn": false, "authMethod": "none"}'
    )
    assert cc.redact(text) == text
    for planted, secret in PLANTS.values():  # and each plant on its own is changed
        assert secret not in cc.redact(planted), planted
    # a word that names OAuth is not a URL
    assert cc.redact("OAuth sign-in is not available") == "OAuth sign-in is not available"


def test_lines_after_a_url_are_kept_where_nothing_wrapped():
    assert cc.report("stdout:\nhttps://x.example/a?b=1\nstderr:\nboom")["lines"] == [
        "stdout:",
        "{url}",
        "stderr:",
        "boom",
    ]


def wrapped_sign_in(tail):
    """A sign-in URL that fills a line of rc-helper's terminal, ``tail`` on the next."""
    start = "visit: https://claude.com/cai/oauth/authorize?code=true&state="
    return start + "A" * (rc_helper.WIDTH - len(start)), tail


def wrapped_token():
    token = "sk-ant-oat01-" + "Q" * 30
    return "x" * (rc_helper.WIDTH - 20) + " " + token[:19], token[19:]


def wrapped_email():
    return "x" * (rc_helper.WIDTH - 10) + " secret.pe", "rson@example.org"


WRAPS = {
    "a sign-in URL": (*wrapped_sign_in("PLANTWRAPTAIL"), ["PLANTWRAPTAIL", "AAAA"]),
    "a token": (*wrapped_token(), ["QQQQ"]),
    "an email address": (*wrapped_email(), ["secret.pe"]),
}


@pytest.mark.parametrize("kind", WRAPS)
def test_a_secret_a_terminal_wrapped_is_blanked_whole(tmp_path, kind):
    first, tail, secrets = WRAPS[kind]
    assert len(first) == rc_helper.WIDTH  # it reaches the edge
    wraps = script(tmp_path, f"print({first!r})\nprint({tail!r})\nprint('Paste code here')")
    run = runner_for(wraps, tmp_path)
    _code, output = run(["--version"])
    assert f"{first}\n{tail}" in output  # the plant took effect: two lines on the terminal
    assert all(secret in output for secret in secrets)
    detail = said(cc.candidate_failures(run, "1.2.3")["version"])
    assert "Paste code here" in detail  # the report is there, and the next line kept
    assert not [secret for secret in secrets if secret in detail], detail


def test_a_line_longer_than_the_terminal_is_not_joined_to_the_next(tmp_path):
    """The terminal itself never wraps what is written, so a longer line was not
    wrapped by the program."""
    first = "visit: https://claude.com/cai/oauth/authorize?state=" + "A" * rc_helper.WIDTH
    long = script(tmp_path, f"print({first!r})\nprint('PLAINNEXT')")
    detail = said(cc.candidate_failures(runner_for(long, tmp_path), "1.2.3")["version"])
    assert "PLAINNEXT" in detail and "AAAA" not in detail


# The sign-in URL the real Claude Code prints, as rc-helper's terminal showed it.
REAL_SIGN_IN = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-"
    "5944d1962f5e&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth"
    "%2Fcode%2Fcallback&scope=org%3Acreate_api_key+user%3Aprofile+user%3Ainference+user%3A"
    "sessions%3Aclaude_code+user%3Amcp_servers+user%3Afile_upload+user%3Aplugins&code_"
    "challenge=3k9JhQyP5kJwvVMqsjVT0_uJiC4IUNOL6dn1HLS5Hto&code_challenge_method=S256&state="
    "SfUtXZtkqoLxb3hjSc-Hce-C-_8pxtUF9ywwKXACvtA&login_hint=check%40example.com"
)


def test_ordinary_words_beside_a_sign_in_url_are_kept(tmp_path):
    words = "callback server platform claude profile"
    beside = script(tmp_path, f"print('visit: ' + {REAL_SIGN_IN!r})\nprint({words!r})")
    run = runner_for(beside, tmp_path)
    _code, output = run(["--version"])
    assert REAL_SIGN_IN in output and words in output  # the plant took effect
    detail = said(cc.candidate_failures(run, "1.2.3")["version"])
    assert "visit: {login_url}" in detail and "3k9JhQ" not in detail  # the URL is blanked
    assert words in detail


def test_redacting_a_screen_with_many_secrets_written_takes_no_longer_than_its_size_does():
    emails = " ".join(f"a{i}@b.io" for i in range(22000))
    assert len(emails) <= cc.MAX_CAPTURE_BYTES
    started = time.monotonic()
    assert cc.redact_screen("on the screen", emails) == "on the screen"
    assert time.monotonic() - started < 10
    values = " ".join(f"token={i:08d}abcdefghijkl" for i in range(cc.MAX_CREDENTIALS + 1))
    started = time.monotonic()
    assert cc.redact_screen("on the screen", values) == cc.WITHHELD
    assert time.monotonic() - started < 10
    few = " ".join(f"token={i:08d}abcdefghijkl" for i in range(cc.MAX_CREDENTIALS))
    assert cc.redact_screen("00000001abcdefghijkl shown", few) == f"{cc.CREDENTIAL} shown"


def test_redacting_a_screen_of_many_lines_takes_no_longer_than_its_size_does():
    started = time.monotonic()
    assert cc.redact_screen("\n" * (cc.MAX_CAPTURE_BYTES * 4), "") == "\n" * (
        cc.MAX_CAPTURE_BYTES * 4
    )
    assert time.monotonic() - started < 10


def test_a_line_after_a_url_that_did_not_reach_the_edge_is_kept(tmp_path):
    first = "visit: https://claude.com/cai/oauth/authorize?state=PLANTSTATE"
    assert len(first) < rc_helper.WIDTH
    short = script(tmp_path, f"print({first!r})\nprint('PLAINNEXT')")
    detail = said(cc.candidate_failures(runner_for(short, tmp_path), "1.2.3")["version"])
    assert "PLAINNEXT" in detail and "PLANTSTATE" not in detail


OVER_URL = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=PLANTCLIENT"
    "&state=PLANTSTATE0123456789abcdef"  # a value shaped as a credential is
)
VALUE_AT = OVER_URL.index("PLANTSTATE")
OVERWRITES = {
    "a carriage return": OVER_URL + "\r" + "X" * 38,
    "an Ink re-render": OVER_URL + "\x1b[2K\x1b[1A\x1b[2K\x1b[G" + "X" * 38,
    "spaces": OVER_URL + "\r" + " " * VALUE_AT,
    "a cursor-forward gap": OVER_URL[:VALUE_AT] + "\x1b[1C" + OVER_URL[VALUE_AT:],
}


@pytest.mark.parametrize("kind", OVERWRITES)
def test_a_sign_in_url_written_over_on_the_terminal_is_still_blanked(tmp_path, kind):
    over_it = script(tmp_path, f"sys.stdout.write({OVERWRITES[kind]!r} + '\\nthe rest\\n')")
    run = runner_for(over_it, tmp_path)
    _code, output = run(["--version"])
    if kind == "an Ink re-render":  # the plant took effect: the line was erased and rewritten
        assert "X" * 38 in output and "PLANTSTATE" not in output
    else:  # the plant took effect: part of it is shown, and not as a whole URL
        assert "PLANTSTATE" in output and OVER_URL not in output
    detail = said(cc.candidate_failures(run, "1.2.3")["version"])
    assert "the rest" in detail and "PLANT" not in detail, detail


def test_control_characters_are_removed_from_what_came_through_pipes(tmp_path):
    raw = b"vis\x1b[31mible\x1b[0m\x1b]0;title\x07 \xc2\x9bkept\x7f\x01\x08 text\x1b(B\r\n\tend"
    noisy = script(
        tmp_path,
        f"sys.stdout.buffer.write({raw!r})\nsys.stderr.buffer.write({raw!r})\nsys.exit(1)",
    )
    run = runner_for(noisy, tmp_path)
    piped = run(["auth", "status"], terminal=False)
    assert "\x1b" in piped.stdout and "\x9b" in piped.stdout and "\x1b" in piped.stderr
    detail = said(cc.candidate_failures(run, "1.2.3")["auth_status"])
    assert detail.count("visible kept text\n\tend") == 2  # stdout and stderr, visible text kept
    controls = [
        ch for ch in detail if (ord(ch) < 0x20 and ch not in "\n\t") or 0x7F <= ord(ch) <= 0x9F
    ]
    assert controls == []


def test_c1_and_delete_are_not_shown_on_a_terminal():
    assert render("a\x9bb\x7fc\x85d") == "abcd"


def test_a_report_is_limited_and_counts_what_it_left_out():
    total = cc.MAX_REPORT_LINES * 3
    lines = [f"line {i}" for i in range(total - 1)] + ["y" * (cc.MAX_REPORT_LINE_LENGTH * 2)]
    lines.insert(1, "z" * (cc.MAX_REPORT_LINE_LENGTH * 2))
    entry = cc.report("\n".join(lines))
    assert entry["lines"][0] == "line 0"
    assert entry["lines"][1] == "z" * (cc.MAX_REPORT_LINE_LENGTH - 1) + cc.CUT
    assert len(entry["lines"]) == cc.MAX_REPORT_LINES
    assert entry["more"] == len(lines) - cc.MAX_REPORT_LINES
    assert cc.report("one\ntwo\n") == {"lines": ["one", "two"], "more": 0}


def test_a_failed_checks_report_is_limited(tmp_path):
    chatty = script(tmp_path, f"for i in range({cc.MAX_REPORT_LINES * 3}): print('line', i)")
    entry = cc.candidate_failures(runner_for(chatty, tmp_path), "1.2.3")["version"]
    assert entry["lines"][0].startswith("ran: claude --version")
    assert len(entry["lines"]) == cc.MAX_REPORT_LINES and entry["more"] > 0


def test_the_tail_of_a_report_keeps_the_last_lines_and_says_so():
    lines = [f"line {i}" for i in range(cc.MAX_REPORT_LINES * 2)]
    entry = cc.report("\n".join(lines), lead=["ran: something"], tail=True)
    assert entry["lines"][:2] == ["ran: something", cc.EARLIER_NOT_SHOWN]
    assert entry["lines"][-1] == lines[-1]
    assert len(entry["lines"]) == cc.MAX_REPORT_LINES
    assert entry["more"] == len(lines) - (cc.MAX_REPORT_LINES - 2)  # every line left out
    cut = cc.report("partial\na\nb", lead=["ran: something"], tail=True, earlier=True)
    assert cut == {"lines": ["ran: something", cc.EARLIER_NOT_SHOWN, "a", "b"], "more": 1}
    short = cc.report("a\nb", lead=["ran: something"], tail=True)
    assert short == {"lines": ["ran: something", "a", "b"], "more": 0}  # nothing left out


def test_redaction_runs_before_the_report_is_cut():
    """A secret that straddles the cut is blanked whole, not left as a fragment."""
    pad = "x" * (cc.MAX_REPORT_LINE_LENGTH - len('"accessToken": "PLANTACC'))
    line = pad + '"accessToken": "PLANTACCESSTOKENVALUE"'
    assert len(line) > cc.MAX_REPORT_LINE_LENGTH  # the cut falls inside the value
    assert "PLANTACC" in line[: cc.MAX_REPORT_LINE_LENGTH]
    entry = cc.report(line)
    assert entry["lines"][0].startswith(pad)  # the line is there
    assert "PLANT" not in entry["lines"][0]
    many = "\n".join(["filler"] * (cc.MAX_REPORT_LINES - 1) + [SIGN_IN_URL[:30], SIGN_IN_URL[30:]])
    assert "SECRET" not in "\n".join(cc.report(many)["lines"])


def test_an_unterminated_secret_is_blanked_to_the_end_of_its_line():
    entry = cc.report('x\n"accessToken": "PLANTPARTIAL\nnext')
    assert entry["lines"] == ["x", '"accessToken": "{token}"', "next"]
    assert "PLANT" not in said(cc.report('"orgName": "Plant Org'))


def test_a_secret_cut_by_the_capture_does_not_survive(tmp_path, monkeypatch):
    """At the real cap a cut line lies beyond what a report shows, so the cap is made
    small enough here for the cut to fall inside a line the report shows."""
    monkeypatch.setattr(cc, "MAX_CAPTURE_BYTES", 40)
    secret = '"accessToken": "PLANTCUTVALUE"'
    head = "kept\n" + "x" * 10
    assert len(head) < cc.MAX_CAPTURE_BYTES < len(head + secret)  # the cap falls inside it
    assert "PLANT" in (head + secret)[: cc.MAX_CAPTURE_BYTES]
    cut = script(tmp_path, f"sys.stdout.write({head + secret!r} + '\\nmore\\n')")
    failures = cc.candidate_failures(runner_for(cut, tmp_path), "1.2.3")
    for name in ("version", "auth_status"):
        detail = said(failures[name])
        assert "kept" in failures[name]["lines"] and "not kept" in detail  # there, and cut
        assert "PLANT" not in detail, name


def test_the_terminal_run_keeps_at_most_its_cap_and_still_finishes(tmp_path):
    size = cc.MAX_CAPTURE_BYTES * 4
    loud = script(
        tmp_path,
        f"sys.stdout.write('first\\n' + 'x' * {size})\nsys.stdout.flush()\nsys.exit(0)",
    )
    run = cc.local_runner(str(loud), env={}, cwd=tmp_path, config_dir=tmp_path / "cfg")
    ran = run([], timeout=30)
    assert ran[0] == 0  # it was read to the end, not left blocked on a full terminal
    assert ran[1] == "first"  # the line the cap cut is left out
    assert ran.dropped >= size - cc.MAX_CAPTURE_BYTES


def test_the_piped_run_keeps_at_most_its_cap_and_still_finishes(tmp_path):
    size = cc.MAX_CAPTURE_BYTES * 4
    loud = script(
        tmp_path,
        f"sys.stdout.write('first\\n' + 'x' * {size})\nsys.stderr.write('e' * {size})\nsys.exit(0)",
    )
    run = cc.local_runner(str(loud), env={}, cwd=tmp_path, config_dir=tmp_path / "cfg")
    ran = run([], timeout=30, terminal=False)
    assert ran[0] == 0
    assert ran.stdout == "first\n" and ran.stderr == ""  # the lines the cap cut are left out
    assert ran.dropped >= 2 * (size - cc.MAX_CAPTURE_BYTES)


def test_a_capture_can_keep_its_tail(tmp_path):
    size = cc.MAX_CAPTURE_BYTES * 2
    loud = script(tmp_path, f"sys.stdout.write('x' * {size} + '\\nlast')\nsys.exit(4)")
    ran = cc.capture([str(loud)], {}, tmp_path, merge=True, tail=True)
    assert ran[0] == 4 and ran.stdout.endswith("\nlast")
    assert len(ran.stdout) <= cc.MAX_CAPTURE_BYTES and ran.dropped > 0


def test_a_capture_without_a_deadline_ends_once_its_program_has_exited(tmp_path):
    """As the installer is run: a process it leaves behind holding its output does not
    keep the run waiting."""
    pid_file = tmp_path / "sleeper"
    leaves = tmp_path / "leaves"
    leaves.write_text(f"#!/bin/sh\nsleep 60 &\necho $! > {pid_file}\necho done\nexit 0\n")
    leaves.chmod(0o755)
    started = time.monotonic()
    try:
        ran = cc.capture([str(leaves)], {"PATH": os.environ["PATH"]}, tmp_path, merge=True)
        assert time.monotonic() - started < 10
        assert (ran[0], ran.stdout) == (0, "done\n")
        sleeper = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while alive(sleeper) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not alive(sleeper)
    finally:
        if pid_file.exists() and alive(int(pid_file.read_text())):
            os.kill(int(pid_file.read_text()), 9)


def leader_and_child(tmp_path, child_body, leader_then):
    """A program that starts a child running ``child_body`` and, only once the child has
    written its first line, does ``leader_then``."""
    started = tmp_path / "child-started"
    child = script(
        tmp_path,
        f"print('first', flush=True)\nopen({str(started)!r}, 'w').close()\n{child_body}",
        "child",
    )
    return script(
        tmp_path,
        f"import os, subprocess\nchild = subprocess.Popen([{str(child)!r}])\n"
        f"while not os.path.exists({str(started)!r}): time.sleep(0.01)\n{leader_then}",
        "leader",
    )


def slow_drain(monkeypatch):
    """A quiet window and a drain long enough that a loaded machine does not end them early."""
    monkeypatch.setattr(cc, "POLL_SECONDS", 0.5)
    monkeypatch.setattr(cc, "DRAIN_SECONDS", 3.0)


def test_a_run_stopped_at_its_pattern_after_its_program_exited_keeps_the_exit_code(
    tmp_path, monkeypatch
):
    """What it started writes the pattern once it has exited, while the terminal is
    still read for what was written last."""
    slow_drain(monkeypatch)
    says = leader_and_child(
        tmp_path,
        "for i in range(20): print(i, flush=True); time.sleep(0.01)\n"
        "print('ready', flush=True); time.sleep(60)",
        "sys.exit(4)",
    )
    ran = cc.run_in_pty([str(says)], {}, tmp_path, "ready", 10)
    assert "first" in ran[1]  # the child started before its leader exited
    assert "ready" in ran[1]  # it stopped at its pattern
    assert ran[0] == 4


def test_a_child_left_holding_the_pipes_is_ended_and_the_exit_code_kept(tmp_path):
    pid_file = tmp_path / "grandchild"
    lingers = script(
        tmp_path,
        "import subprocess\n"
        "child = subprocess.Popen(['sleep', '60'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "print('done', flush=True)",
    )
    started = time.monotonic()
    try:
        ran = cc.capture([str(lingers)], {"PATH": os.environ["PATH"]}, tmp_path, timeout=2)
        grandchild = int(pid_file.read_text())
        assert time.monotonic() - started < 10
        assert (ran[0], ran[1]) == (0, "done\n")  # its own exit, and what it wrote
        deadline = time.monotonic() + 5
        while alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not alive(grandchild)
    finally:
        if pid_file.exists() and alive(int(pid_file.read_text())):
            os.kill(int(pid_file.read_text()), 9)


def alive(pid):
    """Whether ``pid`` runs: neither gone nor a zombie."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().split(") ")[1][0] != "Z"
    except (FileNotFoundError, ProcessLookupError, IndexError):  # gone while being read
        return False


def test_the_line_the_cap_cut_is_counted_as_not_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "MAX_CAPTURE_BYTES", 40)
    cut = script(tmp_path, "sys.stdout.write('kept\\n' + 'x' * 100)")
    ran = cc.capture([str(cut)], {}, tmp_path, timeout=10)
    assert ran.stdout == "kept\n" and ran.dropped == 100


def test_a_program_that_closes_the_terminal_but_does_not_exit_keeps_its_output(tmp_path):
    closes = script(
        tmp_path,
        "import os\nprint('said before closing', flush=True)\n"
        "for fd in (0, 1, 2): os.close(fd)\ntime.sleep(60)",
    )
    run = cc.local_runner(str(closes), env={}, cwd=tmp_path, config_dir=tmp_path / "cfg")
    started = time.monotonic()
    code, output = run([], timeout=3)
    assert code is None and "said before closing" in output
    assert time.monotonic() - started < 10


def test_a_terminal_run_ends_when_its_program_exits_though_a_child_writes_on(tmp_path, monkeypatch):
    slow_drain(monkeypatch)
    pid_file = tmp_path / "grandchild"
    noisy = leader_and_child(
        tmp_path,
        "while True: print('noise', flush=True); time.sleep(0.01)",
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "print('leader done', flush=True)\nsys.exit(5)",
    )
    run = cc.local_runner(str(noisy), env={}, cwd=tmp_path, config_dir=tmp_path / "cfg")
    started = time.monotonic()
    try:
        code, output = run([], timeout=30)
        assert time.monotonic() - started < 15
        assert code == 5 and "leader done" in output and "noise" in output
        grandchild = int(pid_file.read_text())
        deadline = time.monotonic() + 5
        while alive(grandchild) and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not alive(grandchild)
    finally:
        if pid_file.exists() and alive(int(pid_file.read_text())):
            os.kill(int(pid_file.read_text()), 9)


def test_a_program_that_cannot_start_leaves_no_terminal_open(tmp_path):
    before = len(os.listdir("/proc/self/fd"))
    with pytest.raises(FileNotFoundError):
        cc.run_in_pty([str(tmp_path / "missing")], {}, tmp_path, None, 5)
    assert len(os.listdir("/proc/self/fd")) == before


def test_the_report_says_what_was_not_kept(tmp_path):
    loud = script(tmp_path, f"sys.stdout.write('x' * {cc.MAX_CAPTURE_BYTES * 2})")
    detail = said(cc.candidate_failures(runner_for(loud, tmp_path), "1.2.3")["auth_status"])
    assert "not kept" in detail


def test_terminal_output_read_in_pieces_renders_as_it_does_whole():
    raw = (
        "\x1b7\x1b[r\x1b8\x1b[?25hSuccessfully\x1b[14Glogged\x1b[21Gout\r\r\n"
        "\x1b]0;a title\x1b\\caf\u00e9 \x1b[1;32mok\x1b[0m\x1b[3Cdone\rD\x1b[K\n"
    )
    screen = cc.Screen()
    for ch in raw:  # every escape split at every point
        screen.feed(ch)
    screen.feed("", final=True)
    assert screen.text() == render(raw)
    assert "a title" not in screen.text() and "Successfully logged out" in screen.text()


def test_an_unfinished_sequence_is_held_only_so_long():
    screen = cc.Screen()
    screen.feed("before ")
    screen.feed("\x1b]0;")
    for _ in range(cc.MAX_UNFINISHED // 100 + 2):  # an OSC that never ends
        screen.feed("a" * 100)
        assert len(screen._held) <= cc.MAX_UNFINISHED
    assert screen.text().startswith("before ") and "aaaa" in screen.text()


def test_erase_in_line_clears_the_line_or_up_to_the_cursor():
    assert render("abc\x1b[2Kd") == "   d"
    assert render("abcdef\x1b[3D\x1b[1Kx") == "   xef"


def test_a_cursor_move_too_large_to_read_is_still_read():
    assert render("a\x1b[" + "9" * 5000 + "Cb") == "a" + " " * (rc_helper.WIDTH - 2) + "b"


def test_a_tab_goes_no_further_than_the_terminal_is_wide():
    assert render("a" + "\t" * 100 + "b") == "a" + " " * (rc_helper.WIDTH - 2) + "b"


def test_a_cursor_move_goes_no_further_than_the_terminal_is_wide():
    assert render("a\x1b[99999999Cb") == "a" + " " * (rc_helper.WIDTH - 2) + "b"
    assert render("a\x1b[99999999Gb") == "a" + " " * (rc_helper.WIDTH - 2) + "b"


def test_the_pty_runner_finds_its_pattern_across_reads(tmp_path):
    slow = script(
        tmp_path,
        "sys.stdout.write('Paste code'); sys.stdout.flush(); time.sleep(0.5)\n"
        "sys.stdout.write(' here if prompted >'); sys.stdout.flush(); time.sleep(60)",
    )
    run = cc.local_runner(str(slow), env={}, cwd=tmp_path, config_dir=tmp_path / "cfg")
    started = time.monotonic()
    exit_code, output = run([], until=re.escape(rc_helper.LOGIN_PROMPT), timeout=20)
    assert exit_code is None and rc_helper.LOGIN_PROMPT in output
    assert time.monotonic() - started < 10


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

    assert "auth_status" not in cc.candidate_failures(recording, "1.2.3")
    assert (["auth", "status"], False) in seen
    assert all(terminal for args, terminal in seen if args != ["auth", "status"])


def test_terminal_control_sequences_are_stripped():
    raw = (
        '\x1b7\x1b8{\n"loggedIn":false\n}\n\x1b(B\x0f\x1b[>4m\x1b[<u\x1b7\x1b8'
        "\x1b]0;title\x07\x1b[1;32mok\x1b[0m"
    )
    assert render(raw) == '{\n"loggedIn":false\n}\nok'


def test_the_logout_marker_is_in_the_recorded_logout():
    assert cc.LOGGED_OUT_MESSAGE in "\n".join(rec.load_fixtures()["auth_logout"].lines).lower()


def test_cursor_forward_becomes_spaces():
    assert render("Successfully\x1b[1Clogged\x1b[Cout\x1b[3Cnow") == "Successfully logged out   now"


def test_absolute_column_moves_are_rendered():
    raw = (
        "\x1b7\x1b[r\x1b8\x1b[?25hSuccessfully\x1b[14Glogged\x1b[21Gout\x1b[25Gfrom\x1b[30Gyour"
        "\x1b[35GAnthropic\x1b[45Gaccount.\r\r\n\x1b[?25h\x1b(B\x0f\x1b[?1016l\x1b[>4m\x1b[<u"
    )
    assert render(raw) == "Successfully logged out from your Anthropic account.\n"


def test_carriage_return_overwrites_and_erase_clears():
    assert render("loading...\rdone\x1b[K\n") == "done\n"
    assert render("abc\x1b[2Dz\n") == "azc\n"


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


def reported_checks():
    """Every name a refused upgrade can report, in the order they are tried: installing
    the candidate, then the checks."""
    return [ra.INSTALL, *(name for name, _ in cc.CHECKS)]


def test_the_guide_lists_every_check_in_order():
    names = reported_checks()
    assert len(names) > 1  # non-vacuity
    assert guide_checks(GUIDE.read_text(encoding="utf-8")) == names


def test_the_guide_check_fails_on_a_drifted_table():
    names = reported_checks()
    table = "\n".join([HEADER, "|---|---|", *(f"| `{n}` | what |" for n in names)])
    assert guide_checks(table) == names
    missing = table.replace(f"| `{names[-1]}` | what |", "")
    assert missing != table  # the plant took effect
    assert guide_checks(missing) != names
    assert guide_checks(table + "\n| `extra` | what |") != names
    assert guide_checks("") == []
