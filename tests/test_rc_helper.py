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
        ("a/b/..", "path_ends_in_dot"),
        ("a/b/.", "path_ends_in_dot"),
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
    assert error_code(env, "mkdir", f"--name={over}", "--", deep) == "folder_path_too_long"
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
# the command line with the refused value in place of {}.
RULE_INVOCATIONS = {
    "prefix_invalid": ("start", "projects/alpha", "--prefix={}"),
    "name_invalid": ("mkdir", "--name={}", "--", "projects"),
    "email_invalid": ("auth-login", "--email={}"),
    "path_invalid": ("output", "--", "{}"),
}
# Rules rc-helper holds without reporting them itself: subject -> why.
RULES_NOT_REPORTED = {
    "AUTHOR_NAME": "the backend checks it; rc-helper takes no author until git-commit",
}


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
        args = [part.format(value) for part in RULE_INVOCATIONS[code]]
        status, out = helper(env, *args)
        assert status == 1 and out["error"]["code"] == code, (code, out)
        assert out["error"]["message"] == rule.sentence


def rule_code_problems(source: str, codes: dict) -> list[str]:
    """Where a rule's code is raised in a function that does not check that rule: the
    code would then say the rule's sentence about a value the rule accepts."""
    import ast

    names = {code: next(n for n, v in vars(h).items() if v is rule) for code, rule in codes.items()}
    found = []
    for function in ast.walk(ast.parse(source)):
        if not isinstance(function, ast.FunctionDef):
            continue
        text = ast.unparse(function)
        for node in ast.walk(function):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "HelperError"):
                continue
            # The code may be chosen in the call: every constant it could be counts.
            given = {
                c.value
                for arg in node.args[:1]
                for c in ast.walk(arg)
                if isinstance(c, ast.Constant)
            }
            for code in sorted(given & set(names)):
                if f"{names[code]}.fullmatch" not in text:
                    found.append(f"{function.name} raises {code} without checking {names[code]}")
    return found


def test_a_rules_code_is_raised_only_where_the_rule_is_checked():
    source = (REPO / "helpers" / "rc_helper.py").read_text(encoding="utf-8")
    assert 'HelperError("path_invalid")' in source  # non-vacuity
    assert rule_code_problems(source, h.RULE_CODES) == []


def test_a_rules_code_raised_elsewhere_is_found():
    """Planted twin: path_invalid raised by a function that never checks PATH."""
    planted = (
        "def check(rel):\n"
        "    if not PATH.fullmatch(rel):\n"
        "        raise HelperError('path_invalid')\n"
        "def other(rel):\n"
        "    if rel.endswith('.'):\n"
        "        raise HelperError('path_is_home' if rel == '.' else 'path_invalid')\n"
    )
    assert rule_code_problems(planted, h.RULE_CODES) == [
        "other raises path_invalid without checking PATH"
    ]


def test_a_path_ending_in_a_dot_is_refused_in_its_own_words(env):
    for rel in ("projects/beta/inner/..", "projects/alpha/."):
        status, out = helper(env, "delete", "--", rel)
        assert (status, out["error"]["code"]) == (1, "path_ends_in_dot"), rel
        assert out["error"]["message"] == h.MESSAGES["path_ends_in_dot"]
    assert (Path(env["RC_HELPER_HOME"]) / "projects" / "alpha").is_dir()


def test_rc_helper_and_the_backend_refuse_a_value_in_the_same_words(env, client):
    """Every field the backend checks against one of rc-helper's reported rules says
    what rc-helper says for the same value."""
    from tests.conftest import ALICE, CALLS, changing
    from tests.test_problems import request_fields, send

    log_in(env)
    fields = request_fields(client.app)
    for code, rule in h.RULE_CODES.items():
        value = refused_value(rule)
        args = [part.format(value) for part in RULE_INVOCATIONS[code]]
        said = helper(env, *args)[1]["error"]["message"]
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
