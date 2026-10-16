"""The installer's decisions: settings, rendered files, releases.

Real installations are tested in the container suite.
"""

import contextlib
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


def make_release(root, name, workspaces=True):
    """A complete release; with ``workspaces``, one of rcman with workspaces."""
    path = root / "releases" / name
    (path / "backend" / "rcman").mkdir(parents=True)
    if workspaces:
        (path / ins.WORKSPACES_MARK).write_text("")
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
    fetched, started = [], threading.Event()
    tmux = b"tmux source"
    monkeypatch.setattr(ins, "TMUX_SHA256", hashlib.sha256(tmux).hexdigest())

    def download(url: str) -> bytes:
        name = url.rsplit("/", 1)[1]
        fetched.append(url)
        if name == "SHASUMS256.txt":
            if started.is_set():
                started.clear()
            else:
                started.set()
                deadline = time.monotonic() + 1
                while started.is_set() and time.monotonic() < deadline:
                    time.sleep(0.01)
            return "".join(f"{sums[n]}  {n}\n" for n in archives).encode()
        if name == f"tmux-{ins.TMUX_VERSION}.tar.gz":
            return tmux
        for archive_name, digest in sums.items():
            if name in (f"{archive_name}.sha256", f"{archive_name}-sha256sum.txt"):
                return f"{digest}  {archive_name}\n".encode()
        return archives[name]

    monkeypatch.setattr(ins, "_download", download)
    return fetched


def test_the_toolchain_is_fetched_once_and_complete(tmp_path, monkeypatch):
    """Positive control for the fake downloads: one call fills an empty toolchain."""
    fetched = fake_downloads(monkeypatch)
    toolchain = tmp_path / "toolchain"
    assert len(ins.ensure_toolchain(toolchain)) == 4
    for binary in (ins.node_bin(toolchain) / "node", ins.uv_binary(toolchain)):
        assert binary.read_bytes() == b"ok"
    assert ins.oauth2_proxy_binary(toolchain).read_bytes() == b"ok"
    assert len(fetched) == len(set(fetched)) == 7
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
    assert sorted(len(r) for r in results) == [0, 4]
    assert len(fetched) == len(set(fetched)) == 7
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
    argv = ins.tmux_build_argv(Path("/t/tmux.tar.gz"), Path("/out"))
    assert argv[:2] == ["run", "--rm"]
    assert ins.TMUX_BUILD_IMAGE in argv
    assert "--volume=/t/tmux.tar.gz:/tmux.tar.gz:ro" in argv
    assert any("--enable-static" in a for a in argv)


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


def test_the_default_image_is_ubuntu_pinned_by_tag_with_what_rcman_needs():
    containerfile = (REPO / "installer" / "workspace" / "Containerfile").read_text()
    assert containerfile.splitlines()[0] == "FROM docker.io/library/ubuntu:24.04"
    for package in ("git", "ca-certificates", "curl", "python3"):
        assert f" {package}" in containerfile, package
    image = ins.workspace_image()
    assert image.startswith("localhost/rcman-workspace:") and len(image.split(":")[1]) == 12


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


def test_the_builder_image_is_built_with_rcmans_podman_and_older_ones_pruned(monkeypatch):
    image = ins.devcontainer_builder_image()
    old = "localhost/rcman-builder:aaaaaaaaaaaa"
    other = "localhost/rcman-workspace:bbbbbbbbbbbb"
    fake = FakePodman([old, other])
    use_fake_podman(monkeypatch, fake)
    assert ins.ensure_devcontainer_builder() == [f"image {image}"]
    build = next(c for c in fake.calls if c[1] == "build")
    assert build[build.index("--tag") + 1] == image
    assert Path(build[build.index("--file") + 1]).name == "Containerfile"
    assert fake.images == [other, image]  # the old builder gone, others left alone


def test_a_builder_image_that_is_there_is_not_built_again(monkeypatch):
    image = ins.devcontainer_builder_image()
    old = "localhost/rcman-builder:aaaaaaaaaaaa"
    fake = FakePodman([image, old])
    use_fake_podman(monkeypatch, fake)
    assert ins.ensure_devcontainer_builder() == []
    assert "build" not in fake.verbs()
    assert fake.images == [image]  # but the older one is pruned all the same


def test_an_older_builder_image_in_use_does_not_fail_the_install(monkeypatch):
    image = ins.devcontainer_builder_image()
    busy, old = "localhost/rcman-builder:aaaaaaaaaaaa", "localhost/rcman-builder:bbbbbbbbbbbb"
    fake = FakePodman([busy, old], undeletable=[busy])
    use_fake_podman(monkeypatch, fake)
    assert ins.ensure_devcontainer_builder() == [f"image {image}"]
    assert fake.images == [busy, image]  # the one in use stays, the other goes


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


def claude_release(version: str, binary: bytes, checksum: str | None = None) -> dict:
    import hashlib
    import json

    digest = checksum or hashlib.sha256(binary).hexdigest()
    base = "https://downloads.example.com/releases"
    platform = ins.claude_platform()
    manifest = {"version": version, "platforms": {platform: {"checksum": digest}}}
    return {
        f"{base}/{version}/manifest.json": json.dumps(manifest).encode(),
        f"{base}/{version}/{platform}/claude": binary,
    }


def test_the_claude_store_holds_the_version_checked_against_its_release(tmp_path, monkeypatch):
    served = claude_release("2.1.0", b"#!/bin/sh\necho 2.1.0\n")
    fetched = []
    monkeypatch.setattr(ins, "_download", lambda url: (fetched.append(url), served[url])[1])
    store = tmp_path / "claude"
    changed = ins.ensure_claude_store(store, "https://downloads.example.com/releases", "2.1.0")
    assert changed == ["claude 2.1.0"]
    assert (store / "versions" / "2.1.0").read_bytes() == b"#!/bin/sh\necho 2.1.0\n"
    assert os.readlink(store / "current") == "versions/2.1.0"
    assert os.readlink(store / "bin" / "claude") == "../current"
    assert (store / "bin" / "claude").read_text().endswith("echo 2.1.0\n")
    assert stat.S_IMODE((store / "versions" / "2.1.0").stat().st_mode) == 0o755
    fetched.clear()
    assert ins.ensure_claude_store(store, "https://downloads.example.com/releases", "2.1.0") == []
    assert fetched == []  # nothing fetched again


def test_a_claude_that_does_not_match_its_release_is_refused(tmp_path, monkeypatch):
    served = claude_release("2.1.0", b"tampered", checksum="0" * 64)
    monkeypatch.setattr(ins, "_download", lambda url: served[url])
    store = tmp_path / "claude"
    with pytest.raises(ins.InstallError, match="checksum"):
        ins.ensure_claude_store(store, "https://downloads.example.com/releases", "2.1.0")
    assert not (store / "versions" / "2.1.0").exists() and not (store / "current").exists()


def test_the_store_holds_the_tested_version():
    assert ins.claude_version() == (REPO / "helpers" / "claude-version").read_text().strip()


def test_the_first_administrator_is_added_by_rcman_admin_as_rcman():
    argv = ins.admin_argv("Admin@Example.com")
    assert argv[:4] == ["runuser", "-u", ins.APP_USER, "--"]
    assert argv[4] == str(ins.CURRENT / ".venv" / "bin" / "rcman-admin")
    assert argv[5:] == ["--env-file", str(ins.ETC / "rcman.env"), "add", "--admin", "--",
                        "Admin@Example.com"]  # fmt: skip


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


@pytest.mark.parametrize("left", ["sudoers", "users", "database"])
def test_an_install_from_before_workspaces_is_refused(tmp_path, monkeypatch, left):
    import sqlite3

    sudoers, users, state = (
        tmp_path / "sudoers.d-rcman",
        tmp_path / "users.toml",
        tmp_path / "state",
    )
    state.mkdir()
    monkeypatch.setattr(ins, "OLD_INSTALL_FILES", (sudoers, users))
    monkeypatch.setattr(ins, "STATE", state)
    ins.check_not_an_old_install()  # positive control: nothing of one
    if left == "database":
        con = sqlite3.connect(state / "rcman.db")
        con.execute("create table user_settings (email text primary key)")
        con.close()
    else:
        {"sudoers": sudoers, "users": users}[left].write_text("x")
    with pytest.raises(ins.InstallError, match="install rcman afresh"):
        ins.check_not_an_old_install()


def test_a_database_of_workspaces_is_no_old_install(tmp_path, monkeypatch):
    from rcman import db

    monkeypatch.setattr(ins, "OLD_INSTALL_FILES", ())
    monkeypatch.setattr(ins, "STATE", tmp_path)
    db.migrate(tmp_path / "rcman.db")
    ins.check_not_an_old_install()


def test_rollback_refuses_a_release_from_before_workspaces(tmp_path):
    old = make_release(tmp_path, "old", workspaces=False)
    make_release(tmp_path, "new")
    ins.switch(tmp_path, "old")
    ins.switch(tmp_path, "new")
    with pytest.raises(ins.InstallError, match="before workspaces"):
        ins.rollback(tmp_path)
    assert os.readlink(tmp_path / "current") == "releases/new"
    assert old.exists()
