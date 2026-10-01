"""rc-helper: the per-user control script, driven through its real command line.

Tests run ``helpers/rc-helper`` as a subprocess with its test hooks pointing at
a temporary home, a private tmux socket and the fake `claude`. Screen
classification is held to every recorded fixture.
"""

import json
import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

from devtools import fake_claude as fake
from devtools import record_claude as rec
from helpers import rc_helper as h

REPO = Path(__file__).resolve().parent.parent
HELPER = REPO / "helpers" / "rc-helper"


# --- harness ----------------------------------------------------------------------


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    (home / "projects" / "alpha").mkdir(parents=True)
    (home / "projects" / "beta" / "inner").mkdir(parents=True)
    (home / ".hidden").mkdir()
    sock = Path(os.environ.get("TMPDIR", "/tmp")) / f"rch-{os.getpid()}-{tmp_path.name[-12:]}"
    sock.mkdir()
    wrapper = tmp_path / "claude"
    wrapper.write_text(
        f'#!/bin/sh\nFAKE_CLAUDE_READY_SECONDS=0.3 exec {rec.FAKE_CLAUDE} "$@"\n', encoding="utf-8"
    )
    wrapper.chmod(0o755)
    e = {
        "PATH": os.environ["PATH"],
        "RC_HELPER_HOME": str(home),
        "RC_HELPER_CLAUDE": str(wrapper),
        "RC_HELPER_TMUX_SOCKET": str(sock / "rcman.sock"),
    }
    yield e
    subprocess.run(["tmux", "-S", e["RC_HELPER_TMUX_SOCKET"], "kill-server"], capture_output=True)


def helper(env, *args, stdin=None):
    proc = subprocess.run(
        [str(HELPER), *args], env=env, input=stdin, capture_output=True, text=True, timeout=60
    )
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        pytest.fail(f"not JSON (exit {proc.returncode}): {proc.stdout!r} {proc.stderr!r}")
    return proc.returncode, out


def ok(env, *args, stdin=None):
    code, out = helper(env, *args, stdin=stdin)
    assert code == 0, out
    return out


def error_code(env, *args, stdin=None):
    code, out = helper(env, *args, stdin=stdin)
    assert code == 1, out
    return out["error"]["code"]


def log_in(env):
    fake.write_state(Path(env["RC_HELPER_HOME"]) / ".claude-fake", logged_in=True)


def wait_state(env, path, wanted, tries=100):
    import time

    for _ in range(tries):
        server = next((s for s in ok(env, "list")["servers"] if s["path"] == path), None)
        if server and server["state"] == wanted:
            return server
        time.sleep(0.1)
    pytest.fail(f"{path} never reached {wanted}; last: {server}")


# --- classification against every recorded fixture ---------------------------------

SERVER_STATES = {
    "rc_logged_out": "logged_out",
    "trust_prompt": "trust_prompt",
    "trust_answer_too_fast": "trust_prompt",
    "trust_declined": "trust_declined",
    "server_connected_after_trust": "connected",
    "server_connected_resumed": "connected",
    "server_ready_after_trust": "ready",
    "server_ready_resumed": "ready",
    "server_stopped_after_trust": "stopped",
    "server_stopped_resumed": "stopped",
}
LOGIN_STATES = {
    "login_prompt": "awaiting_code",
    "login_failed": "failed",
    "login_succeeded": "succeeded",
    "login_succeeded_done": "succeeded",
}
NOT_READ_AS_SCREENS = {
    "version": "read by `claude --version` output, not a screen",
    "auth_logout": "rc-admin runs it and reads only the exit status",
    "auth_login_help": "the upgrade checks look for options in it",
    "auth_status_logged_in": "parsed as JSON by auth-status",
    "auth_status_logged_out": "parsed as JSON by auth-status",
}
VALUES = {
    **rec.ANY_VALUES,
    "cwd": "/home/u/projects/x",
    "folder": "x",
    "login_url": rec.LOGIN_URL_PREFIX + "code=true&state=abc",
    "environment_url": rec.ENVIRONMENT_URL_PREFIX + "env_abc",
    "session_name": "pfx-quiet-otter",
    "time": "01:02:03",
}


def filled(name):
    fixture = rec.load_fixtures()[name]
    return rec.fill("\n".join(fixture.lines), VALUES).split("\n"), fixture.exit_code


def test_every_fixture_is_accounted_for_exactly_once():
    accounted = [*SERVER_STATES, *LOGIN_STATES, *NOT_READ_AS_SCREENS]
    assert len(accounted) == len(set(accounted))
    assert set(accounted) == set(rec.load_fixtures())


@pytest.mark.parametrize("name,state", sorted(SERVER_STATES.items()))
def test_server_screens_classify_as_recorded(name, state):
    lines, exit_code = filled(name)
    assert h.classify(lines, exit_code)["state"] == state


@pytest.mark.parametrize("name,state", sorted(LOGIN_STATES.items()))
def test_login_screens_classify_as_recorded(name, state):
    lines, exit_code = filled(name)
    assert h.classify_login(lines, exit_code)["state"] == state


def test_ready_screen_yields_url_and_session_name():
    info = h.classify(*filled("server_ready_resumed"))
    assert info["environment_url"] == VALUES["environment_url"]
    assert info["session_name"] == VALUES["session_name"]


def test_connected_screen_has_no_session_name():
    assert h.classify(*filled("server_connected_resumed"))["session_name"] is None


def test_login_prompt_yields_the_url():
    assert h.classify_login(*filled("login_prompt"))["url"] == VALUES["login_url"]


def test_an_unknown_live_screen_is_unrecognised_with_raw_output():
    info = h.classify(["something new and strange"], None)
    assert info["state"] == "unrecognised"
    assert info["screen"] == ["something new and strange"]


def test_an_unknown_exit_is_exited_with_its_code():
    info = h.classify(["crashed"], 3)
    assert (info["state"], info["exit_code"]) == ("exited", 3)


def test_an_empty_live_screen_is_starting():
    assert h.classify([], None)["state"] == "starting"


# --- paths ----------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    (home / "a" / "b").mkdir(parents=True)
    (home / "file").write_text("x")
    (tmp_path / "outside").mkdir()
    (home / "escape").symlink_to(tmp_path / "outside")
    (home / "inner-link").symlink_to(home / "a")
    return home


@pytest.mark.parametrize(
    "rel,code",
    [
        ("", "path_is_home"),
        (".", "path_is_home"),
        ("a/..", "path_is_home"),
        ("/etc", "path_invalid"),
        ("a\0b", "path_invalid"),
        ("../x", "path_outside_home"),
        ("a/../../x", "path_outside_home"),
        ("escape", "path_outside_home"),
        ("missing", "path_not_found"),
        ("file", "path_not_directory"),
    ],
)
def test_folder_refusals(home, rel, code):
    with pytest.raises(h.HelperError) as err:
        h.resolve_folder(home, rel)
    assert err.value.code == code


def test_folder_inside_home_resolves(home):
    assert h.resolve_folder(home, "a/b") == (home / "a" / "b").resolve()


def test_symlink_inside_home_resolves_to_its_target(home):
    assert h.resolve_folder(home, "inner-link") == (home / "a").resolve()


def test_home_itself_is_allowed_only_for_listing(home):
    assert h.resolve_folder(home, "", allow_home=True) == home.resolve()


def test_refusals_through_the_command_line(env):
    assert error_code(env, "start", "", "--prefix", "p") == "path_is_home"
    assert error_code(env, "start", "../x", "--prefix", "p") == "path_outside_home"
    assert error_code(env, "tree", "nope") == "path_not_found"


# --- tree -----------------------------------------------------------------------------


def test_tree_lists_subfolders_and_hides_dot_folders(env):
    out = ok(env, "tree")
    assert [e["path"] for e in out["entries"]] == ["projects"]
    assert out["entries"][0]["has_children"] is True


def test_tree_shows_dot_folders_on_request(env):
    paths = [e["path"] for e in ok(env, "tree", "--all")["entries"]]
    assert ".hidden" in paths


def test_tree_lists_a_subfolder(env):
    entries = ok(env, "tree", "projects")["entries"]
    assert [(e["path"], e["has_children"]) for e in entries] == [
        ("projects/alpha", False),
        ("projects/beta", True),
    ]


def test_tree_skips_symlinks_leading_outside_home(env, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    (Path(env["RC_HELPER_HOME"]) / "projects" / "out").symlink_to(tmp_path / "elsewhere")
    paths = [e["path"] for e in ok(env, "tree", "projects")["entries"]]
    assert "projects/out" not in paths


def test_tree_depth_is_bounded(env):
    assert error_code(env, "tree", "--depth", str(h.MAX_TREE_DEPTH + 1)) == "depth_invalid"
    nested = ok(env, "tree", "--depth", "2")["entries"]
    projects = next(e for e in nested if e["path"] == "projects")
    assert [c["path"] for c in projects["children"]] == ["projects/alpha", "projects/beta"]


# --- server lifecycle through the fake ---------------------------------------------


def test_start_refuses_without_a_claude_login(env):
    assert error_code(env, "start", "projects/alpha", "--prefix", "pfx") == "claude_not_logged_in"


def test_start_trust_confirm_ready_stop_resume_forget(env):
    log_in(env)
    started = ok(env, "start", "projects/alpha", "--prefix", "pfx")
    assert started["server"]["state"] == "trust_prompt"

    confirmed = ok(env, "confirm", "projects/alpha", "--answer", "y")
    assert confirmed["server"]["state"] in ("connected", "ready")
    ready = wait_state(env, "projects/alpha", "ready")
    assert ready["session_name"].startswith("pfx-")
    assert ready["environment_url"].startswith(rec.ENVIRONMENT_URL_PREFIX)

    assert error_code(env, "start", "projects/alpha", "--prefix", "pfx") == "already_running"

    stopped = ok(env, "stop", "projects/alpha")
    assert stopped["server"]["state"] == "stopped"
    assert error_code(env, "stop", "projects/alpha") == "not_running"

    resumed = ok(env, "resume", "projects/alpha", "--prefix", "pfx")
    assert resumed["server"]["state"] in ("connected", "ready")
    again = wait_state(env, "projects/alpha", "ready")
    assert again["session_name"] == ready["session_name"]

    ok(env, "stop", "projects/alpha")
    ok(env, "forget", "projects/alpha")
    assert [s["path"] for s in ok(env, "list")["servers"]] == []


def test_resume_needs_a_stopped_server(env):
    log_in(env)
    assert error_code(env, "resume", "projects/alpha", "--prefix", "pfx") == "not_stopped"


def test_forget_refuses_a_running_server(env):
    log_in(env)
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    assert error_code(env, "forget", "projects/alpha") == "still_running"


def test_declining_trust_ends_the_server(env):
    log_in(env)
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    out = ok(env, "confirm", "projects/alpha", "--answer", "n")
    assert out["server"]["state"] == "trust_declined"


def test_confirm_refuses_when_nothing_is_asked(env):
    log_in(env)
    fake.write_state(
        Path(env["RC_HELPER_HOME"]) / ".claude-fake",
        logged_in=True,
        trusted=[str(Path(env["RC_HELPER_HOME"]).resolve() / "projects" / "alpha")],
    )
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    wait_state(env, "projects/alpha", "ready")
    assert error_code(env, "confirm", "projects/alpha", "--answer", "y") == "nothing_to_confirm"


def test_list_reports_each_folder_separately(env):
    log_in(env)
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    ok(env, "start", "projects/beta", "--prefix", "pfx")
    assert sorted(s["path"] for s in ok(env, "list")["servers"]) == [
        "projects/alpha",
        "projects/beta",
    ]


def test_output_returns_the_raw_screen(env):
    log_in(env)
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    out = ok(env, "output", "projects/alpha")
    assert any("[y/N]" in line for line in out["screen"])


def test_unrecognised_screen_is_reported_with_its_output(env, tmp_path):
    log_in(env)
    wrapper = Path(env["RC_HELPER_CLAUDE"])
    wrapper.write_text(
        f'#!/bin/sh\nFAKE_CLAUDE_SCENARIO=unrecognised exec {rec.FAKE_CLAUDE} "$@"\n',
        encoding="utf-8",
    )
    out = ok(env, "start", "projects/alpha", "--prefix", "pfx")
    assert out["server"]["state"] == "unrecognised"
    assert fake.UNRECOGNISED_SCREEN in out["server"]["screen"]


@pytest.mark.parametrize("prefix", ["", "-x", "a b", "a;b", "x" * (h.MAX_PREFIX_LENGTH + 1)])
def test_invalid_prefix_is_refused(env, prefix):
    assert error_code(env, "start", "projects/alpha", f"--prefix={prefix}") == "prefix_invalid"


# --- what the server is started with ---------------------------------------------


def test_server_command_is_fixed_and_shell_free(tmp_path):
    cfg = h.Config(home=tmp_path, claude=tmp_path / "claude", tmux_socket=None)
    argv = h.server_command(cfg, prefix="pfx")
    assert argv[0] == "/usr/bin/env" and argv[1] == "-i"
    command = argv[argv.index(str(tmp_path / "claude")) :]
    assert command == [
        str(tmp_path / "claude"),
        "remote-control",
        "--remote-control-session-name-prefix",
        "pfx",
        "--permission-mode",
        "default",
    ]


def test_server_environment_is_the_allowlist(tmp_path):
    cfg = h.Config(home=tmp_path, claude=tmp_path / "claude", tmux_socket=None)
    argv = h.server_command(cfg, prefix="pfx")
    assigned = [a.split("=", 1)[0] for a in argv[2:] if "=" in a and not a.startswith("/")]
    assert sorted(assigned) == sorted(h.child_env(cfg))
    assert h.child_env(cfg)["HOME"] == str(tmp_path)
    assert h.child_env(cfg)["DISABLE_AUTOUPDATER"] == "1"


def test_production_config_ignores_nothing_but_the_test_hooks():
    cfg = h.config_from_env({})
    import pwd

    assert cfg.home == Path(pwd.getpwuid(os.getuid()).pw_dir)
    assert cfg.claude == cfg.home / ".local" / "bin" / "claude"
    assert cfg.tmux_socket is None  # the named socket h.TMUX_SOCKET_NAME
    assert h.config_from_env({"HOME": "/elsewhere"}).home == cfg.home  # $HOME is not trusted


def test_refuses_to_run_as_root(monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 0)
    assert h.main(["list"], {}) == 1


# --- sign-in -------------------------------------------------------------------------


def test_auth_status_logged_out_and_in(env):
    assert ok(env, "auth-status") == {"logged_in": False}
    log_in(env)
    out = ok(env, "auth-status")
    assert out["logged_in"] is True
    assert out["email"] == fake.FAKE_ACCOUNT["email"]


def test_login_returns_the_sign_in_url(env):
    out = ok(env, "auth-login", "--email", "someone@example.com")
    assert out["url"].startswith(rec.LOGIN_URL_PREFIX)
    assert "someone%40example.com" in out["url"]


def test_login_code_is_read_from_stdin_and_never_echoed(env):
    ok(env, "auth-login", "--email", "someone@example.com")
    code, out = helper(env, "auth-code", stdin="SECRET-CODE-123\n")
    assert code == 1 and out["error"]["code"] == "login_failed"  # the fake never accepts
    assert "SECRET-CODE-123" not in json.dumps(out)


def test_login_code_never_reaches_any_argv(tmp_path):
    seen = []

    def runner(args, **kw):
        seen.append((list(args), kw.get("input")))
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    cfg = h.Config(home=tmp_path, claude=tmp_path / "claude", tmux_socket=tmp_path / "s")
    h.Tmux(cfg, runner=runner).paste_line("rc-login", "SECRET-CODE-123")
    assert seen, "nothing was run"
    assert not any("SECRET-CODE-123" in " ".join(args) for args, _ in seen)
    assert any(inp and "SECRET-CODE-123" in inp for _, inp in seen)


def test_auth_code_without_a_login_in_progress(env):
    assert error_code(env, "auth-code", stdin="x\n") == "no_login_in_progress"


@pytest.mark.parametrize("email", ["", "no-at", "a@b@c", "a b@c.d", "x@y;rm"])
def test_invalid_email_is_refused(env, email):
    assert error_code(env, "auth-login", "--email", email) == "email_invalid"


# --- the command-line surface ----------------------------------------------------


def test_every_error_code_has_a_message():
    used = set(
        re.findall(r'HelperError\(\s*"([a-z_]+)"', (REPO / "helpers" / "rc_helper.py").read_text())
    )
    assert used, "no error codes found"  # non-vacuity
    assert used <= set(h.MESSAGES)
    assert set(h.MESSAGES) <= used | {"internal_error", "usage"}


def test_usage_errors_are_json_too(env):
    code, out = helper(env, "no-such-verb")
    assert code == 2 and out["error"]["code"] == "usage"


def test_documented_verbs_match_the_command_line():
    doc = (REPO / "project-knowledge" / "rc-helper.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`rc-helper ([a-z][a-z-]*)", doc))
    assert documented == set(h.VERBS)


def test_launcher_is_executable_and_runs_isolated():
    mode = HELPER.stat().st_mode
    assert mode & stat.S_IXUSR
    assert HELPER.read_text(encoding="utf-8").splitlines()[0] == "#!/usr/bin/python3 -I"


# --- sudoers template --------------------------------------------------------------


def test_sudoers_template_is_valid_and_narrow(tmp_path):
    template = (REPO / "helpers" / "sudoers.rc-helper").read_text(encoding="utf-8")
    rendered = template.replace("@HELPER@", "/opt/rcman/helpers/rc-helper").replace(
        "@APP_USER@", "rcman"
    )
    path = tmp_path / "sudoers"
    path.write_text(rendered, encoding="utf-8")
    check = subprocess.run(
        ["/usr/sbin/visudo", "-c", "-f", str(path)], capture_output=True, text=True
    )
    assert check.returncode == 0, check.stdout + check.stderr
    rules = [line for line in rendered.splitlines() if line.strip() and not line.startswith("#")]
    assert "rcman ALL=(%rcusers) NOPASSWD: /opt/rcman/helpers/rc-helper" in rules
    assert not any("env_keep" in line or "SETENV" in line for line in rules)
    assert any(
        line.startswith("Defaults!/opt/rcman/helpers/rc-helper") and "env_reset" in line
        for line in rules
    )


# --- sign-in success and login expiry ------------------------------------------------


def credentials(env, **oauth):
    path = Path(env["RC_HELPER_HOME"]) / ".claude" / ".credentials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"claudeAiOauth": {"accessToken": "AT-SECRET", "refreshToken": "RT-SECRET", **oauth}}
        )
    )


def test_an_accepted_code_connects_the_account(env):
    fake.write_state(
        Path(env["RC_HELPER_HOME"]) / ".claude-fake", logged_in=False, accept_codes=["GOOD"]
    )
    ok(env, "auth-login", "--email", "someone@example.com")
    out = ok(env, "auth-code", stdin="GOOD\n")
    assert out["logged_in"] is True
    assert ok(env, "auth-status")["logged_in"] is True


def test_auth_status_reports_when_the_login_expires(env):
    log_in(env)
    credentials(env, refreshTokenExpiresAt=1_900_000_000_000)
    out = ok(env, "auth-status")
    assert out["login_expires_at"] == "2030-03-17T17:46:40+00:00"
    assert "SECRET" not in json.dumps(out)


def test_login_expiry_is_unknown_without_the_field(env):
    log_in(env)
    assert ok(env, "auth-status")["login_expires_at"] is None  # no credentials file
    credentials(env)
    assert ok(env, "auth-status")["login_expires_at"] is None  # no expiry field
    path = Path(env["RC_HELPER_HOME"]) / ".claude" / ".credentials.json"
    path.write_text("not json")
    assert ok(env, "auth-status")["login_expires_at"] is None  # unreadable
