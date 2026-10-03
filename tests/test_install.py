"""The installer's decisions: settings, rendered files, releases.

Real installations are tested in the container suite.
"""

import os
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
        sudo_user="dev",
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
    settings = ins.gather(
        saved, environ={"RCMAN_INSTALL_APP_PORT": "9001"}, flags={}, ask=None, sudo_user="dev"
    )
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
    assert ins.gather(saved, environ=env, flags={}, ask=None, sudo_user="dev")["CADDY_USER"] == (
        "caddy"
    )
    flags = {"caddy_user": "www-data"}
    assert ins.gather(saved, environ=env, flags=flags, ask=None, sudo_user="dev")["CADDY_USER"] == (
        "www-data"
    )


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

    settings = ins.gather(tmp_path / "install.env", environ={}, flags={}, ask=ask, sudo_user="dev")
    assert ("GOOGLE_CLIENT_SECRET", True) in asked
    assert ("DOMAIN", False) in asked
    assert settings["ADMIN_LINUX"] == "dev"  # defaults are not asked for


def test_without_a_terminal_every_missing_value_is_named(tmp_path):
    with pytest.raises(ins.InstallError) as err:
        ins.gather(tmp_path / "install.env", environ={}, flags={}, ask=None, sudo_user="dev")
    for key in ("DOMAIN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "ADMIN_EMAIL"):
        assert key in str(err.value)


def test_cookie_secret_is_generated_once_and_kept(tmp_path):
    saved = tmp_path / "install.env"
    env = {
        f"RCMAN_INSTALL_{k}": "x.example.com"
        for k in ("DOMAIN", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")
    }
    env["RCMAN_INSTALL_ADMIN_EMAIL"] = "a@example.com"
    first = ins.gather(saved, environ=env, flags={}, ask=None, sudo_user="dev")
    ins.save_settings(saved, first)
    second = ins.gather(saved, environ=env, flags={}, ask=None, sudo_user="dev")
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
        ("ADMIN_LINUX", "Root!"),
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
        ins.gather(tmp_path / "install.env", environ=env, flags={}, ask=None, sudo_user="dev")


def test_a_value_refused_by_a_helpers_rule_is_explained_by_it(tmp_path):
    from helpers.rules import Rule

    ruled = {key: field[3] for key, field in ins.FIELDS.items() if isinstance(field[3], Rule)}
    assert {"ADMIN_EMAIL", "ADMIN_LINUX", "CADDY_USER"} <= set(ruled)  # non-vacuity
    for key, rule in ruled.items():
        env = {
            "RCMAN_INSTALL_DOMAIN": "x.example.com",
            "RCMAN_INSTALL_GOOGLE_CLIENT_ID": "id",
            "RCMAN_INSTALL_GOOGLE_CLIENT_SECRET": "secret",
            "RCMAN_INSTALL_ADMIN_EMAIL": "a@example.com",
            f"RCMAN_INSTALL_{key}": "Not one!",
        }
        with pytest.raises(ins.InstallError) as error:
            ins.gather(tmp_path / "install.env", environ=env, flags={}, ask=None, sudo_user="dev")
        assert key in str(error.value) and rule.sentence in str(error.value)


def test_the_installer_takes_its_rules_from_the_users_file():
    from tests.test_rc_helper import rule_copy_problems

    source = (REPO / "installer" / "install.py").read_text(encoding="utf-8")
    assert "LINUX_NAME" in source and "EMAIL" in source  # positive control: it uses both
    assert rule_copy_problems(source, ("EMAIL", "LINUX_NAME")) == []


def test_the_installer_rule_check_catches_a_copy():
    """Planted twin: an installer keeping its own copy of either rule is caught."""
    from tests.test_rc_helper import rule_copy_problems

    names = ("EMAIL", "LINUX_NAME")
    right = "import re\nfrom helpers.users_file import EMAIL, LINUX_NAME\n"
    assert rule_copy_problems(right, names) == []
    copy = 'LINUX_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")\n'
    assert rule_copy_problems(right + copy, names) != []
    alone = right.replace(", LINUX_NAME", "")
    assert alone != right and rule_copy_problems(alone + copy, names) != []


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
    "ADMIN_LINUX": "dev",
    "PROXY_PORT": "4180",
    "CADDY_USER": "caddy",
}


def test_app_environment_points_at_the_current_release():
    env = ins.render("rcman.env", SETTINGS)
    assert "RCMAN_PUBLIC_ORIGIN=https://rcman.example.com\n" in env
    assert f"RCMAN_HELPERS_DIR={ins.CURRENT}/helpers\n" in env
    assert f"RCMAN_FRONTEND_DIR={ins.CURRENT}/frontend/dist\n" in env


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
    assert f'authenticated_emails_file = "{ins.ETC}/allowed-emails"' in cfg
    assert 'redirect_url = "https://rcman.example.com/oauth2/callback"' in cfg
    assert 'http_address = "127.0.0.1:4180"' in cfg
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
    assert not any(
        line.startswith("NoNewPrivileges=") for line in app.splitlines()
    )  # sudo needs it


def test_caddy_snippet_strips_the_identity_header_before_authenticating():
    snippet = ins.render("Caddyfile", SETTINGS)
    assert snippet.startswith("rcman.example.com {")
    strip = snippet.index("request_header -X-Forwarded-Email")
    auth = snippet.index("forward_auth 127.0.0.1:4180")
    assert strip < auth and "route {" in snippet[:strip]
    assert "copy_headers X-Auth-Request-Email>X-Forwarded-Email" in snippet
    assert "reverse_proxy unix//run/rcman/rcman.sock\n" in snippet
    assert snippet.count("reverse_proxy") == 2  # oauth2-proxy's pages, and rcman


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


def test_a_caddy_admin_api_on_loopback_is_noticed():
    import socket

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert ins.caddy_admin_exposed(port=port) is True  # positive control
    assert ins.caddy_admin_exposed(port=port) is False
    assert "admin off" in ins.CADDY_ADMIN_WARNING


def test_a_caddy_admin_api_on_ipv6_loopback_is_noticed_too():
    import socket

    with socket.socket(socket.AF_INET6) as listener:
        listener.bind(("::1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        assert ins.caddy_admin_exposed(port=port) is True
    assert ins.caddy_admin_exposed(port=port) is False
    assert "127.0.0.1" in ins.CADDY_ADMIN_WARNING and "[::1]" in ins.CADDY_ADMIN_WARNING


def test_sudoers_render_and_validate(tmp_path):
    text = ins.sudoers()
    path = tmp_path / "sudoers"
    path.write_text(text)
    check = subprocess.run(
        ["/usr/sbin/visudo", "-c", "-f", str(path)], capture_output=True, text=True
    )
    assert check.returncode == 0, check.stdout + check.stderr
    assert f"rcman ALL=(%rcusers) NOPASSWD: {ins.CURRENT}/helpers/rc-helper" in text
    assert f"rcman ALL=(root) NOPASSWD: {ins.CURRENT}/helpers/rc-admin" in text


def test_every_template_is_used():
    templates = {p.name for p in (REPO / "installer" / "templates").iterdir()}
    assert templates == set(ins.TEMPLATES)


# --- releases --------------------------------------------------------------------------


def make_release(root, name):
    path = root / "releases" / name
    (path / "helpers").mkdir(parents=True)
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
        ins.SUDOERS,
        ins.SOCKET,
        ins.TMPFILES,
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


# --- prerequisites ---------------------------------------------------------------------


def test_this_machines_git_is_new_enough():
    ins.check_git()  # positive control for the refusals below


def test_a_git_older_than_rcman_needs_is_refused(monkeypatch):
    monkeypatch.setattr(ins, "GIT_MIN_VERSION", (99, 0))
    with pytest.raises(ins.InstallError, match=r"git .* rcman needs git 99\.0 or later"):
        ins.check_git()


def test_no_git_on_the_system_path_is_refused(monkeypatch, tmp_path):
    from helpers import git_run

    monkeypatch.setattr(git_run, "GIT_SEARCH_PATH", str(tmp_path))
    with pytest.raises(ins.InstallError, match="no git"):
        ins.check_git()


def test_git_is_checked_before_anything_is_changed(monkeypatch):
    import pwd

    ran, real = [], ins.run

    def recording(argv, *args, **kwargs):
        if argv[1:] == ["--version"]:  # asking git its version changes nothing
            return real(argv, *args, **kwargs)
        ran.append(argv)

    monkeypatch.setattr(ins, "run", recording)
    monkeypatch.setattr(ins, "GIT_MIN_VERSION", (99, 0))
    with pytest.raises(ins.InstallError, match="git"):
        ins.ensure_accounts(pwd.getpwuid(os.getuid()).pw_name)
    assert ran == []
