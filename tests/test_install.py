"""The installer's decisions: settings, rendered files, releases.

Real installations are tested in the container suite.
"""

import ast
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from installer import install as ins
from tests.hang_guard import HANG_GUARD

REPO = Path(__file__).resolve().parent.parent
PODMAN_SOURCES = REPO / "backend" / "rcman" / "podman"
INSTRUCTION_SOURCES = REPO / "backend" / "rcman" / "instructions"


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


def test_the_installer_runs_as_install_sh_runs_it_and_writes_nothing_into_the_clone(tmp_path):
    """install.sh runs it with the system interpreter in isolated mode, as root, from the clone."""
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


# --- rendered files ----------------------------------------------------------------------

SETTINGS = {
    "DOMAIN": "rcman.example.com",
    "GOOGLE_CLIENT_ID": "client-id",
    "GOOGLE_CLIENT_SECRET": "client-secret",
    "COOKIE_SECRET": "c" * 32,
    "ADMIN_EMAIL": "admin@example.com",
    "PROXY_PORT": "444",
    "ASK_PORT": "445",
    "CADDY_USER": "caddy",
    "CLAUDE_DOWNLOAD": "https://downloads.example.com/releases",
    "WEB_UPGRADE": "no",
    # what the installer finds out and renders along with the settings
    "APP_UID": "990",
    "WORKSPACE_IMAGE": "localhost/rcman-workspace:0123456789ab",
    "BUILDER_IMAGE": "localhost/rcman-builder:0123456789ab",
    "WEB_UPGRADE_ENABLED": "false",
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
    assert "RCMAN_FORWARD_SOCKET=/run/rcman/forward.sock\n" in env


def unknown_settings(rendered: str) -> set[str]:
    """The RCMAN_ variables of a rendered environment that rcman's settings model has not."""
    from rcman.settings import Settings

    keys = {line.split("=", 1)[0] for line in rendered.splitlines() if "=" in line}
    return keys - {f"RCMAN_{name.upper()}" for name in Settings.model_fields}


def test_every_rendered_app_setting_exists_in_the_settings_model():
    rendered = ins.render("rcman.env", SETTINGS)
    assert "RCMAN_PUBLIC_ORIGIN" in rendered  # non-vacuity: the file is read
    assert unknown_settings(rendered) == set()
    assert unknown_settings(rendered + "RCMAN_PLANTED=x\n") == {"RCMAN_PLANTED"}  # the twin


def test_oauth2_proxy_config_allows_only_the_allowlist():
    cfg = ins.render("oauth2-proxy.cfg", SETTINGS)
    assert 'provider = "google"' in cfg
    assert f'authenticated_emails_file = "{ins.STATE}/allowed-emails"' in cfg
    assert 'redirect_url = "https://rcman.example.com/oauth2/callback"' in cfg
    assert 'http_address = "127.0.0.1:444"' in cfg
    assert "email_domains" not in cfg  # any domain would bypass the allowlist
    assert 'cookie_secret = "' + "c" * 32 + '"' in cfg
    assert "set_xauthrequest = true" in cfg
    # a __Host- cookie, which a forwarded port's subdomain can neither set nor replace
    assert 'cookie_name = "__Host-rcman-auth"\ncookie_secure = true\n' in cfg


def test_units_run_as_their_own_users_and_the_app_on_its_socket():
    app = ins.render("rcman.service", SETTINGS)
    proxy = ins.render("rcman-oauth2-proxy.service", SETTINGS)
    assert "User=rcman\n" in app and "User=rcman-proxy\n" in proxy
    exec_start = next(line for line in app.splitlines() if line.startswith("ExecStart="))
    assert exec_start.endswith(" --uds /run/rcman/rcman.sock")
    assert str(ins.SOCKET) == "/run/rcman/rcman.sock"
    assert "--port" not in app and "--host" not in app  # no TCP listener at all
    assert f"EnvironmentFile={ins.ETC}/rcman.env" in app
    assert "NoNewPrivileges=yes\n" in app and "UMask=0077\n" in app
    # rcman reaches its own user manager's Podman socket
    assert "Environment=XDG_RUNTIME_DIR=/run/user/990\n" in app
    wants = next(line for line in app.splitlines() if line.startswith("Wants="))
    assert "user@990.service" in wants.split("=", 1)[1].split()


def test_caddy_snippet_strips_the_identity_header_before_authenticating():
    snippet = ins.render("Caddyfile", SETTINGS)
    assert snippet.startswith("rcman.example.com {")
    signed_in = snippet.rindex("\thandle {\n", 0, snippet.index("forward_auth"))
    strip = snippet.index("request_header -X-Forwarded-Email", signed_in)
    auth = snippet.index("forward_auth 127.0.0.1:444")
    assert strip < auth and "route {" in snippet[signed_in:strip]
    assert "copy_headers X-Auth-Request-Email>X-Forwarded-Email" in snippet
    assert "reverse_proxy unix//run/rcman/rcman.sock\n" in snippet
    # oauth2-proxy's pages, the installed app's files, rcman, the forwarded ports and the ask
    assert snippet.count("reverse_proxy") == 5
    # Back to the page asked for, as a path: oauth2-proxy refuses an absolute rd it is not told
    # to trust, and sends the browser to / instead.
    assert "redir * /oauth2/start?rd={uri}\n" in snippet


def test_the_app_files_reach_rcman_without_sign_in_or_an_identity():
    """Browsers fetch the manifest, and the system its icons, without the sign-in's cookie, so the
    main site sends exactly the files the manifest and index.html name to rcman, ahead of the
    signed-in handle, with no identity; every other path still goes through forward_auth."""
    frontend = REPO / "frontend"
    manifest = json.loads((frontend / "public" / "manifest.webmanifest").read_text())
    index = (frontend / "index.html").read_text()
    linked = re.findall(r'<link rel="(?:icon|apple-touch-icon)"[^>]*href="([^"]+)"', index)
    assert "/favicon.svg" in linked  # the reader finds the links index.html has
    named = sorted({"/manifest.webmanifest", *(i["src"] for i in manifest["icons"]), *linked})
    snippet = ins.render("Caddyfile", SETTINGS)
    site = snippet_site(snippet, "rcman.example.com")
    matcher = re.search(r'\n\t@app_files expression "([^"]+)"\n', site)
    assert matcher, site
    target = "{http.request.orig_uri.path}"
    matched = re.findall(re.escape(target) + r" == '([^']+)'", matcher.group(1))
    assert sorted(matched) == named
    assert matcher.group(1) == " || ".join(f"{target} == '{path}'" for path in matched)
    exempt = site.index("\thandle @app_files {\n")
    signed_in = site.rindex("\thandle {\n")
    assert exempt < signed_in
    block = site[exempt : site.index("\n\t}\n", exempt) + 1]
    strip = block.index("request_header -X-Forwarded-Email\n")
    assert strip < block.index("reverse_proxy unix//run/rcman/rcman.sock\n")
    assert "forward_auth" not in block
    assert "forward_auth 127.0.0.1:444" in site[signed_in:]


def snippet_site(snippet: str, address: str) -> str:
    """The site block of ``snippet`` that begins with ``address``, to its closing brace."""
    text = "\n" + snippet
    start = text.index(f"\n{address} {{\n") + 1
    end = text.index("\n}\n", start)
    return text[start : end + 2]


def test_the_snippet_passes_every_forwarded_port_to_the_forward_socket_without_identity():
    """The forwarded ports' wildcard site: certificates on demand, the sign-in's identity headers
    deleted, the forwarding server's socket as its upstream, and a log of its own that keeps no
    cookie, no referrer, no Location and no query string, in the forms Ubuntu's Caddy loads."""
    snippet = ins.render("Caddyfile", SETTINGS)
    site = snippet_site(snippet, "*.ws.rcman.example.com")
    assert "\ttls {\n\t\ton_demand\n\t}\n" in site
    for header in ("X-Forwarded-Email", "X-Auth-Request-*", "X-Forwarded-User"):
        assert f"\trequest_header -{header}\n" in site, header
    assert "\treverse_proxy unix//run/rcman/forward.sock\n" in site
    assert "rcman.sock" not in site and "forward_auth" not in site
    assert str(ins.FORWARD_SOCKET) == "/run/rcman/forward.sock"
    assert "output file /var/log/caddy/rcman-forward-access.log {" in site
    for option in ("roll_size", "roll_keep", "roll_keep_for"):
        assert f"\t\t\t{option} " in site
    filtered = (
        "\t\tformat filter {\n\t\t\twrap json\n\t\t\tfields {\n"
        "\t\t\t\trequest>headers>Cookie delete\n"
        "\t\t\t\trequest>headers>Referer delete\n"
        "\t\t\t\tresp_headers>Set-Cookie delete\n"
        "\t\t\t\tresp_headers>Location delete\n"
        '\t\t\t\trequest>uri regexp [?].* ""\n'
        "\t\t\t}\n\t\t}\n"
    )
    assert filtered in site


# --- Caddy's configuration ---------------------------------------------------------------

# Ubuntu's own /etc/caddy/Caddyfile, the conffile of its caddy package (2.6.2-14),
# recorded from the package's file, whose md5 is the one `dpkg-query -W -f='${Conffiles}' caddy`
# gives; tests/container/install_checks.py holds the system image's to that md5 as well.
RECORDED_CADDYFILE = REPO / "tests" / "fakes" / "ubuntu-Caddyfile"
UBUNTU_CADDYFILE_MD5 = "8cbf072a3e390217a88c242a7f18ee76"
UBUNTU_CADDYFILE = RECORDED_CADDYFILE.read_text(encoding="utf-8")
ASK = "http://127.0.0.1:445/.rcman/ask"
IMPORT = "import /etc/rcman/Caddyfile.snippet\n"
ON_DEMAND = f"\ton_demand_tls {{\n\t\task {ASK}\n\t}}\n"
GLOBAL = "{\n" + ON_DEMAND + "}\n\n"


def test_the_caddyfile_is_edited_to_import_rcman_and_ask_it_and_then_left_alone():
    snippet = ins.render("Caddyfile", SETTINGS)
    assert ins.ask_url("445") == ASK
    assert hashlib.md5(RECORDED_CADDYFILE.read_bytes()).hexdigest() == UBUNTU_CADDYFILE_MD5
    edited = GLOBAL + UBUNTU_CADDYFILE + "\n" + IMPORT
    actions = ["global options with on_demand_tls added", f"import {ins.CADDY_SNIPPET} added"]
    assert ins.edit_caddyfile(UBUNTU_CADDYFILE, snippet, ASK) == (edited, actions)
    assert ins.edit_caddyfile(edited, snippet, ASK) == (edited, [])  # idempotent


class CaddyMachine:
    """``ins.run`` standing in for systemctl and Caddy: caddy-api.service is neither active nor
    enabled, caddy.service is active once restarted, and ``caddy validate`` passes. It records
    each argv and the account it ran as."""

    def __init__(self):
        self.active, self.calls = False, []

    def __call__(self, argv, user=None, check=True, **kw):
        self.calls.append((list(argv), user))
        code = 0
        if argv[:2] == ["systemctl", "restart"]:
            self.active = True
        elif argv[:2] in (["systemctl", "is-active"], ["systemctl", "is-enabled"]):
            code = 0 if argv[-1] == ins.CADDY_UNIT and self.active else 3
        if check and code:
            raise ins.InstallError(f"{argv} failed")
        return subprocess.CompletedProcess(argv, code, "", "")

    def ran(self, *start: str) -> list:
        return [(argv, user) for argv, user in self.calls if argv[: len(start)] == list(start)]


def test_ensure_caddy_validates_the_edit_as_caddy_puts_it_in_place_and_restarts_caddy(
    tmp_path, monkeypatch
):
    folder, etc = tmp_path / "caddy", tmp_path / "etc"
    folder.mkdir(mode=0o755)
    caddyfile = folder / "Caddyfile"
    caddyfile.write_text(UBUNTU_CADDYFILE)
    caddyfile.chmod(0o644)
    monkeypatch.setattr(ins, "CADDYFILE", caddyfile)
    monkeypatch.setattr(ins, "CADDY_BACKUP", folder / ".Caddyfile.rcman-backup")
    monkeypatch.setattr(ins, "CADDY_SNIPPET", etc / "Caddyfile.snippet")
    monkeypatch.setattr(ins, "CADDY_APPLIED", etc / "caddy.applied")
    caddy = CaddyMachine()
    monkeypatch.setattr(ins, "run", caddy)
    changes = ins.ensure_caddy(SETTINGS, False, owner_uid=os.getuid())
    assert changes == [
        str(ins.CADDY_SNIPPET),
        f"{caddyfile}: global options with on_demand_tls added",
        f"{caddyfile}: import {ins.CADDY_SNIPPET} added",
        "caddy.service restarted",
    ]
    [(validate, user)] = caddy.ran("caddy", "validate")
    candidate = Path(validate[validate.index("--config") + 1])
    assert user == "caddy" and candidate.parent == folder and candidate != caddyfile
    assert caddyfile.read_text() == GLOBAL + UBUNTU_CADDYFILE + f"\nimport {ins.CADDY_SNIPPET}\n"
    assert stat.S_IMODE(caddyfile.stat().st_mode) == 0o644
    assert ins.CADDY_BACKUP.read_text() == UBUNTU_CADDYFILE
    assert ins.CADDY_SNIPPET.read_text() == ins.render("Caddyfile", SETTINGS)
    assert caddy.ran("systemctl", "restart") == [(["systemctl", "restart", "caddy.service"], None)]


def template_mismatch(folder: Path) -> set[str]:
    """The files of ``folder`` that are not TEMPLATES, and the TEMPLATES not in it."""
    return {p.name for p in folder.iterdir()} ^ set(ins.TEMPLATES)


def test_every_template_is_used(tmp_path):
    assert ins.TEMPLATES and template_mismatch(REPO / "installer" / "templates") == set()
    planted = tmp_path / "templates"
    shutil.copytree(REPO / "installer" / "templates", planted)
    (planted / "planted.service").write_text("[Unit]\n")  # a template no one renders
    assert template_mismatch(planted) == {"planted.service"}


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


def test_rollback_swaps_current_and_previous(tmp_path):
    for name in ("a", "b"):
        make_release(tmp_path, name)
    ins.switch(tmp_path, "a")
    ins.switch(tmp_path, "b")
    ins.rollback(tmp_path)
    assert os.readlink(tmp_path / "current") == "releases/a"
    assert os.readlink(tmp_path / "previous") == "releases/b"


# --- the installation guide ------------------------------------------------------------


def guide_mismatch(guide: str) -> set[str]:
    """The options and variables the guide names that the installer has not, and the reverse."""
    options = {f"--{key.lower().replace('_', '-')}" for key in ins.FIELDS} | {"--rollback"}
    variables = {f"RCMAN_INSTALL_{key}" for key in ins.FIELDS}
    named = set(re.findall(r"`(--[a-z-]+)", guide))
    named |= set(re.findall(r"`(RCMAN_INSTALL_[A-Z_]+)`", guide))
    return named ^ (options | variables)


def test_the_guide_documents_every_option_and_setting_and_nothing_else():
    guide = (REPO / "docs" / "installation.md").read_text(encoding="utf-8")
    assert guide_mismatch(guide) == set()
    planted = guide.replace("`RCMAN_INSTALL_DOMAIN`", "`RCMAN_INSTALL_PLANTED`")
    assert planted != guide  # the plant took effect
    assert guide_mismatch(planted) == {"RCMAN_INSTALL_DOMAIN", "RCMAN_INSTALL_PLANTED"}


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
    "PODMAN_SECCOMP": "Podman's own seccomp profile, read to derive the workspaces'",
    "CGROUP_ROOT": "the kernel's, read",
    "LINGER": "systemd's folder; rcman's entry in it is LINGER / APP_USER, listed below",
    "SYSTEMD": "systemd's folder; the files made in it are the units and the drop-in",
    "COMMAND_DIR": "the system's folder; the files made in it are COMMANDS",
    "DATABASE": "rcman's, in its home; the installer asks rcman for its schema version",
    "UPGRADE_SOURCE": "in the clone; the wrapper is installed from it",
    "CADDYFILE": "the operator's Caddy configuration, which the installer edits and does not make",
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
    for name, units in namespace.items():  # UNITS, and the units kept out of it
        if name.isupper() and name.endswith("UNITS"):
            for unit in units:
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
    for name, reason in NOT_MADE.items():  # each exemption still names a constant, with a reason
        assert name in vars(ins) and reason, name


def test_the_removal_guard_fails_on_a_name_taken_out_of_the_section():
    guide = (REPO / "docs" / "installation.md").read_text(encoding="utf-8")
    for text in ("/etc/tmpfiles.d/rcman.conf", ins.APP_USER, ins.UNITS[0], ins.PACKAGES[0]):
        assert text in removal_section(guide)
        body = guide.index(REMOVING) + len(REMOVING)  # the heading itself stays
        planted = guide[:body] + guide[body:].replace(text, "x")
        assert planted != guide  # the plant took effect
        assert missing_from_removal(planted, vars(ins)) != [], text


# --- rootless Podman for rcman -----------------------------------------------------------


def test_a_new_subordinate_range_starts_after_every_range_there():
    from rcman import people
    from rcman.runtime import RANGE

    # a range for every person's slot and the builder's, which no person has
    assert people.BUILDER_SLOT > people.MAX_PEOPLE
    assert ins.SUBORDINATE_COUNT >= (people.BUILDER_SLOT + 1) * RANGE and ins.RANGE == RANGE
    lines = ["dev:100000:65536", "other:9000000:65536"]
    assert ins.subordinate_start(lines, "rcman", ins.SUBORDINATE_COUNT) == 9065536


def static_elf() -> bytes:
    """The head of a static 64-bit ELF program: its header and one loaded segment's."""
    import struct

    header = b"\x7fELF" + bytes([2, 1, 1]) + bytes(9)
    header += struct.pack("<HHIQQQIHHHHHH", 2, 62, 1, 0, 64, 0, 0, 64, 56, 1, 0, 0, 0)
    return header + struct.pack("<II", 1, 5) + bytes(48)  # PT_LOAD


class ToolsHost:
    """What ensure_tools needs of the machine: a root-owned tools folder, a busybox and
    terminal descriptions of the test's, and rcman's Podman, which builds tmux by handing its
    program over on standard output."""

    def __init__(self, tmp_path, monkeypatch):
        self.tools = tmp_path / "opt" / "tools"
        self.state = tmp_path / "state"
        self.calls: list[list[str]] = []
        self.built = static_elf()
        # what the container says of the program it wrote: the build's noise and a line with
        # the program's sha256
        self.stderr = f"compiling...\nsha256 {hashlib.sha256(self.built).hexdigest()}\n".encode()
        busybox = tmp_path / "busybox"
        busybox.write_bytes(static_elf() + b"busybox")
        terminfo = tmp_path / "terminfo"
        for entry in ins.TERMINFO:
            (terminfo / entry).parent.mkdir(parents=True, exist_ok=True)
            (terminfo / entry).write_bytes(entry.encode())
        self.conf = tmp_path / "tmux.conf"
        self.conf.write_text("set -g mouse on\n")
        self.podman = PODMAN_SOURCES
        self.fragments = INSTRUCTION_SOURCES
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
    changes = ins.ensure_tools(
        host.tools, conf=host.conf, podman=host.podman, fragments=host.fragments
    )
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
    host.calls.clear()
    again = ins.ensure_tools(
        host.tools, conf=host.conf, podman=host.podman, fragments=host.fragments
    )
    assert again == [] and host.calls == []  # not built again


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


def test_the_archives_of_node_uv_and_oauth2_proxy_are_pinned_with_their_versions():
    from helpers import claude_release

    architectures = set(claude_release.ARCHITECTURES.values())
    assert architectures == {"x64", "arm64"}  # non-vacuity: the two the installer downloads for
    assert pin_problems(ins.UV_VERSION, ins.UV_SHA256, architectures) == []
    assert pin_problems(ins.OAUTH2_PROXY_VERSION, ins.OAUTH2_PROXY_SHA256, architectures) == []
    assert pin_problems(ins.node_version(), ins.NODE_SHA256, architectures) == []


def test_a_version_changed_without_its_checksums_is_found():
    architectures = {"x64", "arm64"}
    good = {("1.0", "x64"): "a" * 64, ("1.0", "arm64"): "b" * 64}
    assert pin_problems("1.0", good, architectures) == []
    assert pin_problems("1.1", good, architectures) != []  # the version moved on, the pins did not
    assert pin_problems("1.0", {("1.0", "x64"): "a" * 64}, architectures) != []  # an arch missing
    assert pin_problems("1.1", {**good, ("1.1", "x64"): "a" * 64}, {"x64"}) != []  # a stale entry
    assert pin_problems("1.0", {**good, ("1.0", "x64"): "xyz"}, architectures) != []  # not a hash


# A base image the installer builds from: a registry name, a tag and a digest.
PINNED_IMAGE = re.compile(r"[a-z0-9.-]+(?::\d+)?(?:/[a-z0-9._-]+)+:[\w.-]+@sha256:[0-9a-f]{64}")


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
    assert PINNED_IMAGE.fullmatch(ins.TMUX_BUILD_REFERENCE)  # tmux's, named in the installer


def test_a_base_image_without_a_tag_and_a_digest_is_found_unpinned(tmp_path):
    planted = tmp_path / "Containerfile"
    planted.write_text("FROM docker.io/library/ubuntu:26.04\nRUN true\n")
    assert unpinned_bases([planted]) != []
    planted.write_text("FROM docker.io/library/ubuntu:26.04@sha256:" + "a" * 64 + "\n")
    assert unpinned_bases([planted]) == []  # the same file, pinned: the guard can pass


def from_lines(containerfile: Path) -> list[str]:
    return [
        line.split()[1]
        for line in containerfile.read_text().splitlines()
        if line.startswith("FROM ")
    ]


# --- the default image and the devcontainer builder image -----------------------------------


class FakePodman:
    """rcman's Podman as the installer drives it: images by name, and every call made."""

    def __init__(self, images):
        self.images, self.calls = list(images), []

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
            self.images.remove(args[1])
        elif args[0] == "build":
            self.images.append(args[args.index("--tag") + 1])
        return subprocess.CompletedProcess(argv, code, stdout=out, stderr="")


def use_fake_podman(monkeypatch, fake):
    monkeypatch.setattr(ins, "app_uid", lambda: 990)
    monkeypatch.setattr(ins, "as_rcman_podman", fake.argv)
    monkeypatch.setattr(ins.subprocess, "run", fake.run)
    monkeypatch.setattr(ins, "run", fake.run)


# An apt-get install command's words, up to the end of its command, its continued lines with it.
APT_INSTALL = re.compile(r"apt-get\s+install\s+((?:\\\n|[^&;|\n])*)")


def package_lines(text: str) -> list[str]:
    """The lines of the apt-get install commands of a Containerfile's ``text`` that ask for a
    package, and not only options, as they are written but for the continuation."""
    return [
        line
        for command in APT_INSTALL.findall(text)
        for line in command.split("\\\n")
        if any(not word.startswith("-") for word in line.split())
    ]


def test_the_containerfile_keeps_its_packages_on_one_line():
    """One line to read and to change: a package added on a line of its own is a second line."""
    text = ins.CONTAINERFILE.read_text()
    lines = package_lines(text)
    assert len(lines) == 1 and "git" in lines[0].split(), lines  # the line is the packages'
    planted = text.replace(" gh ", " gh \\\n      ", 1)  # the twin: a package moved down
    assert planted != text and len(package_lines(planted)) == 2


def test_both_images_are_built_without_keeping_step_images(monkeypatch):
    """A plain build keeps an image of each step, which nothing names; neither build keeps
    them, and nothing is removed after a build, which would race the running service."""
    fake = FakePodman([])
    use_fake_podman(monkeypatch, fake)
    ins.ensure_image()
    ins.ensure_devcontainer_builder()
    builds = [c for c in fake.calls if c[1] == "build"]
    assert [b[b.index("--tag") + 1] for b in builds] == [
        ins.workspace_image(),
        ins.devcontainer_builder_image(),
    ]
    for build in builds:
        assert build[1:] == ins.build_argv(build[build.index("--tag") + 1], Path(build[-1]))
        assert "--layers=false" in build
    # Nothing is removed (an older builder image goes in its own step, which
    # test_older_builder_images_are_pruned_by_their_own_step_and_others_left_alone shows).
    assert [c for c in fake.calls if c[1] in ("rmi", "prune") or c[1:3] == ["image", "rm"]] == []


def test_older_builder_images_are_pruned_by_their_own_step_and_others_left_alone(monkeypatch):
    image = ins.devcontainer_builder_image()
    old = "localhost/rcman-builder:aaaaaaaaaaaa"
    other = "localhost/rcman-workspace:bbbbbbbbbbbb"
    fake = FakePodman([old, other, image])
    use_fake_podman(monkeypatch, fake)
    ins.prune_devcontainer_builder_images(990, image)
    assert fake.images == [other, image]  # the old builder gone, others left alone


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
    as rcman is run for real as the test's user, so that what it writes is there to read."""
    real = ins.run

    def run(argv, user=None, **kw):
        done = as_rcman_for_real(argv, lambda rest: real(rest, **kw))
        return done if done is not None else real(argv, user=user, **kw)

    monkeypatch.setattr(ins, "run", run)
    monkeypatch.setattr(ins, "app_uid", lambda: 990)


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
    # the version the installer puts there is the tested one
    assert ins.claude_version() == (REPO / "helpers" / "claude-version").read_text().strip()


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


def imports_urllib(source: str) -> bool:
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
    """One piece of code fetches releases, helpers/claude_release.py, rcman's as well."""
    assert not imports_urllib((REPO / "installer" / "install.py").read_text(encoding="utf-8"))
    assert imports_urllib("from urllib import request\n")  # the guard fires on a plant


def test_the_first_administrator_is_added_by_rcman_admin_as_rcman(monkeypatch):
    seen = []

    def run(argv, **kw):
        seen.append((argv, kw))
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(ins, "app_uid", lambda: 990)
    monkeypatch.setattr(ins, "run", run)
    added = ins.ensure_admin({"ADMIN_EMAIL": "Admin@Example.com"})
    assert added == ["administrator admin@example.com"]
    assert len(seen) == 2  # the list, then the addition, as rcman from a neutral folder
    assert all(argv[:4] == RUNUSER and kw.get("cwd") == "/" for argv, kw in seen)
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
    is run, answers where rcman's user manager's control group is, and has every controller
    there, on cgroup v2. A command run as rcman is run for real as the test's user, so that what
    it writes is there to read; a path of the state folder changed by path is refused
    (``chowned``)."""

    def __init__(self, tmp_path, monkeypatch):
        self.calls: list[list[str]] = []
        self.chowned: list[Path] = []
        self.systemd = tmp_path / "systemd"
        self.state = tmp_path / "state"
        self.cgroups = tmp_path / "cgroup"
        self.group = "/user.slice/user-990.slice/user@990.service"
        self.real_run = ins.run
        controllers = "cpuset cpu io memory pids\n"
        (self.cgroups / self.group.lstrip("/")).mkdir(parents=True)
        (self.cgroups / "cgroup.controllers").write_text(controllers)
        (self.cgroups / self.group.lstrip("/") / "cgroup.controllers").write_text(controllers)
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


def test_the_guard_refuses_a_command_run_as_rcman_that_opens_a_descriptor_by_its_path():
    """The planted twin of what failed in a real install: ``install -D /dev/stdin`` as rcman."""
    planted = [*RUNUSER, "/usr/bin/env", "-i", "install", "-D", "-m", "644", "/dev/stdin", "/x/y"]
    with pytest.raises(AssertionError):
        as_rcman_for_real(planted, lambda rest: pytest.fail("run"))
    assert not opens_a_descriptor_by_path(["install", "/dev/null"])


def test_no_installer_command_opens_a_descriptor_by_its_path():
    """Nothing in the installer names such a path, whichever helper builds the command."""
    for node in ast.walk(ast.parse(INSTALLER.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert not DESCRIPTOR_PATHS.match(node.value), node.lineno
            for text in ("/dev/stdin", "/dev/fd/", "/proc/self/fd"):
                assert text not in node.value, node.lineno


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
    [reexec] = manager.reexecutions()  # the user manager reads its new default, as rcman
    assert reexec[:4] == RUNUSER and reexec[-3:] == ["systemctl", "--user", "daemon-reexec"]
    assert manager.chowned == []  # root changed nothing in rcman's home by path
    in_the_home = [c for c in manager.calls if any(str(manager.state) in a for a in c)]
    assert in_the_home and all(call[:4] == RUNUSER for call in in_the_home)


# --- running it again: nothing is reported changed ---------------------------------------


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


# --- rcman's own files are made by rcman --------------------------------------------------


def string_constants(source: str) -> list[str]:
    return [n.value for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Constant)
            if isinstance(n.value, str)]  # fmt: skip


def test_the_installer_names_the_commands_that_change_accounts_and_run_as_one_in_one_place():
    source = INSTALLER.read_text(encoding="utf-8")
    constants = string_constants(source)
    assert constants.count("useradd") == 1 and constants.count("runuser") == 1
    planted = source + '\nSECOND = "runuser"\n'  # the twin: a second place in it names it
    assert string_constants(planted).count("runuser") == 2


def test_the_allowed_list_is_made_by_rcman_and_only_when_it_is_not_there(tmp_path, monkeypatch):
    state, etc = tmp_path / "state", tmp_path / "etc"
    state.mkdir()
    forbidden: list = []
    forbid_path_based_changes(monkeypatch, forbidden, state)
    monkeypatch.setattr(ins, "ETC", etc)
    monkeypatch.setattr(ins, "SYSTEMD", tmp_path / "systemd")
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


# --- rolling back to a release the database is newer than ----------------------------------


def previous_release(root: Path, migrations: int) -> None:
    release = root / "releases" / "old"
    (release / "backend" / "rcman").mkdir(parents=True)
    (release / "backend" / "rcman" / "db.py").write_text(f"MIGRATIONS = {['m'] * migrations!r}\n")
    (root / "previous").symlink_to("releases/old")


def test_a_rollback_to_a_release_older_than_the_database_is_refused_in_words(tmp_path, monkeypatch):
    previous_release(tmp_path, migrations=2)
    monkeypatch.setattr(ins, "database_schema_version", lambda database=ins.DATABASE: 2)
    ins.check_rollback(tmp_path)  # the positive: a release that reads the database goes ahead
    monkeypatch.setattr(ins, "database_schema_version", lambda database=ins.DATABASE: 3)
    with pytest.raises(ins.InstallError) as refused:
        ins.check_rollback(tmp_path)
    said = str(refused.value)
    assert "3" in said and "2" in said and "database" in said
    assert "install.sh" in said  # the way on is to install a newer release, not back


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


def test_root_acts_on_nothing_in_its_reach_that_rcman_can_write_by_its_path_in_a_function():
    findings, _, writers = installer_findings()
    assert findings == []
    assert {"write_if_changed", "_install_file", "_set_link"} <= writers  # the guard sees them


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


# --- Podman in a workspace: the wrappers, their configuration and the seccomp profile ---------
# Podman's default seccomp profile as recorded, a fixed recording (tests/test_seccomp.py).
RECORDED_SECCOMP = REPO / "tests" / "fakes" / "podman-seccomp.json"


@pytest.fixture(autouse=True)
def recorded_seccomp(monkeypatch):
    """The installer reads Podman's profile as recorded, not the machine's."""
    monkeypatch.setattr(ins, "PODMAN_SECCOMP", RECORDED_SECCOMP)


def test_the_podman_tools_are_written_root_readable_and_a_second_run_changes_nothing(tmp_path):
    tools = tmp_path / "tools"
    (tools / "bin").mkdir(parents=True)
    changes = ins.ensure_podman_tools(tools, PODMAN_SOURCES)
    folder = tools / ins.PODMAN_TOOLS
    wrapper = folder / "bin" / "podman"
    assert wrapper.read_bytes() == (PODMAN_SOURCES / "podman").read_bytes()
    assert stat.S_IMODE(wrapper.stat().st_mode) == 0o755
    assert os.readlink(folder / "bin" / "docker") == "podman"
    for name in ins.PODMAN_CONFIGURATION:
        assert (folder / name).read_text() == (PODMAN_SOURCES / name).read_text()
        assert stat.S_IMODE((folder / name).stat().st_mode) == 0o644
    assert set(ins.PODMAN_CONFIGURATION) == {"containers.conf", "storage.conf", "registries.conf"}
    # what the wrapper sources from beside its folder, $HERE, and runs nothing of on its own
    sourced = folder / ins.PODMAN_PUBLISH
    assert sourced.read_bytes() == (PODMAN_SOURCES / ins.PODMAN_PUBLISH).read_bytes()
    assert stat.S_IMODE(sourced.stat().st_mode) == 0o644
    seccomp = tools / ins.WORKSPACE_SECCOMP
    from helpers import seccomp as profiles

    assert seccomp.read_text() == profiles.workspace_profile(RECORDED_SECCOMP.read_text())
    assert stat.S_IMODE(seccomp.stat().st_mode) == 0o644
    for path in (folder, folder / "bin"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert changes  # positive control for the second run
    assert ins.ensure_podman_tools(tools, PODMAN_SOURCES) == []
    from rcman import runtime as rt

    assert ins.WORKSPACE_SECCOMP == rt.SECCOMP  # where the runtime looks for it


def extra_commands(tools: Path) -> set[str]:
    """What the Podman tools' bin folder holds that is not one of PODMAN_COMMANDS: everything
    there comes before the image's own programs on every workspace's PATH, git and Claude's
    among them."""
    return {p.name for p in (tools / ins.PODMAN_TOOLS / "bin").iterdir()} - set(ins.PODMAN_COMMANDS)


def test_the_wrappers_folder_holds_only_the_podman_commands(tmp_path):
    tools = tmp_path / "tools"
    (tools / "bin").mkdir(parents=True)
    ins.ensure_podman_tools(tools, PODMAN_SOURCES)
    # derived from the sources: their programs, and docker for podman
    programs = {p.name for p in PODMAN_SOURCES.iterdir() if os.access(p, os.X_OK)}
    assert set(ins.PODMAN_COMMANDS) == programs | {"docker"} == {"podman", "docker"}
    assert {p.name for p in (tools / ins.PODMAN_TOOLS / "bin").iterdir()} == set(
        ins.PODMAN_COMMANDS
    )
    assert extra_commands(tools) == set()


def test_the_wrappers_folder_guard_finds_a_planted_command(tmp_path):
    tools = tmp_path / "tools"
    (tools / "bin").mkdir(parents=True)
    ins.ensure_podman_tools(tools, PODMAN_SOURCES)
    (tools / ins.PODMAN_TOOLS / "bin" / "git").write_text("#!/bin/sh\n")  # the plant
    assert extra_commands(tools) == {"git"}


# --- what Claude is told in a workspace -------------------------------------------------------


def test_the_instructions_are_written_each_of_its_fragments_in_order_and_root_readable(tmp_path):
    from helpers import instructions as told

    tools = tmp_path / "tools"
    changes = ins.ensure_instructions(tools, INSTRUCTION_SOURCES)
    assert told.FILES == {
        told.OWN_IMAGE: (told.COMMON,),
        told.DEFAULT_IMAGE: (told.COMMON, told.GH),
        told.DEFAULT_TOOLS: (told.COMMON, told.GH, told.TOOLS),
    }
    folder = tools / told.FOLDER
    assert {p.name for p in folder.iterdir()} == set(told.FILES)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o755
    fragments = {p.name: p.read_text() for p in INSTRUCTION_SOURCES.iterdir()}
    assert set(fragments) == {told.COMMON, told.GH, told.TOOLS}
    for name, parts in told.FILES.items():
        written = (folder / name).read_text()
        assert written == "\n".join(fragments[part] for part in parts), name
        assert stat.S_IMODE((folder / name).stat().st_mode) == 0o644
    assert fragments[told.COMMON].startswith("# This workspace\n")
    assert fragments[told.GH].startswith("## GitHub\n")
    assert fragments[told.TOOLS].startswith("## Tools\n")
    assert changes  # positive control for the second run
    assert ins.ensure_instructions(tools, INSTRUCTION_SOURCES) == []
    assert not [p for p in folder.iterdir() if p.name.startswith(".")]  # nothing left staged


# --- upgrading from the web interface ------------------------------------------------------

from installer import upgrade  # noqa: E402 - the wrapper the upgrade unit runs

UPGRADE_SETTINGS = {**SETTINGS, "WEB_UPGRADE": "yes", "WEB_UPGRADE_ENABLED": "true"}
ME = __import__("pwd").getpwuid(os.getuid()).pw_name  # the test's own account


def unit_keys(text: str) -> dict[str, list[str]]:
    """A unit file's keys and their values, in order, as systemd reads them."""
    found: dict[str, list[str]] = {}
    for line in text.splitlines():
        if "=" in line and not line.lstrip().startswith(("#", "[")):
            key, _, value = line.partition("=")
            found.setdefault(key.strip(), []).append(value.strip())
    return found


def unit_environment(values: list[str]) -> dict[str, str]:
    """The variables of a unit's Environment= lines, unquoted as systemd unquotes them."""
    return dict(item.split("=", 1) for value in values for item in shlex.split(value))


def answer(key, question, secret):
    """What someone at the terminal answers, a valid value for every setting asked."""
    assert key != "WEB_UPGRADE"  # never asked: it has a default
    return {"DOMAIN": "rcman.example.com", "ADMIN_EMAIL": "x@example.com"}.get(key, "value")


def test_web_upgrade_is_off_unless_asked_for_and_is_saved(tmp_path):
    saved = tmp_path / "install.env"
    values = ins.gather(saved, {}, {}, ask=answer)
    assert values["WEB_UPGRADE"] == "no"  # off by default, and never asked
    turned_on = ins.gather(saved, {}, {"web_upgrade": "yes"}, ask=answer)
    assert turned_on["WEB_UPGRADE"] == "yes"
    ins.save_settings(saved, turned_on)
    assert "WEB_UPGRADE=yes\n" in saved.read_text()
    again = ins.gather(saved, {}, {}, ask=None)
    assert again["WEB_UPGRADE"] == "yes"  # the saved answer is kept


def test_the_upgrade_service_runs_the_wrapper_as_root_with_a_fixed_argv_and_environment():
    text = ins.render("rcman-upgrade.service", UPGRADE_SETTINGS)
    keys = unit_keys(text)
    assert keys["Type"] == ["oneshot"]
    assert "User" not in keys and "Group" not in keys  # root: see test_no_privilege_boundary
    run = f"/usr/bin/python3 -I {ins.UPGRADE_WRAPPER}"
    assert keys["ExecStart"] == [f"{run} --clone {ins.REPO} --caddy-user caddy"]
    assert keys["ExecStopPost"] == [f"{run} --finish"]
    assert str(ins.UPGRADE_WRAPPER) == "/opt/rcman/bin/rcman-upgrade"
    assert unit_environment(keys["Environment"]) == upgrade.ENVIRONMENT
    [timeout] = keys["TimeoutStartSec"]
    assert re.fullmatch(r"[1-9][0-9]*(min|h|s)?", timeout), timeout  # finite: not infinity
    assert "[Install]" not in text  # started by its path unit alone
    assert "@" not in text.replace("@APP_UID@", "")  # every placeholder filled


class Systemd:
    """Stands in for systemctl, for the upgrade units: records what is asked, in folders of the
    test's for systemd's units, the wrapper and its status folder."""

    def __init__(self, tmp_path, monkeypatch):
        self.calls: list[list[str]] = []
        self.systemd = tmp_path / "systemd"
        self.wrapper = tmp_path / "opt" / "bin" / "rcman-upgrade"
        self.made: list[Path] = []
        monkeypatch.setattr(ins, "SYSTEMD", self.systemd)
        monkeypatch.setattr(ins, "UPGRADE_WRAPPER", self.wrapper)
        monkeypatch.setattr(ins, "_gid", lambda group: os.getgid())
        monkeypatch.setattr(ins, "run", self.run)
        monkeypatch.setattr(
            ins, "ensure_upgrade_dir", lambda *a: self.made.append(a) or not self.made[:-1]
        )

    def run(self, argv, user=None, **kw):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def units(self) -> list[str]:
        return sorted(p.name for p in self.systemd.iterdir()) if self.systemd.exists() else []


def test_turned_on_the_path_unit_is_enabled_and_the_service_is_never_started(tmp_path, monkeypatch):
    systemd = Systemd(tmp_path, monkeypatch)
    changes = ins.ensure_web_upgrade(UPGRADE_SETTINGS)
    assert systemd.units() == sorted(ins.UPGRADE_UNITS)
    for unit in ins.UPGRADE_UNITS:
        path = systemd.systemd / unit
        assert path.read_text() == ins.render(unit, UPGRADE_SETTINGS)
        assert stat.S_IMODE(path.stat().st_mode) == 0o644
        assert str(path) in changes
    assert systemd.wrapper.read_bytes() == ins.UPGRADE_SOURCE.read_bytes()
    assert stat.S_IMODE(systemd.wrapper.stat().st_mode) == 0o755
    assert systemd.made == [(ins.UPGRADE_DIR, 0, os.getgid())]
    assert systemd.calls == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "rcman-upgrade.path"],
        ["systemctl", "restart", "rcman-upgrade.path"],
    ]


def test_turned_off_the_units_are_disabled_and_removed_with_the_wrapper(tmp_path, monkeypatch):
    monkeypatch.delenv(upgrade.SERVICE_MARK, raising=False)  # not the upgrade service's own run
    systemd = Systemd(tmp_path, monkeypatch)
    ins.ensure_web_upgrade(UPGRADE_SETTINGS)
    assert systemd.units() and systemd.wrapper.exists()  # positive: there to be removed
    systemd.calls.clear()
    changes = ins.ensure_web_upgrade(SETTINGS)
    assert systemd.units() == [] and not systemd.wrapper.exists()
    assert systemd.calls == [
        ["systemctl", "disable", "--now", *ins.UPGRADE_UNITS],
        ["systemctl", "daemon-reload"],
    ]
    assert str(systemd.wrapper) in changes


# turning it on: the clone's checks and its origin


class TurnedOn:
    """Stands in for the checks of the clone and for git, for check_web_upgrade."""

    def __init__(self, monkeypatch):
        self.checked: list[tuple] = []
        self.calls: list[tuple[list[str], dict]] = []
        monkeypatch.setattr(upgrade, "check_clone", self.check_clone)
        monkeypatch.setattr(ins, "run", self.run)

    def check_clone(self, clone, sudo_user, refused):
        self.checked.append((clone, sudo_user, tuple(refused)))
        return ME

    def run(self, argv, user=None, check=True, **kw):
        self.calls.append((argv, user, kw))
        return subprocess.CompletedProcess(argv, 0, "abc\trefs/heads/main\n", "")


def test_turning_it_on_checks_the_clone_and_asks_origin_for_main_as_the_owner(monkeypatch):
    on = TurnedOn(monkeypatch)
    monkeypatch.setenv("SUDO_USER", ME)
    ins.check_web_upgrade(UPGRADE_SETTINGS, ME)
    assert on.checked == [(ins.REPO, ME, (ins.APP_USER, ins.PROXY_USER, "caddy"))]
    [(argv, user, kw)] = on.calls
    assert user == ME  # run as the owner (ins.as_account)
    assert argv[:2] == ["/usr/bin/env", "-i"]
    assert argv[-5:] == ["-C", str(ins.REPO), "ls-remote", "origin", "main"]
    for key, value in upgrade.GIT_ENVIRONMENT.items():
        assert f"{key}={value}" in argv, key
    assert kw["stdin"] == subprocess.DEVNULL and kw["start_new_session"] is True  # no terminal
    assert kw["timeout"] > 0


def test_the_steps_of_web_upgrade_come_where_they_must():
    import inspect

    source = inspect.getsource(ins.main)

    def at(name):
        assert source.count(f"{name}(") == 1, name
        return source.index(f"{name}(")

    # the option is refused before it is saved
    assert at("clone_owner") < at("check_web_upgrade") < at("save_settings")
    # the units after the configuration, before the services start
    start = source.index("start_services(restart=bool(changes))")
    assert at("configure") < at("ensure_web_upgrade") < start


def test_a_successful_run_prints_exactly_the_summary_line(monkeypatch, capsys):
    """What a run did is the summary line, and nothing else is printed: no instructions."""
    monkeypatch.setattr(ins.os, "geteuid", lambda: 0)
    monkeypatch.setattr(ins.os, "chdir", lambda path: None)
    monkeypatch.setattr(ins, "run", lambda *a, **kw: pytest.fail(f"ran {a}"))
    monkeypatch.setattr(ins.subprocess, "run", lambda *a, **kw: pytest.fail(f"ran {a}"))
    monkeypatch.setattr(upgrade, "clone_owner", lambda clone, sudo_user: ME)
    monkeypatch.setattr(ins, "gather", lambda *a: dict(SETTINGS))
    monkeypatch.setattr(ins, "save_settings", lambda path, settings: False)
    monkeypatch.setattr(ins, "build_release", lambda repo, owner: ("abc", True))
    monkeypatch.setattr(ins, "switch", lambda root, release: False)
    monkeypatch.setattr(ins, "found_out", lambda settings: {})
    monkeypatch.setattr(ins, "app_uid", lambda: 990)
    monkeypatch.setattr(ins, "devcontainer_builder_image", lambda: "builder")
    caddy = [f"{ins.CADDYFILE}: import {ins.CADDY_SNIPPET} added", "caddy.service restarted"]
    monkeypatch.setattr(ins, "ensure_caddy", lambda settings, joined: caddy)
    for name in ("check_kernel", "check_lock", "check_web_upgrade", "check_caddy_user",
                 "check_containers_stopped_before_new_ranges", "start_services", "health",
                 "prune_devcontainer_builder_images"):  # fmt: skip
        monkeypatch.setattr(ins, name, lambda *a, **kw: None)
    for name in ("ensure_packages", "ensure_accounts", "ensure_subordinate_ids", "ensure_podman",
                 "ensure_oci_runtime", "ensure_limits", "ensure_runtime_dir", "ensure_toolchain",
                 "ensure_tools", "ensure_image", "ensure_devcontainer_builder",
                 "ensure_claude_store", "configure", "ensure_web_upgrade", "link_commands",
                 "ensure_admin"):  # fmt: skip
        monkeypatch.setattr(ins, name, lambda *a, **kw: [])
    assert ins.main([]) == 0
    printed = capsys.readouterr()
    assert json.loads(printed.out) == {"release": "abc", "changed": ["release abc", *caddy]}
    assert printed.out == ins.summary("abc", ["release abc", *caddy]) + "\n"
    assert printed.err == ""


# the installer's lock


@pytest.fixture
def lock(tmp_path):
    """The lock file, in a folder of its own only its owner can write, held as install.sh
    holds it: opened and locked, its descriptor passed on."""
    folder = tmp_path / "opt"
    folder.mkdir(mode=0o755)
    path = folder / ".install.lock"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX)
    os.set_inheritable(fd, True)
    yield path, fd
    os.close(fd)


def test_the_lock_install_sh_holds_is_taken(lock):
    path, fd = lock
    assert os.get_inheritable(fd)
    ins.check_lock(fd, path, os.getuid())
    assert not os.get_inheritable(fd)  # the installer's children never hold it


# install.sh, run with stand-ins for id and runuser, from a clone of the test's own


RUNUSER_STAND_IN = """#!/bin/bash
# runuser, as the test's own user: says whether fd 9 was handed to it, and runs the command
if [ -e /proc/$$/fd/9 ]; then held=held; else held=closed; fi
printf '%s\\t%s\\n' "$held" "$*" >> "{log}"
while [ "$1" != -- ]; do shift; done
shift
exec "$@"
"""
ID_STAND_IN = """#!/bin/bash
if [ "$*" = -u ]; then echo 0; else exec /usr/bin/id "$@"; fi
"""
# /etc/os-release of the release rcman runs on, and of the one before it, as Ubuntu writes them.
UBUNTU_2604 = """PRETTY_NAME="Ubuntu 26.04.1 LTS"
NAME="Ubuntu"
VERSION_ID="26.04"
VERSION="26.04.1 LTS (Resolute Raccoon)"
VERSION_CODENAME=resolute
ID=ubuntu
ID_LIKE=debian
UBUNTU_CODENAME=resolute
"""
UBUNTU_2510 = """PRETTY_NAME="Ubuntu 25.10"
NAME="Ubuntu"
VERSION_ID="25.10"
VERSION="25.10 (Questing Quokka)"
VERSION_CODENAME=questing
ID=ubuntu
ID_LIKE=debian
UBUNTU_CODENAME=questing
"""
INSTALL_PY_STAND_IN = """import json, os, sys
report = {{"argv": sys.argv[1:], "updated": os.environ.get("RCMAN_INSTALL_UPDATED"),
          "fd": os.environ.get("RCMAN_INSTALL_LOCK_FD")}}
try:
    held = os.fstat(9)
    report["fd9"] = [held.st_dev, held.st_ino]
except OSError:
    report["fd9"] = None
lock = os.stat({lock!r})
report["lock"] = [lock.st_dev, lock.st_ino]
with open({report!r}, "w") as out:
    json.dump(report, out)
"""


class Scratch:
    """A clone of the test's own with install.sh as it is, but for its root and the
    os-release it reads, which is Ubuntu 26.04's until a test writes another, and a stand-in
    for installer/install.py that reports what it was handed."""

    def __init__(self, tmp_path):
        from tests import git_fixtures

        self.git = git_fixtures
        self.root = tmp_path / "opt"
        self.os_release = tmp_path / "os-release"
        self.os_release.write_text(UBUNTU_2604)
        self.lock = self.root / ".install.lock"
        self.report = tmp_path / "report.json"
        self.log = tmp_path / "runuser.log"
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        for name, text in (("runuser", RUNUSER_STAND_IN.format(log=self.log)),
                           ("id", ID_STAND_IN)):  # fmt: skip
            (self.bin / name).write_text(text)
            (self.bin / name).chmod(0o755)
        self.clone = git_fixtures.make_repo(tmp_path / "clone", origin=tmp_path / "origin.git")
        (self.clone / "install.sh").write_text(self.install_sh())
        (self.clone / "install.sh").chmod(0o755)
        stand_in = INSTALL_PY_STAND_IN.format(lock=str(self.lock), report=str(self.report))
        git_fixtures.write(self.clone / "installer" / "install.py", stand_in)
        git_fixtures.fixture_git(self.clone, "add", "-A")
        git_fixtures.fixture_git(self.clone, "commit", "-q", "-m", "install.sh")
        git_fixtures.fixture_git(self.clone, "push", "-q", "origin", "main")

    def install_sh(self) -> str:
        text = (REPO / "install.sh").read_text()
        planted = {
            f"root={ins.ROOT}\n": f"root={self.root}\n",
            "os_release=/etc/os-release\n": f"os_release={self.os_release}\n",
        }
        for line, plant in planted.items():
            assert text.count(line) == 1  # the one place install.sh names it
            text = text.replace(line, plant)
        return text

    def run(self) -> subprocess.CompletedProcess:
        full = {**self.git.fixture_env(self.git.FIXTURE_HOME), "PATH": f"{self.bin}:/usr/bin:/bin"}
        return subprocess.run(
            [str(self.clone / "install.sh")], env=full, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=HANG_GUARD,
        )  # fmt: skip

    def reported(self) -> dict:
        return json.loads(self.report.read_text())

    def runuser_calls(self) -> list[tuple[str, str]]:
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        return [tuple(line.split("\t", 1)) for line in lines]


@pytest.fixture
def scratch(tmp_path):
    return Scratch(tmp_path)


def test_install_sh_takes_the_lock_and_hands_it_to_the_installer_on_fd_9(scratch):
    proc = scratch.run()
    assert proc.returncode == 0, proc.stderr
    report = scratch.reported()
    assert report["fd9"] == report["lock"] and report["fd"] == "9"
    assert stat.S_IMODE(scratch.root.stat().st_mode) == 0o755
    calls = scratch.runuser_calls()  # git, run as the clone's owner, holds no lock
    assert calls and all(held == "closed" and argv.startswith(f"-u {ME} ") for held, argv in calls)


def test_install_sh_refuses_a_machine_that_is_not_ubuntu_26_04(scratch):
    scratch.os_release.write_text(UBUNTU_2510)
    proc = scratch.run()
    assert proc.returncode == 1
    assert proc.stderr == (
        "install.sh: rcman runs on Ubuntu 26.04 only; this machine is Ubuntu 25.10\n"
    )
    assert not scratch.root.exists() and not scratch.report.exists()  # nothing made, nothing run
