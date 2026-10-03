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
from helpers import git_run
from helpers import rc_helper as h
from tests.github_server import UNREACHABLE
from tests.helper_env import helper_env

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
    e = helper_env(home, wrapper, sock / "rcman.sock", path=os.environ["PATH"])
    yield e
    subprocess.run(["tmux", "-S", e["RC_HELPER_TMUX_SOCKET"], "kill-server"], capture_output=True)


def helper(env, *args, stdin=None):
    """rc-helper's exit status and answer; ``stdin`` is text, or bytes sent as they are."""
    if isinstance(stdin, str):
        stdin = stdin.encode()
    proc = subprocess.run(
        [str(HELPER), *args], env=env, input=stdin, capture_output=True, timeout=60
    )
    proc.stdout = proc.stdout.decode("utf-8", "replace")
    proc.stderr = proc.stderr.decode("utf-8", "replace")
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


def test_the_home_folder_relative_to_itself_is_the_empty_path(home):
    assert h.relative(home, (home / "a" / "b").resolve()) == "a/b"  # positive control
    assert h.relative(home, home.resolve()) == ""


def test_the_home_folder_listed_as_dot_is_listed_as_the_home(env):
    assert ok(env, "tree", "--", ".")["path"] == ""
    assert ok(env, "tree", "--", "projects/..")["path"] == ""
    assert ok(env, "tree", "--", "projects")["path"] == "projects"  # positive control


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


def test_resume_needs_a_stopped_server(env, tmp_path):
    log_in(env)
    assert (
        error_code(env, "resume", "projects/alpha", "--prefix", "pfx") == "not_stopped"
    )  # missing

    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    assert (
        ok(env, "confirm", "projects/alpha", "--answer", "n")["server"]["state"] == "trust_declined"
    )
    assert error_code(env, "resume", "projects/alpha", "--prefix", "pfx") == "not_stopped"

    # A server whose claude finds itself signed out, though auth status said otherwise.
    signed_out = tmp_path / "signed-out"
    fake.write_state(signed_out, logged_in=False)
    wrapper = Path(env["RC_HELPER_CLAUDE"])
    wrapper.write_text(
        '#!/bin/sh\nif [ "$1" = remote-control ]; then '
        f"export CLAUDE_CONFIG_DIR={signed_out}; fi\n"
        f'exec {rec.FAKE_CLAUDE} "$@"\n',
        encoding="utf-8",
    )
    ok(env, "start", "projects/beta", "--prefix", "pfx")
    wait_state(env, "projects/beta", "logged_out")
    assert error_code(env, "resume", "projects/beta", "--prefix", "pfx") == "not_stopped"


def test_a_server_that_died_can_be_resumed(env):
    import signal

    log_in(env)
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    ok(env, "confirm", "projects/alpha", "--answer", "y")
    ready = wait_state(env, "projects/alpha", "ready")
    tmux = ["tmux", "-S", env["RC_HELPER_TMUX_SOCKET"]]
    session = h.session_for("projects/alpha")
    pid = subprocess.run(
        [*tmux, "display-message", "-p", "-t", f"={session}:", "#{pane_pid}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    os.kill(int(pid), signal.SIGKILL)
    died = wait_state(env, "projects/alpha", "exited")
    assert died["exit_code"] is not None

    resumed = ok(env, "resume", "projects/alpha", "--prefix", "pfx")
    assert resumed["server"]["state"] in ("connected", "ready")  # no trust question
    again = wait_state(env, "projects/alpha", "ready")
    assert again["session_name"] == ready["session_name"]


def test_a_server_whose_folder_is_gone_can_still_be_reached(env):
    import shutil

    log_in(env)
    ok(env, "start", "projects/beta/inner", "--prefix", "pfx")
    shutil.rmtree(Path(env["RC_HELPER_HOME"]) / "projects" / "beta")
    assert error_code(env, "start", "projects/beta/inner", "--prefix", "pfx") == "path_not_found"

    assert ok(env, "output", "projects/beta/inner")["server"]["state"] == "trust_prompt"
    assert ok(env, "stop", "projects/beta/inner")["server"]["exit_code"] is not None
    assert ok(env, "forget", "projects/beta/inner") == {"forgotten": "projects/beta/inner"}
    assert ok(env, "list")["servers"] == []


def test_a_folder_that_is_gone_is_still_confined_to_the_home(env, tmp_path):
    (tmp_path / "elsewhere").mkdir()
    (Path(env["RC_HELPER_HOME"]) / "projects" / "out").symlink_to(tmp_path / "elsewhere")
    for verb in (["stop"], ["forget"], ["output"], ["confirm", "--answer=y"]):
        assert error_code(env, *verb, "projects/gone") == "unknown_server"  # positive control
        assert error_code(env, *verb, "../gone") == "path_outside_home"
        assert error_code(env, *verb, "projects/out/gone") == "path_outside_home"
        assert error_code(env, *verb, "projects/gone/../..") == "path_is_home"
        assert error_code(env, *verb, "/gone") == "path_invalid"


def test_the_trust_question_of_a_server_whose_folder_is_gone_can_still_be_answered(env):
    import shutil

    log_in(env)
    ok(env, "start", "projects/beta/inner", "--prefix", "pfx")
    shutil.rmtree(Path(env["RC_HELPER_HOME"]) / "projects" / "beta")
    out = ok(env, "confirm", "projects/beta/inner", "--answer", "n")
    assert out["server"]["state"] == "trust_declined"


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
    assert cfg.github == h.GITHUB_URL == "https://github.com/"
    assert h.config_from_env({"RC_HELPER_GITHUB": UNREACHABLE}).github == UNREACHABLE
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


def rule_copy_problems(source: str, names=("EMAIL",)) -> list[str]:
    """What keeps a module's rules from being users_file's: not imported from it, or assigned."""
    import ast

    tree = ast.parse(source)
    problems = []
    for name in names:
        imported = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "helpers.users_file"
            and any(alias.name == name and alias.asname is None for alias in node.names)
            for node in ast.walk(tree)
        )
        if not imported:
            problems.append(f"{name} is not imported from helpers.users_file")
    for node in ast.walk(tree):
        targets = (
            node.targets
            if isinstance(node, ast.Assign)
            else [node.target]
            if isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr))
            else []
        )
        assigned = {n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)}
        for name in names:
            if name in assigned:
                problems.append(f"{name} is assigned on line {node.lineno}")
    return problems


def test_the_email_rule_is_the_users_files_rule():
    source = (REPO / "helpers" / "rc_helper.py").read_text(encoding="utf-8")
    assert "EMAIL" in source  # positive control: the module uses the rule
    assert rule_copy_problems(source) == []


def test_the_email_rule_check_catches_a_copy():
    """Planted twin: a module keeping its own copy of the rule is caught, imported or not."""
    right = "import re\nfrom helpers.users_file import EMAIL\n"
    assert rule_copy_problems(right) == []
    copy = 'EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\\.[A-Za-z0-9-]+)+")\n'
    assert rule_copy_problems("import re\n" + copy) != []
    assert rule_copy_problems(right + copy) != []  # imported, then replaced
    renamed = right.replace("import EMAIL", "import EMAIL as ADDRESS")
    assert renamed != right and rule_copy_problems(renamed) != []


# --- the commit author's name -----------------------------------------------------

AUTHOR_NAMES_ACCEPTED = [
    "Bob B",
    "Сергей Лихарев",
    "O'Brien",
    "Bob Jr.",
    "(Bob)",
    "x",
    "x" * h.MAX_AUTHOR_NAME_LENGTH,
    f"a{chr(0x200C)}b",  # ZERO WIDTH NON-JOINER, see ALLOWED_FORMAT
    f"a{chr(0x200D)}b",  # ZERO WIDTH JOINER
]
AUTHOR_NAMES_REFUSED = [
    "",
    " ",
    " Bob",
    "Bob ",
    "a\nb",
    "a\rb",
    "a\tb",
    "a\x00b",
    "a\x85b",
    f"a{chr(0x2028)}b",  # LINE SEPARATOR
    f"a{chr(0x2029)}b",  # PARAGRAPH SEPARATOR
    "a<b",
    "a>b",
    f"a{chr(0x200B)}b",  # ZERO WIDTH SPACE
    f"a{chr(0x202E)}b",  # RIGHT-TO-LEFT OVERRIDE
    f"a{chr(0x2066)}b",  # LEFT-TO-RIGHT ISOLATE
    f"a{chr(0xFEFF)}b",  # ZERO WIDTH NO-BREAK SPACE
    f"a{chr(0x00AD)}b",  # SOFT HYPHEN
    f"a{chr(0x1D173)}b",  # MUSICAL SYMBOL BEGIN BEAM, beyond the BMP
    f"a{chr(0xE0020)}b",  # TAG SPACE
    f"a{chr(0x3164)}b",  # HANGUL FILLER
    chr(0x3164),
    chr(0x200C),  # ZWNJ and ZWJ: not alone, not at either end
    chr(0x200C) + chr(0x200D),
    chr(0x200C) + "Bob",
    "Bob" + chr(0x200D),
    "'Bob'",
    '"Bob"',
    "Bob;",
    "Bob\\",
    ",Bob",
    ":Bob",
    "x" * (h.MAX_AUTHOR_NAME_LENGTH + 1),
]
AUTHOR_NAMES = [(n, True) for n in AUTHOR_NAMES_ACCEPTED] + [
    (n, False) for n in AUTHOR_NAMES_REFUSED
]

# Format characters (Unicode category Cf) a name may hold, and why: Persian and
# Indic names are written with them.
ALLOWED_FORMAT = {0x200C: "ZERO WIDTH NON-JOINER", 0x200D: "ZERO WIDTH JOINER"}


def hangul_fillers() -> list[str]:
    """The Hangul fillers: letters by category, but they show as nothing."""
    import sys
    import unicodedata

    return [
        chr(c)
        for c in range(sys.maxunicode + 1)
        if {"HANGUL", "FILLER"} <= set(unicodedata.name(chr(c), "").split())
    ]


def format_characters() -> list[str]:
    """Every invisible format character this Python's Unicode database knows."""
    import sys
    import unicodedata

    return [chr(c) for c in range(sys.maxunicode + 1) if unicodedata.category(chr(c)) == "Cf"]


@pytest.mark.parametrize(("name", "accepted"), AUTHOR_NAMES)
def test_author_name_rule(name, accepted):
    assert bool(h.AUTHOR_NAME.fullmatch(name)) is accepted


# The rules that refuse the same invisible characters: a commit author's name,
# and a folder's.
NAME_RULES = {"author name": "AUTHOR_NAME", "folder name": "FOLDER_NAME"}


@pytest.mark.parametrize("rule", NAME_RULES.values(), ids=NAME_RULES)
def test_every_format_character_is_refused_but_zwnj_and_zwj_inside_a_name(rule):
    import unicodedata

    rule = getattr(h, rule)
    found = format_characters()
    assert chr(0x200B) in found and chr(0xE0020) in found  # positive control
    for code, name in ALLOWED_FORMAT.items():  # each exemption is still what it is said to be
        assert chr(code) in found and unicodedata.name(chr(code)) == name
        assert rule.fullmatch(f"a{chr(code)}b")
    let_through = [
        (f"U+{ord(c):04X}", name)
        for c in found
        for name in (f"a{c}b", c, f"{c}b", f"a{c}")
        if rule.fullmatch(name) and (ord(c) not in ALLOWED_FORMAT or name != f"a{c}b")
    ]
    assert let_through == []  # the allowed ones only inside a name, never alone or at an end


@pytest.mark.parametrize("rule", NAME_RULES.values(), ids=NAME_RULES)
def test_hangul_fillers_are_refused_in_a_name(rule):
    rule = getattr(h, rule)
    found = hangul_fillers()
    assert chr(0x3164) in found  # positive control: HANGUL FILLER
    assert {code for first, last in h.HANGUL_FILLERS for code in range(first, last + 1)} == {
        ord(c) for c in found
    }
    for c in found:
        assert not rule.fullmatch(f"a{c}b"), f"U+{ord(c):04X}"
        assert not rule.fullmatch(c), f"U+{ord(c):04X}"


def recorded_by_git(tmp_path, name: str) -> str:
    """The author name real git records for ``name``."""
    proc = subprocess.run(
        ["git", "var", "GIT_AUTHOR_IDENT"],
        env={
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": "bob@example.com",
        },
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout.rpartition(" <bob@example.com>")[0]


def test_git_records_every_accepted_name_as_given(tmp_path):
    assert recorded_by_git(tmp_path, ";Bob;") == "Bob"  # positive control: git does change some
    for name in AUTHOR_NAMES_ACCEPTED:
        assert h.AUTHOR_NAME.fullmatch(name), name
        assert recorded_by_git(tmp_path, name) == name


def test_names_git_would_trim_are_refused(tmp_path):
    """The printable characters git strips from the ends of a name are refused there."""
    printable = [chr(c) for c in range(0x21, 0x7F)]
    trimmed = [c for c in printable if recorded_by_git(tmp_path, f"{c}Bob{c}") == "Bob"]
    assert ";" in trimmed and "." not in trimmed  # positive control, and git keeps a dot
    for c in printable:
        refused = not h.AUTHOR_NAME.fullmatch(f"{c}Bob") and not h.AUTHOR_NAME.fullmatch(f"Bob{c}")
        assert refused is (c in trimmed), repr(c)


# --- folders: mkdir and delete ------------------------------------------------------


def in_process(env, runner=subprocess.run):
    """The helper in this process, for what the command line cannot plant."""
    cfg = h.config_from_env(env)
    return h.Helper(cfg, h.Tmux(cfg, runner=runner))


def refusal(call, *args) -> h.HelperError:
    with pytest.raises(h.HelperError) as err:
        call(*args)
    return err.value


def tmux_sessions(env) -> set[str]:
    out = subprocess.run(
        ["tmux", "-S", env["RC_HELPER_TMUX_SOCKET"], "list-sessions", "-F", "#{session_name}"],
        capture_output=True,
        text=True,
    ).stdout
    return set(out.split())


def trusted(env, *rels):
    home = Path(env["RC_HELPER_HOME"])
    fake.write_state(
        home / ".claude-fake", logged_in=True, trusted=[str(home.resolve() / r) for r in rels]
    )


def name_max(path) -> int:
    return os.pathconf(path, "PC_NAME_MAX")


@pytest.mark.parametrize(
    "rel,code",
    [
        ("", "path_is_home"),
        (".", "path_is_home"),
        ("a/..", "path_is_home"),
        ("a/b/../..", "path_is_home"),
        ("/etc", "path_invalid"),
        ("a\0b", "path_invalid"),
        ("a/b/..", "path_invalid"),
        ("a/b/.", "path_invalid"),
        ("x" * (h.MAX_PATH_LENGTH + 1), "path_invalid"),
        ("../x", "path_outside_home"),
        ("escape/x", "path_outside_home"),
        ("missing", "path_not_found"),
        ("a/missing", "path_not_found"),
        ("missing/b", "path_not_found"),
        ("file", "path_not_directory"),
    ],
)
def test_entry_refusals(home, rel, code):
    assert refusal(h.resolve_entry, home, rel).code == code


def test_an_entry_is_its_resolved_parent_and_its_own_name(home):
    entry = h.resolve_entry(home, "inner-link/b")
    try:
        assert (entry.parent, entry.name, entry.rel, entry.is_link) == (
            (home / "a").resolve(),
            "b",
            "a/b",
            False,
        )
        assert os.path.samestat(os.fstat(entry.parent_fd), os.stat(home / "a"))
    finally:
        os.close(entry.parent_fd)
    entry = h.resolve_entry(home, "inner-link")  # the link itself, never what it leads to
    try:
        assert (entry.parent, entry.name, entry.rel, entry.is_link) == (
            home.resolve(),
            "inner-link",
            "inner-link",
            True,
        )
    finally:
        os.close(entry.parent_fd)
    entry = h.resolve_entry(home, "escape")  # a link leading outside is still only a link
    os.close(entry.parent_fd)
    assert (entry.rel, entry.is_link) == ("escape", True)


def test_a_folder_is_opened_without_following_any_link(home, tmp_path):
    folder = h.resolve_folder(home, "a/b")
    fd = h.open_dir(home.resolve(), folder)
    try:
        assert os.path.samestat(os.fstat(fd), os.stat(folder))  # positive control
    finally:
        os.close(fd)
    (home / "a").rename(tmp_path / "outside" / "a")
    (home / "a").symlink_to(tmp_path / "outside" / "a")  # a/b now leads outside
    assert refusal(h.open_dir, home.resolve(), folder).code == "path_outside_home"


def test_mkdir_makes_a_folder_in_a_folder_and_in_the_home_folder(env):
    home = Path(env["RC_HELPER_HOME"])
    assert ok(env, "mkdir", "--name=new", "--", "projects") == {"created": "projects/new"}
    assert (home / "projects" / "new").is_dir()
    assert ok(env, "mkdir", "--name=top") == {"created": "top"}
    assert (home / "top").is_dir()
    entries = [e["path"] for e in ok(env, "tree", "projects")["entries"]]
    assert "projects/new" in entries


FOLDER_NAMES_ACCEPTED = [
    "new",
    "My Project",
    "проект",
    "a.b",
    "a.",
    "-x",
    "--name",
    "x",
    f"a{chr(0x200C)}b",  # ZERO WIDTH NON-JOINER, see ALLOWED_FORMAT
]
FOLDER_NAMES_REFUSED = [
    "",
    ".",
    "..",
    ".hidden",
    "a/b",
    "/",
    " a",
    "a ",
    " ",
    "a\tb",
    "a\nb",
    "a\x00b",
    "a\x7fb",
    "a\x85b",
    f"a{chr(0x2028)}b",  # LINE SEPARATOR
    f"a{chr(0x200B)}b",  # ZERO WIDTH SPACE
    f"a{chr(0x202E)}b",  # RIGHT-TO-LEFT OVERRIDE
    f"a{chr(0x3164)}b",  # HANGUL FILLER
    f"{chr(0x00A0)}a",  # NO-BREAK SPACE, white space at the start
    f"a{chr(0x3000)}",  # IDEOGRAPHIC SPACE, at the end
    chr(0x200C),
    chr(0x200D) + "a",
]
FOLDER_NAMES = [(n, True) for n in FOLDER_NAMES_ACCEPTED] + [
    (n, False) for n in FOLDER_NAMES_REFUSED
]


@pytest.mark.parametrize(("name", "accepted"), FOLDER_NAMES)
def test_folder_name_rule(name, accepted):
    assert bool(h.FOLDER_NAME.fullmatch(name)) is accepted


@pytest.mark.parametrize(("name", "accepted"), FOLDER_NAMES)
def test_mkdir_makes_exactly_the_folders_the_rule_accepts(env, name, accepted):
    home = Path(env["RC_HELPER_HOME"])
    before = set(os.listdir(home / "projects"))
    if accepted:
        assert ok(env, "mkdir", f"--name={name}", "--", "projects") == {
            "created": f"projects/{name}"
        }
        assert (home / "projects" / name).is_dir()
    elif "\0" in name:  # no command line can hold it
        assert refusal(in_process(env).mkdir, "projects", name).code == "name_invalid"
    else:
        assert error_code(env, "mkdir", f"--name={name}", "--", "projects") == "name_invalid"
    assert accepted or set(os.listdir(home / "projects")) == before


def test_a_name_longer_than_the_file_system_takes_is_refused(env):
    home = Path(env["RC_HELPER_HOME"])
    limit = name_max(home)
    fits = "п" * (limit // 2)  # two bytes each
    assert ok(env, "mkdir", f"--name={fits}", "--", "projects")["created"] == f"projects/{fits}"
    over = "п" * (limit // 2 + 1)
    assert len(over) <= limit  # in characters it would fit; in bytes it does not
    assert error_code(env, "mkdir", f"--name={over}", "--", "projects") == "name_invalid"


def test_a_folder_whose_path_would_be_too_long_is_refused(env):
    home = Path(env["RC_HELPER_HOME"])
    parts, length = [], 0
    while length < h.MAX_PATH_LENGTH - 100:
        parts.append("d" * 99)
        length += 100
    deep = "/".join(parts)
    (home / deep).mkdir(parents=True)
    fits = "n" * (h.MAX_PATH_LENGTH - len(deep) - 1)
    assert ok(env, "mkdir", f"--name={fits}", "--", deep)["created"] == f"{deep}/{fits}"
    over = fits + "n"
    assert error_code(env, "mkdir", f"--name={over}", "--", deep) == "path_invalid"
    assert not (home / deep / over).exists()


def test_mkdir_refuses_a_name_that_is_there_already(env):
    home = Path(env["RC_HELPER_HOME"])
    (home / "projects" / "file").write_text("x")
    (home / "projects" / "dangling").symlink_to(home / "nowhere")
    for name in ("alpha", "file", "dangling"):
        assert error_code(env, "mkdir", f"--name={name}", "--", "projects") == "folder_exists"
    assert (home / "projects" / "file").read_text() == "x"
    assert not (home / "nowhere").exists()


def test_mkdir_refuses_a_parent_outside_the_home_folder(env, tmp_path):
    home = Path(env["RC_HELPER_HOME"])
    (tmp_path / "outside").mkdir()
    (home / "escape").symlink_to(tmp_path / "outside")
    assert error_code(env, "mkdir", "--name=x", "--", "..") == "path_outside_home"
    assert error_code(env, "mkdir", "--name=x", "--", "escape") == "path_outside_home"
    assert error_code(env, "mkdir", "--name=x", "--", "/tmp") == "path_invalid"
    assert error_code(env, "mkdir", "--name=x", "--", "missing") == "path_not_found"
    assert list((tmp_path / "outside").iterdir()) == []
    assert not (tmp_path / "x").exists()


def test_delete_removes_a_folder_and_everything_in_it(env):
    home = Path(env["RC_HELPER_HOME"])
    (home / "projects" / "beta" / "inner" / "deeper").mkdir()
    readonly = home / "projects" / "beta" / "inner" / "readonly"
    readonly.write_text("x")
    readonly.chmod(0o400)
    out = ok(env, "delete", "--", "projects/beta")
    assert out == {"deleted": "projects/beta", "link": False, "stopped": [], "forgotten": []}
    assert not (home / "projects" / "beta").exists()
    assert (home / "projects" / "alpha").is_dir()


@pytest.mark.parametrize("rel", ["", ".", "projects/..", "projects/alpha/../.."])
def test_delete_refuses_the_home_folder(env, rel):
    assert error_code(env, "delete", "--", rel) == "path_is_home"
    assert (Path(env["RC_HELPER_HOME"]) / "projects" / "alpha").is_dir()


def test_delete_refuses_anything_outside_the_home_folder(env, tmp_path):
    home = Path(env["RC_HELPER_HOME"])
    (tmp_path / "x").mkdir()
    (tmp_path / "outside" / "x").mkdir(parents=True)
    (home / "escape").symlink_to(tmp_path / "outside")
    assert error_code(env, "delete", "--", "../x") == "path_outside_home"
    assert error_code(env, "delete", "--", "escape/x") == "path_outside_home"
    assert error_code(env, "delete", "--", "projects/../../x") == "path_outside_home"
    assert error_code(env, "delete", "--", str(tmp_path / "x")) == "path_invalid"
    assert (tmp_path / "x").is_dir() and (tmp_path / "outside" / "x").is_dir()


def test_delete_refuses_a_dot_folder_and_anything_in_one(env):
    home = Path(env["RC_HELPER_HOME"])
    (home / ".hidden" / "inside").mkdir()
    (home / "projects" / ".git").mkdir()
    (home / "dots").symlink_to(home / ".hidden")
    for rel in (".hidden", ".hidden/inside", "projects/.git", "dots/inside", ".hidden/../projects"):
        assert error_code(env, "delete", "--", rel) == "path_hidden", rel
    assert (home / ".hidden" / "inside").is_dir() and (home / "projects" / ".git").is_dir()
    assert ok(env, "delete", "--", "projects/alpha")["deleted"] == "projects/alpha"  # positive


def test_delete_removes_a_link_and_leaves_what_it_leads_to(env):
    home = Path(env["RC_HELPER_HOME"])
    (home / "projects" / "alpha" / "kept").write_text("x")
    (home / "link").symlink_to(home / "projects" / "alpha")
    out = ok(env, "delete", "--", "link")
    assert (out["deleted"], out["link"]) == ("link", True)
    assert not (home / "link").is_symlink()
    assert (home / "projects" / "alpha" / "kept").read_text() == "x"
    # positive control: the real folder at the same place is deleted
    (home / "link").mkdir()
    assert ok(env, "delete", "--", "link")["link"] is False
    assert not (home / "link").exists()


def test_delete_removes_a_link_to_the_home_folder_and_nothing_else(env, tmp_path):
    home = Path(env["RC_HELPER_HOME"])
    (home / "self").symlink_to(home)
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "file").write_text("x")
    (home / "escape").symlink_to(tmp_path / "outside")
    assert ok(env, "delete", "--", "self")["link"] is True
    assert ok(env, "delete", "--", "escape")["link"] is True
    assert not (home / "self").is_symlink() and not (home / "escape").is_symlink()
    assert (home / "projects" / "alpha").is_dir()
    assert (tmp_path / "outside" / "file").read_text() == "x"


def test_delete_never_follows_a_link_inside_the_folder(env, tmp_path):
    home = Path(env["RC_HELPER_HOME"])
    outside = tmp_path / "outside"
    (outside / "folder").mkdir(parents=True)
    (outside / "folder" / "file").write_text("x")
    (outside / "file").write_text("y")
    beta = home / "projects" / "beta"
    (beta / "to-folder").symlink_to(outside / "folder")
    (beta / "inner" / "to-file").symlink_to(outside / "file")
    (beta / "inner" / "real").write_text("z")
    assert ok(env, "delete", "--", "projects/beta")["deleted"] == "projects/beta"
    assert not (beta / "inner" / "real").exists()  # non-vacuity: what was really there is gone
    assert (outside / "folder" / "file").read_text() == "x"
    assert (outside / "file").read_text() == "y"


def test_the_system_python_resists_symlink_attacks_on_rmtree():
    proc = subprocess.run(
        [
            "/usr/bin/python3",
            "-I",
            "-c",
            "import shutil; print(shutil.rmtree.avoids_symlink_attacks)",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.stdout.strip() == "True", proc.stderr
    assert HELPER.read_text(encoding="utf-8").startswith("#!/usr/bin/python3 ")  # what it runs on


def test_delete_refuses_where_rmtree_does_not_resist_symlink_attacks(env, monkeypatch):
    import shutil

    home = Path(env["RC_HELPER_HOME"])
    monkeypatch.setattr(shutil.rmtree, "avoids_symlink_attacks", False)
    assert refusal(in_process(env).delete, "projects/beta").code == "delete_unsafe"
    assert (home / "projects" / "beta" / "inner").is_dir()
    monkeypatch.undo()
    assert in_process(env).delete("projects/beta")["deleted"] == "projects/beta"  # positive


def plant_a_mount(monkeypatch, folder: Path) -> None:
    """``folder`` looks like a mount point: another device, to anything that asks by its fd."""
    real_fstat, planted = os.fstat, os.stat(folder)

    def fstat(fd):
        result = real_fstat(fd)
        if os.path.samestat(result, planted):
            fields = list(result)
            fields[stat.ST_DEV] = result.st_dev + 1
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(os, "fstat", fstat)


def test_delete_refuses_a_folder_with_a_mount_point_inside(env, monkeypatch):
    home = Path(env["RC_HELPER_HOME"])
    trusted(env, "projects/beta")
    ok(env, "start", "projects/beta", "--prefix", "pfx")
    wait_state(env, "projects/beta", "ready")
    plant_a_mount(monkeypatch, home / "projects" / "beta" / "inner")
    assert refusal(in_process(env).delete, "projects/beta").code == "delete_mount_inside"
    assert (home / "projects" / "beta" / "inner").is_dir()
    assert wait_state(env, "projects/beta", "ready")  # not stopped
    monkeypatch.undo()
    out = in_process(env).delete("projects/beta")  # positive control
    assert out["stopped"] == ["projects/beta"] and not (home / "projects" / "beta").exists()


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes anything; the container suite checks")
def test_what_cannot_be_removed_is_reported_and_delete_again_carries_on(env):
    home = Path(env["RC_HELPER_HOME"])
    inner = home / "projects" / "beta" / "inner"
    (inner / "stuck").write_text("x")
    (home / "projects" / "beta" / "gone").write_text("y")
    inner.chmod(0o500)
    try:
        code, out = helper(env, "delete", "--", "projects/beta")
        assert code == 1 and out["error"]["code"] == "delete_incomplete", out
        assert out["error"]["left"] == [
            "projects/beta",
            "projects/beta/inner",
            "projects/beta/inner/stuck",
        ]
        assert out["error"]["forgotten"] == []
        assert not (home / "projects" / "beta" / "gone").exists()  # the rest went
        assert stat.S_IMODE(inner.stat().st_mode) == 0o500  # no permission was changed
    finally:
        inner.chmod(0o700)
    assert ok(env, "delete", "--", "projects/beta")["deleted"] == "projects/beta"
    assert not (home / "projects" / "beta").exists()


def test_delete_refuses_a_path_that_goes_through_a_link(env):
    home = Path(env["RC_HELPER_HOME"])
    log_in(env)
    ok(env, "start", "projects/alpha", "--prefix", "pfx")
    (home / "lnk").symlink_to(home / "projects")
    (home / "projects" / "to-beta").symlink_to(home / "projects" / "beta")
    for rel in ("lnk/alpha", "projects/to-beta/inner", "lnk/beta/inner"):
        assert error_code(env, "delete", "--", rel) == "path_through_link", rel
    assert (home / "projects" / "alpha").is_dir() and (
        home / "projects" / "beta" / "inner"
    ).is_dir()
    assert [s["state"] for s in ok(env, "list")["servers"]] == ["trust_prompt"]  # not stopped
    # positive control: the same folders, named where they are
    assert ok(env, "delete", "--", "projects/beta/inner")["deleted"] == "projects/beta/inner"
    assert ok(env, "delete", "--", "projects/./alpha")["forgotten"] == ["projects/alpha"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes anything; the container suite checks")
def test_a_link_that_cannot_be_removed_is_reported_as_nothing_deleted(env):
    home = Path(env["RC_HELPER_HOME"])
    (home / "projects" / "lnk").symlink_to(home / "projects" / "alpha")
    (home / "projects").chmod(0o500)
    try:
        code, out = helper(env, "delete", "--", "projects/lnk")
    finally:
        (home / "projects").chmod(0o700)
    assert code == 1 and out["error"]["code"] == "delete_failed", out
    assert out["error"]["left"] == ["projects/lnk"]
    assert (home / "projects" / "lnk").is_symlink()


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes anything; the container suite checks")
def test_an_empty_folder_that_cannot_be_removed_is_reported_as_nothing_deleted(env):
    home = Path(env["RC_HELPER_HOME"])
    (home / "projects" / "empty").mkdir()
    (home / "projects").chmod(0o500)
    try:
        code, out = helper(env, "delete", "--", "projects/empty")
    finally:
        (home / "projects").chmod(0o700)
    assert code == 1 and out["error"]["code"] == "delete_failed", out
    assert out["error"]["left"] == ["projects/empty"]
    assert (home / "projects" / "empty").is_dir()
    assert h.MESSAGES["delete_failed"] != h.MESSAGES["delete_incomplete"]


def test_a_path_back_into_the_home_through_its_own_name_is_no_link(env):
    home = Path(env["RC_HELPER_HOME"])
    out = ok(env, "delete", "--", f"../{home.name}/projects/beta")
    assert out["deleted"] == "projects/beta"
    assert not (home / "projects" / "beta").exists()


def test_delete_refuses_a_path_not_written_as_the_names_on_disk(env, monkeypatch):
    """As on a file system that ignores case: Beta reaches beta, whose server is projects/beta."""
    home = Path(env["RC_HELPER_HOME"])
    projects = (home / "projects").resolve()
    real_listdir = os.listdir

    def listdir(path="."):
        names = real_listdir(path)
        return [("Beta" if n == "beta" else n) for n in names] if Path(path) == projects else names

    monkeypatch.setattr(os, "listdir", listdir)
    assert refusal(in_process(env).delete, "projects/beta").code == "path_name_differs"
    assert (home / "projects" / "beta" / "inner").is_dir()
    monkeypatch.undo()
    assert in_process(env).delete("projects/beta")["deleted"] == "projects/beta"  # positive


def listed_as(monkeypatch, folder: Path, name: str, shown: str) -> None:
    """``folder`` lists ``name`` as ``shown``, as a file system that ignores case can."""
    real_listdir, folder = os.listdir, folder.resolve()

    def listdir(path="."):
        names = real_listdir(path)
        return [shown if n == name else n for n in names] if Path(path) == folder else names

    monkeypatch.setattr(os, "listdir", listdir)


def test_a_server_starts_and_a_folder_is_made_only_where_the_path_is_as_on_disk(env, monkeypatch):
    home = Path(env["RC_HELPER_HOME"])
    log_in(env)
    listed_as(monkeypatch, home / "projects", "beta", "Beta")
    helper_ = in_process(env)
    for call, args in (
        (helper_.start, ("projects/beta", "pfx")),
        (helper_.resume, ("projects/beta", "pfx")),
        (helper_.mkdir, ("projects/beta", "x")),
    ):
        assert refusal(call, *args).code == "path_name_differs", call.__name__
    assert not (home / "projects" / "beta" / "x").exists()
    assert ok(env, "list")["servers"] == []
    assert helper_.mkdir("projects", "new") == {"created": "projects/new"}  # positive control
    assert helper_.start("projects/alpha", "pfx")["server"]["path"] == "projects/alpha"


def test_a_recorded_server_is_reached_however_its_folder_is_listed_now(env, monkeypatch):
    home = Path(env["RC_HELPER_HOME"])
    log_in(env)
    ok(env, "start", "projects/beta", "--prefix", "pfx")
    listed_as(monkeypatch, home / "projects", "beta", "Beta")
    helper_ = in_process(env)
    assert helper_.output("projects/beta")["server"]["state"] == "trust_prompt"
    assert helper_.confirm("projects/beta", "n")["server"]["state"] == "trust_declined"
    assert helper_.forget("projects/beta") == {"forgotten": "projects/beta"}


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes anything; the container suite checks")
@pytest.mark.parametrize("mode", [0o500, 0o000])
def test_a_folder_of_which_nothing_can_be_removed_is_reported_as_nothing_deleted(env, mode):
    home = Path(env["RC_HELPER_HOME"])
    locked = home / "projects" / "gamma" / "locked"
    locked.mkdir(parents=True)
    (locked / "file").write_text("x")
    locked.chmod(mode)
    try:
        code, out = helper(env, "delete", "--", "projects/gamma")
    finally:
        locked.chmod(0o700)
    assert code == 1 and out["error"]["code"] == "delete_failed", out
    assert (locked / "file").read_text() == "x"


def gamma_with_a_locked_folder(home: Path) -> Path:
    gamma = home / "projects" / "gamma"
    (gamma / "locked").mkdir(parents=True)
    (gamma / "locked" / "file").write_text("x")
    (gamma / "locked").chmod(0o500)
    return gamma


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes anything; the container suite checks")
def test_something_removed_while_something_else_appears_is_some_removed(env, monkeypatch):
    home = Path(env["RC_HELPER_HOME"])
    gamma = gamma_with_a_locked_folder(home)
    (gamma / "a").write_text("yours")
    real_unlink = os.unlink

    def unlink(path, *, dir_fd=None):
        real_unlink(path, dir_fd=dir_fd)
        if path == "a":  # as it goes, something else appears beside it
            (gamma / "b").write_text("new")

    monkeypatch.setattr(os, "unlink", unlink)
    try:
        error = refusal(in_process(env).delete, "projects/gamma")
    finally:
        monkeypatch.undo()
        (gamma / "locked").chmod(0o700)
    assert not (gamma / "a").exists() and (gamma / "b").exists()  # the probe took effect
    assert error.code == "delete_incomplete"


@pytest.mark.skipif(os.geteuid() == 0, reason="root removes anything; the container suite checks")
def test_something_appearing_while_nothing_is_removed_is_nothing_removed(env, monkeypatch):
    home = Path(env["RC_HELPER_HOME"])
    gamma = gamma_with_a_locked_folder(home)
    real_unlink = os.unlink

    def unlink(path, *, dir_fd=None):
        (gamma / "added").write_text("new")
        real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", unlink)
    try:
        error = refusal(in_process(env).delete, "projects/gamma")
    finally:
        monkeypatch.undo()
        (gamma / "locked").chmod(0o700)
    assert (gamma / "added").exists() and (gamma / "locked" / "file").exists()
    assert error.code == "delete_failed"


def test_the_report_of_what_is_left_is_capped(env, monkeypatch):
    home = Path(env["RC_HELPER_HOME"])
    for i in range(h.DELETE_REPORT_LIMIT + 5):
        (home / "projects" / "beta" / f"f{i:03}").write_text("x")
    real_unlink = os.unlink

    def unlink(path, *, dir_fd=None):
        if str(path).startswith("f"):
            raise PermissionError(13, "planted", path)
        return real_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr(os, "unlink", unlink)
    error = refusal(in_process(env).delete, "projects/beta")
    assert error.code == "delete_incomplete"
    assert len(error.detail["left"]) == h.DELETE_REPORT_LIMIT
    assert error.detail["left"] == sorted(error.detail["left"])


def test_delete_stops_then_forgets_every_server_in_the_folder_and_below_it(env):
    home = Path(env["RC_HELPER_HOME"])
    for rel in ("projects/beta/done", "projects/beta2"):
        (home / rel).mkdir()
    log_in(env)
    trusted(env, "projects/beta", "projects/beta/done", "projects/beta2", "projects/alpha")
    for rel in ("projects/beta", "projects/beta/done", "projects/beta2", "projects/alpha"):
        ok(env, "start", rel, "--prefix", "pfx")
        wait_state(env, rel, "ready")
    ok(env, "stop", "projects/beta/done")
    # projects/beta/inner is not trusted: it waits at the trust question
    assert ok(env, "start", "projects/beta/inner", "--prefix", "pfx")["server"]["state"] == (
        "trust_prompt"
    )
    out = ok(env, "delete", "--", "projects/beta")
    assert out == {
        "deleted": "projects/beta",
        "link": False,
        "stopped": ["projects/beta", "projects/beta/inner"],
        "forgotten": ["projects/beta", "projects/beta/done", "projects/beta/inner"],
    }
    assert not (home / "projects" / "beta").exists()
    servers = {s["path"]: s["state"] for s in ok(env, "list")["servers"]}
    assert servers == {"projects/alpha": "ready", "projects/beta2": "ready"}
    assert (home / "projects" / "beta2").is_dir()


def test_delete_stops_everything_before_forgetting_anything_and_forgets_before_removing(env):
    home = Path(env["RC_HELPER_HOME"])
    beta = home / "projects" / "beta"
    trusted(env, "projects/beta", "projects/beta/inner")
    for rel in ("projects/beta", "projects/beta/inner"):
        ok(env, "start", rel, "--prefix", "pfx")
        wait_state(env, rel, "ready")
    steps = []

    def runner(args, **kwargs):
        if "send-keys" in args and "C-c" in args:
            steps.append(("interrupt", beta.exists()))
        if "kill-session" in args:
            steps.append(("forget", beta.exists()))
        return subprocess.run(args, **kwargs)

    in_process(env, runner).delete("projects/beta")
    assert steps == [("interrupt", True)] * 2 + [("forget", True)] * 2
    assert not beta.exists()


def ignoring_interrupts(env):
    """Servers started from now on ignore Ctrl+C while Claude asks to trust the folder."""
    Path(env["RC_HELPER_CLAUDE"]).write_text(
        f"#!/bin/sh\ntrap '' INT\nexec {rec.FAKE_CLAUDE} \"$@\"\n", encoding="utf-8"
    )


def helper_with(env, settings: dict, *args, timeout: float):
    """rc-helper's answer, run in its own process with some of its settings changed.

    A call that outlasts ``timeout`` fails the test, so one that would wait
    for ever is caught as a failure rather than a hang.
    """
    import sys

    program = (
        "import os, sys\nfrom helpers import rc_helper as h\n"
        + "".join(f"h.{name} = {value!r}\n" for name, value in settings.items())
        + "sys.exit(h.main(sys.argv[1:], dict(os.environ)))\n"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            f"import sys; sys.path.insert(0, {str(REPO)!r})\n" + program,
            *args,
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, json.loads(proc.stdout)


def test_when_a_server_does_not_stop_nothing_is_forgotten_or_deleted(env):
    home = Path(env["RC_HELPER_HOME"])
    trusted(env, "projects/beta")
    ok(env, "start", "projects/beta", "--prefix", "pfx")
    wait_state(env, "projects/beta", "ready")
    ignoring_interrupts(env)
    ok(env, "start", "projects/beta/inner", "--prefix", "pfx")
    sessions = tmux_sessions(env)
    # a stop deadline of one second, so a delete that waits longer has missed it
    code, out = helper_with(env, {"STOP_TIMEOUT": 1.0}, "delete", "--", "projects/beta", timeout=15)
    assert code == 1, out
    error = out["error"]
    assert error["code"] == "delete_stop_failed"
    assert error["server"]["path"] == "projects/beta/inner"
    assert any("[y/N]" in line for line in error["server"]["screen"])
    assert error["stopped"] == ["projects/beta"]
    assert tmux_sessions(env) == sessions  # nothing forgotten
    assert (home / "projects" / "beta" / "inner").is_dir()
    servers = {s["path"]: s["state"] for s in ok(env, "list")["servers"]}
    assert servers == {"projects/beta": "stopped", "projects/beta/inner": "trust_prompt"}


def test_when_a_server_cannot_be_forgotten_nothing_is_deleted(env, monkeypatch):
    home = Path(env["RC_HELPER_HOME"])
    log_in(env)
    ok(env, "start", "projects/beta", "--prefix", "pfx")
    monkeypatch.setattr(h.Tmux, "kill", lambda self, session: None)  # kill-session does nothing
    error = refusal(in_process(env).delete, "projects/beta")
    assert error.code == "delete_forget_failed"
    assert error.detail["server"]["path"] == "projects/beta"
    assert error.detail["forgotten"] == []
    assert (home / "projects" / "beta" / "inner").is_dir()


def hold_the_lock(env, exclusive):
    import fcntl

    fd = os.open(Path(env["RC_HELPER_HOME"]).resolve(), os.O_RDONLY | os.O_DIRECTORY)
    fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
    return fd


def test_while_a_delete_runs_nothing_starts_and_no_folder_is_made(env):
    import time

    log_in(env)
    fd = hold_the_lock(env, exclusive=True)
    try:
        began = time.monotonic()
        assert error_code(env, "start", "projects/alpha", "--prefix", "pfx") == "folder_busy"
        assert error_code(env, "resume", "projects/alpha", "--prefix", "pfx") == "folder_busy"
        assert error_code(env, "mkdir", "--name=new") == "folder_busy"
        assert error_code(env, "stop", "projects/alpha") == "folder_busy"
        assert error_code(env, "forget", "projects/alpha") == "folder_busy"
        assert error_code(env, "confirm", "--answer=y", "projects/alpha") == "folder_busy"
        assert error_code(env, "delete", "--", "projects/beta") == "folder_busy"
        assert time.monotonic() - began < 10  # refused at once, not after waiting
    finally:
        os.close(fd)
    assert ok(env, "start", "projects/alpha", "--prefix", "pfx")  # positive control
    assert ok(env, "mkdir", "--name=new")


def test_while_a_server_starts_or_a_folder_is_made_nothing_is_deleted(env):
    fd = hold_the_lock(env, exclusive=False)
    try:
        assert error_code(env, "delete", "--", "projects/beta") == "folder_busy"
        assert ok(env, "mkdir", "--name=new")  # these share it
    finally:
        os.close(fd)
    assert ok(env, "delete", "--", "projects/beta")["deleted"] == "projects/beta"  # positive


def test_delete_holds_the_lock_while_it_stops_servers(env, monkeypatch):
    log_in(env)
    ok(env, "start", "projects/beta", "--prefix", "pfx")
    during = []
    real = h.Tmux.interrupt

    def interrupt(self, session):
        during.append(error_code(env, "start", "projects/alpha", "--prefix", "pfx"))
        real(self, session)

    monkeypatch.setattr(h.Tmux, "interrupt", interrupt)
    in_process(env).delete("projects/beta")
    assert during == ["folder_busy"]


def test_a_start_holds_the_lock_and_its_server_does_not(env, monkeypatch):
    log_in(env)
    during = []
    real = h.Tmux.new

    def new(self, *args):
        during.append(error_code(env, "delete", "--", "projects/beta"))
        return real(self, *args)

    monkeypatch.setattr(h.Tmux, "new", new)
    in_process(env).start("projects/alpha", "pfx")
    assert during == ["folder_busy"]
    monkeypatch.undo()
    # a server keeps running once started, and holds nothing: a delete goes ahead
    ok(env, "start", "projects/beta", "--prefix", "pfx")
    assert ok(env, "delete", "--", "projects/beta")["forgotten"] == ["projects/beta"]
    assert [s["path"] for s in ok(env, "list")["servers"]] == ["projects/alpha"]


# --- the command-line surface ----------------------------------------------------


def test_every_error_code_has_a_message():
    used = set(
        re.findall(r'HelperError\(\s*"([a-z_]+)"', (REPO / "helpers" / "rc_helper.py").read_text())
    )
    assert used, "no error codes found"  # non-vacuity
    assert used <= set(h.MESSAGES)
    assert set(h.MESSAGES) <= used | {"internal_error", "usage"}


# What rc-helper is given that breaks each rule it reports, as an error code ->
# the command line, and what it reads on stdin, with the refused value in place
# of {}.
RULE_INVOCATIONS = {
    "prefix_invalid": (("start", "projects/alpha", "--prefix={}"), None),
    "name_invalid": (("mkdir", "--name={}", "--", "projects"), None),
    "email_invalid": (("auth-login", "--email={}"), None),
    "author_name_invalid": (
        ("git-commit", "--author-name={}", "--author-email=a@example.com", "--", "projects/alpha"),
        "A message",
    ),
    "message_invalid": (
        ("git-commit", "--author-name=A", "--author-email=a@example.com", "--", "projects/alpha"),
        "{}",
    ),
    "path_invalid": (("output", "--", "{}"), None),
    "branch_name_invalid": (("git-switch", "--branch={}", "--", "projects/alpha"), None),
    "token_invalid": (("github-token",), "{}"),
}
# Rules rc-helper holds without reporting them itself: subject -> why.
RULES_NOT_REPORTED: dict[str, str] = {}


def invoked(code: str, value: str) -> tuple[list[str], str | None]:
    """The command line and stdin that give rc-helper ``value`` for the rule of ``code``."""
    args, stdin = RULE_INVOCATIONS[code]
    return [part.format(value) for part in args], stdin and stdin.format(value)


# Values that break most rules, each one a command line can carry.
REFUSAL_CANDIDATES = ("\x01", " ", "", "/")


def refused_value(rule) -> str:
    """A value the rule refuses, from a few that break most rules."""
    for value in REFUSAL_CANDIDATES:
        if not rule.fullmatch(value):
            return value
    raise AssertionError(f"no candidate value breaks {rule.pattern!r}")


def test_a_rule_reported_by_code_is_reported_in_its_own_words():
    assert h.RULE_CODES  # non-vacuity
    for code, rule in h.RULE_CODES.items():
        assert h.MESSAGES[code] is rule.sentence, code


def test_every_rule_rc_helper_holds_is_reported_or_exempt():
    from tests.test_rules import held_rules

    held = held_rules(h)
    assert {"PREFIX", "FOLDER_NAME", "AUTHOR_NAME"} <= set(held)  # non-vacuity
    reported = list(h.RULE_CODES.values())
    for name in RULES_NOT_REPORTED:  # stale check: each exemption still needs to be one
        assert name in held, f"{name} is exempt but rc-helper no longer holds it"
        assert held[name] not in reported, f"{name} is exempt but has a code"
    for name, rule in held.items():
        assert name in RULES_NOT_REPORTED or rule in reported, f"{name} has no error code"


def test_each_rule_is_refused_through_the_command_line_with_its_sentence(env):
    assert set(RULE_INVOCATIONS) == set(h.RULE_CODES)
    log_in(env)
    for code, rule in h.RULE_CODES.items():
        value = refused_value(rule)
        args, stdin = invoked(code, value)
        status, out = helper(env, *args, stdin=stdin)
        assert status == 1 and out["error"]["code"] == code, (code, out)
        assert out["error"]["message"] == rule.sentence


def test_rc_helper_and_the_backend_refuse_a_value_in_the_same_words(env, client):
    """Every field the backend checks against one of rc-helper's reported rules says
    what rc-helper says for the same value."""
    from tests.conftest import ALICE, CALLS, changing
    from tests.test_problems import request_fields, send

    log_in(env)
    fields = request_fields(client.app)
    for code, rule in h.RULE_CODES.items():
        value = refused_value(rule)
        args, stdin = invoked(code, value)
        said = helper(env, *args, stdin=stdin)[1]["error"]["message"]
        checked = [f for f in fields if f.schema.get("pattern") == rule.anchored]
        assert checked, f"the backend does not check {code}'s rule"
        for f in checked:
            response = send(client, f, value, CALLS, changing(ALICE))
            assert response.json()["errors"] == [{"field": f.name, "message": said}], f


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


# --- git ------------------------------------------------------------------------------

from tests.git_fixtures import (  # noqa: E402
    fixture_git,
    make_repo,
    push_branch_to_origin,
    write,
)

REPO_REL = "projects/repo"


@pytest.fixture
def repo(env, tmp_path):
    """A repository in the home folder, with an origin outside it holding main and feature."""
    origin = tmp_path / "origins" / "repo.git"
    return make_repo(Path(env["RC_HELPER_HOME"]) / REPO_REL, origin=origin)


def origin_of(repo: Path) -> Path:
    return Path(fixture_git(repo, "remote", "get-url", "origin").strip())


def status(env, rel=REPO_REL) -> dict:
    return ok(env, "git-status", "--", rel)


def listed(out: dict, which: str) -> list[tuple[str, str]]:
    return [(f["path"], f["change"]) for f in out[which]["files"]]


def stage(env, files, rel=REPO_REL, verb="git-stage"):
    return helper(env, verb, "--", rel, stdin=json.dumps(files))


def commit(env, message="A message", name="Ann Author", email="ann@example.com", rel=REPO_REL):
    args = ("git-commit", f"--author-name={name}", f"--author-email={email}", "--", rel)
    return helper(env, *args, stdin=message)


def branch_of(path: Path) -> str:
    return fixture_git(path, "branch", "--show-current").strip()


def test_tree_marks_repositories(env, repo, tmp_path):
    home = Path(env["RC_HELPER_HOME"])
    linked = home / "projects" / "linked"
    fixture_git(repo, "worktree", "add", "-q", str(linked))
    assert (linked / ".git").is_file()
    marked = {e["path"]: e["repository"] for e in ok(env, "tree", "projects")["entries"]}
    assert marked["projects/repo"] is True  # a .git folder
    assert marked["projects/linked"] is True  # a .git file
    assert marked["projects/alpha"] is False


def test_status_reports_the_branch_its_upstream_and_how_far_apart_they_are(env, repo):
    branch = status(env)["branch"]
    assert branch["name"] == "main" and branch["upstream"] == "origin/main"
    assert (branch["ahead"], branch["behind"]) == (0, 0)
    assert branch["commit"] == fixture_git(repo, "rev-parse", "HEAD").strip()
    write(repo / "local.txt")
    fixture_git(repo, "add", ".")
    fixture_git(repo, "commit", "-q", "-m", "local")
    push_branch_to_origin(repo, origin_of(repo), "elsewhere")
    fixture_git(origin_of(repo), "update-ref", "refs/heads/main", "refs/heads/elsewhere")
    fixture_git(repo, "fetch", "-q")
    branch = status(env)["branch"]
    assert (branch["ahead"], branch["behind"]) == (1, 1)


def test_status_of_a_detached_head_and_of_no_commits_yet(env, repo):
    fixture_git(repo, "switch", "-q", "--detach", "HEAD")
    assert status(env)["branch"]["name"] is None
    fresh = Path(env["RC_HELPER_HOME"]) / "projects" / "fresh"
    fresh.mkdir()
    fixture_git(fresh, "init", "-q", "-b", "trunk")
    branch = status(env, "projects/fresh")["branch"]
    assert branch == {
        "name": "trunk",
        "commit": None,
        "upstream": None,
        "ahead": None,
        "behind": None,
    }


def test_status_lists_each_kind_of_change_in_its_list(env, repo):
    write(repo / "both.txt")
    fixture_git(repo, "add", "both.txt")
    fixture_git(repo, "commit", "-q", "-m", "both")
    write(repo / "README.md", "changed\n")
    write(repo / "new.txt")
    fixture_git(repo, "add", "new.txt")
    write(repo / "folder" / "inside.txt")
    write(repo / "both.txt", "staged\n")
    fixture_git(repo, "add", "both.txt")
    write(repo / "both.txt", "and not\n")
    out = status(env)
    assert listed(out, "staged") == [("both.txt", "modified"), ("new.txt", "added")]
    assert sorted(listed(out, "not_staged")) == [
        ("README.md", "modified"),
        ("both.txt", "modified"),
        ("folder/", "untracked"),
    ]
    assert out["staged"]["more"] == 0 and out["path"] == REPO_REL


def test_a_rename_is_listed_with_where_it_came_from(env, repo):
    fixture_git(repo, "mv", "README.md", "MOVED.txt")
    [moved] = status(env)["staged"]["files"]
    assert moved == {"path": "MOVED.txt", "change": "renamed", "from": "README.md"}


def test_a_conflict_is_listed_as_unmerged(env, repo):
    fixture_git(repo, "switch", "-q", "-c", "other")
    write(repo / "README.md", "theirs\n")
    fixture_git(repo, "commit", "-q", "-am", "theirs")
    fixture_git(repo, "switch", "-q", "main")
    write(repo / "README.md", "ours\n")
    fixture_git(repo, "commit", "-q", "-am", "ours")
    from tests.git_fixtures import FIXTURE_HOME, fixture_env

    merge = subprocess.run(
        ["git", "merge", "-q", "other"],
        cwd=repo,
        env=fixture_env(FIXTURE_HOME),
        capture_output=True,
    )
    assert merge.returncode != 0  # positive control: there is a conflict
    assert listed(status(env), "not_staged") == [("README.md", "unmerged")]


def test_a_submodule_is_marked(env, repo, tmp_path):
    from tests.git_fixtures import add_submodule

    add_submodule(repo, make_repo(tmp_path / "library"), "library")
    files = status(env)["staged"]["files"]
    assert {"path": "library", "change": "added", "submodule": True} in files
    assert {"path": ".gitmodules", "change": "added"} in files


NAMES = ["with space.txt", "with\nnewline.txt", "ünïcode.txt", "tab\there.txt"]


def test_names_with_spaces_and_newlines_round_trip_through_staging(env, repo):
    for name in NAMES:
        write(repo / name)
    out = status(env)
    assert sorted(path for path, _ in listed(out, "not_staged")) == sorted(NAMES)
    assert ok(env, "git-stage", "--", REPO_REL, stdin=json.dumps(NAMES))
    assert sorted(listed(status(env), "staged")) == sorted((n, "added") for n in NAMES)


def test_a_long_list_is_capped_and_says_how_many_more(env, repo):
    for i in range(h.MAX_LISTED_FILES + 3):
        write(repo / f"f{i:05}.txt")
    out = status(env)["not_staged"]
    assert len(out["files"]) == h.MAX_LISTED_FILES
    assert out["more"] == 3


def test_a_folder_that_is_not_a_repository_is_refused(env, repo):
    assert status(env)["path"] == REPO_REL  # positive control
    assert error_code(env, "git-status", "--", "projects/alpha") == "not_a_repository"
    assert error_code(env, "git-branches", "--", "projects/alpha") == "not_a_repository"


def test_a_folder_inside_a_repository_is_not_one(env, repo):
    write(repo / "inner" / "x.txt")
    (repo / "empty-git" / ".git").mkdir(parents=True)
    assert error_code(env, "git-status", "--", f"{REPO_REL}/inner") == "not_a_repository"
    assert error_code(env, "git-status", "--", f"{REPO_REL}/empty-git") == "not_a_repository"


def test_a_home_that_is_a_repository_makes_no_folder_one(env):
    home = Path(env["RC_HELPER_HOME"])
    fixture_git(home, "init", "-q")
    (home / "projects" / "alpha" / ".git").mkdir()  # not a repository, inside one
    assert error_code(env, "git-status", "--", "projects/alpha") == "not_a_repository"
    assert error_code(env, "git-status", "--", "projects/beta") == "not_a_repository"


def test_a_repository_whose_git_data_is_outside_the_home_is_refused(env, tmp_path):
    outside = make_repo(tmp_path / "outside-repo")
    linked = Path(env["RC_HELPER_HOME"]) / "projects" / "linked"
    fixture_git(outside, "worktree", "add", "-q", str(linked))
    assert error_code(env, "git-status", "--", "projects/linked") == "repository_outside_home"
    inside = make_repo(Path(env["RC_HELPER_HOME"]) / "projects" / "inside")
    fixture_git(inside, "worktree", "add", "-q", str(inside.parent / "linked-inside"))
    assert status(env, "projects/linked-inside")["branch"]["name"]  # positive control


def test_status_writes_nothing_into_the_repository(env, repo):
    import time

    index = repo / ".git" / "index"
    time.sleep(1.1)
    os.utime(repo / "README.md")  # the index's record of it is now stale
    before = index.stat().st_mtime_ns
    status(env)
    assert index.stat().st_mtime_ns == before
    fixture_git(repo, "status")  # positive control: git itself refreshes the index
    assert index.stat().st_mtime_ns != before


def test_stage_stages_only_the_files_given(env, repo):
    write(repo / "a.txt")
    write(repo / "b.txt")
    write(repo / "README.md", "changed\n")
    assert ok(env, "git-stage", "--", REPO_REL, stdin=json.dumps(["a.txt", "README.md"]))
    out = status(env)
    assert listed(out, "staged") == [("README.md", "modified"), ("a.txt", "added")]
    assert listed(out, "not_staged") == [("b.txt", "untracked")]


def test_unstage_unstages_only_the_files_given(env, repo):
    write(repo / "a.txt")
    write(repo / "b.txt")
    fixture_git(repo, "add", ".")
    assert ok(env, "git-unstage", "--", REPO_REL, stdin=json.dumps(["a.txt"]))
    out = status(env)
    assert listed(out, "staged") == [("b.txt", "added")]
    assert listed(out, "not_staged") == [("a.txt", "untracked")]


def test_unstaging_a_rename_unstages_both_its_sides(env, repo):
    fixture_git(repo, "mv", "README.md", "MOVED.txt")
    assert ok(env, "git-unstage", "--", REPO_REL, stdin=json.dumps(["MOVED.txt"]))
    out = status(env)
    assert listed(out, "staged") == []
    assert sorted(listed(out, "not_staged")) == [
        ("MOVED.txt", "untracked"),
        ("README.md", "deleted"),
    ]


def test_unstage_works_before_the_first_commit(env):
    fresh = Path(env["RC_HELPER_HOME"]) / "projects" / "fresh"
    fresh.mkdir()
    fixture_git(fresh, "init", "-q", "-b", "main")
    write(fresh / "a.txt")
    fixture_git(fresh, "add", "a.txt")
    assert listed(status(env, "projects/fresh"), "staged") == [("a.txt", "added")]
    assert ok(env, "git-unstage", "--", "projects/fresh", stdin=json.dumps(["a.txt"]))
    assert listed(status(env, "projects/fresh"), "not_staged") == [("a.txt", "untracked")]


def test_a_deleted_file_and_an_untracked_folder_are_staged(env, repo):
    (repo / "README.md").unlink()
    write(repo / "folder" / "x.txt")
    assert ok(env, "git-stage", "--", REPO_REL, stdin=json.dumps(["README.md", "folder/"]))
    assert listed(status(env), "staged") == [("README.md", "deleted"), ("folder/x.txt", "added")]


def test_names_are_taken_literally(env, repo):
    for name in ("*", "a", "ab", ":(glob)a*"):
        write(repo / name)
    assert ok(env, "git-stage", "--", REPO_REL, stdin=json.dumps(["*", ":(glob)a*"]))
    assert sorted(listed(status(env), "staged")) == [("*", "added"), (":(glob)a*", "added")]


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("../x", "file_outside_repository"),
        ("/etc/passwd", "file_outside_repository"),
        ("a/../../x", "file_outside_repository"),
        (".git/config", "file_outside_repository"),
        (".GIT/config", "file_outside_repository"),
        ("a//b", "file_invalid"),
        ("./a", "file_invalid"),
        ("", "file_invalid"),
        ("a\0b", "file_invalid"),
        ("x" * (h.MAX_PATH_LENGTH + 1), "file_invalid"),
    ],
)
def test_a_file_outside_the_repository_is_refused(env, repo, name, code):
    write(repo / "a")
    write(repo / "README.md", "changed\n")
    for verb in ("git-stage", "git-unstage"):
        status_code, out = stage(env, ["README.md", name], verb=verb)
        assert (status_code, out["error"]["code"]) == (1, code), out
    assert listed(status(env), "staged") == []  # nothing staged, not even README.md


def test_a_file_through_a_link_out_of_the_repository_is_refused(env, repo, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    write(elsewhere / "secret.txt")
    (repo / "out").symlink_to(elsewhere)
    assert error_code(env, "git-stage", "--", REPO_REL, stdin='["out/secret.txt"]') == (
        "file_outside_repository"
    )
    assert listed(status(env), "staged") == []
    assert (elsewhere / "secret.txt").read_text() == "content\n"
    assert not (elsewhere / ".git").exists()


def test_a_link_in_the_repository_is_staged_as_the_link(env, repo):
    (repo / "link").symlink_to("README.md")
    assert ok(env, "git-stage", "--", REPO_REL, stdin='["link"]')
    tree = fixture_git(repo, "ls-files", "-s", "link")
    assert tree.startswith("120000 ")  # a link, not the file it points at


@pytest.mark.parametrize(
    "stdin",
    [
        "not json",
        '{"a": 1}',
        "[1]",
        '"a.txt"',
        "[]",
        json.dumps(["a"] * (h.MAX_FILES_PER_CALL + 1)),
    ],
)
def test_a_list_of_files_that_is_not_one_is_refused(env, repo, stdin):
    assert error_code(env, "git-stage", "--", REPO_REL, stdin=stdin) == "file_list_invalid"


def test_a_name_that_is_not_utf8_is_listed_but_cannot_be_staged(env, repo):
    os.close(os.open(bytes(repo) + b"/caf\xe9.txt", os.O_CREAT | os.O_WRONLY))
    write(repo / "plain.txt")
    files = status(env)["not_staged"]["files"]
    assert {"path": "plain.txt", "change": "untracked"} in files  # positive control
    odd = next(f for f in files if f["path"] != "plain.txt")
    assert odd["selectable"] is False and odd["path"].startswith("caf")
    assert "selectable" not in next(f for f in files if f["path"] == "plain.txt")
    assert (
        error_code(env, "git-stage", "--", REPO_REL, stdin='["caf\\udce9.txt"]') == "file_invalid"
    )
    assert listed(status(env), "staged") == []


def test_commit_records_the_message_from_stdin_and_the_author_given(env, repo):
    (Path(env["RC_HELPER_HOME"]) / ".gitconfig").write_text(
        "[user]\n\tname = Wrong Name\n\temail = wrong@example.com\n"
    )
    write(repo / "a.txt")
    fixture_git(repo, "add", "a.txt")
    message = "Add a.txt, ünïcode\n\nWith a body: --author-name=x; $(rm -rf ~)\n"
    code, out = commit(env, message)
    assert code == 0, out
    assert out == {
        "path": REPO_REL,
        "commit": fixture_git(repo, "rev-parse", "HEAD").strip(),
        "branch": "main",
    }
    shown = fixture_git(repo, "log", "-1", "--format=%an <%ae>%n%cn <%ce>%n%B")
    assert shown.startswith("Ann Author <ann@example.com>\nAnn Author <ann@example.com>\n")
    assert shown.split("\n", 2)[2].rstrip("\n") == message.rstrip("\n")
    assert listed(status(env), "staged") == []
    assert "Add a.txt" not in json.dumps(out)


def test_the_commit_message_never_reaches_any_argv(env, repo, tmp_path):
    seen = tmp_path / "seen"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        f"cat /proc/$PPID/cmdline > {seen}.git\n"
        f"helper=$(awk '{{print $4}}' /proc/$PPID/stat)\n"
        f"cat /proc/$helper/cmdline > {seen}.helper\n"
    )
    hook.chmod(0o755)
    write(repo / "a.txt")
    fixture_git(repo, "add", "a.txt")
    assert commit(env, "SECRET-MESSAGE-42")[0] == 0
    git_argv = Path(f"{seen}.git").read_bytes()
    helper_argv = Path(f"{seen}.helper").read_bytes()
    assert b"commit" in git_argv and b"git-commit" in helper_argv  # positive controls
    assert b"SECRET-MESSAGE-42" not in git_argv + helper_argv
    assert "SECRET-MESSAGE-42" in fixture_git(repo, "log", "-1", "--format=%B")


def test_commit_is_refused_when_nothing_is_staged(env, repo):
    write(repo / "README.md", "changed but not staged\n")
    code, out = commit(env)
    assert (code, out["error"]["code"]) == (1, "nothing_staged")
    fixture_git(repo, "add", "README.md")
    assert commit(env)[0] == 0  # positive control


@pytest.mark.parametrize(
    ("message", "name", "email", "code"),
    [
        ("", "Ann", "a@example.com", "message_invalid"),
        (" \n\t\n", "Ann", "a@example.com", "message_invalid"),
        ("a\0b", "Ann", "a@example.com", "message_invalid"),
        ("x" * (h.MAX_COMMIT_MESSAGE_LENGTH + 1), "Ann", "a@example.com", "message_invalid"),
        (b"caf\xe9", "Ann", "a@example.com", "message_invalid"),
        ("Fine", "", "a@example.com", "author_name_invalid"),
        ("Fine", "Ann <x>", "a@example.com", "author_name_invalid"),
        ("Fine", "Ann", "not-an-address", "email_invalid"),
        ("Fine", "Ann", "", "email_invalid"),
    ],
)
def test_an_invalid_message_name_or_address_is_refused_in_its_rules_words(
    env, repo, message, name, email, code
):
    write(repo / "a.txt")
    fixture_git(repo, "add", "a.txt")
    status_code, out = commit(env, message, name, email)
    assert (status_code, out["error"]["code"]) == (1, code), out
    assert out["error"]["message"] == h.RULE_CODES[code].sentence
    assert listed(status(env), "staged") == [("a.txt", "added")]  # nothing committed


def test_a_message_as_long_as_allowed_is_committed(env, repo):
    write(repo / "a.txt")
    fixture_git(repo, "add", "a.txt")
    assert commit(env, "x" * h.MAX_COMMIT_MESSAGE_LENGTH)[0] == 0


def test_hooks_run_and_a_refusal_comes_back_in_their_words(env, repo):
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho 'No commits on Fridays.' >&2\nexit 1\n")
    hook.chmod(0o755)
    write(repo / "a.txt")
    fixture_git(repo, "add", "a.txt")
    code, out = commit(env)
    assert (code, out["error"]["code"]) == (1, "git_failed")
    assert out["error"]["git"] == ["No commits on Fridays."] and out["error"]["git_more"] == 0
    assert out["error"]["message"] == h.MESSAGES["git_failed"]


def test_branches_lists_local_and_remote_tracking_branches(env, repo):
    fixture_git(repo, "remote", "set-head", "origin", "main")
    fixture_git(repo, "branch", "local-only")
    out = ok(env, "git-branches", "--", REPO_REL)
    assert out["local"] == {
        "branches": [
            {"name": "local-only", "current": False, "upstream": None},
            {"name": "main", "current": True, "upstream": "origin/main"},
        ],
        "more": 0,
    }
    assert out["remote"] == {
        "branches": [
            {"name": "origin/feature", "local": None},
            {"name": "origin/main", "local": "main"},
        ],
        "more": 0,
    }


def test_a_long_list_of_branches_is_capped(env, repo):
    head = fixture_git(repo, "rev-parse", "HEAD").strip()
    lines = "".join(f"create refs/heads/b{i:05} {head}\n" for i in range(h.MAX_LISTED_BRANCHES + 1))
    fixture_git(repo, "update-ref", "--stdin", input=lines.encode())
    local = ok(env, "git-branches", "--", REPO_REL)["local"]
    assert len(local["branches"]) == h.MAX_LISTED_BRANCHES
    assert local["more"] == 2  # the extra one, and main


def test_switch_to_a_local_branch(env, repo):
    fixture_git(repo, "branch", "other")
    assert ok(env, "git-switch", "--branch=other", "--", REPO_REL)["branch"] == "other"
    assert branch_of(repo) == "other"


def test_switch_to_a_remote_only_branch_makes_a_local_branch_tracking_it(env, repo):
    out = ok(env, "git-switch", "--branch=origin/feature", "--remote", "--", REPO_REL)
    assert out["branch"] == "feature"
    assert branch_of(repo) == "feature"
    assert fixture_git(repo, "config", "branch.feature.remote").strip() == "origin"
    assert fixture_git(repo, "config", "branch.feature.merge").strip() == "refs/heads/feature"


@pytest.mark.parametrize(
    ("args", "code"),
    [
        (("--branch=nope",), "branch_not_found"),
        (("--branch=feature",), "branch_not_found"),  # only on origin
        (("--branch=main", "--remote"), "branch_not_found"),
        (("--branch=origin/feature",), "branch_not_found"),
        (("--branch=-f",), "branch_name_invalid"),
        (("--branch=",), "branch_name_invalid"),
        ((f"--branch={'x' * (h.MAX_BRANCH_NAME_LENGTH + 1)}",), "branch_name_invalid"),
    ],
)
def test_switch_refuses_a_branch_that_is_not_there(env, repo, args, code):
    assert error_code(env, "git-switch", *args, "--", REPO_REL) == code
    assert branch_of(repo) == "main"


def test_a_switch_blocked_by_local_changes_passes_on_gits_words(env, repo):
    fixture_git(repo, "switch", "-q", "-c", "other")
    write(repo / "README.md", "on other\n")
    fixture_git(repo, "commit", "-q", "-am", "other")
    fixture_git(repo, "switch", "-q", "main")
    write(repo / "README.md", "my unsaved work\n")
    code, out = helper(env, "git-switch", "--branch=other", "--", REPO_REL)
    assert (code, out["error"]["code"]) == (1, "git_failed")
    assert any("would be overwritten" in line for line in out["error"]["git"])
    assert branch_of(repo) == "main"
    assert (repo / "README.md").read_text() == "my unsaved work\n"


def test_a_lock_held_on_the_index_passes_on_gits_words(env, repo):
    write(repo / "a.txt")
    (repo / ".git" / "index.lock").write_text("")
    code, out = stage(env, ["a.txt"])
    assert (code, out["error"]["code"]) == (1, "git_failed")
    assert any("index.lock" in line for line in out["error"]["git"])


def test_new_branch_makes_a_branch_and_switches_to_it(env, repo):
    out = ok(env, "git-branch-new", "--name=topic/new", "--", REPO_REL)
    assert out["branch"] == "topic/new"
    assert branch_of(repo) == "topic/new"
    assert fixture_git(repo, "rev-parse", "topic/new") == fixture_git(repo, "rev-parse", "main")


@pytest.mark.parametrize(
    ("name", "code"),
    [
        ("a..b", "branch_invalid"),
        ("@{-1}", "branch_invalid"),
        ("a b", "branch_invalid"),
        ("x.lock", "branch_invalid"),
        ("a~1", "branch_invalid"),
        ("HEAD", "branch_invalid"),
        ("-x", "branch_name_invalid"),
        ("", "branch_name_invalid"),
        ("x" * (h.MAX_BRANCH_NAME_LENGTH + 1), "branch_name_invalid"),
    ],
)
def test_new_branch_refuses_a_name_git_would_not_take_as_given(env, repo, name, code):
    fixture_git(repo, "switch", "-q", "-c", "previous")
    fixture_git(repo, "switch", "-q", "main")  # so that @{-1} names a branch
    assert error_code(env, "git-branch-new", f"--name={name}", "--", REPO_REL) == code
    assert branch_of(repo) == "main"


def test_new_branch_with_a_name_taken_passes_on_gits_words(env, repo):
    code, out = helper(env, "git-branch-new", "--name=main", "--", REPO_REL)
    assert (code, out["error"]["code"]) == (1, "git_failed")
    assert any("already exists" in line for line in out["error"]["git"])


def test_new_branch_before_the_first_commit(env):
    fresh = Path(env["RC_HELPER_HOME"]) / "projects" / "fresh"
    fresh.mkdir()
    fixture_git(fresh, "init", "-q", "-b", "main")
    out = ok(env, "git-branch-new", "--name=start", "--", "projects/fresh")
    assert out["branch"] == "start"
    assert status(env, "projects/fresh")["branch"] == {
        "name": "start",
        "commit": None,
        "upstream": None,
        "ahead": None,
        "behind": None,
    }


def remote_names(env) -> list[str]:
    return [b["name"] for b in ok(env, "git-branches", "--", REPO_REL)["remote"]["branches"]]


def test_fetch_adds_new_remote_branches_and_prunes_gone_ones(env, repo):
    push_branch_to_origin(repo, origin_of(repo), "later")
    fixture_git(origin_of(repo), "branch", "-D", "feature")
    before = remote_names(env)
    assert "origin/feature" in before and "origin/later" not in before  # positive control
    assert ok(env, "git-fetch", "--", REPO_REL)["path"] == REPO_REL
    assert remote_names(env) == ["origin/later", "origin/main"]
    assert branch_of(repo) == "main"
    assert listed(status(env), "not_staged") == []


@pytest.fixture
def asks_for_a_password():
    """An HTTP server that answers every request with a demand for a password."""
    import http.server
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="rcman test"')
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}/repo.git"
    server.shutdown()


def test_fetch_from_a_remote_that_needs_a_password_fails_without_asking(
    env, repo, asks_for_a_password
):
    import time

    fixture_git(repo, "remote", "set-url", "origin", asks_for_a_password)
    began = time.monotonic()
    code, out = helper(env, "git-fetch", "--", REPO_REL)
    assert (code, out["error"]["code"]) == (1, "git_failed"), out
    assert any("terminal prompts disabled" in line for line in out["error"]["git"]), out
    assert time.monotonic() - began < h.GIT_NETWORK_TIMEOUT / 4


def test_git_runs_with_the_allowlisted_environment(env, repo, tmp_path):
    dumped = tmp_path / "env.json"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/usr/bin/python3\n"
        "import json, os\n"
        "helper = open(f'/proc/{os.getppid()}/stat').read().rsplit(')', 1)[1].split()[1]\n"
        "helper_env = open(f'/proc/{helper}/environ', 'rb').read().split(b'\\0')\n"
        f"json.dump({{'git': dict(os.environ), 'helper': [e.decode() for e in helper_env]}},"
        f" open({str(dumped)!r}, 'w'))\n"
    )
    hook.chmod(0o755)
    write(repo / "a.txt")
    fixture_git(repo, "add", "a.txt")
    decoyed = {**env, "RCMAN_DECOY": "present"}
    assert helper(decoyed, *commit_args(), stdin="m")[0] == 0
    seen = json.loads(dumped.read_text())
    assert "RCMAN_DECOY=present" in seen["helper"]  # positive control: the helper had it
    assert "RCMAN_DECOY" not in seen["git"] and "RC_HELPER_HOME" not in seen["git"]
    expected = h.git_env(h.config_from_env(env))
    # git puts its own programs first on the PATH its hooks get
    exec_path = fixture_git(repo, "--exec-path").strip()
    assert seen["git"]["PATH"].startswith(f"{exec_path}:")
    seen["git"]["PATH"] = seen["git"]["PATH"].removeprefix(f"{exec_path}:")
    assert {k: v for k, v in seen["git"].items() if not k.startswith("GIT_")} == {
        k: v for k, v in expected.items() if not k.startswith("GIT_")
    }
    assert seen["git"]["GIT_TERMINAL_PROMPT"] == "0"
    assert seen["git"]["GIT_OPTIONAL_LOCKS"] == "0"
    assert seen["git"]["PATH"].startswith(f"{env['RC_HELPER_HOME']}/.local/bin:")
    assert "GIT_CONFIG_NOSYSTEM" not in seen["git"]


def commit_args(rel=REPO_REL):
    return ("git-commit", "--author-name=Ann", "--author-email=ann@example.com", "--", rel)


def test_sigterm_ends_git_and_its_hooks(env, repo, tmp_path):
    import signal
    import time

    pid_file = tmp_path / "hook.pid"
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(f"#!/bin/sh\necho $$ > {pid_file}\nexec sleep 60\n")
    hook.chmod(0o755)
    write(repo / "a.txt")
    fixture_git(repo, "add", "a.txt")
    proc = subprocess.Popen(
        [str(HELPER), *commit_args()],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    proc.stdin.write(b"m")
    proc.stdin.close()
    deadline = time.monotonic() + 20
    while not pid_file.exists() or not pid_file.read_text().strip():
        assert time.monotonic() < deadline, "the hook never ran"
        time.sleep(0.05)
    pid = int(pid_file.read_text())
    os.kill(pid, 0)  # positive control: the hook is running
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=10)
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"the hook {pid} outlived the helper")


IN_REPO = ("--", REPO_REL)
# Every verb that runs git: its command line and what it reads on stdin.
GIT_VERBS = [
    (("git-status", *IN_REPO), None),
    (("git-branches", *IN_REPO), None),
    (("git-stage", *IN_REPO), '["a.txt"]'),
    (("git-unstage", *IN_REPO), '["a.txt"]'),
    (("git-commit", "--author-name=Ann", "--author-email=ann@example.com", *IN_REPO), "m"),
    (("git-switch", "--branch=main", *IN_REPO), None),
    (("git-branch-new", "--name=new", *IN_REPO), None),
    (("git-fetch", *IN_REPO), None),
    (("git-push", *IN_REPO), None),
    (("github-token",), "a-token\n"),
    (("github-status",), None),
    (("github-forget",), None),
]


def test_every_git_verb_is_driven_here():
    git_verbs = {v for v in h.VERBS if v.startswith(("git-", "github-"))}
    assert {"git-status", "github-token"} <= git_verbs  # non-vacuity
    assert {args[0] for args, _ in GIT_VERBS} == git_verbs


@pytest.mark.parametrize(("args", "stdin"), GIT_VERBS, ids=[a[0] for a, _ in GIT_VERBS])
def test_git_verbs_wait_for_no_folder_being_deleted(env, repo, args, stdin):
    write(repo / "a.txt")
    fd = hold_the_lock(env, exclusive=True)
    try:
        assert error_code(env, *args, stdin=stdin) == "folder_busy"
    finally:
        os.close(fd)
    shared = hold_the_lock(env, exclusive=False)
    try:  # positive control: they share the lock with each other
        code, out = helper(env, *args, stdin=stdin)
        assert out.get("error", {}).get("code") != "folder_busy", out
    finally:
        os.close(shared)


def changes_table(markdown: str) -> list[str]:
    """The change codes in the first column of the table under "| Change |" in a guide."""
    lines = markdown.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("| Change |")), None)
    if start is None:
        return []
    codes = []
    for line in lines[start + 2 :]:
        if not line.startswith("|"):
            break
        codes.append(re.fullmatch(r"\| `([a-z_]+)` \|.*", line).group(1))
    return codes


def test_the_git_guides_table_of_changes_is_every_change_rc_helper_reports():
    guide = (REPO / "docs" / "git.md").read_text(encoding="utf-8")
    assert changes_table(guide) == list(h.CHANGES)


def test_the_changes_table_check_fails_on_a_drifted_table():
    """Planted twins: a row missing, or one too many."""
    rows = [f"| `{c}` | x | y |" for c in h.CHANGES]
    right = "| Change | Shown as | Meaning |\n|---|---|---|\n" + "\n".join(rows) + "\n"
    assert changes_table(right) == list(h.CHANGES)
    assert changes_table(right.replace(rows[0] + "\n", "")) != list(h.CHANGES)
    assert changes_table(right + "| `forged` | x | y |\n") != list(h.CHANGES)
    assert changes_table("nothing here") == []


def test_git_env_is_the_server_environment_cut_down_for_git(tmp_path):
    cfg = h.Config(home=tmp_path, claude=tmp_path / "claude", tmux_socket=None)
    env = h.git_env(cfg)
    child = h.child_env(cfg)
    for key in ("HOME", "USER", "LOGNAME", "PATH", "LANG"):
        assert env[key] == child[key]
    assert env["LC_ALL"] == "C.UTF-8"
    assert set(env) == {
        "HOME",
        "USER",
        "LOGNAME",
        "PATH",
        "LANG",
        "LC_ALL",
        "GIT_TERMINAL_PROMPT",
        "GIT_OPTIONAL_LOCKS",
    }


def test_git_env_points_github_at_the_hook_only_when_set(tmp_path):
    import dataclasses

    plain = h.Config(home=tmp_path, claude=tmp_path / "claude", tmux_socket=None)
    assert plain.github == h.GITHUB_URL
    assert not any(key.startswith("GIT_CONFIG") for key in h.git_env(plain))
    hooked = h.git_env(dataclasses.replace(plain, github="http://127.0.0.1:9/"))
    assert {k: v for k, v in hooked.items() if k.startswith("GIT_CONFIG")} == {
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "url.http://127.0.0.1:9/.insteadOf",
        "GIT_CONFIG_VALUE_0": "https://github.com/",
    }


@pytest.fixture
def hooked(env, github):
    """``env`` with https://github.com/ pointed at the tests' GitHub."""
    return {**env, "RC_HELPER_GITHUB": github.url}


GH_REL = "projects/from-github"


@pytest.fixture
def on_github(env, github, tmp_path):
    """A repository in the home whose origin is https://github.com/owner/<unique>.git,
    served by the tests' GitHub; its bare repository there."""
    name = tmp_path.name
    bare = github.bare("owner", name)
    make_repo(
        Path(env["RC_HELPER_HOME"]) / GH_REL, origin=bare, url=github.github_url("owner", name)
    )
    return bare


def test_the_github_hook_sends_git_for_github_to_the_tests_server(env, hooked, on_github):
    repo = Path(env["RC_HELPER_HOME"]) / GH_REL
    assert fixture_git(repo, "remote", "get-url", "origin").startswith("https://github.com/")
    push_branch_to_origin(repo, on_github, "later")
    code, out = helper(env, "git-fetch", "--", GH_REL)  # unhooked: the unreachable address
    assert (code, out["error"]["code"]) == (1, "git_failed"), out
    assert any("127.0.0.1 port 1" in line for line in out["error"]["git"]), out
    ok(hooked, "git-fetch", "--", GH_REL)
    branches = ok(hooked, "git-branches", "--", GH_REL)["remote"]["branches"]
    assert "origin/later" in [b["name"] for b in branches]


# --- the GitHub token -----------------------------------------------------------------

TOKEN = "github_pat_rcman-test-0123456789"


def run_helper(env, *args, stdin=None) -> subprocess.CompletedProcess:
    """rc-helper's whole run: exit status, standard output and standard error."""
    return subprocess.run(
        [str(HELPER), *args], env=env, input=stdin, capture_output=True, text=True, timeout=60
    )


def set_token(env, token=TOKEN):
    return helper(env, "github-token", stdin=token + "\n")


def git_credentials(env) -> Path:
    return Path(env["RC_HELPER_HOME"]) / ".git-credentials"


def server_base(github) -> str:
    """The address the hook sends GitHub to, as a credential and its helper know it."""
    return github.url.removesuffix("/")


def stored_host(github) -> str:
    """The hook's host as git credential-store writes it, its colon percent-encoded."""
    return server_base(github).removeprefix("http://").replace(":", "%3a")


def global_config(env, *args) -> list[str]:
    """What the person's own ~/.gitconfig says, read with git outside rc-helper."""
    proc = subprocess.run(
        ["git", "config", "--file", str(Path(env["RC_HELPER_HOME"]) / ".gitconfig"), *args],
        capture_output=True,
        text=True,
    )
    return proc.stdout.split("\n")[:-1]


def test_the_token_is_read_from_stdin_and_stored_for_github(hooked, github):
    proc = run_helper(hooked, "github-token", stdin=TOKEN + "\n")
    assert proc.returncode == 0, proc.stdout
    assert json.loads(proc.stdout) == {"token": True}
    assert TOKEN not in proc.stdout + proc.stderr
    stored = git_credentials(hooked)
    assert stored.read_text() == f"http://{h.GITHUB_USERNAME}:{TOKEN}@{stored_host(github)}\n"
    assert stat.S_IMODE(stored.stat().st_mode) == 0o600
    assert global_config(hooked, "--get-all", f"credential.{server_base(github)}.helper") == [
        "store"
    ]
    proc = run_helper(hooked, "github-status")
    assert json.loads(proc.stdout) == {"token": True} and TOKEN not in proc.stdout + proc.stderr


def test_without_the_hook_the_token_is_stored_for_github_com(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    cfg = h.Config(home=home, claude=tmp_path / "claude", tmux_socket=None)
    assert cfg.github == h.GITHUB_URL
    helper_ = h.Helper(cfg)
    assert helper_.github_token(TOKEN.encode() + b"\n") == {"token": True}
    assert (home / ".git-credentials").read_text() == (
        f"https://{h.GITHUB_USERNAME}:{TOKEN}@github.com\n"
    )
    env = helper_env(home, tmp_path / "claude", tmp_path / "tmux.sock")
    assert global_config(env, "--get-all", "credential.https://github.com.helper") == ["store"]
    assert helper_.github_status() == {"token": True}
    assert helper_.github_forget() == {"token": False}
    assert helper_.github_status() == {"token": False}


def test_the_store_helper_is_set_once(hooked, github):
    key = f"credential.{server_base(github)}.helper"
    assert set_token(hooked)[0] == 0
    assert set_token(hooked, "another-token")[0] == 0
    assert global_config(hooked, "--get-all", key) == ["store"]


def test_a_store_helper_set_already_is_not_set_again(hooked, github):
    key = f"credential.{server_base(github)}.helper"
    gitconfig = Path(hooked["RC_HELPER_HOME"]) / ".gitconfig"
    gitconfig.write_text(f'[credential "{server_base(github)}"]\n\thelper = store\n')
    assert set_token(hooked)[0] == 0
    assert global_config(hooked, "--get-all", key) == ["store"]


def test_a_new_token_replaces_the_old(hooked, github):
    assert set_token(hooked, "first-token")[0] == 0
    assert set_token(hooked, "second-token")[0] == 0
    host = stored_host(github)
    assert (
        git_credentials(hooked).read_text() == f"http://{h.GITHUB_USERNAME}:second-token@{host}\n"
    )


def test_status_needs_both_the_token_and_its_helper(hooked, github):
    assert ok(hooked, "github-status") == {"token": False}  # neither
    assert set_token(hooked)[0] == 0
    assert ok(hooked, "github-status") == {"token": True}  # both
    key = f"credential.{server_base(github)}.helper"
    subprocess.run(
        ["git", "config", "--file", str(Path(hooked["RC_HELPER_HOME"]) / ".gitconfig"),
         "--unset-all", key],
        check=True,
    )  # fmt: skip
    assert ok(hooked, "github-status") == {"token": False}  # the token, but git would not use it
    assert set_token(hooked)[0] == 0
    assert ok(hooked, "github-forget") == {"token": False}
    assert global_config(hooked, "--get-all", key) == ["store"]  # the helper is left
    assert ok(hooked, "github-status") == {"token": False}  # the helper, but no token


def test_forget_removes_rcmans_token_only(hooked, github):
    host = server_base(github).removeprefix("http://")
    theirs = f"http://someone:their-own-token@{host}\n"
    git_credentials(hooked).write_text(theirs)
    git_credentials(hooked).chmod(0o600)
    assert set_token(hooked)[0] == 0
    assert TOKEN in git_credentials(hooked).read_text()  # positive control
    assert ok(hooked, "github-forget") == {"token": False}
    assert git_credentials(hooked).read_text() == theirs


@pytest.mark.parametrize(
    "stdin",
    [
        "",
        "\n",
        "has space\n",
        "two\nlines\n",
        "tab\there\n",
        "carriage-return\r\n",
        "ünïcode\n",
        "too long",
        b"\xff\xfe-not-utf8\n",
    ],
)
def test_a_token_that_is_not_one_line_of_visible_characters_is_refused(hooked, stdin):
    if stdin == "too long":
        stdin = "x" * (h.MAX_GITHUB_TOKEN_LENGTH + 1) + "\n"
    code, out = helper(hooked, "github-token", stdin=stdin)
    assert (code, out["error"]["code"]) == (1, "token_invalid"), out
    assert out["error"]["message"] == h.GITHUB_TOKEN.sentence
    assert not git_credentials(hooked).exists()


def test_a_token_as_long_as_allowed_and_of_every_visible_character_is_stored(hooked):
    visible = "".join(map(chr, range(0x21, 0x7F)))
    assert set_token(hooked, visible)[0] == 0
    assert set_token(hooked, "x" * h.MAX_GITHUB_TOKEN_LENGTH)[0] == 0
    assert ok(hooked, "github-status") == {"token": True}


def test_the_token_rule_states_its_limit():
    assert str(h.MAX_GITHUB_TOKEN_LENGTH) in h.GITHUB_TOKEN.sentence


def push_as_the_person(env, repo: Path) -> subprocess.CompletedProcess:
    """A plain git push, as from a Claude session: the server's environment, no terminal,
    and the hook's address for GitHub, as rc-helper gives git."""
    cfg = h.config_from_env(env)
    hook = {k: v for k, v in h.git_env(cfg).items() if k.startswith("GIT_CONFIG")}
    assert hook  # positive control: the push goes to the tests' GitHub
    return subprocess.run(
        ["git", "push", "origin", "HEAD:refs/heads/pushed"],
        cwd=repo,
        env={**h.child_env(cfg), **hook},
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        start_new_session=True,
        timeout=60,
    )


def pushed(bare: Path) -> str | None:
    found = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "--verify", "-q", "refs/heads/pushed"],
        capture_output=True,
        text=True,
    )
    return found.stdout.strip() or None


def test_a_plain_git_push_as_the_person_uses_the_stored_token(hooked, github, on_github):
    repo = Path(hooked["RC_HELPER_HOME"]) / GH_REL
    before = len(github.attempts)
    refused = push_as_the_person(hooked, repo)
    assert refused.returncode != 0 and pushed(on_github) is None
    assert ("x-access-token", True) not in github.attempts[before:]
    assert set_token(hooked, github.token)[0] == 0
    before = len(github.attempts)
    accepted = push_as_the_person(hooked, repo)
    assert accepted.returncode == 0, accepted.stderr
    assert pushed(on_github) == fixture_git(repo, "rev-parse", "HEAD").strip()
    assert set(github.attempts[before:]) == {(h.GITHUB_USERNAME, True)}


def test_rcmans_token_is_used_before_an_older_one(hooked, github, on_github):
    host = server_base(github).removeprefix("http://")
    git_credentials(hooked).write_text(f"http://older:not-the-token@{host}\n")
    git_credentials(hooked).chmod(0o600)
    assert set_token(hooked, github.token)[0] == 0
    before = len(github.attempts)
    proc = push_as_the_person(hooked, Path(hooked["RC_HELPER_HOME"]) / GH_REL)
    assert proc.returncode == 0, proc.stderr
    assert set(github.attempts[before:]) == {(h.GITHUB_USERNAME, True)}


def test_a_token_github_refuses_is_forgotten_by_git(hooked, github, on_github):
    assert set_token(hooked, "not-the-token")[0] == 0
    assert ok(hooked, "github-status") == {"token": True}
    proc = push_as_the_person(hooked, Path(hooked["RC_HELPER_HOME"]) / GH_REL)
    assert proc.returncode != 0 and "Authentication failed" in proc.stderr
    assert ok(hooked, "github-status") == {"token": False}


import time  # noqa: E402


def traced(monkeypatch, tmp_path) -> Path:
    """GIT_TRACE for every git rc-helper runs in this process: the file it writes to."""
    trace = tmp_path / "trace"
    plain = h.git_env
    monkeypatch.setattr(h, "git_env", lambda cfg: {**plain(cfg), "GIT_TRACE": str(trace)})
    return trace


def test_the_token_reaches_no_argv_git_traces(hooked, monkeypatch, tmp_path):
    trace = traced(monkeypatch, tmp_path)
    helper_ = in_process(hooked)
    helper_.github_token(TOKEN.encode() + b"\n")
    helper_.github_status()
    helper_.github_forget()
    text = trace.read_text()
    assert "credential-store" in text and "config" in text  # positive control
    assert TOKEN not in text


def test_a_push_reaches_github_with_the_token_in_no_argv(
    hooked, github, on_github, monkeypatch, tmp_path
):
    trace = traced(monkeypatch, tmp_path)
    helper_ = in_process(hooked)
    helper_.github_token(github.token.encode() + b"\n")
    repo = Path(hooked["RC_HELPER_HOME"]) / GH_REL
    commit_in(repo, "pushed.txt")
    assert helper_.git_push(GH_REL, False)["upstream_set"] is False
    text = trace.read_text()
    assert "remote-http" in text and "push" in text  # positive control: it went over HTTP
    assert github.token not in text


def test_the_trace_check_sees_what_is_in_an_argv(hooked, monkeypatch, tmp_path):
    """Planted twin: a marker in a git argv is in the trace the check above reads."""
    trace = traced(monkeypatch, tmp_path)
    marker = "PLANTED-MARKER-IN-ARGV"
    home = Path(hooked["RC_HELPER_HOME"])
    git_run.Git(h.git_env(h.config_from_env(hooked))).run(
        home,
        ["config", "--file", str(tmp_path / "cfg"), "rcman.marker", marker],
        deadline=time.monotonic() + 30,
    )
    assert marker in trace.read_text()


# --- push ---------------------------------------------------------------------------


def commit_in(repo: Path, name: str) -> str:
    """A new commit in ``repo`` adding ``name``; its hash."""
    write(repo / name)
    fixture_git(repo, "add", name)
    fixture_git(repo, "commit", "-q", "-m", f"Add {name}")
    return fixture_git(repo, "rev-parse", "HEAD").strip()


def on_remote(bare: Path, branch: str) -> str | None:
    found = subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "--verify", "-q", f"refs/heads/{branch}"],
        capture_output=True,
        text=True,
    )
    return found.stdout.strip() or None


def push(env, rel, *options):
    return helper(env, "git-push", *options, "--", rel)


def test_a_branch_with_no_upstream_is_pushed_and_set_to_track_it(hooked, github, on_github):
    repo = Path(hooked["RC_HELPER_HOME"]) / GH_REL
    assert set_token(hooked, github.token)[0] == 0
    fixture_git(repo, "switch", "-q", "-c", "topic")
    head = commit_in(repo, "topic.txt")
    code, out = push(hooked, GH_REL)
    assert code == 0, out
    assert out == {
        "path": GH_REL,
        "branch": "topic",
        "upstream": "origin/topic",
        "upstream_set": True,
    }
    assert on_remote(on_github, "topic") == head
    tracked = fixture_git(repo, "for-each-ref", "--format=%(upstream)", "refs/heads/topic")
    assert tracked.strip() == "refs/remotes/origin/topic"


def test_a_branch_with_an_upstream_is_pushed_there(hooked, github, on_github):
    repo = Path(hooked["RC_HELPER_HOME"]) / GH_REL
    assert set_token(hooked, github.token)[0] == 0
    head = commit_in(repo, "more.txt")
    code, out = push(hooked, GH_REL)
    assert code == 0, out
    assert out == {
        "path": GH_REL,
        "branch": "main",
        "upstream": "origin/main",
        "upstream_set": False,
    }
    assert on_remote(on_github, "main") == head


def test_a_push_to_github_is_refused_without_a_token(hooked, github, on_github):
    repo = Path(hooked["RC_HELPER_HOME"]) / GH_REL
    before = on_remote(on_github, "main")
    commit_in(repo, "more.txt")
    code, out = push(hooked, GH_REL)
    assert (code, out["error"]["code"]) == (1, "github_token_not_set"), out
    assert out["error"]["message"] == h.MESSAGES["github_token_not_set"]
    assert on_remote(on_github, "main") == before
    assert set_token(hooked, github.token)[0] == 0
    assert push(hooked, GH_REL)[0] == 0  # positive control: with the token it goes


def test_a_push_to_another_remote_needs_no_token(env, repo):
    head = commit_in(repo, "more.txt")
    assert ok(env, "github-status") == {"token": False}
    code, out = push(env, REPO_REL)
    assert code == 0, out
    assert out["upstream"] == "origin/main"
    assert on_remote(origin_of(repo), "main") == head


def test_a_token_github_refuses_comes_back_in_gits_words_without_it(hooked, github, on_github):
    repo = Path(hooked["RC_HELPER_HOME"]) / GH_REL
    wrong = "a-token-github-refuses"
    assert set_token(hooked, wrong)[0] == 0
    commit_in(repo, "more.txt")
    proc = run_helper(hooked, "git-push", "--", GH_REL)
    out = json.loads(proc.stdout)
    assert out["error"]["code"] == "git_failed", out
    assert any("Authentication failed" in line for line in out["error"]["git"]), out
    assert wrong not in proc.stdout + proc.stderr


def test_a_rejected_push_comes_back_in_gits_words(env, repo):
    push_branch_to_origin(repo, origin_of(repo), "elsewhere")
    fixture_git(origin_of(repo), "update-ref", "refs/heads/main", "refs/heads/elsewhere")
    commit_in(repo, "local.txt")
    code, out = push(env, REPO_REL)
    assert (code, out["error"]["code"]) == (1, "git_failed"), out
    assert any("rejected" in line for line in out["error"]["git"]), out


def test_a_branch_with_no_commit_yet_comes_back_in_gits_words(env, repo):
    fresh = Path(env["RC_HELPER_HOME"]) / "projects" / "fresh"
    fresh.mkdir()
    fixture_git(fresh, "init", "-q", "-b", "trunk")
    fixture_git(fresh, "remote", "add", "origin", str(origin_of(repo)))
    assert ok(env, "git-status", "--", "projects/fresh")["push_to_github"] is False
    code, out = push(env, "projects/fresh")
    assert (code, out["error"]["code"]) == (1, "git_failed"), out
    assert any("does not match any" in line for line in out["error"]["git"]), out


def test_a_detached_head_is_not_pushed(env, repo):
    commit_in(repo, "more.txt")
    fixture_git(repo, "switch", "-q", "--detach", "HEAD")
    before = on_remote(origin_of(repo), "main")
    code, out = push(env, REPO_REL)
    assert (code, out["error"]["code"]) == (1, "detached_head"), out
    assert on_remote(origin_of(repo), "main") == before


def test_check_says_whether_a_push_could_start_and_pushes_nothing(env, hooked, github, on_github):
    repo = Path(hooked["RC_HELPER_HOME"]) / GH_REL
    before = on_remote(on_github, "main")
    commit_in(repo, "more.txt")
    code, out = push(hooked, GH_REL, "--check")
    assert (code, out["error"]["code"]) == (1, "github_token_not_set"), out
    assert set_token(hooked, github.token)[0] == 0
    assert ok(hooked, "git-push", "--check", "--", GH_REL) == {"path": GH_REL, "branch": "main"}
    assert on_remote(on_github, "main") == before
    fixture_git(repo, "switch", "-q", "--detach", "HEAD")
    assert error_code(hooked, "git-push", "--check", "--", GH_REL) == "detached_head"


def test_status_says_whether_a_push_goes_to_github(env, hooked, repo, on_github, tmp_path):
    assert ok(hooked, "git-status", "--", GH_REL)["push_to_github"] is True
    assert ok(hooked, "git-status", "--", REPO_REL)["push_to_github"] is False
    gh_repo = Path(hooked["RC_HELPER_HOME"]) / GH_REL
    fixture_git(gh_repo, "switch", "-q", "-c", "no-upstream")
    assert ok(hooked, "git-status", "--", GH_REL)["push_to_github"] is True  # to origin
    fixture_git(gh_repo, "switch", "-q", "main")
    elsewhere = tmp_path / "elsewhere.git"
    fixture_git(tmp_path, "init", "-q", "--bare", str(elsewhere))
    fixture_git(gh_repo, "remote", "set-url", "--push", "origin", str(elsewhere))
    assert ok(hooked, "git-status", "--", GH_REL)["push_to_github"] is False
    assert ok(hooked, "git-push", "--check", "--", GH_REL)["branch"] == "main"  # no token asked


def test_a_push_remote_of_its_own_decides_where_a_branch_is_pushed(hooked, repo, on_github):
    """A branch tracking a remote that is not GitHub, with GitHub as its push remote."""
    gh_url = fixture_git(Path(hooked["RC_HELPER_HOME"]) / GH_REL, "remote", "get-url", "origin")
    fixture_git(repo, "remote", "add", "github", gh_url.strip())
    assert ok(hooked, "git-status", "--", REPO_REL)["push_to_github"] is False
    fixture_git(repo, "config", "branch.main.pushRemote", "github")
    assert ok(hooked, "git-status", "--", REPO_REL)["push_to_github"] is True
    assert error_code(hooked, "git-push", "--", REPO_REL) == "github_token_not_set"


def test_a_repository_with_no_remote_is_no_push_to_github(env):
    repo = make_repo(Path(env["RC_HELPER_HOME"]) / "projects" / "lonely")
    assert ok(env, "git-status", "--", "projects/lonely")["push_to_github"] is False
    commit_in(repo, "x.txt")
    assert error_code(env, "git-push", "--", "projects/lonely") == "git_failed"


SESSION_DUMP = """#!/usr/bin/python3
import json, os
sid = os.getsid(0)
helper = open(f"/proc/{sid}/stat").read().rsplit(")", 1)[1].split()[1]
seen = []
for pid in [p for p in os.listdir("/proc") if p.isdigit()]:
    try:
        if os.getsid(int(pid)) != sid and pid != helper:
            continue
        seen.append([open(f"/proc/{pid}/{part}", "rb").read().decode("utf-8", "replace")
                     for part in ("cmdline", "environ")])
    except OSError:
        pass
json.dump(seen, open(OUT, "w"))
"""


def test_while_a_push_runs_no_process_holds_the_token(hooked, github, on_github, tmp_path):
    """Every process of the push's session, and rc-helper itself, read from /proc by a
    pre-push hook while git's HTTP helper is talking to the server."""
    repo = Path(hooked["RC_HELPER_HOME"]) / GH_REL
    dumped = tmp_path / "seen.json"
    hook = repo / ".git" / "hooks" / "pre-push"
    hook.write_text(SESSION_DUMP.replace("OUT", repr(str(dumped))))
    hook.chmod(0o755)
    assert set_token(hooked, github.token)[0] == 0
    commit_in(repo, "more.txt")
    assert push(hooked, GH_REL)[0] == 0
    seen = json.loads(dumped.read_text())
    commands = [cmdline for cmdline, _ in seen]
    assert any("remote-http" in c for c in commands), commands  # positive control
    assert any("git-push" in c for c in commands), commands  # rc-helper itself
    assert all(github.token not in cmdline + environ for cmdline, environ in seen)
