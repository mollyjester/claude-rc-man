"""The installer's decisions: settings, rendered files, releases.

Real installations are tested in the container suite.
"""

import ast
import contextlib
import hashlib
import os
import re
import shutil
import socket
import stat
import subprocess
from pathlib import Path

import pytest

from installer import install as ins

REPO = Path(__file__).resolve().parent.parent


# --- settings --------------------------------------------------------------------------


def test_flags_beat_environment_beat_saved_file(tmp_path):
    saved = tmp_path / "install.env"
    saved.write_text("DOMAIN=saved.example.com\nADMIN_EMAIL=saved@example.com\nPROXY_PORT=9000\n")
    settings = ins.gather(
        saved,
        environ={
            "RCMAN_INSTALL_DOMAIN": "env.example.com",
            "RCMAN_INSTALL_ADMIN_EMAIL": "env@example.com",
            "RCMAN_INSTALL_GOOGLE_CLIENT_ID": "id",
            "RCMAN_INSTALL_GOOGLE_CLIENT_SECRET": "secret",
        },
        flags={"domain": "flag.example.com"},
        ask=None,
    )
    assert settings["DOMAIN"] == "flag.example.com"
    assert settings["ADMIN_EMAIL"] == "env@example.com"
    assert settings["PROXY_PORT"] == "9000"


def test_saved_settings_outside_the_fields_are_dropped(tmp_path):
    """A setting the installer no longer has, such as a saved APP_PORT, is not kept."""
    saved = tmp_path / "install.env"
    saved.write_text(
        "DOMAIN=saved.example.com\nADMIN_EMAIL=saved@example.com\nGOOGLE_CLIENT_ID=id\n"
        "GOOGLE_CLIENT_SECRET=secret\nCOOKIE_SECRET=" + "c" * 32 + "\nAPP_PORT=8001\n"
    )
    settings = ins.gather(saved, environ={"RCMAN_INSTALL_APP_PORT": "901"}, flags={}, ask=None)
    assert "APP_PORT" not in settings
    assert settings["COOKIE_SECRET"] == "c" * 32  # kept: generated once, not a field
    assert set(settings) == {*ins.FIELDS, "COOKIE_SECRET"}
    ins.save_settings(saved, settings)
    assert "APP_PORT" not in saved.read_text()


def test_there_is_no_app_port_option(capsys):
    with pytest.raises(SystemExit) as exited:
        ins.main(["--app-port", "8001"])
    assert exited.value.code == 2
    assert "--app-port" in capsys.readouterr().err


def test_caddy_runs_as_caddy_unless_told_otherwise(tmp_path):
    env = {
        "RCMAN_INSTALL_DOMAIN": "x.example.com",
        "RCMAN_INSTALL_GOOGLE_CLIENT_ID": "id",
        "RCMAN_INSTALL_GOOGLE_CLIENT_SECRET": "secret",
        "RCMAN_INSTALL_ADMIN_EMAIL": "a@example.com",
    }
    saved = tmp_path / "install.env"
    assert ins.gather(saved, environ=env, flags={}, ask=None)["CADDY_USER"] == ("caddy")
    flags = {"caddy_user": "www-data"}
    assert ins.gather(saved, environ=env, flags=flags, ask=None)["CADDY_USER"] == ("www-data")


def test_caddys_account_must_exist():
    import pwd

    present = pwd.getpwuid(os.getuid()).pw_name
    ins.check_caddy_user(present)  # positive control: an account that exists passes
    with pytest.raises(ins.InstallError, match="no-such-caddy"):
        ins.check_caddy_user("no-such-caddy")


def test_missing_values_are_asked_for_when_there_is_a_terminal(tmp_path):
    asked = []

    def ask(key, prompt, secret):
        asked.append((key, secret))
        return {"GOOGLE_CLIENT_SECRET": "s3cret", "ADMIN_EMAIL": "a@example.com"}.get(
            key, "x.example.com"
        )

    settings = ins.gather(tmp_path / "install.env", environ={}, flags={}, ask=ask)
    assert ("GOOGLE_CLIENT_SECRET", True) in asked
    assert ("DOMAIN", False) in asked
    assert settings["CLAUDE_DOWNLOAD"] == ins.CLAUDE_DOWNLOAD  # defaults are not asked for


def test_without_a_terminal_every_missing_value_is_named(tmp_path):
    with pytest.raises(ins.InstallError) as err:
        ins.gather(tmp_path / "install.env", environ={}, flags={}, ask=None)
    for key in ("DOMAIN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "ADMIN_EMAIL"):
        assert key in str(err.value)


def test_cookie_secret_is_generated_once_and_kept(tmp_path):
    saved = tmp_path / "install.env"
    env = {
        f"RCMAN_INSTALL_{k}": "x.example.com"
        for k in ("DOMAIN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")
    }
    env["RCMAN_INSTALL_ADMIN_EMAIL"] = "a@example.com"
    first = ins.gather(saved, environ=env, flags={}, ask=None)
    ins.save_settings(saved, first)
    second = ins.gather(saved, environ=env, flags={}, ask=None)
    assert len(first["COOKIE_SECRET"]) == 32  # oauth2-proxy wants 16, 24 or 32 bytes
    assert second["COOKIE_SECRET"] == first["COOKIE_SECRET"]


def test_saved_settings_are_private_and_round_trip(tmp_path):
    saved = tmp_path / "etc" / "install.env"
    ins.save_settings(saved, {"DOMAIN": "x.example.com", "GOOGLE_CLIENT_SECRET": "a b=c"})
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert ins.load_settings(saved) == {"DOMAIN": "x.example.com", "GOOGLE_CLIENT_SECRET": "a b=c"}


@pytest.mark.parametrize(
    "key,value",
    [
        ("DOMAIN", "https://x.example.com"),
        ("DOMAIN", "x .com"),
        ("ADMIN_EMAIL", "nope"),
        ("PROXY_PORT", "80a"),
        ("CADDY_USER", "Caddy!"),
        ("CLAUDE_DOWNLOAD", "ftp://downloads.example.com"),
        ("CLAUDE_DOWNLOAD", "https://downloads.example.com/"),
    ],
)
def test_invalid_values_are_refused(tmp_path, key, value):
    env = {
        "RCMAN_INSTALL_DOMAIN": "x.example.com",
        "RCMAN_INSTALL_GOOGLE_CLIENT_ID": "id",
        "RCMAN_INSTALL_GOOGLE_CLIENT_SECRET": "secret",
        "RCMAN_INSTALL_ADMIN_EMAIL": "a@example.com",
        f"RCMAN_INSTALL_{key}": value,
    }
    with pytest.raises(ins.InstallError, match=key):
        ins.gather(tmp_path / "install.env", environ=env, flags={}, ask=None)


def test_a_value_refused_by_a_rule_is_explained_by_it(tmp_path):
    from helpers.rules import Rule

    ruled = {key: field[3] for key, field in ins.FIELDS.items() if isinstance(field[3], Rule)}
    assert {"ADMIN_EMAIL"} <= set(ruled)  # non-vacuity
    for key, rule in ruled.items():
        env = {
            "RCMAN_INSTALL_DOMAIN": "x.example.com",
            "RCMAN_INSTALL_GOOGLE_CLIENT_ID": "id",
            "RCMAN_INSTALL_GOOGLE_CLIENT_SECRET": "secret",
            "RCMAN_INSTALL_ADMIN_EMAIL": "a@example.com",
            f"RCMAN_INSTALL_{key}": "Not one!",
        }
        with pytest.raises(ins.InstallError) as error:
            ins.gather(tmp_path / "install.env", environ=env, flags={}, ask=None)
        assert key in str(error.value) and rule.sentence in str(error.value)


def test_the_installer_takes_its_email_rule_from_the_rules():
    import ast

    source = (REPO / "installer" / "install.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "helpers.rules"
        for alias in node.names
    }
    assigned = {
        target.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    assert "EMAIL" in imported and "EMAIL" not in assigned


def test_the_installer_runs_as_install_sh_runs_it_and_writes_nothing_into_the_clone(tmp_path):
    """install.sh runs it with the system interpreter in isolated mode, as root, from the clone."""
    import shutil

    clone = tmp_path / "clone"
    for part in ("installer", "helpers"):
        shutil.copytree(REPO / part, clone / part, ignore=shutil.ignore_patterns("__pycache__"))
    proc = subprocess.run(
        ["/usr/bin/python3", "-I", str(clone / "installer" / "install.py"), "--help"],
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "usage" in proc.stdout
    assert list(clone.rglob("__pycache__")) == []


def test_the_installer_takes_the_clone_off_the_path_once_it_has_the_rules(tmp_path):
    """Run under -I as install.sh runs it, it leaves nothing of the clone on the path."""
    import shutil

    clone = tmp_path / "clone"
    for part in ("installer", "helpers"):
        shutil.copytree(REPO / part, clone / part, ignore=shutil.ignore_patterns("__pycache__"))
    probe = (
        "import runpy, sys\n"
        f"found = runpy.run_path({str(clone / 'installer' / 'install.py')!r})\n"
        "print(found['EMAIL'].pattern != '')\n"
        f"print({str(clone)!r} in sys.path)\n"
    )
    proc = subprocess.run(
        ["/usr/bin/python3", "-I", "-c", probe],
        env={"PATH": "/usr/bin:/bin"},
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split() == ["True", "False"]  # it has the rules, and the path is clean


# --- rendered files ----------------------------------------------------------------------

SETTINGS = {
    "DOMAIN": "rcman.example.com",
    "GOOGLE_CLIENT_ID": "client-id",
    "GOOGLE_CLIENT_SECRET": "client-secret",
    "COOKIE_SECRET": "c" * 32,
    "ADMIN_EMAIL": "admin@example.com",
    "PROXY_PORT": "444",
    "CADDY_USER": "caddy",
    "CLAUDE_DOWNLOAD": "https://downloads.example.com/releases",
    # what the installer finds out and renders along with the settings
    "APP_UID": "990",
    "WORKSPACE_IMAGE": "localhost/rcman-workspace:0123456789ab",
    "BUILDER_IMAGE": "localhost/rcman-builder:0123456789ab",
}


def test_app_environment_points_at_the_current_release():
    env = ins.render("rcman.env", SETTINGS)
    assert "RCMAN_PUBLIC_ORIGIN=https://rcman.example.com\n" in env
    assert f"RCMAN_FRONTEND_DIR={ins.CURRENT}/frontend/dist\n" in env
    assert f"RCMAN_ALLOWED_EMAILS={ins.STATE}/allowed-emails\n" in env
    assert "RCMAN_WORKSPACE_IMAGE=localhost/rcman-workspace:0123456789ab\n" in env
    assert "RCMAN_BUILDER_IMAGE=localhost/rcman-builder:0123456789ab\n" in env
    assert f"RCMAN_TOOLS_DIR={ins.TOOLS}\n" in env
    assert f"RCMAN_CLAUDE_STORE={ins.CLAUDE_STORE}\n" in env
    assert "RCMAN_CLAUDE_DOWNLOAD=https://downloads.example.com/releases\n" in env


def test_every_rendered_app_setting_exists_in_the_settings_model():
    from rcman.settings import Settings

    keys = {
        line.split("=", 1)[0]
        for line in ins.render("rcman.env", SETTINGS).splitlines()
        if "=" in line
    }
    assert keys <= {f"RCMAN_{name.upper()}" for name in Settings.model_fields}


def test_oauth2_proxy_config_allows_only_the_allowlist():
    cfg = ins.render("oauth2-proxy.cfg", SETTINGS)
    assert 'provider = "google"' in cfg
    assert f'authenticated_emails_file = "{ins.STATE}/allowed-emails"' in cfg
    assert 'redirect_url = "https://rcman.example.com/oauth2/callback"' in cfg
    assert 'http_address = "127.0.0.1:444"' in cfg
    assert "email_domains" not in cfg  # any domain would bypass the allowlist
    assert 'cookie_secret = "' + "c" * 32 + '"' in cfg
    assert "set_xauthrequest = true" in cfg


def test_units_run_as_their_own_users_and_the_app_on_its_socket():
    app = ins.render("rcman.service", SETTINGS)
    proxy = ins.render("rcman-oauth2-proxy.service", SETTINGS)
    assert "User=rcman\n" in app and "User=rcman-proxy\n" in proxy
    exec_start = next(line for line in app.splitlines() if line.startswith("ExecStart="))
    assert exec_start.endswith(" --uds /run/rcman/rcman.sock")
    assert str(ins.SOCKET) == "/run/rcman/rcman.sock"
    assert "--port" not in app and "--host" not in app  # no TCP listener at all
    assert f"EnvironmentFile={ins.ETC}/rcman.env" in app
    assert "NoNewPrivileges=yes\n" in app
    # rcman reaches its own user manager's Podman socket
    assert "Environment=XDG_RUNTIME_DIR=/run/user/990\n" in app
    wants = next(line for line in app.splitlines() if line.startswith("Wants="))
    assert "user@990.service" in wants.split("=", 1)[1].split()


def test_caddy_snippet_strips_the_identity_header_before_authenticating():
    snippet = ins.render("Caddyfile", SETTINGS)
    assert snippet.startswith("rcman.example.com {")
    strip = snippet.index("request_header -X-Forwarded-Email")
    auth = snippet.index("forward_auth 127.0.0.1:444")
    assert strip < auth and "route {" in snippet[:strip]
    assert "copy_headers X-Auth-Request-Email>X-Forwarded-Email" in snippet
    assert "reverse_proxy unix//run/rcman/rcman.sock\n" in snippet
    assert snippet.count("reverse_proxy") == 2  # oauth2-proxy's pages, and rcman


def test_caddy_snippet_logs_each_request_to_a_file_it_rotates_and_expires():
    snippet = ins.render("Caddyfile", SETTINGS)
    block = snippet[snippet.index("\tlog {") : snippet.index("\thandle /oauth2/*")]
    assert "output file /var/log/caddy/rcman-access.log {" in block
    # Caddy deletes a rolled file past either limit; both are set, not left to defaults.
    for option in ("roll_size", "roll_keep", "roll_keep_for"):
        assert f"\t\t\t{option} " in block
    assert "log_skip" not in snippet  # every request, the polls among them


def test_the_socket_directory_is_made_at_boot_for_rcman_and_caddys_group():
    assert ins.tmpfiles() == "d /run/rcman 0750 rcman rcman-web -\n"
    assert ins.SOCKET.parent == ins.RUN == Path("/run/rcman")
    assert ins.TMPFILES == Path("/etc/tmpfiles.d/rcman.conf")


def test_the_site_block_notice_appears_exactly_when_the_snippet_changed():
    changed = ins.caddy_notices([str(ins.CADDY_SNIPPET), "/etc/rcman/rcman.env"], "caddy")
    assert len(changed) == 1
    notice = changed[0]
    assert notice.startswith("The Caddy site block changed") and "restart Caddy" in notice
    # It fits a Caddyfile that imports the snippet as well as one that holds a copy.
    assert str(ins.CADDY_SNIPPET) in notice and "import" in notice and "copy" in notice
    assert "replace yours" not in notice
    assert ins.caddy_notices(["/etc/rcman/rcman.env"], "caddy") == []
    assert ins.caddy_notices([], "caddy") == []


def test_caddy_is_told_to_restart_when_it_joins_the_socket_group():
    notices = ins.caddy_notices([f"caddy in group {ins.WEB_GROUP}"], "caddy")
    assert len(notices) == 1 and "restart Caddy" in notices[0]
    assert ins.WEB_GROUP == "rcman-web"


# What localhost, the host of Caddy's default admin API address, is bound to.
CADDY_LOCALHOST = "127.0.0.1"


@contextlib.contextmanager
def reserved_port():
    """A port bound on Caddy's admin host without listening: a connection to it is
    refused, and while it is held no other process, a parallel test included, can take
    it. Yields the port and its socket."""
    with socket.socket(socket.AF_INET) as held:
        held.bind((CADDY_LOCALHOST, 0))
        yield held.getsockname()[1], held


def bind(host, port):
    with socket.socket(socket.AF_INET) as other:
        other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        other.bind((host, port))


def test_a_reserved_port_cannot_be_taken_and_refuses_connections():
    host = CADDY_LOCALHOST
    bind(host, 0)  # positive control: the same bind succeeds on a port not held
    with reserved_port() as (port, _):
        with pytest.raises(OSError):
            bind(host, port)
        with pytest.raises(ConnectionRefusedError):
            socket.create_connection((host, port), timeout=2).close()


def test_a_caddy_admin_api_on_loopback_is_noticed():
    with reserved_port() as (port, held):
        assert ins.caddy_admin_exposed(port=port) is False
        held.listen()
        assert ins.caddy_admin_exposed(port=port) is True  # positive control
    assert ins.CADDY_ADMIN_PORT == 2019  # Caddy's default
    assert "admin off" in ins.CADDY_ADMIN_WARNING
    assert f"{CADDY_LOCALHOST}:{ins.CADDY_ADMIN_PORT}" in ins.CADDY_ADMIN_WARNING


def test_every_template_is_used():
    templates = {p.name for p in (REPO / "installer" / "templates").iterdir()}
    assert templates == set(ins.TEMPLATES)


# --- releases --------------------------------------------------------------------------


def make_release(root, name):
    """A complete release."""
    path = root / "releases" / name
    (path / "backend" / "rcman").mkdir(parents=True)
    (path / ins.COMPLETE).write_text(name)
    return path


def test_switching_keeps_the_previous_release_and_prunes_the_rest(tmp_path):
    for name in ("a", "b", "c"):
        make_release(tmp_path, name)
    ins.switch(tmp_path, "a")
    ins.switch(tmp_path, "b")
    ins.switch(tmp_path, "c")
    assert os.readlink(tmp_path / "current") == "releases/c"
    assert os.readlink(tmp_path / "previous") == "releases/b"
    assert sorted(p.name for p in (tmp_path / "releases").iterdir()) == ["b", "c"]


def test_switching_to_the_current_release_changes_nothing(tmp_path):
    for name in ("a", "b"):
        make_release(tmp_path, name)
    ins.switch(tmp_path, "a")
    ins.switch(tmp_path, "b")
    assert ins.switch(tmp_path, "b") is False
    assert os.readlink(tmp_path / "previous") == "releases/a"


def test_rollback_swaps_current_and_previous(tmp_path):
    for name in ("a", "b"):
        make_release(tmp_path, name)
    ins.switch(tmp_path, "a")
    ins.switch(tmp_path, "b")
    ins.rollback(tmp_path)
    assert os.readlink(tmp_path / "current") == "releases/a"
    assert os.readlink(tmp_path / "previous") == "releases/b"


def test_rollback_without_a_previous_release_is_refused(tmp_path):
    make_release(tmp_path, "a")
    ins.switch(tmp_path, "a")
    with pytest.raises(ins.InstallError, match="previous"):
        ins.rollback(tmp_path)


def test_each_command_put_on_the_path_is_a_script_of_the_project():
    import tomllib

    scripts = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]["scripts"]
    assert ins.COMMANDS  # positive control: there is something to check
    assert set(ins.COMMANDS) <= set(scripts)


def test_the_commands_are_linked_to_the_current_releases(tmp_path):
    bin_dir, current = tmp_path / "sbin", tmp_path / "opt" / "current"
    (current / ".venv" / "bin").mkdir(parents=True)
    bin_dir.mkdir()
    for name in ins.COMMANDS:
        (current / ".venv" / "bin" / name).write_text(name)
    changed = ins.link_commands(bin_dir, current)
    assert changed == [str(bin_dir / name) for name in ins.COMMANDS]
    for name in ins.COMMANDS:
        # through current, so a switch or a rollback needs no change to it
        assert os.readlink(bin_dir / name) == str(current / ".venv" / "bin" / name)
        assert (bin_dir / name).read_text() == name
    assert ins.link_commands(bin_dir, current) == []


def test_a_command_linked_elsewhere_is_linked_again(tmp_path):
    bin_dir, current = tmp_path / "sbin", tmp_path / "current"
    bin_dir.mkdir()
    name = ins.COMMANDS[0]
    os.symlink(tmp_path / "elsewhere", bin_dir / name)
    assert str(bin_dir / name) in ins.link_commands(bin_dir, current)
    assert os.readlink(bin_dir / name) == str(current / ".venv" / "bin" / name)


def test_a_file_where_a_command_goes_is_refused_and_left_alone(tmp_path):
    bin_dir, current = tmp_path / "sbin", tmp_path / "current"
    bin_dir.mkdir()
    name = ins.COMMANDS[0]
    (bin_dir / name).write_text("someone else's")
    with pytest.raises(ins.InstallError, match=name):
        ins.link_commands(bin_dir, current)
    assert not (bin_dir / name).is_symlink()
    assert (bin_dir / name).read_text() == "someone else's"


def test_an_incomplete_release_is_never_switched_to(tmp_path):
    (tmp_path / "releases" / "half").mkdir(parents=True)
    with pytest.raises(ins.InstallError, match="incomplete"):
        ins.switch(tmp_path, "half")


def test_write_if_changed_reports_changes_and_sets_mode(tmp_path):
    path = tmp_path / "f"
    assert ins.write_if_changed(path, "x", 0o640) is True
    assert ins.write_if_changed(path, "x", 0o640) is False
    assert ins.write_if_changed(path, "y", 0o640) is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_install_sh_hands_over_to_the_python_installer():
    text = (REPO / "install.sh").read_text()
    assert text.startswith("#!/bin/bash\n")
    assert "installer/install.py" in text
    assert os.access(REPO / "install.sh", os.X_OK)


# --- the installation guide ------------------------------------------------------------


def test_the_guide_documents_every_option_and_setting_and_nothing_else():
    import re

    guide = (REPO / "docs" / "installation.md").read_text(encoding="utf-8")
    options = {f"--{key.lower().replace('_', '-')}" for key in ins.FIELDS} | {"--rollback"}
    assert set(re.findall(r"`(--[a-z-]+)", guide)) == options
    variables = {f"RCMAN_INSTALL_{key}" for key in ins.FIELDS}
    assert set(re.findall(r"`(RCMAN_INSTALL_[A-Z_]+)`", guide)) == variables


def test_the_guide_names_the_paths_the_installer_uses():
    guide = (REPO / "docs" / "installation.md").read_text(encoding="utf-8")
    for path in (
        ins.ROOT,
        ins.ETC / "install.env",
        ins.CADDY_SNIPPET,
        ins.STATE,
        ins.TOOLS,
        ins.CLAUDE_STORE,
        ins.SOCKET,
        ins.TMPFILES,
        *(ins.COMMAND_DIR / name for name in ins.COMMANDS),
    ):
        assert str(path) in guide, path
    assert "/oauth2/callback" in guide
    assert f"`{ins.WEB_GROUP}`" in guide and "admin off" in guide


# --- removing rcman: the guide lists everything the installer makes ------------------------

REMOVING = "## Removing rcman"
# Path constants of the installer that it reads and does not make: constant -> why not.
NOT_MADE = {
    "REPO": "the clone, which stays",
    "TEMPLATES_DIR": "in the clone",
    "CONTAINERFILE": "in the clone",
    "BUILDER_DIR": "in the clone",
    "TERMINFO_DIRS": "the system's terminal descriptions, copied from",
    "BUSYBOX": "the system's, copied from",
    "CGROUP_ROOT": "the kernel's, read",
    "LINGER": "systemd's folder; rcman's entry in it is LINGER / APP_USER, listed below",
    "SYSTEMD": "systemd's folder; the files made in it are the units and the drop-in",
    "COMMAND_DIR": "the system's folder; the files made in it are COMMANDS",
    "DATABASE": "rcman's, in its home; the installer asks rcman for its schema version",
}


def removal_section(guide: str) -> str:
    """The guide's section on removing rcman, up to the next section."""
    start = guide.index(REMOVING)
    end = guide.find("\n## ", start + 1)
    return guide[start : end if end != -1 else len(guide)]


def removal_needs(namespace: dict) -> dict[str, str]:
    """What the section must name, label -> text, from the installer's own constants: every
    path (but NOT_MADE's), the accounts and groups, the unit files, the commands it links,
    rcman's linger entry and the packages. A constant added later is needed at once."""
    needs = {}
    for name, value in namespace.items():
        if not name.isupper() or name in NOT_MADE:
            continue
        paths = value if isinstance(value, tuple) else (value,)
        for path in (p for p in paths if isinstance(p, Path)):
            needs[f"{name} {path}"] = str(path).replace("{uid}", "<uid>")
        if name.endswith(("_USER", "_GROUP")) and isinstance(value, str):
            needs[name] = value
    for unit in namespace["UNITS"]:
        needs[f"unit {unit}"] = str(namespace["SYSTEMD"] / unit)
    for command in namespace["COMMANDS"]:
        needs[f"command {command}"] = str(namespace["COMMAND_DIR"] / command)
    needs["linger"] = str(namespace["LINGER"] / namespace["APP_USER"])
    for package in namespace["PACKAGES"]:
        needs[f"package {package}"] = package
    return needs


def missing_from_removal(guide: str, namespace: dict) -> list[str]:
    section = removal_section(guide)
    return [label for label, text in removal_needs(namespace).items() if text not in section]


def test_removing_rcman_names_everything_the_installer_makes():
    guide = (REPO / "docs" / "installation.md").read_text(encoding="utf-8")
    needs = removal_needs(vars(ins))
    # Known positives: an empty derivation, or an empty section, would pass.
    assert {"APP_USER", "PROXY_USER", "WEB_GROUP", "linger"} <= set(needs)
    assert needs["ROOT /opt/rcman"] == "/opt/rcman"
    assert any(label.startswith("unit ") for label in needs)
    assert len(removal_section(guide)) > len(REMOVING)
    assert missing_from_removal(guide, vars(ins)) == []


def test_the_constants_that_are_not_made_are_still_constants_of_the_installer():
    for name, reason in NOT_MADE.items():
        assert name in vars(ins) and reason, name


def test_the_removal_guard_fails_on_a_name_taken_out_of_the_section():
    guide = (REPO / "docs" / "installation.md").read_text(encoding="utf-8")
    for text in ("/etc/tmpfiles.d/rcman.conf", ins.APP_USER, ins.UNITS[0], ins.PACKAGES[0]):
        assert text in removal_section(guide)
        body = guide.index(REMOVING) + len(REMOVING)  # the heading itself stays
        planted = guide[:body] + guide[body:].replace(text, "x")
        assert planted != guide  # the plant took effect
        assert missing_from_removal(planted, vars(ins)) != [], text


def test_the_removal_guard_fails_on_a_name_only_outside_the_section():
    guide = (REPO / "docs" / "installation.md").read_text(encoding="utf-8")
    planted = guide.replace(REMOVING, "## Elsewhere")  # the section's name taken away
    assert planted != guide
    with pytest.raises(ValueError):
        missing_from_removal(planted, vars(ins))
    only_elsewhere = guide.replace(REMOVING, REMOVING + "\n\nNothing here.\n\n## Elsewhere")
    assert missing_from_removal(only_elsewhere, vars(ins)) != []


@pytest.mark.parametrize(
    "name,value",
    [
        ("NEW_STATE", Path("/var/lib/planted-thing")),
        ("NEW_FILES", (Path("/etc/planted-one"), Path("/etc/planted-two"))),
        ("NEW_USER", "planted-account"),
        ("NEW_GROUP", "planted-group"),
    ],
)
def test_a_new_path_account_or_group_constant_must_be_in_the_section(name, value):
    guide = (REPO / "docs" / "installation.md").read_text(encoding="utf-8")
    namespace = {**vars(ins), name: value}
    assert namespace != vars(ins)  # the plant took effect
    assert missing_from_removal(guide, namespace) != []


def test_a_path_the_installer_only_reads_is_named_in_not_made_with_a_reason(monkeypatch):
    guide = (REPO / "docs" / "installation.md").read_text(encoding="utf-8")
    namespace = {**vars(ins), "NEW_READ": Path("/usr/share/planted")}
    assert missing_from_removal(guide, namespace) != []
    monkeypatch.setitem(NOT_MADE, "NEW_READ", "planted: read, not made")
    assert missing_from_removal(guide, namespace) == []


def test_a_new_unit_command_or_package_must_be_in_the_section():
    guide = (REPO / "docs" / "installation.md").read_text(encoding="utf-8")
    for name, value in (
        ("UNITS", (*ins.UNITS, "planted.service")),
        ("COMMANDS", (*ins.COMMANDS, "planted-command")),
        ("PACKAGES", (*ins.PACKAGES, "planted-package")),
    ):
        namespace = {**vars(ins), name: value}
        assert missing_from_removal(guide, namespace) != [], name


@pytest.mark.parametrize("name", ["rcman.example.com", "a-b.c-d.org", "x.io"])
def test_host_names_accepted(name):
    assert ins.HOSTNAME.fullmatch(name)


@pytest.mark.parametrize(
    "name", ["localhost", "-a.com", "a-.com", "a..com", "a_b.com", "a.com/", "https://a.com"]
)
def test_host_names_refused(name):
    assert not ins.HOSTNAME.fullmatch(name)


def test_run_returns_bytes_when_asked_and_reports_failures():
    assert ins.run(["printf", "\\xff"], text=False).stdout == b"\xff"
    with pytest.raises(ins.InstallError, match="boom"):
        ins.run(["sh", "-c", "printf boom >&2; exit 3"], text=False)


def test_unpacked_tools_are_usable_by_other_users(tmp_path):
    import io
    import tarfile

    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w:gz") as tar:
        info = tarfile.TarInfo("pkg-1/tool")
        info.mode = 0o755
        info.size = 2
        tar.addfile(info, io.BytesIO(b"ok"))
    dest = tmp_path / "pkg-1"
    ins._unpack(data.getvalue(), dest, "r:gz", strip=1)
    assert stat.S_IMODE(dest.stat().st_mode) == 0o755  # not mkdtemp's 0700
    assert (dest / "tool").read_bytes() == b"ok"


def test_toolchain_directories_are_repaired_to_be_readable(tmp_path):
    for sub in ("a", "a/b"):
        (tmp_path / sub).mkdir()
        os.chmod(tmp_path / sub, 0o700)
    ins.open_up(tmp_path)
    assert stat.S_IMODE((tmp_path / "a" / "b").stat().st_mode) == 0o755


def fake_downloads(monkeypatch):
    """The toolchain's downloads served from archives made here, each with its checksum
    file; returns the URLs fetched. The first archive fetched waits briefly for another
    caller to start fetching, so that two unlocked callers overlap."""
    import hashlib
    import io
    import tarfile
    import threading
    import time

    def archive(top: str, member: str, mode: str) -> bytes:
        data = io.BytesIO()
        with tarfile.open(fileobj=data, mode=mode) as tar:
            info = tarfile.TarInfo(f"{top}/{member}")
            info.mode, info.size = 0o755, 2
            tar.addfile(info, io.BytesIO(b"ok"))
        return data.getvalue()

    arch = ins._arch()
    node = f"node-v{ins.node_version()}-linux-{arch}"
    triple = {"x64": "x86_64", "arm64": "aarch64"}[arch] + "-unknown-linux-gnu"
    proxy = f"oauth2-proxy-{ins.OAUTH2_PROXY_VERSION}.linux-" + {"x64": "amd64"}.get(arch, arch)
    archives = {
        f"{node}.tar.xz": archive(node, "bin/node", "w:xz"),
        f"uv-{triple}.tar.gz": archive(f"uv-{triple}", "uv", "w:gz"),
        f"{proxy}.tar.gz": archive(proxy, "oauth2-proxy", "w:gz"),
    }
    sums = {name: hashlib.sha256(data).hexdigest() for name, data in archives.items()}
    # uv and oauth2-proxy are checked against the pins of the repository, not a list served
    # with them
    monkeypatch.setattr(ins, "NODE_SHA256", {(ins.node_version(), arch): sums[f"{node}.tar.xz"]})
    monkeypatch.setattr(ins, "UV_SHA256", {(ins.UV_VERSION, arch): sums[f"uv-{triple}.tar.gz"]})
    monkeypatch.setattr(
        ins, "OAUTH2_PROXY_SHA256",
        {(ins.OAUTH2_PROXY_VERSION, arch): sums[f"{proxy}.tar.gz"]},
    )  # fmt: skip
    fetched, started = [], threading.Event()
    tmux = b"tmux source"
    monkeypatch.setattr(ins, "TMUX_SHA256", hashlib.sha256(tmux).hexdigest())

    def download(url: str) -> bytes:
        name = url.rsplit("/", 1)[1]
        fetched.append(url)
        if name == f"{node}.tar.xz":
            if started.is_set():
                started.clear()
            else:
                started.set()
                deadline = time.monotonic() + 1
                while started.is_set() and time.monotonic() < deadline:
                    time.sleep(0.01)
            return archives[name]
        if name == f"tmux-{ins.TMUX_VERSION}.tar.gz":
            return tmux
        return archives[name]

    monkeypatch.setattr(ins, "_download", download)
    return fetched


# What an empty toolchain is filled with: Node.js, uv, oauth2-proxy and tmux's source.
MADE = 4


def downloaded(fetched: list[str]) -> list[str]:
    """The files fetched, by name: Node.js's list of checksums and archive, uv's archive,
    oauth2-proxy's and tmux's source, each once and no list of checksums for the pinned ones."""
    names = sorted(url.rsplit("/", 1)[1] for url in fetched)
    assert len(names) == len(set(names)), names
    assert not [n for n in names if n.endswith((".sha256", "-sha256sum.txt"))], names
    return names


def test_the_toolchain_is_fetched_once_and_complete(tmp_path, monkeypatch):
    """Positive control for the fake downloads: one call fills an empty toolchain."""
    fetched = fake_downloads(monkeypatch)
    toolchain = tmp_path / "toolchain"
    assert len(ins.ensure_toolchain(toolchain)) == MADE
    for binary in (ins.node_bin(toolchain) / "node", ins.uv_binary(toolchain)):
        assert binary.read_bytes() == b"ok"
    assert ins.oauth2_proxy_binary(toolchain).read_bytes() == b"ok"
    assert len(downloaded(fetched)) == MADE
    assert ins.ensure_toolchain(toolchain) == []  # and finds it there the next time


def test_two_callers_at_once_make_one_complete_toolchain(tmp_path, monkeypatch):
    """Two callers filling the same empty toolchain at once, as two parallel test runs
    sharing a cache do: one fetches and unpacks each tool, the other then finds it."""
    import threading

    fetched = fake_downloads(monkeypatch)
    toolchain = tmp_path / "toolchain"
    results, errors = [], []

    def call():
        try:
            results.append(ins.ensure_toolchain(toolchain))
        except Exception as error:  # noqa: BLE001 - reported below
            errors.append(error)

    threads = [threading.Thread(target=call) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert errors == []
    assert sorted(len(r) for r in results) == [0, MADE]
    downloaded(fetched)  # each file once
    assert ins.oauth2_proxy_binary(toolchain).read_bytes() == b"ok"
    assert [p.name for p in toolchain.iterdir() if p.name.startswith(".") and p.is_dir()] == []


def test_the_installer_runs_python_in_utf8_mode():
    script = (REPO / "install.sh").read_text(encoding="utf-8")
    runs = [line for line in script.splitlines() if "/usr/bin/python3" in line]
    assert runs  # non-vacuity
    for line in runs:
        assert "/usr/bin/python3 -I -X utf8 " in line, line


# --- rootless Podman for rcman -----------------------------------------------------------


def test_rcman_gets_a_subordinate_range_for_every_persons_slot():
    from rcman.people import MAX_PEOPLE
    from rcman.runtime import RANGE

    assert ins.SUBORDINATE_COUNT >= (MAX_PEOPLE + 1) * RANGE


def test_the_subordinate_range_reaches_the_builders_slot():
    from rcman import people
    from rcman.runtime import RANGE

    assert people.BUILDER_SLOT > people.MAX_PEOPLE  # a slot no person has
    assert ins.SUBORDINATE_COUNT >= (people.BUILDER_SLOT + 1) * RANGE
    assert ins.RANGE == RANGE


@pytest.mark.parametrize(
    "lines,start",
    [
        ([], ins.SUBORDINATE_FIRST),
        (["dev:100000:65536"], 165536),
        (["dev:100000:65536", "other:9000000:65536"], 9065536),
        (["dev:100000:65536", "rcman:5000000:10"], 5000010),  # one too small: another after
        (["# a comment", "", "dev:100000:65536"], 165536),
        (["dev:1000:10"], ins.SUBORDINATE_FIRST),  # never below the first useradd leaves
    ],
)
def test_a_new_subordinate_range_starts_after_every_range_there(lines, start):
    assert ins.subordinate_start(lines, "rcman", ins.SUBORDINATE_COUNT) == start


def test_a_machine_with_the_old_range_gets_a_second_full_one():
    old = ins.SUBORDINATE_COUNT - ins.RANGE  # what an install before the builder's slot had
    lines = ["dev:100000:65536", f"rcman:165536:{old}"]
    assert ins.subordinate_start(lines, "rcman", ins.SUBORDINATE_COUNT) == 165536 + old


def test_a_range_big_enough_is_kept():
    lines = ["dev:100000:65536", f"rcman:900000:{ins.SUBORDINATE_COUNT}"]
    assert ins.subordinate_start(lines, "rcman", ins.SUBORDINATE_COUNT) is None


def test_the_packages_are_those_podman_and_the_tools_need():
    assert {"podman", "uidmap", "catatonit", "slirp4netns", "busybox-static"} <= set(ins.PACKAGES)


def test_crun_is_installed_and_is_the_runtime_podman_reports(monkeypatch):
    """The runtime the workspaces and the builder are tested with is crun: a host with only
    another one would run them differently, so the installer asks Podman which it uses."""
    assert "crun" in ins.PACKAGES
    assert ins.OCI_RUNTIME == "crun"
    calls = []

    def reports(name):
        def run(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, f"{name}\n", "")

        return run

    monkeypatch.setattr(ins, "app_uid", lambda: 990)
    monkeypatch.setattr(ins, "run", reports("crun"))
    assert ins.ensure_oci_runtime() == []
    [argv] = calls
    assert argv[:4] == RUNUSER  # asked as rcman, through the helper that runs Podman as rcman
    assert argv[argv.index("podman") :][-3:] == ["info", "--format", "{{.Host.OCIRuntime.Name}}"]
    for other in ("runc", "", "crun-vm"):
        monkeypatch.setattr(ins, "run", reports(other))
        with pytest.raises(ins.InstallError, match="crun") as refused:
            ins.ensure_oci_runtime()
        assert repr(other) in str(refused.value) or other == ""


def test_the_runtime_is_checked_once_rcmans_podman_runs_and_before_anything_is_built():
    import inspect

    source = inspect.getsource(ins.main)
    order = [
        source.index(f"{name}(")
        for name in ("ensure_podman", "ensure_oci_runtime", "ensure_toolchain", "ensure_tools")
    ]
    assert order == sorted(order) and len(set(order)) == len(order)


def elf(interpreter: bool) -> bytes:
    """The head of a 64-bit ELF program: its header and its program headers, a loaded
    segment and, with ``interpreter``, the dynamic loader's."""
    import struct

    types = [1, 3] if interpreter else [1]  # PT_LOAD, PT_INTERP
    header = b"\x7fELF" + bytes([2, 1, 1]) + bytes(9)
    header += struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0, 64, 0, 0, 64, 56, len(types), 0, 0, 0)
    return header + b"".join(struct.pack("<II", t, 5) + bytes(48) for t in types)


def test_a_static_program_is_told_from_a_dynamic_one(tmp_path):
    import shutil

    assert not ins.is_static(Path(shutil.which("ls")))  # positive control: a dynamic one
    static = tmp_path / "static"
    static.write_bytes(elf(interpreter=False))
    dynamic = tmp_path / "dynamic"
    dynamic.write_bytes(elf(interpreter=True))
    assert not ins.is_static(dynamic)
    assert ins.is_static(static)
    text = tmp_path / "script"
    text.write_text("#!/bin/sh\n")
    assert not ins.is_static(text)


def test_tmux_is_built_from_the_pinned_tarball():
    assert ins.TMUX_VERSION and len(ins.TMUX_SHA256) == 64
    assert ins.TMUX_URL.endswith(f"/{ins.TMUX_VERSION}/tmux-{ins.TMUX_VERSION}.tar.gz")
    assert ins.tmux_tarball(Path("/x")).name == f"tmux-{ins.TMUX_VERSION}.tar.gz"
    argv = ins.tmux_build_argv(Path("/t/tmux.tar.gz"))
    assert argv[:2] == ["run", "--rm"]
    assert ins.TMUX_BUILD_REFERENCE in argv
    assert "--volume=/t/tmux.tar.gz:/tmux.tar.gz:ro" in argv
    assert any("--enable-static" in a for a in argv)


def test_tmux_is_built_in_an_image_pinned_by_tag_and_digest():
    assert ins.TMUX_BUILD_REFERENCE == f"{ins.TMUX_BUILD_IMAGE}@{ins.TMUX_BUILD_DIGEST}"
    assert re.fullmatch(r"docker\.io/library/alpine:[\w.-]+", ins.TMUX_BUILD_IMAGE)
    assert DIGEST.fullmatch(ins.TMUX_BUILD_DIGEST)


def test_the_tmux_build_hands_the_program_over_on_standard_output_only():
    """No folder of rcman's is mounted for the program to be written in: it comes out of the
    container on standard output, and what the build says goes to standard error."""
    build = ins.TMUX_BUILD
    assert not any("/out" in a for a in ins.tmux_build_argv(Path("/t/tmux.tar.gz")))
    assert ">&2" in build and ">/dev/null" not in build
    assert build.index("sha256sum") < build.index("cat /tmp")  # the checksum is said first
    assert build.rstrip().rsplit("&&", 1)[1].strip().startswith("cat ")  # the last step


def test_the_tmux_build_container_is_not_logged_so_its_bytes_stay_out_of_the_journal():
    """The program leaves the container on standard output. A log driver of the journal would
    write those bytes, which are not UTF-8, into the system journal as lines, and make every
    reader of the journal that decodes it as text fail."""
    argv = ins.tmux_build_argv(Path("/t/tmux.tar.gz"))
    assert "--log-driver=none" in argv
    assert argv.index("--log-driver=none") < argv.index(ins.TMUX_BUILD_REFERENCE)


class ToolsHost:
    """What ensure_tools needs of the machine: a root-owned tools folder, a busybox and
    terminal descriptions of the test's, and rcman's Podman, which builds tmux by handing its
    program over on standard output."""

    def __init__(self, tmp_path, monkeypatch, built=None, reported="its own"):
        self.tools = tmp_path / "opt" / "tools"
        self.state = tmp_path / "state"
        self.calls: list[list[str]] = []
        self.built = elf(interpreter=False) if built is None else built
        # what the container says of the program it wrote: the build's noise and a line with
        # the program's sha256 (``reported``: its own, another's, or no line at all)
        digest = hashlib.sha256(self.built).hexdigest() if reported == "its own" else reported
        self.stderr = b"compiling...\n" + (f"sha256 {digest}\n".encode() if digest else b"")
        busybox = tmp_path / "busybox"
        busybox.write_bytes(elf(interpreter=False) + b"busybox")
        terminfo = tmp_path / "terminfo"
        for entry in ins.TERMINFO:
            (terminfo / entry).parent.mkdir(parents=True, exist_ok=True)
            (terminfo / entry).write_bytes(entry.encode())
        self.conf = tmp_path / "tmux.conf"
        self.conf.write_text("set -g mouse on\n")
        tarball = tmp_path / "toolchain" / "tmux.tar.gz"
        tarball.parent.mkdir()
        tarball.write_bytes(b"source")
        monkeypatch.setattr(ins, "STATE", self.state)
        monkeypatch.setattr(ins, "BUSYBOX", busybox)
        monkeypatch.setattr(ins, "TERMINFO_DIRS", (terminfo,))
        monkeypatch.setattr(ins, "tmux_tarball", lambda toolchain=None: tarball)
        monkeypatch.setattr(ins, "app_uid", lambda: 990)
        monkeypatch.setattr(ins, "run", self.run)

    def run(self, argv, user=None, **kw):
        self.calls.append(argv)
        assert "podman" in argv, argv  # the only program ensure_tools runs
        assert kw.get("text") is False  # a program is bytes
        return subprocess.CompletedProcess(argv, 0, self.built, self.stderr)


def test_the_tools_are_built_into_a_folder_of_roots_and_nothing_is_made_in_rcmans_home(
    tmp_path, monkeypatch
):
    host = ToolsHost(tmp_path, monkeypatch)
    changes = ins.ensure_tools(host.tools, conf=host.conf)
    assert f"tmux {ins.TMUX_VERSION}" in changes and "busybox" in changes and "tmux.conf" in changes
    assert (host.tools / "bin" / "tmux").read_bytes() == host.built
    assert os.readlink(host.tools / "bin" / "sh") == "busybox"
    assert (host.tools / "tmux.conf").read_text() == host.conf.read_text()
    for entry in ins.TERMINFO:
        assert (host.tools / "terminfo" / entry).read_bytes() == entry.encode()
    for path in (host.tools, host.tools / "bin", host.tools / "terminfo"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert stat.S_IMODE((host.tools / "bin" / "tmux").stat().st_mode) == 0o755
    [build] = host.calls
    assert build[:4] == RUNUSER  # tmux is built by rcman's Podman, as every container is
    assert not host.state.exists()  # no scratch folder in rcman's home, the build is not run there
    assert not [p for p in host.tools.rglob(".*") if p.name != ".tmux-version"]


def test_a_tmux_that_arrived_short_is_refused_by_the_checksum_the_container_reports(
    tmp_path, monkeypatch
):
    """The header of a program that was cut short on its way over the attach stream still
    says it is static; the container's own checksum of what it wrote says it is not whole."""
    short = elf(interpreter=False)
    whole = short + b"the rest of the program"
    cases = [hashlib.sha256(whole).hexdigest(), "0" * 64, ""]  # cut short, wrong, none said
    for number, reported in enumerate(cases):
        folder = tmp_path / f"case{number}"
        folder.mkdir()
        host = ToolsHost(folder, monkeypatch, built=short, reported=reported)
        assert ins.is_static(_written(folder, short))  # positive control: it looks fine
        with pytest.raises(ins.InstallError, match="sha256"):
            ins.ensure_tools(host.tools, conf=host.conf)
        assert not (host.tools / "bin" / "tmux").exists()
        assert not [p for p in host.tools.rglob(".*")]
    folder = tmp_path / "ok"
    folder.mkdir()
    ok = ToolsHost(folder, monkeypatch)
    assert f"tmux {ins.TMUX_VERSION}" in ins.ensure_tools(ok.tools, conf=ok.conf)


def _written(folder: Path, data: bytes) -> Path:
    path = folder / "written-program"
    path.write_bytes(data)
    return path


def test_the_tools_are_not_built_again_and_a_second_run_changes_nothing(tmp_path, monkeypatch):
    host = ToolsHost(tmp_path, monkeypatch)
    ins.ensure_tools(host.tools, conf=host.conf)
    host.calls.clear()
    assert ins.ensure_tools(host.tools, conf=host.conf) == []
    assert host.calls == []


def test_a_new_pin_builds_tmux_again(tmp_path, monkeypatch):
    host = ToolsHost(tmp_path, monkeypatch)
    ins.ensure_tools(host.tools, conf=host.conf)
    monkeypatch.setattr(ins, "TMUX_SHA256", "1" * 64)
    assert f"tmux {ins.TMUX_VERSION}" in ins.ensure_tools(host.tools, conf=host.conf)


@pytest.mark.parametrize("built", [elf(interpreter=True), b"#!/bin/sh\n", b""])
def test_a_tmux_that_is_not_static_is_refused_and_not_installed(tmp_path, monkeypatch, built):
    host = ToolsHost(tmp_path, monkeypatch, built=built)
    with pytest.raises(ins.InstallError, match="static"):
        ins.ensure_tools(host.tools, conf=host.conf)
    assert not (host.tools / "bin" / "tmux").exists()
    assert not [p for p in host.tools.rglob(".*")]  # and no partial file


def test_the_tools_folder_is_not_in_rcmans_home_where_rcman_can_replace_it():
    """Every workspace mounts the tools, and root makes them: they are out of rcman's reach."""
    assert ins.STATE not in ins.TOOLS.parents
    assert ins.ROOT in ins.TOOLS.parents
    env = ins.render("rcman.env", {**SETTINGS, "APP_UID": "990", "WORKSPACE_IMAGE": "x",
                                   "BUILDER_IMAGE": "y"})  # fmt: skip
    assert f"RCMAN_TOOLS_DIR={ins.TOOLS}\n" in env


def test_the_tmux_tarball_is_checked_against_its_pin(tmp_path, monkeypatch):
    fake_downloads(monkeypatch)
    toolchain = tmp_path / "toolchain"
    ins.ensure_toolchain(toolchain)
    assert ins.tmux_tarball(toolchain).read_bytes() == b"tmux source"
    monkeypatch.setattr(ins, "TMUX_SHA256", "0" * 64)
    other = tmp_path / "other"
    with pytest.raises(ins.InstallError, match="checksum"):
        ins.ensure_toolchain(other)
    assert not ins.tmux_tarball(other).exists()


def pin_problems(version: str, table: dict, architectures: set[str]) -> list[str]:
    """Why ``table`` (checksums by (version, architecture)) does not pin the archives of
    ``version`` for ``architectures``: an architecture without one, a checksum that is not a
    sha256, and an entry of another version, which a changed version left behind."""
    problems = [f"no pin for {version} on {a}" for a in sorted(architectures)
                if (version, a) not in table]  # fmt: skip
    for (pinned, arch), digest in table.items():
        if pinned != version:
            problems.append(f"pin for {pinned} on {arch} but the version is {version}")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            problems.append(f"pin for {pinned} on {arch} is not a sha256")
    return problems


def test_the_archives_of_uv_and_oauth2_proxy_are_pinned_with_their_versions():
    from helpers import claude_release

    architectures = set(claude_release.ARCHITECTURES.values())
    assert architectures == {"x64", "arm64"}  # non-vacuity: the two the installer downloads for
    assert pin_problems(ins.UV_VERSION, ins.UV_SHA256, architectures) == []
    assert pin_problems(ins.OAUTH2_PROXY_VERSION, ins.OAUTH2_PROXY_SHA256, architectures) == []


def test_a_version_changed_without_its_checksums_is_found():
    architectures = {"x64", "arm64"}
    good = {("1.0", "x64"): "a" * 64, ("1.0", "arm64"): "b" * 64}
    assert pin_problems("1.0", good, architectures) == []
    assert pin_problems("1.1", good, architectures) != []  # the version moved on, the pins did not
    assert pin_problems("1.0", {("1.0", "x64"): "a" * 64}, architectures) != []  # an arch missing
    assert pin_problems("1.1", {**good, ("1.1", "x64"): "a" * 64}, {"x64"}) != []  # a stale entry
    assert pin_problems("1.0", {**good, ("1.0", "x64"): "xyz"}, architectures) != []  # not a hash


def test_a_toolchain_download_that_is_not_the_pinned_one_is_refused(tmp_path, monkeypatch):
    """The release page cannot vouch for itself: a swapped archive, even with a list of
    checksums served to match, is not what the repository pinned."""
    fetch_log = fake_downloads(monkeypatch)
    arch = ins._arch()
    for table, version in (("NODE_SHA256", ins.node_version()),
                           ("UV_SHA256", ins.UV_VERSION),
                           ("OAUTH2_PROXY_SHA256", ins.OAUTH2_PROXY_VERSION)):  # fmt: skip
        toolchain = tmp_path / table
        with monkeypatch.context() as swapped:
            swapped.setitem(getattr(ins, table), (version, arch), "0" * 64)
            with pytest.raises(ins.InstallError, match="checksum"):
                ins.ensure_toolchain(toolchain)
        tool = {
            "NODE_SHA256": ins.node_bin(toolchain) / "node",
            "UV_SHA256": ins.uv_binary(toolchain),
            "OAUTH2_PROXY_SHA256": ins.oauth2_proxy_binary(toolchain),
        }[table]
        assert not tool.exists()
        assert not any(u.endswith(("sha256", "sha256sum.txt", "SHASUMS256.txt")) for u in fetch_log)


@pytest.mark.parametrize("table", ["NODE_SHA256", "UV_SHA256", "OAUTH2_PROXY_SHA256"])
def test_a_version_without_a_pin_for_this_machine_is_refused(tmp_path, monkeypatch, table):
    fake_downloads(monkeypatch)
    monkeypatch.setattr(ins, table, {})
    with pytest.raises(ins.InstallError, match="pinned"):
        ins.ensure_toolchain(tmp_path / "toolchain")


def test_the_node_version_the_frontend_names_has_its_archives_pinned():
    from helpers import claude_release

    architectures = set(claude_release.ARCHITECTURES.values())
    assert pin_problems(ins.node_version(), ins.NODE_SHA256, architectures) == []
    moved = {(f"{ins.node_version()}.1", a): d for (_, a), d in ins.NODE_SHA256.items()}
    assert pin_problems(ins.node_version(), moved, architectures) != []  # a version moved on


DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
# A base image the installer builds from: a registry name, a tag and a digest.
PINNED_IMAGE = re.compile(r"[a-z0-9.-]+(?::\d+)?(?:/[a-z0-9._-]+)+:[\w.-]+@sha256:[0-9a-f]{64}")


def test_the_default_image_is_ubuntu_pinned_by_tag_and_digest_with_what_rcman_needs():
    containerfile = ins.CONTAINERFILE.read_text()
    [base] = from_lines(ins.CONTAINERFILE)
    assert base.startswith("docker.io/library/ubuntu:24.04@")
    assert PINNED_IMAGE.fullmatch(base), base
    for package in ("git", "ca-certificates", "curl", "python3"):
        assert f" {package}" in containerfile, package
    image = ins.workspace_image()
    assert image.startswith("localhost/rcman-workspace:") and len(image.split(":")[1]) == 12


def containerfiles() -> list[Path]:
    """Every build file under installer/, found by walking it."""
    return sorted(
        p
        for p in (REPO / "installer").rglob("*")
        if p.is_file() and p.name.startswith("Containerfile")
    )


def unpinned_bases(files: list[Path]) -> list[str]:
    """The FROM lines of ``files`` that name a base without both a tag and a digest, or that
    name a stage of the same file (which is pinned where it is defined)."""
    found = []
    for path in files:
        stages = set()
        for line in path.read_text().splitlines():
            words = line.split()
            if not words or words[0].upper() != "FROM":
                continue
            image = next(w for w in words[1:] if not w.startswith("--"))
            if image not in stages and not PINNED_IMAGE.fullmatch(image):
                found.append(f"{path.name}: {image}")
            if "AS" in [w.upper() for w in words]:
                stages.add(words[-1])
    return found


def test_every_base_image_the_installer_builds_from_is_pinned_by_tag_and_digest():
    files = containerfiles()
    assert {p.parent.name for p in files} >= {"workspace", "builder"}  # non-vacuity: both found
    assert sum(len(from_lines(p)) for p in files) >= 3  # and the FROM lines in them
    assert unpinned_bases(files) == []


@pytest.mark.parametrize(
    "line",
    [
        "FROM docker.io/library/ubuntu:24.04",
        "FROM docker.io/library/ubuntu@sha256:" + "a" * 64,  # a digest and no tag
        "FROM docker.io/library/ubuntu:24.04@sha256:" + "a" * 63,  # a digest too short
        "FROM --platform=linux/amd64 docker.io/library/ubuntu:latest",
        "FROM ubuntu",
    ],
)
def test_a_base_image_without_a_tag_and_a_digest_is_found_unpinned(tmp_path, line):
    planted = tmp_path / "Containerfile"
    planted.write_text(f"{line}\nRUN true\n")
    assert unpinned_bases([planted]) != []
    planted.write_text("FROM docker.io/library/ubuntu:24.04@sha256:" + "a" * 64 + "\n")
    assert unpinned_bases([planted]) == []  # the same file, pinned: the guard can pass


def test_a_stage_named_by_a_later_from_is_not_taken_for_an_unpinned_base(tmp_path):
    planted = tmp_path / "Containerfile"
    pinned = "docker.io/library/node:24@sha256:" + "b" * 64
    planted.write_text(f"FROM {pinned} AS cli\nFROM cli\n")
    assert unpinned_bases([planted]) == []


def test_the_image_tag_follows_the_containerfile(tmp_path, monkeypatch):
    first = ins.workspace_image()
    changed = tmp_path / "Containerfile"
    changed.write_text(ins.CONTAINERFILE.read_text() + "RUN true\n")
    monkeypatch.setattr(ins, "CONTAINERFILE", changed)
    assert ins.workspace_image() != first


# --- the devcontainer builder image ---------------------------------------------------------

BUILDER_DIR = REPO / "installer" / "builder"


def from_lines(containerfile: Path) -> list[str]:
    return [
        line.split()[1]
        for line in containerfile.read_text().splitlines()
        if line.startswith("FROM ")
    ]


def test_the_builder_image_is_tagged_after_every_file_of_its_folder(tmp_path, monkeypatch):
    folder = tmp_path / "builder"
    shutil.copytree(BUILDER_DIR, folder)
    monkeypatch.setattr(ins, "BUILDER_DIR", folder)
    first = ins.devcontainer_builder_image()
    assert first.startswith("localhost/rcman-builder:") and len(first.split(":")[1]) == 12
    assert ins.devcontainer_builder_image() == first  # stable
    files = sorted(p.name for p in folder.iterdir())
    assert {"Containerfile", "containers.conf", "storage.conf"} <= set(files)  # non-vacuity
    tags = {first}
    for name in files:
        with (folder / name).open("a") as changed:
            changed.write("\n")
        tag = ins.devcontainer_builder_image()
        assert tag not in tags, name
        tags.add(tag)
    (folder / "another").write_text("a new file")  # a file added counts too
    assert ins.devcontainer_builder_image() not in tags


def test_the_builder_image_is_tagged_after_names_as_well_as_content(tmp_path, monkeypatch):
    folder = tmp_path / "builder"
    folder.mkdir()
    (folder / "a").write_text("x")
    monkeypatch.setattr(ins, "BUILDER_DIR", folder)
    first = ins.devcontainer_builder_image()
    (folder / "a").rename(folder / "b")
    assert ins.devcontainer_builder_image() != first


def test_the_builder_image_pins_its_node_and_the_cli_by_integrity():
    import json

    node, base = from_lines(BUILDER_DIR / "Containerfile")
    assert re.fullmatch(r"docker\.io/library/node:[\w.-]+@sha256:[0-9a-f]{64}", node), node
    default_base = from_lines(ins.CONTAINERFILE)[0]
    assert base == default_base  # the builder starts from what the default image does
    assert PINNED_IMAGE.fullmatch(base)
    lock = json.loads((BUILDER_DIR / "package-lock.json").read_text())
    package = json.loads((BUILDER_DIR / "package.json").read_text())
    wanted = package["dependencies"]["@devcontainers/cli"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", wanted)  # an exact version, no range
    entry = lock["packages"]["node_modules/@devcontainers/cli"]
    assert entry["version"] == wanted
    assert re.fullmatch(r"sha512-[A-Za-z0-9+/]+=*", entry["integrity"])
    assert "npm ci" in (BUILDER_DIR / "Containerfile").read_text()


def test_the_builder_image_has_what_building_in_podman_needs():
    text = (BUILDER_DIR / "Containerfile").read_text()
    for package in ("podman", "crun", "fuse-overlayfs", "slirp4netns", "tar"):
        assert f" {package}" in text, package
    assert "VOLUME" not in text  # a stray anonymous volume per builder
    assert "COPY --from=" in text  # Node and the CLI come from the first stage
    conf = (BUILDER_DIR / "containers.conf").read_text()
    assert 'userns="host"' in conf and 'runtime="crun"' in conf
    assert 'mount_program="/usr/bin/fuse-overlayfs"' in (BUILDER_DIR / "storage.conf").read_text()


class FakePodman:
    """rcman's Podman as the installer drives it: images by name, and every call made."""

    def __init__(self, images, undeletable=()):
        self.images, self.undeletable, self.calls = list(images), set(undeletable), []

    def argv(self, uid, *args):
        return ["podman", *args]

    def run(self, argv, **kw):
        self.calls.append(argv)
        args = argv[1:]
        code, out = 0, ""
        if args[:2] == ["image", "exists"]:
            code = 0 if args[2] in self.images else 1
        elif args[:2] == ["image", "ls"]:
            out = "".join(f"{name}\n" for name in self.images)
        elif args[0] == "rmi":
            if args[1] in self.undeletable:
                code = 1  # an image in use refuses
            else:
                self.images.remove(args[1])
        elif args[0] == "build":
            self.images.append(args[args.index("--tag") + 1])
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")

    def verbs(self):
        return [" ".join(c[1:3]) if c[1] == "image" else c[1] for c in self.calls]


def use_fake_podman(monkeypatch, fake):
    monkeypatch.setattr(ins, "app_uid", lambda: 990)
    monkeypatch.setattr(ins, "as_rcman_podman", fake.argv)
    monkeypatch.setattr(ins.subprocess, "run", fake.run)
    monkeypatch.setattr(ins, "run", fake.run)


def test_the_builder_image_is_built_with_rcmans_podman_and_no_older_one_is_removed_by_it(
    monkeypatch,
):
    image = ins.devcontainer_builder_image()
    old = "localhost/rcman-builder:aaaaaaaaaaaa"
    other = "localhost/rcman-workspace:bbbbbbbbbbbb"
    fake = FakePodman([old, other])
    use_fake_podman(monkeypatch, fake)
    assert ins.ensure_devcontainer_builder() == [f"image {image}"]
    build = next(c for c in fake.calls if c[1] == "build")
    assert build[build.index("--tag") + 1] == image
    assert Path(build[build.index("--file") + 1]).name == "Containerfile"
    # the running service is still configured with the old name, and builds in the meantime
    # need it: building removes nothing
    assert fake.images == [old, other, image]
    assert "rmi" not in fake.verbs()


def test_a_builder_image_that_is_there_is_not_built_again(monkeypatch):
    image = ins.devcontainer_builder_image()
    old = "localhost/rcman-builder:aaaaaaaaaaaa"
    fake = FakePodman([image, old])
    use_fake_podman(monkeypatch, fake)
    assert ins.ensure_devcontainer_builder() == []
    assert "build" not in fake.verbs() and "rmi" not in fake.verbs()


def test_older_builder_images_are_pruned_by_their_own_step_and_others_left_alone(monkeypatch):
    image = ins.devcontainer_builder_image()
    old = "localhost/rcman-builder:aaaaaaaaaaaa"
    other = "localhost/rcman-workspace:bbbbbbbbbbbb"
    fake = FakePodman([old, other, image])
    use_fake_podman(monkeypatch, fake)
    ins.prune_devcontainer_builder_images(990, image)
    assert fake.images == [other, image]  # the old builder gone, others left alone


def test_an_older_builder_image_in_use_does_not_fail_the_install(monkeypatch):
    image = ins.devcontainer_builder_image()
    busy, old = "localhost/rcman-builder:aaaaaaaaaaaa", "localhost/rcman-builder:bbbbbbbbbbbb"
    fake = FakePodman([busy, old, image], undeletable=[busy])
    use_fake_podman(monkeypatch, fake)
    ins.prune_devcontainer_builder_images(990, image)
    assert fake.images == [busy, image]  # the one in use stays, the other goes


def test_old_builder_images_are_removed_only_after_rcman_runs_with_the_new_name():
    """Between building the image and the restart the old service runs on, configured with
    the old name; removing that image then makes every devcontainer build fail."""
    import inspect

    source = inspect.getsource(ins.main)
    source = source[source.index("SUDO_USER") :]  # after the rollback, which restarts as well
    order = [
        source.index(f"{name}(")
        for name in (
            "ensure_devcontainer_builder",
            "configure",
            "start_services",
            "health",
            "prune_devcontainer_builder_images",
        )  # fmt: skip
    ]
    assert order == sorted(order) and len(set(order)) == len(order)
    assert "prune_devcontainer_builder_images(" not in inspect.getsource(
        ins.ensure_devcontainer_builder
    )


def test_the_installer_finds_out_both_images_and_rcmans_uid(monkeypatch):
    monkeypatch.setattr(ins, "app_uid", lambda: 990)
    found = ins.found_out()
    assert found == {
        "APP_UID": "990",
        "WORKSPACE_IMAGE": ins.workspace_image(),
        "BUILDER_IMAGE": ins.devcontainer_builder_image(),
    }
    env = ins.render("rcman.env", {**SETTINGS, **found})
    assert f"RCMAN_BUILDER_IMAGE={ins.devcontainer_builder_image()}\n" in env


@pytest.fixture
def releases(tmp_path):
    """A release site: ``put(version, kind)`` makes a release, and the address is served."""
    from tests import claude_download

    folder = tmp_path / "releases"
    folder.mkdir()
    with claude_download.served(folder) as url:

        def put(version, kind="good"):
            claude_download.release(folder, version, kind)

        yield url, put, folder


@pytest.fixture
def account(monkeypatch):
    """rcman's account on this machine, for the steps that write in its folders: a command run
    as rcman is run for real as the test's user, so that what it writes is there to read, and
    every command is recorded."""
    calls = []
    real = ins.run

    def run(argv, user=None, **kw):
        calls.append(argv)
        done = as_rcman_for_real(argv, lambda rest: real(rest, **kw))
        return done if done is not None else real(argv, user=user, **kw)

    monkeypatch.setattr(ins, "run", run)
    monkeypatch.setattr(ins, "app_uid", lambda: 990)
    return calls


def test_the_claude_store_holds_the_version_checked_against_its_release(
    tmp_path, releases, account
):
    url, put, folder = releases
    put("2.1.0")
    store = tmp_path / "claude"
    changed = ins.ensure_claude_store(store, url, "2.1.0")
    assert changed == ["claude 2.1.0"]
    expected = (folder / "2.1.0" / ins.claude_platform() / "claude").read_bytes()
    assert (store / "versions" / "2.1.0").read_bytes() == expected
    assert os.readlink(store / "current") == "versions/2.1.0"
    assert os.readlink(store / "bin" / "claude") == "../current"
    assert (store / "bin" / "claude").read_bytes() == expected
    assert stat.S_IMODE((store / "versions" / "2.1.0").stat().st_mode) == 0o755
    assert sorted(p.name for p in (store / "versions").iterdir()) == ["2.1.0"]  # no partial file
    (folder / "2.1.0" / "manifest.json").unlink()
    assert ins.ensure_claude_store(store, url, "2.1.0") == []  # nothing fetched again


def test_a_claude_that_does_not_match_its_release_is_refused(tmp_path, releases, account):
    url, put, _ = releases
    put("2.1.0", "failing")
    store = tmp_path / "claude"
    with pytest.raises(ins.InstallError, match="checksum"):
        ins.ensure_claude_store(store, url, "2.1.0")
    assert not (store / "versions" / "2.1.0").exists() and not (store / "current").exists()
    assert [p.name for p in (store / "versions").iterdir()] == []  # no partial file either


def test_a_release_the_site_does_not_have_or_has_no_build_of_is_refused_in_words(
    tmp_path, releases, account
):
    url, put, folder = releases
    put("2.1.0")
    with pytest.raises(ins.InstallError, match="2.0.0"):
        ins.ensure_claude_store(tmp_path / "a", url, "2.0.0")
    (folder / "2.1.0" / "manifest.json").write_text('{"platforms": {}}')
    with pytest.raises(ins.InstallError, match="no build"):
        ins.ensure_claude_store(tmp_path / "b", url, "2.1.0")


def test_the_store_keeps_the_version_current_names_and_only_adds_the_tested_one(
    tmp_path, releases, account
):
    """current is rcman's to switch once it runs (an approval in its database): the installer
    sets it only where there is none."""
    url, put, _ = releases
    put("2.0.0")
    put("2.1.0")
    store = tmp_path / "claude"
    ins.ensure_claude_store(store, url, "2.0.0")
    assert ins.ensure_claude_store(store, url, "2.1.0") == ["claude 2.1.0"]
    assert os.readlink(store / "current") == "versions/2.0.0"  # not reset to the tested one
    assert sorted(p.name for p in (store / "versions").iterdir()) == ["2.0.0", "2.1.0"]
    assert ins.ensure_claude_store(store, url, "2.1.0") == []
    assert os.readlink(store / "current") == "versions/2.0.0"


def test_the_store_gets_a_current_where_there_is_none_or_it_dangles(tmp_path, releases, account):
    url, put, _ = releases
    put("2.1.0")
    store = tmp_path / "claude"
    (store / "versions").mkdir(parents=True)
    (store / "current").symlink_to("versions/9.9.9")  # names a version that is not there
    ins.ensure_claude_store(store, url, "2.1.0")
    assert os.readlink(store / "current") == "versions/2.1.0"


def test_the_store_is_made_by_rcman_and_readable_by_the_workspaces(
    tmp_path, releases, account, monkeypatch
):
    """The store is rcman's to write at run time, so rcman makes all of it: root changes
    nothing in it by path, and every command that reaches into it runs as rcman."""
    url, put, _ = releases
    put("2.1.0")
    put("2.2.0")
    store = tmp_path / "claude"
    seen: list = []
    forbid_path_based_changes(monkeypatch, seen, store)
    ins.ensure_claude_store(store, url, "2.1.0")
    ins.ensure_claude_store(store, url, "2.2.0")
    assert seen == []
    everything = {store, *store.rglob("*")}
    assert len(everything) > 5  # non-vacuity
    for path in everything:
        assert path.lstat().st_uid == os.getuid(), path  # made by the account the test runs as
        if path.is_symlink():
            continue
        mode = stat.S_IMODE(path.stat().st_mode)
        assert mode & 0o055 == 0o055, path  # readable, and for a folder enterable, by anyone
        assert not mode & 0o022, path  # and writable by its owner alone
    reaching = [c for c in account if any(str(store) in a for a in c)]
    assert reaching  # non-vacuity
    assert all(c[:4] == RUNUSER for c in reaching), [c for c in reaching if c[:4] != RUNUSER]


def test_a_download_is_staged_where_rcman_cannot_write_and_installed_by_rcman(
    tmp_path, releases, account, monkeypatch
):
    """Root fetches and checks the program in a folder of its own, outside the store; rcman
    copies the checked file in. A store that rcman can replace by a link is never written by
    root."""
    url, put, _ = releases
    put("2.1.0")
    store = tmp_path / "claude"
    staged = []
    real = ins.claude_release.fetch

    def fetch(download, version, destination):
        staged.append(destination)
        real(download, version, destination)

    monkeypatch.setattr(ins.claude_release, "fetch", fetch)
    ins.ensure_claude_store(store, url, "2.1.0")
    [destination] = staged
    assert store not in destination.parents and destination.name == "2.1.0"
    assert not destination.exists()  # and the staging folder is gone
    installing = [c for c in account if c[:4] == RUNUSER and str(destination) in c]
    assert installing  # rcman is the one that copied it


def test_the_installer_downloads_with_the_code_rcman_does(monkeypatch, tmp_path, account):
    """One piece of code fetches releases; the installer has no downloader of its own."""
    from helpers import claude_release

    assert ins.claude_release is claude_release
    assert ins.CLAUDE_DOWNLOAD == claude_release.DOWNLOAD
    assert ins.DOWNLOAD_ADDRESS is claude_release.DOWNLOAD_ADDRESS
    seen = []

    def fetch(download, version, destination):
        seen.append((download, version, destination.name))
        destination.write_bytes(b"#!/bin/sh\n")

    monkeypatch.setattr(claude_release, "fetch", fetch)
    ins.ensure_claude_store(tmp_path / "claude", "https://downloads.example.com", "2.1.0")
    assert seen == [("https://downloads.example.com", "2.1.0", "2.1.0")]


def imports_urllib(source: str) -> bool:
    import ast

    for node in ast.walk(ast.parse(source)):
        names = (
            [a.name for a in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else []
        )
        if any(name == "urllib" or name.startswith("urllib.") for name in names):
            return True
    return False


def test_the_installer_has_no_downloader_of_its_own():
    assert not imports_urllib((REPO / "installer" / "install.py").read_text(encoding="utf-8"))
    for planted in ("import urllib.request\n", "from urllib import request\n"):
        assert imports_urllib(planted)  # the guard fires on a plant


def test_a_download_of_the_toolchain_is_checked_by_the_shared_code(releases):
    import hashlib

    url, _, folder = releases
    (folder / "file").write_bytes(b"content")
    assert ins._verified(f"{url}/file", hashlib.sha256(b"content").hexdigest()) == b"content"
    with pytest.raises(ins.InstallError, match="checksum mismatch"):
        ins._verified(f"{url}/file", "0" * 64)
    with pytest.raises(ins.InstallError):
        ins._download(f"{url}/nothing")
    assert ins._download(f"{url}/file") == b"content"


def test_the_store_holds_the_tested_version():
    assert ins.claude_version() == (REPO / "helpers" / "claude-version").read_text().strip()


def test_the_first_administrator_is_added_by_rcman_admin_as_rcman(monkeypatch):
    monkeypatch.setattr(ins, "app_uid", lambda: 990)
    argv = ins.admin_argv("Admin@Example.com")
    assert argv[:4] == RUNUSER
    # in rcman's own clean environment, not root's: no HOME, PATH or variable of root's
    assert argv[4:7] == ["/usr/bin/env", "-i", "PATH=/usr/bin:/bin"]
    assert f"HOME={ins.STATE}" in argv
    program = argv.index(str(ins.CURRENT / ".venv" / "bin" / "rcman-admin"))
    assert argv[program:] == [
        str(ins.CURRENT / ".venv" / "bin" / "rcman-admin"), "--env-file",
        str(ins.ETC / "rcman.env"), "add", "--admin", "--", "Admin@Example.com",
    ]  # fmt: skip


def test_rcman_admin_runs_in_a_neutral_folder_and_not_in_the_clone(monkeypatch):
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(ins, "app_uid", lambda: 990)
    monkeypatch.setattr(ins, "run", run)
    assert ins.ensure_admin({"ADMIN_EMAIL": "Admin@Example.com"}) == [
        "administrator admin@example.com"
    ]
    assert len(seen) == 2  # the list, then the addition
    for argv, kw in seen:
        assert kw.get("cwd") == "/", argv
        assert "-i" in argv and argv[:4] == RUNUSER


def test_podman_runs_as_rcman_through_its_own_socket():
    argv = ins.as_rcman_podman(990, "image", "exists", "x")
    assert argv[:5] == ["runuser", "-u", ins.APP_USER, "--", "/usr/bin/env"]
    assert "XDG_RUNTIME_DIR=/run/user/990" in argv
    assert argv[argv.index("podman") :] == [
        "podman", "--remote", "--url", "unix:///run/user/990/podman/podman.sock",
        "image", "exists", "x",
    ]  # fmt: skip


def test_rcman_makes_its_files_private_to_itself():
    app = ins.render("rcman.service", SETTINGS)
    assert "UMask=0077\n" in app


# --- limits: what the installer leaves for the slices to work -----------------------------


RUNUSER = ["runuser", "-u", ins.APP_USER, "--"]
PROGRAMS_OF_THE_MACHINE = ("systemctl", "podman", "loginctl")


DESCRIPTOR_PATHS = re.compile(r"^(/dev/(stdin|stdout|stderr|fd/\d+)|/proc/(self|\d+)/fd/\d+)$")


def opens_a_descriptor_by_path(argv: list[str]) -> bool:
    """Whether a command run as rcman is given a path that re-opens a descriptor it inherited
    from root (/dev/stdin, /dev/fd/N, /proc/self/fd/N). After runuser changes the uid, opening
    one by path is a new open of root's pipe or file, which its owner's mode refuses to rcman
    (``install: cannot open '/dev/stdin' for reading: Permission denied``); a command run as
    the test's user cannot show that, so no command is allowed to try. Read standard input by
    its descriptor instead (a script that reads sys.stdin)."""
    return any(DESCRIPTOR_PATHS.match(a) for a in argv)


def as_rcman_for_real(argv: list[str], real_run) -> subprocess.CompletedProcess | None:
    """What a command run as rcman does when the test is not root: the same command, run as
    the test's user, in the same environment, so that the files it makes are really made. None
    for a command that is not run as rcman, and for a program of the machine's own."""
    if argv[:4] != RUNUSER:
        return None
    rest = argv[4:]
    assert not opens_a_descriptor_by_path(rest), rest
    command = [a for a in rest if "=" not in a and a not in ("/usr/bin/env", "-i")]
    if command[0] in PROGRAMS_OF_THE_MACHINE:
        return subprocess.CompletedProcess(argv, 0, "", "")
    return real_run(rest)


class Manager:
    """Stands in for systemctl and the user manager's cgroup, for ensure_limits: records what
    is run, answers where rcman's user manager's control group is (``group``), and has the
    controllers that cgroup lists as a test plants them. ``root`` is the controllers of the
    root cgroup, which a machine on cgroup v2 has and one on v1 or a hybrid does not (None).
    A command run as rcman is run for real as the test's user, so that what it writes is
    there to read; a path of the state folder changed by path is refused (``chowned``)."""

    def __init__(
        self,
        tmp_path,
        monkeypatch,
        controllers="cpuset cpu io memory pids",
        group="/user.slice/user-990.slice/user@990.service",
        root="cpuset cpu io memory pids",
    ):
        self.calls: list[list[str]] = []
        self.chowned: list[Path] = []
        self.systemd = tmp_path / "systemd"
        self.state = tmp_path / "state"
        self.cgroups = tmp_path / "cgroup"
        self.group = group
        self.real_run = ins.run
        self.cgroups.mkdir()
        if root is not None:
            (self.cgroups / "cgroup.controllers").write_text(root + "\n")
        if controllers is not None and group.strip("/"):
            listing = self.cgroups / group.lstrip("/") / "cgroup.controllers"
            listing.parent.mkdir(parents=True)
            listing.write_text(controllers + "\n")
        for name, value in (("SYSTEMD", self.systemd), ("STATE", self.state),
                            ("CGROUP_ROOT", self.cgroups)):  # fmt: skip
            monkeypatch.setattr(ins, name, value)
        monkeypatch.setattr(ins, "app_uid", lambda: 990)
        monkeypatch.setattr(ins, "run", self.run)
        # the account's home is the state folder, and root changes nothing in it by path
        forbid_path_based_changes(monkeypatch, self.chowned, self.state)
        monkeypatch.setattr(
            ins.pwd, "getpwnam", lambda name: type("A", (), {"pw_uid": 990, "pw_gid": 990,
                                                             "pw_dir": str(self.state)})()
        )  # fmt: skip

    def run(self, argv, user=None, **kw):
        self.calls.append(argv)
        done = as_rcman_for_real(argv, lambda rest: self.real_run(rest, **kw))
        if done is not None:
            return done
        out = f"{self.group}\n" if argv[:2] == ["systemctl", "show"] else ""
        return subprocess.CompletedProcess(argv, 0, out, "")

    def written(self) -> set[Path]:
        return {
            p for root in (self.systemd, self.state) if root.exists() for p in root.rglob("*")
            if p.is_file()
        }  # fmt: skip

    def reexecutions(self) -> list[list[str]]:
        return [c for c in self.calls if "daemon-reexec" in c]


@pytest.mark.parametrize(
    "path", ["/dev/stdin", "/dev/fd/0", "/dev/fd/12", "/proc/self/fd/0", "/proc/1234/fd/3"]
)
def test_the_guard_refuses_a_command_run_as_rcman_that_opens_a_descriptor_by_its_path(path):
    """The planted twin of what failed in a real install: ``install -D /dev/stdin`` as rcman."""
    planted = [*RUNUSER, "/usr/bin/env", "-i", "install", "-D", "-m", "644", path, "/x/y"]
    with pytest.raises(AssertionError):
        as_rcman_for_real(planted, lambda rest: pytest.fail("run"))
    assert opens_a_descriptor_by_path(["install", path])
    for fine in ("/dev/null", "/dev/fdx", "/var/lib/rcman/fd/0", "stdin"):
        assert not opens_a_descriptor_by_path(["install", fine])


def test_no_installer_command_opens_a_descriptor_by_its_path():
    """Nothing in the installer names such a path, whichever helper builds the command."""
    for node in ast.walk(ast.parse(INSTALLER.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not DESCRIPTOR_PATHS.match(node.value), node.lineno
            for text in ("/dev/stdin", "/dev/fd/", "/proc/self/fd"):
                assert text not in node.value, node.lineno


def test_rcman_writes_a_file_from_standard_input_by_a_rename_with_its_mode_and_folders(
    tmp_path, monkeypatch
):
    seen = []
    real = ins.run

    def run(argv, user=None, **kw):
        seen.append(argv)
        done = as_rcman_for_real(argv, lambda rest: real(rest, **kw))
        return done if done is not None else real(argv, user, **kw)

    monkeypatch.setattr(ins, "run", run)
    monkeypatch.setattr(ins, "app_uid", lambda: 990)
    target = tmp_path / "state" / ".config" / "x.d" / "rcman.conf"
    assert ins.rcman_write(target, "[Manager]\nA=1\n", 0o640) is True
    assert target.read_text() == "[Manager]\nA=1\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640
    assert [p.name for p in target.parent.iterdir()] == ["rcman.conf"]  # no partial file left
    assert ins.rcman_write(target, "[Manager]\nA=1\n", 0o640) is False
    assert ins.rcman_write(target, "[Manager]\nA=2\n", 0o640) is True
    assert target.read_text() == "[Manager]\nA=2\n"
    link = tmp_path / "elsewhere"
    link.write_text("kept")
    (target.parent / ".rcman.conf.new").symlink_to(link)  # a link where the partial file goes
    assert ins.rcman_write(target, "[Manager]\nA=3\n", 0o640) is True
    assert link.read_text() == "kept" and target.read_text() == "[Manager]\nA=3\n"
    assert [a for a in seen if ins.WRITE_SCRIPT in a]  # written by the script, not by install


SLICE_DROP_IN = "user-990.slice.d/rcman.conf"
USER_DROP_IN = ".config/systemd/user.conf.d/rcman.conf"


def test_the_user_slice_has_no_limit_on_tasks_and_rcmans_own_default_has_none(
    tmp_path, monkeypatch, capsys
):
    manager = Manager(tmp_path, monkeypatch)
    changes = ins.ensure_limits()
    slice_file = manager.systemd / SLICE_DROP_IN
    user_file = manager.state / USER_DROP_IN
    assert "[Slice]\nTasksMax=infinity\n" in slice_file.read_text()
    assert "[Manager]\nDefaultTasksMax=infinity\n" in user_file.read_text()
    assert stat.S_IMODE(slice_file.stat().st_mode) == 0o644
    assert stat.S_IMODE(user_file.stat().st_mode) == 0o644
    assert str(slice_file) in changes and str(user_file) in changes
    assert manager.written() == {slice_file, user_file}  # nothing else, a Delegate drop-in least
    assert capsys.readouterr().err == ""  # and nothing to warn of


def test_the_slice_drop_in_sorts_after_the_one_the_distribution_ships(tmp_path, monkeypatch):
    """systemd reads drop-ins of one name in the order of their file names; the later wins, and
    the distribution's is 10-defaults.conf."""
    Manager(tmp_path, monkeypatch)
    ins.ensure_limits()
    assert "rcman.conf" > "10-defaults.conf"


def test_the_user_manager_reads_its_new_default_by_reexecuting_as_rcman(tmp_path, monkeypatch):
    manager = Manager(tmp_path, monkeypatch)
    ins.ensure_limits()
    [reexec] = manager.reexecutions()
    assert reexec[:5] == ["runuser", "-u", ins.APP_USER, "--", "/usr/bin/env"]
    assert "XDG_RUNTIME_DIR=/run/user/990" in reexec and f"HOME={manager.state}" in reexec
    assert reexec[-3:] == ["systemctl", "--user", "daemon-reexec"]
    assert not [c for c in manager.calls if c[:1] == ["systemctl"] and "daemon-reexec" in c]
    # never a restart or a stop of the user manager or of anything in it
    assert not [c for c in manager.calls if {"restart", "stop", "kill", "start"} & set(c)]


def test_the_reexecution_comes_after_the_drop_in_is_in_place(tmp_path, monkeypatch):
    manager = Manager(tmp_path, monkeypatch)
    seen = []
    real = manager.run
    manager.run = lambda argv, *a, **k: (
        seen.append((argv, (manager.state / USER_DROP_IN).exists())) or real(argv, *a, **k)
    )
    monkeypatch.setattr(ins, "run", manager.run)
    ins.ensure_limits()
    [(_, there)] = [(c, e) for c, e in seen if "daemon-reexec" in c]
    assert there


def test_running_it_again_changes_nothing_but_asks_the_manager_to_reexecute_again(
    tmp_path, monkeypatch
):
    """A reexecution that failed is not left undone because the file is already there: it kills
    nothing, so it is asked for on every install."""
    manager = Manager(tmp_path, monkeypatch)
    ins.ensure_limits()
    before = {p: p.read_text() for p in manager.written()}
    manager.calls.clear()
    assert ins.ensure_limits() == []
    assert {p: p.read_text() for p in manager.written()} == before
    assert len(manager.reexecutions()) == 1


def test_a_reexecution_that_failed_is_asked_for_again_by_the_next_install(tmp_path, monkeypatch):
    manager = Manager(tmp_path, monkeypatch)
    real = manager.run

    def failing(argv, *a, **k):
        if "daemon-reexec" in argv:
            manager.calls.append(argv)
            raise ins.InstallError("daemon-reexec failed")
        return real(argv, *a, **k)

    monkeypatch.setattr(ins, "run", failing)
    with pytest.raises(ins.InstallError):
        ins.ensure_limits()
    assert (manager.state / USER_DROP_IN).exists()  # the file is there, the manager has not read it
    monkeypatch.setattr(ins, "run", real)
    manager.calls.clear()
    assert ins.ensure_limits() == []
    assert len(manager.reexecutions()) == 1


def test_a_changed_user_default_is_written_again(tmp_path, monkeypatch):
    manager = Manager(tmp_path, monkeypatch)
    ins.ensure_limits()
    (manager.state / USER_DROP_IN).write_text("[Manager]\nDefaultTasksMax=100\n")
    manager.calls.clear()
    assert ins.ensure_limits() == [str(manager.state / USER_DROP_IN)]
    assert "DefaultTasksMax=infinity" in (manager.state / USER_DROP_IN).read_text()
    assert manager.reexecutions()


@pytest.mark.parametrize("missing", ["cpu", "memory", "pids"])
def test_a_user_manager_without_a_controller_is_a_warning_and_the_install_goes_on(
    tmp_path, monkeypatch, capsys, missing
):
    """Limits are off until an administrator sets one: an install must not fail for want of
    what only they need. The drop-ins are written, and what is missing is said."""
    have = [c for c in ("cpuset", "cpu", "io", "memory", "pids") if c != missing]
    manager = Manager(tmp_path, monkeypatch, controllers=" ".join(have))
    changes = ins.ensure_limits()  # no InstallError
    said = capsys.readouterr().err
    assert said.startswith("Warning: ") and "limits" in said
    assert missing in said and "user@990.service" in said
    assert "Delegate" in said  # and what to do about it
    assert len(changes) == 2 and manager.written() == {
        manager.systemd / SLICE_DROP_IN,
        manager.state / USER_DROP_IN,
    }
    assert len(manager.reexecutions()) == 1


def test_the_warning_names_every_controller_that_is_missing_and_only_those(
    tmp_path, monkeypatch, capsys
):
    Manager(tmp_path, monkeypatch, controllers="cpuset io memory")
    ins.ensure_limits()
    named = re.search(r"has not been given: ([a-z, ]+)\.", capsys.readouterr().err)
    assert named and named.group(1).split(", ") == ["cpu", "pids"]


def test_a_failing_systemctl_show_is_a_warning_that_cannot_tell_and_not_a_failure(
    tmp_path, monkeypatch, capsys
):
    manager = Manager(tmp_path, monkeypatch)
    real = manager.run

    def refusing(argv, *a, **k):
        if argv[:2] == ["systemctl", "show"]:
            raise ins.InstallError("systemctl show failed")
        return real(argv, *a, **k)

    monkeypatch.setattr(ins, "run", refusing)
    ins.ensure_limits()
    assert "cannot tell" in capsys.readouterr().err
    assert manager.written()


def test_the_controllers_checked_are_those_of_the_properties_limits_set():
    from rcman import limits

    controller_of = {
        "MemoryMax": "memory",
        "MemorySwapMax": "memory",
        "CPUQuota": "cpu",
        "TasksMax": "pids",
    }
    assert set(limits.PROPERTIES) == set(controller_of)  # a property added needs a controller named
    assert set(ins.LIMIT_CONTROLLERS) == {controller_of[name] for name in limits.PROPERTIES}


def test_a_controller_whose_name_is_part_of_another_is_not_taken_for_it(
    tmp_path, monkeypatch, capsys
):
    Manager(tmp_path, monkeypatch, controllers="cpuset io memory pids")  # cpuset is not cpu
    ins.ensure_limits()
    assert re.search(r"\bcpu\b", capsys.readouterr().err)


def test_a_user_manager_whose_controllers_cannot_be_read_is_a_warning_that_cannot_tell(
    tmp_path, monkeypatch, capsys
):
    manager = Manager(tmp_path, monkeypatch)
    (manager.cgroups / manager.group.lstrip("/") / "cgroup.controllers").unlink()
    ins.ensure_limits()
    said = capsys.readouterr().err
    assert said.startswith("Warning: ") and "cannot tell" in said
    assert manager.written()  # the drop-ins are written all the same


@pytest.mark.parametrize("group", ["", "/", "\n"])
def test_an_empty_control_group_is_never_read_as_the_root_cgroup_and_passed(
    tmp_path, monkeypatch, capsys, group
):
    """An empty path would be the root cgroup, which lists every controller there is: a false
    pass. It is not known where the user manager is, which is a warning."""
    manager = Manager(tmp_path, monkeypatch, group=group, root="cpuset cpu io memory pids")
    ins.ensure_limits()
    said = capsys.readouterr().err
    assert said.startswith("Warning: ") and "cannot tell" in said
    assert manager.written()


def test_the_root_cgroup_does_list_the_controllers_the_check_would_have_passed_on(
    tmp_path, monkeypatch
):
    """Positive control for the test above: the plant's root cgroup does hold all three."""
    manager = Manager(tmp_path, monkeypatch, group="")
    listed = (manager.cgroups / "cgroup.controllers").read_text().split()
    assert set(ins.LIMIT_CONTROLLERS) <= set(listed)


def test_a_machine_on_cgroup_v1_or_a_hybrid_is_a_warning_that_says_v2_is_needed(
    tmp_path, monkeypatch, capsys
):
    manager = Manager(tmp_path, monkeypatch, root=None)  # no unified hierarchy at the root
    changes = ins.ensure_limits()
    said = capsys.readouterr().err
    assert said.startswith("Warning: ") and "cgroup v2" in said
    assert len(changes) == 2 and manager.written()


def test_limits_are_made_ready_after_the_user_manager_runs_and_before_anything_is_built():
    import inspect

    source = inspect.getsource(ins.main)
    order = [
        source.index(f"{name}(") for name in ("ensure_podman", "ensure_limits", "ensure_toolchain")
    ]
    assert order == sorted(order) and len(set(order)) == 3


# --- what root writes in the account's own folders ---------------------------------------


def test_what_the_account_owns_is_written_as_the_account_and_never_by_root(tmp_path, monkeypatch):
    """Root follows a link, and every folder of rcman's home is one rcman can put a link in:
    root changes nothing there by path, rcman does it all."""
    manager = Manager(tmp_path, monkeypatch)
    ins.ensure_limits()
    assert manager.chowned == []  # no path-based change of owner or mode by root
    in_the_home = [c for c in manager.calls if any(str(manager.state) in a for a in c)]
    assert in_the_home  # non-vacuity
    for call in in_the_home:
        assert call[:4] == RUNUSER, call


def test_a_link_in_the_account_s_folders_is_followed_by_the_account_and_not_by_root(
    tmp_path, monkeypatch
):
    manager = Manager(tmp_path, monkeypatch)
    manager.state.mkdir()
    (manager.state / ".config").symlink_to(tmp_path)  # rcman can plant it
    ins.ensure_limits()
    assert manager.chowned == []
    # every command that reached into the home ran as rcman, so a link was followed with
    # rcman's rights, which are the rights it planted the link with
    for call in manager.calls:
        if any(str(manager.state) in a for a in call):
            assert call[:4] == RUNUSER, call


# --- running it again: nothing is reported changed ---------------------------------------


class Accounts:
    """Stands in for the account tools, for ensure_accounts: every account and group is there
    already, as on a second run, and rcman's home is a folder of the test's."""

    def __init__(self, tmp_path, monkeypatch):
        self.state = tmp_path / "state"
        self.calls: list[list[str]] = []
        monkeypatch.setattr(ins, "STATE", self.state)
        monkeypatch.setattr(ins, "ETC", tmp_path / "etc")
        monkeypatch.setattr(ins, "_exists", lambda kind, name: True)
        monkeypatch.setattr(ins, "run", self.run)
        self.chowned: list[Path] = []
        forbid_path_based_changes(monkeypatch, self.chowned, self.state)
        monkeypatch.setattr(
            ins.pwd,
            "getpwnam",
            lambda name: type(
                "A", (), {"pw_uid": os.getuid(), "pw_gid": os.getgid(), "pw_dir": str(self.state)}
            )(),
        )

    def run(self, argv, user=None, **kw):
        self.calls.append(argv)
        out = ""
        if argv[:2] == ["id", "-nG"]:
            out = "caddy rcman-web" if argv[2] == "caddy" else "rcman-proxy rcman"
        elif argv[:2] == ["getent", "group"]:
            out = "rcman-web:x:900:caddy"
        return subprocess.CompletedProcess(argv, 0, out, "")


def forbid_path_based_changes(monkeypatch, seen: list, under: Path) -> None:
    """In the installer, os.chown, os.lchown and os.chmod by a path in or at ``under`` record it
    and fail: root follows links in rcman's folders, so it changes what is there through a
    descriptor opened without following a link, or not at all. Any other path is changed as it
    would be, and so is everything the test itself does."""
    import types

    proxy = types.SimpleNamespace(**{name: getattr(os, name) for name in dir(os)})

    def guard(name):
        real = getattr(os, name)

        def guarded(path, *a, **k):
            where = Path(os.fsdecode(path)) if not isinstance(path, int) else None
            if where is not None and (where == under or under in where.parents):
                seen.append(where)
                raise AssertionError(f"root changed {where} by path with os.{name}")
            return real(path, *a, **k)

        return guarded

    for name in ("chown", "lchown", "chmod"):
        setattr(proxy, name, guard(name))
    monkeypatch.setattr(ins, "os", proxy)


def test_a_second_run_of_the_account_step_reports_nothing(tmp_path, monkeypatch):
    accounts = Accounts(tmp_path, monkeypatch)
    accounts.state.mkdir()
    accounts.state.chmod(0o750)
    assert ins.ensure_accounts("caddy") == []
    assert accounts.chowned == []


def test_a_home_podman_made_enterable_is_still_in_order_and_is_left_as_it_is(tmp_path, monkeypatch):
    """Podman adds the right to enter to every folder above a container that runs in a user
    namespace of its own, rcman's home among them. That lets no one read what is in it, and
    taking it away again would be undone at the next container: it is not a change, and it is
    not undone."""
    accounts = Accounts(tmp_path, monkeypatch)
    accounts.state.mkdir()
    accounts.state.chmod(0o751)
    assert ins.ensure_accounts("caddy") == []
    assert stat.S_IMODE(accounts.state.stat().st_mode) == 0o751


@pytest.mark.parametrize("mode", [0o700, 0o755, 0o770, 0o757, 0o777, 0o640])
def test_a_home_with_any_other_mode_is_put_right_and_reported(tmp_path, monkeypatch, mode):
    accounts = Accounts(tmp_path, monkeypatch)
    accounts.state.mkdir()
    accounts.state.chmod(mode)
    assert ins.ensure_accounts("caddy") == [str(accounts.state)]
    assert stat.S_IMODE(accounts.state.stat().st_mode) == 0o750
    assert accounts.chowned == []


def test_a_home_that_is_a_link_is_refused_and_what_it_leads_to_is_left_alone(tmp_path, monkeypatch):
    accounts = Accounts(tmp_path, monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    elsewhere.chmod(0o777)
    accounts.state.symlink_to(elsewhere)
    with pytest.raises(ins.InstallError, match="link"):
        ins.ensure_accounts("caddy")
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o777


def test_a_missing_home_is_made_and_put_right(tmp_path, monkeypatch):
    accounts = Accounts(tmp_path, monkeypatch)
    assert not accounts.state.exists()
    assert ins.ensure_accounts("caddy") == [str(accounts.state)]
    assert stat.S_IMODE(accounts.state.stat().st_mode) == 0o750


def test_the_steps_that_write_in_rcmans_home_report_nothing_the_second_time(tmp_path, monkeypatch):
    """The account step and the limits step, run twice with a container started between them
    (which gives the home the right to be entered): the second run changes nothing."""
    manager = Manager(tmp_path, monkeypatch)
    monkeypatch.setattr(ins, "ETC", tmp_path / "etc")
    monkeypatch.setattr(ins, "_exists", lambda kind, name: True)
    real = manager.run

    def answering(argv, *a, **k):
        if argv[:2] == ["id", "-nG"]:
            return subprocess.CompletedProcess(
                argv, 0, "caddy rcman-web" if argv[2] == "caddy" else "rcman-proxy rcman", ""
            )
        if argv[:2] == ["getent", "group"]:
            return subprocess.CompletedProcess(argv, 0, "rcman-web:x:900:caddy", "")
        return real(argv, *a, **k)

    monkeypatch.setattr(ins, "run", answering)
    monkeypatch.setattr(
        ins.pwd,
        "getpwnam",
        lambda name: type(
            "A", (), {"pw_uid": os.getuid(), "pw_gid": os.getgid(), "pw_dir": str(manager.state)}
        )(),
    )

    def both() -> list[str]:
        return [*ins.ensure_accounts("caddy"), *ins.ensure_limits()]

    assert both()  # the first run changes things
    manager.state.chmod(0o751)  # a container started
    assert both() == []
    assert both() == []
    assert manager.chowned == []


def test_the_socket_directory_is_changed_through_its_descriptor_and_a_link_is_refused(
    tmp_path, monkeypatch
):
    run_dir = tmp_path / "run-rcman"
    monkeypatch.setattr(ins, "RUN", run_dir)
    monkeypatch.setattr(ins, "TMPFILES", tmp_path / "tmpfiles.conf")
    monkeypatch.setattr(ins, "_gid", lambda group: os.getgid())
    monkeypatch.setattr(ins.pwd, "getpwnam", lambda name: type("A", (), {"pw_uid": os.getuid()})())
    seen: list = []
    forbid_path_based_changes(monkeypatch, seen, run_dir)
    assert str(run_dir) in ins.ensure_runtime_dir()
    assert stat.S_IMODE(run_dir.stat().st_mode) == 0o750
    assert ins.ensure_runtime_dir() == []
    run_dir.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    elsewhere.chmod(0o777)
    run_dir.symlink_to(elsewhere)
    with pytest.raises(ins.InstallError, match="link"):
        ins.ensure_runtime_dir()
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o777
    assert seen == []


# --- rcman's own files are made by rcman --------------------------------------------------


def test_the_allowed_list_is_made_by_rcman_and_only_when_it_is_not_there(tmp_path, monkeypatch):
    state, etc = tmp_path / "state", tmp_path / "etc"
    state.mkdir()
    forbidden: list = []
    forbid_path_based_changes(monkeypatch, forbidden, state)
    monkeypatch.setattr(ins, "ETC", etc)
    monkeypatch.setattr(ins, "SYSTEMD", tmp_path / "systemd")
    monkeypatch.setattr(ins, "CADDY_SNIPPET", etc / "Caddyfile.snippet")
    monkeypatch.setattr(ins, "STATE", state)
    monkeypatch.setattr(ins, "_gid", lambda group: os.getgid())
    calls: list[list[str]] = []
    real = ins.run

    def run(argv, user=None, **kw):
        calls.append(argv)
        done = as_rcman_for_real(argv, lambda rest: real(rest, **kw))
        return done if done is not None else real(argv, user=user, **kw)

    monkeypatch.setattr(ins, "run", run)
    monkeypatch.setattr(ins, "app_uid", lambda: 990)
    settings = {**SETTINGS, "APP_UID": "990", "WORKSPACE_IMAGE": "x", "BUILDER_IMAGE": "y"}
    allowed = state / "allowed-emails"
    assert str(allowed) in ins.configure(settings)
    assert allowed.read_text() == ""
    assert stat.S_IMODE(allowed.stat().st_mode) == 0o640
    assert forbidden == []
    touching = [c for c in calls if str(allowed) in c]
    assert touching and all(c[:4] == RUNUSER for c in touching)
    allowed.write_text("someone@example.com\n")  # rcman wrote it as it started
    assert str(allowed) not in ins.configure(settings)
    assert allowed.read_text() == "someone@example.com\n"


def test_the_two_system_accounts_are_made_by_one_helper_alike(tmp_path, monkeypatch):
    accounts = Accounts(tmp_path, monkeypatch)
    accounts.state.mkdir()
    accounts.state.chmod(0o750)
    monkeypatch.setattr(
        ins,
        "_exists",
        lambda kind, name: not (kind == "passwd" and name in (ins.APP_USER, ins.PROXY_USER)),
    )
    changes = ins.ensure_accounts("caddy")
    assert f"user {ins.APP_USER}" in changes and f"user {ins.PROXY_USER}" in changes
    made = {c[-1]: c for c in accounts.calls if c[:1] == ["useradd"]}
    assert set(made) == {ins.APP_USER, ins.PROXY_USER}
    for name, argv in made.items():
        assert {"--system", "--user-group"} <= set(argv), name
        assert argv[argv.index("--shell") + 1] == "/usr/sbin/nologin"
    assert argv_option(made[ins.APP_USER], "--home-dir") == str(accounts.state)
    assert "--no-create-home" in made[ins.PROXY_USER]


def argv_option(argv: list[str], option: str) -> str:
    return argv[argv.index(option) + 1]


def string_constants(source: str) -> list[str]:
    return [n.value for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Constant)
            if isinstance(n.value, str)]  # fmt: skip


def test_the_installer_names_the_commands_that_change_accounts_and_run_as_one_in_one_place():
    source = INSTALLER.read_text(encoding="utf-8")
    constants = string_constants(source)
    assert constants.count("useradd") == 1
    assert constants.count("runuser") == 1
    for planted in (
        'x = ["useradd", "a"]\ny = ["useradd", "b"]\n',
        'a = "runuser"\nb = "runuser"\n',
    ):
        assert (
            string_constants(planted).count(string_constants(planted)[0]) == 2
        )  # plant took effect


# --- no podman system migrate while a workspace runs -----------------------------------------


class RunningContainers:
    """rcman's Podman, asked which containers run, for the check before new subordinate ranges
    are taken: whether its socket is there, and what it lists."""

    def __init__(self, tmp_path, monkeypatch, *, socket=True, running=()):
        self.calls: list[list[str]] = []
        self.running = list(running)
        self.files = [tmp_path / "subuid", tmp_path / "subgid"]
        monkeypatch.setattr(ins, "SUBORDINATE_FILES", tuple(self.files))
        monkeypatch.setattr(ins, "app_uid", lambda: 990)
        monkeypatch.setattr(ins, "podman_socket_exists", lambda uid: socket)
        monkeypatch.setattr(ins, "run", self.run)

    def give_ranges(self, count=None):
        for path in self.files:
            path.write_text(
                f"{ins.APP_USER}:{ins.SUBORDINATE_FIRST}:{count or ins.SUBORDINATE_COUNT}\n"
            )

    def run(self, argv, user=None, **kw):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "".join(f"{n}\n" for n in self.running), "")


def test_new_ranges_are_refused_while_a_container_runs_and_the_remedy_is_the_exact_commands(
    tmp_path, monkeypatch
):
    """rcman's interface cannot stop a container, and rcman starts a stopped one again: the
    way on is to stop rcman, then the containers, as rcman, and run the installer again."""
    import shlex

    host = RunningContainers(tmp_path, monkeypatch, running=["rcman-ws-a", "rcman-ws-b"])
    host.give_ranges(count=ins.RANGE)  # too small: another would be added
    with pytest.raises(ins.InstallError) as refused:
        ins.check_containers_stopped_before_new_ranges()
    said = str(refused.value)
    assert "rcman-ws-a" in said and "rcman-ws-b" in said
    assert "systemctl stop rcman" in said
    stop_all = shlex.join(ins.as_rcman(990, "podman", "stop", "--all"))  # as the installer runs it
    assert stop_all in said
    assert said.index("systemctl stop rcman") < said.index(stop_all) < said.rindex("install.sh")
    assert "interface" not in said  # which cannot do it
    # what has been done by then is said, and that running again is safe
    assert "Nothing was changed" not in said
    assert "settings" in said and "packages" in said and "accounts" in said
    assert "safe to run" in said
    [ask] = host.calls
    assert ask[:4] == RUNUSER and ask[ask.index("podman") :][1:3] == ["--remote", "--url"]
    assert "ps" in ask


def test_the_refusal_comes_before_any_range_is_added_so_that_the_next_run_asks_again(
    tmp_path, monkeypatch
):
    import inspect

    source = inspect.getsource(ins.main)
    assert source.index("check_containers_stopped_before_new_ranges(") < source.index(
        "ensure_subordinate_ids("
    )


def test_a_machine_with_no_range_yet_and_no_podman_socket_has_nothing_to_stop(
    tmp_path, monkeypatch
):
    host = RunningContainers(tmp_path, monkeypatch, socket=False)
    ins.check_containers_stopped_before_new_ranges()  # a first install: nothing could run
    assert host.calls == []


def test_new_ranges_go_ahead_when_no_container_runs(tmp_path, monkeypatch):
    host = RunningContainers(tmp_path, monkeypatch, running=[])
    ins.check_containers_stopped_before_new_ranges()
    assert len(host.calls) == 1  # it did ask: the quiet is an answer and not an absence of one


def test_ranges_that_are_there_need_no_migration_and_so_no_question(tmp_path, monkeypatch):
    """A second install ends nothing and asks nothing of Podman here."""
    host = RunningContainers(tmp_path, monkeypatch, running=["rcman-ws-a"])
    host.give_ranges()
    ins.check_containers_stopped_before_new_ranges()
    assert host.calls == []
    assert ins.ensure_subordinate_ids() == []


def test_migrate_is_asked_for_only_when_a_range_was_added_and_as_rcman(tmp_path, monkeypatch):
    calls: list[list[str]] = []

    def answer(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    linger = tmp_path / "linger"
    linger.mkdir()
    (linger / ins.APP_USER).touch()  # lingering is on already
    monkeypatch.setattr(ins, "LINGER", linger)
    monkeypatch.setattr(ins, "app_uid", lambda: 990)
    monkeypatch.setattr(ins, "podman_socket_exists", lambda uid: True)
    monkeypatch.setattr(ins, "run", answer)
    monkeypatch.setattr(ins.subprocess, "run", answer)
    ins.ensure_podman(subordinate_changed=False)
    assert not [c for c in calls if "migrate" in c]
    calls.clear()
    ins.ensure_podman(subordinate_changed=True)
    [migrate] = [c for c in calls if "migrate" in c]
    assert migrate[:4] == RUNUSER and migrate[-3:] == ["podman", "system", "migrate"]


# --- rolling back to a release the database is newer than ----------------------------------


def test_the_schema_a_release_has_is_the_number_of_its_migrations():
    from rcman import db

    source = (REPO / "backend" / "rcman" / "db.py").read_text(encoding="utf-8")
    assert ins.release_schema_version(source) == len(db.MIGRATIONS) > 0
    assert ins.release_schema_version("MIGRATIONS = ['a', 'b', 'c']\n") == 3
    assert ins.release_schema_version("x = 1\nMIGRATIONS = []\n") == 0


@pytest.mark.parametrize("source", ["x = 1\n", "MIGRATIONS = make()\n", "def broken(:\n", ""])
def test_a_release_whose_schema_cannot_be_told_is_not_rolled_back_to(source):
    with pytest.raises(ins.InstallError, match="schema"):
        ins.release_schema_version(source)


def previous_release(root: Path, migrations: int) -> None:
    release = root / "releases" / "old"
    (release / "backend" / "rcman").mkdir(parents=True)
    (release / "backend" / "rcman" / "db.py").write_text(f"MIGRATIONS = {['m'] * migrations!r}\n")
    (root / "previous").symlink_to("releases/old")


def test_a_rollback_to_a_release_older_than_the_database_is_refused_in_words(tmp_path, monkeypatch):
    previous_release(tmp_path, migrations=2)
    monkeypatch.setattr(ins, "database_schema_version", lambda database=ins.DATABASE: 3)
    with pytest.raises(ins.InstallError) as refused:
        ins.check_rollback(tmp_path)
    said = str(refused.value)
    assert "3" in said and "2" in said and "database" in said
    assert "install.sh" in said  # the way on is to install a newer release, not back


@pytest.mark.parametrize("database", [0, 1, 2])
def test_a_rollback_to_a_release_that_can_read_the_database_goes_ahead(
    tmp_path, monkeypatch, database
):
    previous_release(tmp_path, migrations=2)
    monkeypatch.setattr(ins, "database_schema_version", lambda database_=None, d=database: d)
    ins.check_rollback(tmp_path)


def test_a_rollback_with_no_previous_release_is_refused_as_before(tmp_path, monkeypatch):
    monkeypatch.setattr(ins, "database_schema_version", lambda *a: 0)
    with pytest.raises(ins.InstallError, match="no previous release"):
        ins.check_rollback(tmp_path)


def test_the_rollback_checks_the_database_before_it_switches_anything(
    tmp_path, monkeypatch, capsys
):
    switched = []
    monkeypatch.setattr(ins.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ins, "ROOT", tmp_path)
    monkeypatch.setattr(
        ins, "check_rollback", lambda root: (_ for _ in ()).throw(ins.InstallError("newer"))
    )
    monkeypatch.setattr(ins, "rollback", lambda root: switched.append("link"))
    monkeypatch.setattr(ins, "start_services", lambda restart: switched.append("restart"))
    assert ins.main(["--rollback"]) == 1
    assert switched == []
    assert "newer" in capsys.readouterr().err


def sqlite_version(path: Path, version: int, keep_open: bool):
    import sqlite3

    con = sqlite3.connect(path)
    con.execute("pragma journal_mode = wal")
    con.execute("pragma wal_autocheckpoint = 0")
    con.execute("create table t (x)")
    con.execute(f"pragma user_version = {version}")
    con.commit()
    if not keep_open:
        con.close()
    return con


def test_the_database_version_is_read_as_rcman_with_what_only_the_log_holds(tmp_path, monkeypatch):
    """The header of the file lags behind in WAL mode, until a checkpoint: asking SQLite is
    the way to know."""
    database = tmp_path / "rcman.db"
    con = sqlite_version(database, 7, keep_open=True)
    header = database.read_bytes()[60:64]
    assert int.from_bytes(header, "big") != 7  # positive control: the header alone says wrong
    calls = []
    real = ins.run
    monkeypatch.setattr(ins, "app_uid", lambda: 990)

    def run(argv, user=None, **kw):
        calls.append(argv)
        return as_rcman_for_real(argv, lambda rest: real(rest, **kw))

    monkeypatch.setattr(ins, "run", run)
    assert ins.database_schema_version(database) == 7
    assert calls[0][:4] == RUNUSER  # and root never opened it, which could leave files of its own
    con.close()
    assert sorted(p.name for p in tmp_path.iterdir() if p.is_file()) == ["rcman.db"]
    assert ins.database_schema_version(tmp_path / "none.db") == 0
    assert not (tmp_path / "none.db").exists()  # asking made no database


# --- where the installer's root may touch what rcman can write ---------------------------------

INSTALLER = REPO / "installer" / "install.py"
# Attributes that lead into rcman's home whatever the variable: an account's home directory.
TAINTED_ATTRIBUTES = {"pw_dir"}
OS_PATH_CALLS = {
    "chown", "lchown", "chmod", "lchmod", "mkdir", "makedirs", "remove", "unlink", "rmdir",
    "rename", "replace", "symlink", "link", "truncate", "utime", "stat", "lstat", "readlink",
    "listdir", "scandir", "walk", "open", "access", "mkfifo", "mknod", "chdir", "chroot",
}  # fmt: skip
PATH_METHODS = {
    "write_text", "write_bytes", "mkdir", "chmod", "lchmod", "unlink", "rmdir", "touch", "rename",
    "replace", "symlink_to", "hardlink_to", "open", "read_text", "read_bytes", "exists", "is_file",
    "is_dir", "is_symlink", "stat", "lstat", "iterdir", "glob", "rglob", "resolve", "readlink",
    "owner", "group", "samefile", "is_socket",
}  # fmt: skip
TEMPFILE_CALLS = {"mkdtemp", "mkstemp", "TemporaryDirectory", "NamedTemporaryFile", "TemporaryFile"}
SOCKET_CALLS = {"create_connection", "create_server", "fromfd", "socketpair"}
PROCESS_CALLS = {"run", "Popen", "call", "check_call", "check_output"}
# Named, reasoned exemptions: function -> why a path of rcman's is touched there by root. Each
# is checked to still be there (see test_the_exemptions_still_have_their_sites).
EXEMPT = {
    "own_directory": (
        "opens the directory itself with O_NOFOLLOW | O_DIRECTORY from a root-owned parent "
        "(/var/lib, /run) and changes it through the descriptor, so a link is refused and never "
        "followed; this is the one place root changes what rcman owns"
    ),
}


def rcman_writable_constants(namespace: dict) -> set[str]:
    """The installer's path constants in rcman's home or in the socket's folder, which rcman
    owns: derived from the values, so one added later is covered."""
    state, run_dir = namespace["STATE"], namespace["RUN"]
    return {
        name
        for name, value in namespace.items()
        if name.isupper()
        and isinstance(value, Path)
        and (value in (state, run_dir) or state in value.parents or run_dir in value.parents)
    }


def mentions(node: ast.AST, tainted: set[str]) -> bool:
    return any(
        (isinstance(n, ast.Name) and n.id in tainted)
        or (isinstance(n, ast.Attribute) and n.attr in TAINTED_ATTRIBUTES)
        for n in ast.walk(node)
    )


def functions_of(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def parameters(fn: ast.FunctionDef) -> dict[str, ast.expr | None]:
    """Each parameter and its default, if it has one."""
    args = fn.args
    positional = [*args.posonlyargs, *args.args]
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    found = {a.arg: d for a, d in zip(positional, defaults, strict=True)}
    found.update({a.arg: d for a, d in zip(args.kwonlyargs, args.kw_defaults, strict=True)})
    return found


def path_parameters(fn: ast.FunctionDef) -> set[str]:
    """The parameters annotated as paths."""
    args = fn.args
    return {
        a.arg
        for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]
        if a.annotation is not None and "Path" in ast.unparse(a.annotation)
    }


def tainted_in(fn: ast.FunctionDef, seeds: set[str], path_arguments: bool) -> set[str]:
    """The names in ``fn`` that lead into a rcman path: the seeds, the parameters whose default
    does (or, with ``path_arguments``, every parameter that is a path), and whatever is
    assigned from one, to a fixed point."""
    tainted = set(seeds)
    if path_arguments:
        tainted |= path_parameters(fn)
    for name, default in parameters(fn).items():
        if default is not None and mentions(default, tainted):
            tainted.add(name)
    pairs = []  # (targets, source)
    for node in ast.walk(fn):
        if isinstance(node, ast.Assign):
            pairs.append((node.targets, node.value))
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)) and node.value:
            pairs.append(([node.target], node.value))
        elif isinstance(node, (ast.For, ast.comprehension)):
            pairs.append(([node.target], node.iter))
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            pairs.append(([node.optional_vars], node.context_expr))
    changed = True
    while changed:
        changed = False
        for targets, source in pairs:
            if mentions(source, tainted):
                for target in targets:
                    for n in ast.walk(target):
                        if isinstance(n, ast.Name) and n.id not in tainted:
                            tainted.add(n.id)
                            changed = True
    return tainted


def account_functions(tree: ast.Module) -> set[str]:
    """The functions that make a command run as an account: the one that names runuser, and
    those that build on one of them, to a fixed point."""
    functions = functions_of(tree)
    found = {
        name for name, fn in functions.items()
        if any(isinstance(n, ast.Constant) and n.value == "runuser" for n in ast.walk(fn))
    }  # fmt: skip
    changed = True
    while changed:
        changed = False
        for name, fn in functions.items():
            used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)}
            if name not in found and used & found:
                found.add(name)
                changed = True
    return found


def touches(fn: ast.FunctionDef, tainted: set[str], writers: set[str], as_account: set[str]):
    """Each (line, what, kind) at which ``fn`` has root act by path on something in ``tainted``:
    a call of the os module, shutil, a method of a path, a temporary file made there, a function
    that itself acts on a path it is given, or a command run as root with such a path."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        values = [*node.args, *(k.value for k in node.keywords)]
        hit = any(mentions(v, tainted) for v in values)
        owner = func.value if isinstance(func, ast.Attribute) else None
        owner_name = owner.id if isinstance(owner, ast.Name) else None
        if isinstance(func, ast.Attribute):
            if owner_name == "os" and func.attr in OS_PATH_CALLS and hit:
                yield node.lineno, f"os.{func.attr}", "path"
            elif isinstance(owner, ast.Attribute) and owner.attr == "path" and hit:
                yield node.lineno, f"os.path.{func.attr}", "path"
            elif owner_name == "shutil" and hit:
                yield node.lineno, f"shutil.{func.attr}", "path"
            elif owner_name == "socket" and func.attr in SOCKET_CALLS and hit:
                yield node.lineno, f"socket.{func.attr}", "path"
            elif owner_name == "tempfile" and func.attr in TEMPFILE_CALLS and hit:
                yield node.lineno, f"tempfile.{func.attr}", "path"
            elif owner_name == "subprocess" and func.attr in PROCESS_CALLS and hit:
                if not run_as_account(node, as_account):
                    yield node.lineno, f"subprocess.{func.attr} as root", "run"
            elif func.attr == "connect" and hit:
                yield node.lineno, "a socket connected to it", "path"
            elif func.attr in PATH_METHODS and mentions(owner, tainted):
                yield node.lineno, f".{func.attr}()", "path"
        elif isinstance(func, ast.Name):
            if func.id == "open" and hit:
                yield node.lineno, "open", "path"
            elif func.id == "run" and hit and not run_as_account(node, as_account):
                yield node.lineno, "run as root", "run"
            elif func.id in writers and hit:
                yield node.lineno, f"{func.id}() acts on the path it is given", "path"


def run_as_account(call: ast.Call, as_account: set[str]) -> bool:
    """Whether a command run is made to run as an account: built by one of the functions that
    do that, or given a user."""
    if any(k.arg == "user" for k in call.keywords):
        return True
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in as_account
        for v in [*call.args, *(k.value for k in call.keywords)]
        for n in ast.walk(v)
    )


def run_user_functions(tree: ast.Module, as_account: set[str]) -> set[str]:
    """The functions that name a path in /run/user/, the runtime folder of rcman's user
    manager, which rcman owns: what they return is in rcman's reach as a constant is."""
    return {
        name
        for name, fn in functions_of(tree).items()
        if name not in as_account
        and any(
            isinstance(n, ast.Constant) and isinstance(n.value, str) and "/run/user/" in n.value
            for n in ast.walk(fn)
        )
    }


def root_touches(source: str, constants: set[str], exempt: dict[str, str]):
    """(findings, exemptions used, writers) for the module ``source``: where its functions
    have root act by path on what ``constants`` (names of paths rcman can write) lead to. It
    reads one function at a time, so it finds what is done in the function that has the path
    in hand; it is a net for the mistakes that can be seen there, not a proof about the
    program."""
    tree = ast.parse(source)
    functions = functions_of(tree)
    as_account = account_functions(tree)
    constants = constants | run_user_functions(tree, as_account)
    writers: set[str] = set()
    changed = True
    while (
        changed
    ):  # a function that acts by path on a parameter is one root must not hand a path in
        changed = False
        for name, fn in functions.items():
            if name in exempt or name in writers or name in as_account:
                continue
            every = tainted_in(fn, set(), path_arguments=True)
            if any(kind == "path" for *_, kind in touches(fn, every, writers, as_account)):
                writers.add(name)
                changed = True
    findings, used = [], set()
    for name, fn in functions.items():
        tainted = tainted_in(fn, constants, path_arguments=name in exempt)
        for line, what, _kind in touches(fn, tainted, writers, as_account):
            if name in exempt:
                used.add(name)
            else:
                findings.append(f"{name}, line {line}: {what}")
    return findings, used, writers


def installer_findings():
    namespace = vars(ins)
    return root_touches(
        INSTALLER.read_text(encoding="utf-8"), rcman_writable_constants(namespace), EXEMPT
    )


def test_the_path_constants_in_rcmans_reach_are_derived_from_the_installer():
    constants = rcman_writable_constants(vars(ins))
    assert {"STATE", "CLAUDE_STORE", "RUN", "SOCKET"} <= constants  # known positives
    tree = ast.parse(INSTALLER.read_text(encoding="utf-8"))
    runtime = run_user_functions(tree, account_functions(tree))
    assert "podman_socket" in runtime and "as_rcman" not in runtime
    assert "TOOLS" not in constants and "ROOT" not in constants and "ETC" not in constants
    planted = {**vars(ins), "PLANTED": ins.STATE / "planted", "PLANTED_RUN": ins.RUN / "x"}
    assert {"PLANTED", "PLANTED_RUN"} <= rcman_writable_constants(planted)
    source = INSTALLER.read_text(encoding="utf-8")
    functions = functions_of(ast.parse(source))
    touching = {
        name for name, fn in functions.items()
        if any(isinstance(n, ast.Name) and n.id in constants for n in ast.walk(fn))
    }  # fmt: skip
    assert {"ensure_accounts", "ensure_claude_store", "ensure_runtime_dir", "configure"} <= touching


def test_root_acts_on_nothing_in_its_reach_that_rcman_can_write_by_its_path_in_a_function():
    findings, _, writers = installer_findings()
    assert findings == []
    assert {"write_if_changed", "_install_file", "_set_link"} <= writers  # the guard sees them


FAKE_ACCOUNT = """
def as_account(user, argv):
    return ["runuser", "-u", user, "--", *argv]
def as_rcman(*argv):
    return as_account("rcman", list(argv))
def run_path(uid):
    return Path(f"/run/user/{uid}/podman/podman.sock")
"""


@pytest.mark.parametrize(
    "body",
    [
        'os.chown(STATE / "x", 0, 0)',
        "os.chmod(STATE, 0o700)",
        "os.lchown(CLAUDE_STORE, 0, 0)",
        '(RUN / "x").mkdir()',
        "SOCKET.unlink()",
        'shutil.rmtree(CLAUDE_STORE / "v")',
        'tempfile.mkdtemp(dir=STATE, prefix="x")',
        'open(STATE / "f", "w")',
        'Path(pwd.getpwnam("rcman").pw_dir).joinpath("f").write_text("x")',
        'home = pwd.getpwnam("rcman").pw_dir\n    os.makedirs(home)',
        'a = STATE / "a"\n    b = a / "b"\n    b.write_text("x")',
        'for p in (STATE / "a").rglob("*"):\n        os.lchown(p, 0, 0)',
        'run(["chown", "rcman", str(STATE / "x")])',
        'subprocess.run(["rm", "-rf", str(CLAUDE_STORE)])',
        'write_it(STATE / "x")',
        "write_it(CLAUDE_STORE)",
        "s = socket.socket(socket.AF_UNIX)\n    s.connect(str(SOCKET))",
        "socket.create_connection((str(RUN), 1))",
        "run_path(1).is_socket()",
        "os.stat(run_path(1))",
    ],
)
def test_root_acting_by_path_in_rcmans_reach_is_found(body):
    source = FAKE_ACCOUNT + (
        f"def write_it(path: Path):\n    path.write_text('x')\n\ndef step():\n    {body}\n"
    )
    findings, _, _ = root_touches(source, {"STATE", "CLAUDE_STORE", "RUN", "SOCKET"}, {})
    assert any(f.startswith("step,") for f in findings), (body, findings)


def test_a_parameter_whose_default_is_a_path_of_rcmans_is_in_its_reach():
    source = FAKE_ACCOUNT + 'def step(store=CLAUDE_STORE):\n    (store / "v").write_text("x")\n'
    findings, _, _ = root_touches(source, {"CLAUDE_STORE"}, {})
    assert findings and findings[0].startswith("step,")
    other = FAKE_ACCOUNT + 'def step(store=SOMEWHERE):\n    (store / "v").write_text("x")\n'
    assert root_touches(other, {"CLAUDE_STORE"}, {})[0] == []


@pytest.mark.parametrize(
    "body",
    [
        'run(as_rcman("touch", str(STATE / "x")))',
        'run(as_account("rcman", ["rm", str(STATE / "x")]))',
        'subprocess.run(as_rcman("cat", str(STATE / "x")))',
        'os.chown(ETC / "x", 0, 0)',
        '(ROOT / "x").write_text("x")',
        "print(STATE)",
        "name = str(STATE)",
        'write_it(ETC / "x")',
        "os.fchown(fd, 0, 0)",
        'socket.create_connection(("127.0.0.1", 2019))',
        'run(as_rcman("test", "-S", str(run_path(1))))',
    ],
)
def test_what_rcman_does_and_what_root_does_elsewhere_is_not_found(body):
    source = FAKE_ACCOUNT + (
        f"def write_it(path: Path):\n    path.write_text('x')\n\ndef step():\n    {body}\n"
    )
    findings, _, _ = root_touches(source, {"STATE", "CLAUDE_STORE"}, {})
    assert findings == [], (body, findings)


def test_an_exemption_covers_its_function_only_and_is_stale_when_the_site_is_gone():
    source = FAKE_ACCOUNT + (
        "def own(p: Path):\n    os.chown(p, 0, 0)\n\ndef step():\n    os.chown(STATE, 0, 0)\n"
    )
    findings, used, _ = root_touches(source, {"STATE"}, {"own": "reasoned"})
    assert [f.split(",")[0] for f in findings] == ["step"]  # the exempt one is not reported
    assert used == {"own"}  # and its site was there
    helper = source + "def uses():\n    own(STATE)\n"
    findings, _, _ = root_touches(helper, {"STATE"}, {"own": "reasoned"})
    assert not [f for f in findings if f.startswith("uses")]  # calling the exempt one is fine
    gone = FAKE_ACCOUNT + "def own(p: Path):\n    return p\n"
    assert root_touches(gone, {"STATE"}, {"own": "reasoned"})[1] == set()  # stale: no site left


def test_the_exemptions_still_have_their_sites():
    _, used, _ = installer_findings()
    assert used == set(EXEMPT)  # one with no site left in the installer is stale
    functions = functions_of(ast.parse(INSTALLER.read_text(encoding="utf-8")))
    assert set(EXEMPT) <= set(functions)
    assert all(reason.strip() for reason in EXEMPT.values())


def test_a_violation_planted_in_the_installer_itself_is_found():
    source = INSTALLER.read_text(encoding="utf-8")
    planted = source + '\n\ndef planted():\n    os.chown(STATE / "x", 0, 0)\n'
    assert planted != source
    findings, _, _ = root_touches(planted, rcman_writable_constants(vars(ins)), EXEMPT)
    assert [f for f in findings if f.startswith("planted,")]
    # the one function that may change what rcman owns, no longer the exempt one: every caller
    # of it is handing root a path of rcman's to act on
    again = source.replace("own_directory", "renamed_directory")
    assert again != source
    found, _, _ = root_touches(again, rcman_writable_constants(vars(ins)), EXEMPT)
    assert {f.split(",")[0] for f in found} >= {"ensure_accounts", "ensure_runtime_dir"}


# --- the guide, held to the hardening ------------------------------------------------------

GUIDE = REPO / "docs" / "installation.md"


def guide_text() -> str:
    return GUIDE.read_text(encoding="utf-8")


def flat(text: str) -> str:
    """Text on one line: the guide breaks its sentences wherever a line ends."""
    return " ".join(text.split())


def removal_steps(guide: str) -> dict[int, str]:
    parts = re.split(r"^(\d+)\. ", removal_section(guide), flags=re.M)
    return {int(number): flat(text) for number, text in zip(parts[1::2], parts[2::2], strict=True)}


# What a step of the removal says of another step: the words before its number, and a word only
# the step it must name has.
STEP_REFERENCES = {
    r"restart Caddy after step (\d+)": "groupdel",
    r"step (\d+) ends them": "podman system reset",
    r"goes with the home in step (\d+)": "install.env",
    r"step (\d+) checks": "getent passwd",
}


def wrong_step_references(guide: str) -> list[str]:
    steps, text, wrong = removal_steps(guide), flat(removal_section(guide)), []
    for phrase, word in STEP_REFERENCES.items():
        for match in re.finditer(phrase, text):
            number = int(match.group(1))
            if number not in steps or word not in steps[number]:
                wrong.append(f"{match.group(0)!r} names a step without {word!r}")
        if not re.search(phrase, text):
            wrong.append(f"{phrase!r} is not in the section")
    return wrong


def test_every_step_the_removal_names_is_the_step_that_does_it():
    guide = guide_text()
    assert len(removal_steps(guide)) > 5  # non-vacuity
    assert wrong_step_references(guide) == []


def test_the_step_references_guard_fails_on_a_number_moved_or_a_phrase_removed():
    guide = guide_text()
    for phrase in STEP_REFERENCES:
        loose = re.compile(phrase.replace(" ", r"\s+"))  # the guide breaks lines anywhere
        planted = loose.sub(lambda m: m.group(0).replace(m.group(1), "99"), guide)
        assert planted != guide, phrase  # the plant took effect
        assert wrong_step_references(planted) != [], phrase
    gone = guide.replace("step 3 ends them", "the next step ends them")
    assert gone != guide and wrong_step_references(gone) != []


def test_the_final_check_of_the_removal_looks_at_rcmans_entries_only():
    check = flat(removal_steps(guide_text())[max(removal_steps(guide_text()))])
    drop_in = ins.SLICE_DROP_IN_FILE
    assert (
        f"{ins.SYSTEMD}/user-" in check
        and f"{drop_in.parent.name.replace('{uid}', '*')}/{drop_in.name}" in check
    )
    assert f"ls {ins.LINGER / ins.APP_USER}" in check  # rcman's entry, not every account's
    assert f"ls {ins.LINGER} " not in check and not check.endswith(f"ls {ins.LINGER}")
    assert f"ls {ins.SYSTEMD} | grep rcman" in check


def test_the_removal_keeps_the_caddy_import_from_dangling_and_never_validates_as_root():
    section = flat(removal_section(guide_text()))
    assert "keep" in section and "empty" in section and str(ins.CADDY_SNIPPET) in section
    assert "between" in section and "reinstall" in section.lower() or "install again" in section
    assert "caddy validate" in section and "as root" in section


def test_the_guide_says_install_sh_runs_from_a_clone_of_its_own():
    guide = flat(guide_text())
    assert "dedicated clone" in guide and "npm ci" in guide and "imports Python" in guide


def test_the_guide_says_what_the_installer_pins_and_checks():
    guide = flat(guide_text())
    for name in ("NODE_SHA256", "UV_SHA256", "OAUTH2_PROXY_SHA256", "TMUX_SHA256",
                 "TMUX_BUILD_DIGEST"):  # fmt: skip
        assert name in guide, name
    assert "digest" in guide


def test_the_guide_says_which_runtime_podman_must_use_and_that_the_installer_checks_it():
    guide = flat(guide_text())
    assert ins.OCI_RUNTIME in guide and "podman info" in guide


def test_the_guide_says_a_range_is_not_added_while_a_workspace_runs():
    guide = flat(guide_text())
    assert "podman system migrate" in guide
    assert "refuses" in guide and "running" in guide
    assert "stops its running containers" not in guide  # the installer stops none


def test_the_guide_says_a_rollback_is_refused_for_a_newer_database():
    guide = flat(guide_text())
    start = guide.index("go back to the release before")
    rollback = guide[start : start + 900]
    assert "database" in rollback and "refuses" in rollback and "schema" in rollback


def test_the_guide_no_longer_claims_a_refusal_it_does_not_make():
    assert "as it does for no other file" not in flat(guide_text())
    assert str(ins.TOOLS) in guide_text() and "/var/lib/rcman/tools" not in guide_text()


# --- the health check, the built tree, the Caddy restart -----------------------------------


# The installer's default for oauth2-proxy's port; the check is stubbed, nothing listens.
HEALTH_SETTINGS = {"PROXY_PORT": ins.FIELDS["PROXY_PORT"][2]}


def test_the_health_of_rcman_is_asked_as_rcman_through_its_socket_and_not_by_root(monkeypatch):
    """The socket is in a folder rcman owns, so a link there could lead a connection of root's
    elsewhere: the question is put by rcman."""
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "200\n", "")

    monkeypatch.setattr(ins, "app_uid", lambda: 990)
    monkeypatch.setattr(ins, "run", run)
    monkeypatch.setattr(ins, "_status_over_tcp", lambda host, port, path: 200)
    ins.health(HEALTH_SETTINGS)
    [(argv, kw)] = calls
    assert argv[:4] == RUNUSER and str(ins.SOCKET) in argv and "/api/health" in argv
    assert kw.get("cwd") == "/"
    assert "socket." not in inspect_source(ins.health)  # and root opens no socket in it
    assert ".connect(" not in inspect_source(ins._status_over_socket)


def inspect_source(function) -> str:
    import inspect

    return inspect.getsource(function)


@pytest.mark.parametrize("answer", [(1, ""), (0, "503\n"), (0, "")])
def test_rcman_that_does_not_answer_ok_is_not_healthy(monkeypatch, answer):
    code, out = answer
    monkeypatch.setattr(ins, "app_uid", lambda: 990)
    monkeypatch.setattr(
        ins, "run", lambda argv, **kw: subprocess.CompletedProcess(argv, code, out, "")
    )
    monkeypatch.setattr(ins, "_status_over_tcp", lambda host, port, path: 200)
    monkeypatch.setattr(ins, "HEALTH_TIMEOUT", 0.0)
    monkeypatch.setattr(ins.time, "sleep", lambda seconds: None)
    with pytest.raises(ins.InstallError, match="rcman"):
        ins.health(HEALTH_SETTINGS)


def test_the_health_script_reports_the_status_rcman_answers_on_its_socket(tmp_path):
    import http.server
    import socketserver
    import tempfile
    import threading

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200 if self.path == "/api/health" else 404)
            self.end_headers()

        def log_message(self, *args):
            pass

    class Server(socketserver.UnixStreamServer):
        def get_request(self):
            request, _ = super().get_request()
            return request, ("rcman", 0)

    folder = Path(tempfile.mkdtemp(prefix="rcman-health."))
    try:
        path = folder / "s"
        server = Server(str(path), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            for url, status in (("/api/health", "200"), ("/other", "404")):
                done = subprocess.run(
                    ["/usr/bin/python3", "-I", "-c", ins.HEALTH_SCRIPT, str(path), url],
                    capture_output=True,
                    text=True,
                )
                assert done.returncode == 0 and done.stdout.strip() == status, done.stderr
        finally:
            server.shutdown()
            server.server_close()
        gone = subprocess.run(
            ["/usr/bin/python3", "-I", "-c", ins.HEALTH_SCRIPT, str(path), "/api/health"],
            capture_output=True,
            text=True,
        )
        assert gone.returncode != 0  # nothing listening: not an answer
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def test_rcmans_podman_socket_is_looked_for_by_rcman(monkeypatch):
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(ins, "app_uid", lambda: 990)
    monkeypatch.setattr(ins, "run", run)
    assert ins.podman_socket_exists(990) is True
    [argv] = seen
    assert argv[:4] == RUNUSER and argv[-3:-1] == ["test", "-S"]
    assert argv[-1] == str(ins.podman_socket(990))


def plant_link(tree: Path) -> Path:
    outside = tree.parent / "outside-the-tree"
    outside.write_text("secret")
    (tree / "assets").mkdir(parents=True, exist_ok=True)
    (tree / "assets" / "index.js").write_text("x")
    link = tree / "assets" / "leak"
    link.symlink_to(outside)
    return link


def test_a_built_tree_with_a_link_in_it_is_refused_and_not_copied(tmp_path):
    """npm and the build run as the clone's owner, and the copy is root's: a link written by
    the build would be followed out of the tree."""
    tree, copy = tmp_path / "dist", tmp_path / "release" / "dist"
    (tree / "assets").mkdir(parents=True)
    (tree / "assets" / "index.js").write_text("x")
    ins.copy_built_tree(tree, copy)  # positive control: a tree with no link is copied
    assert (copy / "assets" / "index.js").read_text() == "x"
    shutil.rmtree(copy)
    link = plant_link(tree)
    assert link.is_symlink()  # the plant took effect
    with pytest.raises(ins.InstallError, match="link"):
        ins.copy_built_tree(tree, copy)
    assert not copy.exists()  # refused before anything was copied


def test_the_release_is_built_with_the_checked_copy_of_the_built_tree():
    source = INSTALLER.read_text(encoding="utf-8")
    assert "shutil.copytree(frontend" not in source
    assert "copy_built_tree(frontend" in source


def test_the_removal_restarts_caddy_after_the_group_is_gone():
    """Caddy keeps the group it started with until it starts again: restarted before
    ``groupdel`` it still holds rcman-web's old id."""
    steps = removal_steps(guide_text())
    [removing] = [n for n, text in steps.items() if "groupdel" in text]
    assert "**restart** Caddy" in steps[removing]
    before = [n for n, text in steps.items() if n < removing and "**restart**" in text]
    assert before == []
    assert "after step" in flat(removal_section(guide_text()))


def test_the_guide_gives_the_commands_that_stop_rcman_and_its_containers():
    guide = flat(guide_text())
    assert "systemctl stop rcman" in guide and "podman stop --all" in guide
    assert "in rcman's interface" not in guide
