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
    saved.write_text("DOMAIN=saved.example.com\nADMIN_EMAIL=saved@example.com\nAPP_PORT=9000\n")
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
    assert settings["APP_PORT"] == "9000"


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
        ("APP_PORT", "80a"),
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


# --- rendered files ----------------------------------------------------------------------

SETTINGS = {
    "DOMAIN": "rcman.example.com",
    "GOOGLE_CLIENT_ID": "client-id",
    "GOOGLE_CLIENT_SECRET": "client-secret",
    "COOKIE_SECRET": "c" * 32,
    "ADMIN_EMAIL": "admin@example.com",
    "ADMIN_LINUX": "dev",
    "APP_PORT": "8001",
    "PROXY_PORT": "4180",
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


def test_units_run_as_their_own_users_on_loopback():
    app = ins.render("rcman.service", SETTINGS)
    proxy = ins.render("rcman-oauth2-proxy.service", SETTINGS)
    assert "User=rcman\n" in app and "User=rcman-proxy\n" in proxy
    assert "--host 127.0.0.1 --port 8001" in app
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
    assert "reverse_proxy 127.0.0.1:8001" in snippet


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
    for path in (ins.ROOT, ins.ETC / "install.env", ins.CADDY_SNIPPET, ins.STATE, ins.SUDOERS):
        assert str(path) in guide, path
    assert "/oauth2/callback" in guide


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
