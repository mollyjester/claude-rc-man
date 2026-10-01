"""Installing rcman for real, as root, inside the test container.

``tests/test_container_suite.py`` runs this module in a fresh container of its
own. The working tree at /src becomes a git origin; a user ``dev`` clones it
and runs ``install.sh`` through the real path. The toolchain comes from a
cache the host fills with the installer's own download code. systemctl is the
stand-in in tests/container/fake-systemctl, which starts each service from
the unit file the installer wrote.

The tests share one installation and run in order.
"""

import json
import os
import pwd
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RCMAN_IN_CONTAINER") != "1" or os.geteuid() != 0,
    reason="runs only as root inside the test container",
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

ROOT = Path("/opt/rcman")
ETC = Path("/etc/rcman")
DOMAIN = "rcman.example.com"
SETTINGS = {
    "RCMAN_INSTALL_DOMAIN": DOMAIN,
    "RCMAN_INSTALL_GOOGLE_CLIENT_ID": "test-client-id",
    "RCMAN_INSTALL_GOOGLE_CLIENT_SECRET": "test-client-secret",
    "RCMAN_INSTALL_ADMIN_EMAIL": "dev@example.com",
}


from clone import CLONE, ORIGIN, SRC, git, make_clone, sh  # noqa: E402


def install(*args, env=None):
    full = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "SUDO_USER": "dev",
        "RC_ADMIN_INSTALLER": str(SRC / "tests" / "container" / "fake-installer"),
        **SETTINGS,
        **(env or {}),
    }
    return subprocess.run(
        [str(CLONE / "install.sh"), *args],
        env=full,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=1500,
    )


def changes(proc):
    line = next(line for line in proc.stdout.splitlines() if line.startswith("{"))
    return json.loads(line)


def get(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def commit_in_origin(path, text, message):
    work = Path("/tmp/origin-work")
    if work.exists():
        shutil.rmtree(work)
    sh("git", "clone", "-q", str(ORIGIN), str(work), user="dev")
    target = work / path
    target.write_text(target.read_text() + text)
    git("commit", "-qam", message, cwd=work)
    git("push", "-q", "origin", "main", cwd=work)


@pytest.fixture(scope="module")
def first_install():
    make_clone()
    proc = install()
    assert proc.returncode == 0, proc.stdout + proc.stderr + service_logs()
    return proc


def service_logs():
    """What the services printed, from the systemctl stand-in, for failure messages."""
    logs = (
        sorted(Path("/run/fake-systemd").glob("*.log"))
        if Path("/run/fake-systemd").exists()
        else []
    )
    return "".join(f"\n--- {log.name}\n{log.read_text(errors='replace')[-3000:]}" for log in logs)


# --- the first installation ---------------------------------------------------------


def test_installs_the_clones_commit_as_the_current_release(first_install):
    commit = git("rev-parse", "HEAD", cwd=CLONE)
    assert os.readlink(ROOT / "current") == f"releases/{commit[:12]}"
    release = ROOT / "current"
    assert (release / ".complete").read_text().strip() == commit
    assert (release / "frontend" / "dist" / "index.html").is_file()
    assert (release / ".venv" / "bin" / "uvicorn").is_file()
    assert (release / "helpers" / "rc-helper").is_file()


def test_the_release_holds_only_runtime_files_owned_by_root(first_install):
    release = (ROOT / "current").resolve()
    for absent in (
        "tests",
        "devtools",
        "installer",
        "frontend/src",
        "install.sh",
        "project-knowledge",
    ):
        assert not (release / absent).exists(), absent
    for path in [release, *release.rglob("*")]:
        if path.is_symlink():
            continue
        st = path.lstat()
        assert st.st_uid == 0, path
        assert not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH), path


def test_accounts_exist(first_install):
    assert sh("getent", "group", "rcusers").returncode == 0
    assert sh("id", "-nG", "rcman-proxy").stdout.split().count("rcman") == 1
    assert "rcusers" in sh("id", "-nG", "dev").stdout.split()  # the first administrator, adopted
    state = Path("/var/lib/rcman")
    assert state.stat().st_uid == pwd.getpwnam("rcman").pw_uid
    assert stat.S_IMODE(state.stat().st_mode) == 0o750


def test_settings_are_saved_privately(first_install):
    saved = ETC / "install.env"
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    text = saved.read_text()
    assert f"DOMAIN={DOMAIN}\n" in text and "COOKIE_SECRET=" in text


def test_configuration_files_and_their_owners(first_install):
    import grp

    cfg = ETC / "oauth2-proxy.cfg"
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o640
    assert grp.getgrgid(cfg.stat().st_gid).gr_name == "rcman-proxy"
    env = ETC / "rcman.env"
    assert grp.getgrgid(env.stat().st_gid).gr_name == "rcman"
    assert f"RCMAN_PUBLIC_ORIGIN=https://{DOMAIN}" in env.read_text()
    sudoers = Path("/etc/sudoers.d/rcman")
    assert stat.S_IMODE(sudoers.stat().st_mode) == 0o440
    assert sh("visudo", "-c").returncode == 0
    assert "dev@example.com" in (ETC / "users.toml").read_text()
    assert (ETC / "allowed-emails").read_text() == "dev@example.com\n"


def test_services_answer(first_install):
    assert get("http://127.0.0.1:8001/api/health") == (200, '{"status":"ok"}')
    assert get("http://127.0.0.1:4180/ping")[0] == 200
    status, body = get("http://127.0.0.1:8001/")
    assert status == 200 and '<div id="root">' in body


def test_the_app_reaches_the_helpers_through_sudo(first_install):
    me = {"X-Forwarded-Email": "dev@example.com"}
    status, body = get("http://127.0.0.1:8001/api/me", me)
    assert status == 200 and json.loads(body)["admin"] is True
    status, body = get("http://127.0.0.1:8001/api/folders", me)  # rcman -> sudo -u dev rc-helper
    assert status == 200, body
    # dev's own home: the clone and the projects folder rc-admin created
    assert [e["path"] for e in json.loads(body)["entries"]] == ["projects", "rcman"]
    status, body = get("http://127.0.0.1:8001/api/users", me)  # rcman -> sudo rc-admin
    assert status == 200, body


def test_the_caddy_snippet_is_printed_and_valid(first_install):
    assert "Add this to your Caddyfile" in first_install.stdout
    snippet = (ETC / "Caddyfile.snippet").read_text()
    assert snippet in first_install.stdout
    path = Path("/tmp/Caddyfile")
    path.write_text(snippet)
    check = sh("caddy", "validate", "--adapter", "caddyfile", "--config", str(path), check=False)
    assert check.returncode == 0, check.stdout + check.stderr


# --- running it again -----------------------------------------------------------------


def test_running_again_changes_nothing(first_install):
    release = os.readlink(ROOT / "current")
    proc = install()
    assert proc.returncode == 0, proc.stderr
    assert changes(proc)["changed"] == []
    assert os.readlink(ROOT / "current") == release
    assert get("http://127.0.0.1:8001/api/health")[0] == 200


def test_saved_settings_need_no_answers(first_install):
    proc = install(env={k: "" for k in SETTINGS})
    assert proc.returncode == 0, proc.stderr


def test_a_dirty_clone_is_refused(first_install):
    readme = CLONE / "README.md"
    original = readme.read_text()
    readme.write_text(original + "local change\n")
    try:
        proc = install()
        assert proc.returncode == 1 and "uncommitted" in proc.stderr
    finally:
        readme.write_text(original)


def test_an_update_of_install_sh_restarts_it_and_builds_a_new_release(first_install):
    old = os.readlink(ROOT / "current")
    commit_in_origin("install.sh", "# a change to the installer\n", "change install.sh")
    proc = install()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "install.sh changed; starting it again." in proc.stdout
    head = git("rev-parse", "HEAD", cwd=CLONE)
    assert (
        head == sh("git", "--git-dir", str(ORIGIN), "rev-parse", "main", user="dev").stdout.strip()
    )
    assert os.readlink(ROOT / "current") == f"releases/{head[:12]}"
    assert os.readlink(ROOT / "previous") == old
    assert get("http://127.0.0.1:8001/api/health")[0] == 200


def test_rollback_returns_to_the_previous_release(first_install):
    current, previous = os.readlink(ROOT / "current"), os.readlink(ROOT / "previous")
    proc = subprocess.run([str(CLONE / "install.sh"), "--rollback"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert os.readlink(ROOT / "current") == previous
    assert os.readlink(ROOT / "previous") == current
    deadline = time.monotonic() + 30
    while get("http://127.0.0.1:8001/api/health")[0] != 200 and time.monotonic() < deadline:
        time.sleep(0.5)
    assert get("http://127.0.0.1:8001/api/health")[0] == 200


def test_a_clone_that_cannot_fast_forward_is_refused(first_install):
    commit_in_origin("README.md", "upstream change\n", "upstream")
    readme = CLONE / "README.md"
    readme.write_text(readme.read_text() + "local commit\n")
    git("commit", "-qam", "local", cwd=CLONE)
    proc = install()
    assert proc.returncode == 1 and "fast-forward" in proc.stderr


# --- Caddy in front -------------------------------------------------------------------


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


FAKE_AUTH = """
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/oauth2/auth"):
            if "session=alice" in (self.headers.get("Cookie") or ""):
                self.send_response(202)
                self.send_header("X-Auth-Request-Email", "alice@example.com")
            else:
                self.send_response(401)
            self.end_headers(); return
        self.send_response(200); self.end_headers(); self.wfile.write(b"oauth2-proxy page")
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""
FAKE_APP = """
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(("email=" + str(self.headers.get("X-Forwarded-Email"))).encode())
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""


def test_caddy_lets_only_oauth2_proxy_set_the_identity(tmp_path):
    sys.path.insert(0, str(SRC))
    from installer import install as ins

    auth, app, site = free_port(), free_port(), free_port()
    snippet = ins.render(
        "Caddyfile",
        {"DOMAIN": f"http://127.0.0.1:{site}", "PROXY_PORT": str(auth), "APP_PORT": str(app)},
    )
    config = tmp_path / "Caddyfile"
    config.write_text("{\n\tadmin off\n\tauto_https off\n}\n" + snippet)
    procs = [
        subprocess.Popen([sys.executable, "-c", FAKE_AUTH, str(auth)]),
        subprocess.Popen([sys.executable, "-c", FAKE_APP, str(app)]),
        subprocess.Popen(
            ["caddy", "run", "--adapter", "caddyfile", "--config", str(config)],
            stderr=subprocess.DEVNULL,
        ),
    ]
    try:
        base = f"http://127.0.0.1:{site}"
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", site), timeout=1).close()
                break
            except OSError:
                time.sleep(0.2)
        opener = urllib.request.build_opener(
            type(
                "NoRedirect",
                (urllib.request.HTTPRedirectHandler,),
                {"redirect_request": lambda *a: None},
            )()
        )

        def fetch(path, headers):
            try:
                with opener.open(
                    urllib.request.Request(base + path, headers=headers), timeout=10
                ) as r:
                    return r.status, r.read().decode(), dict(r.headers)
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode(), dict(e.headers)

        spoof = {"X-Forwarded-Email": "evil@example.com"}
        status, body, headers = fetch("/api/me", spoof)
        assert status == 302 and headers["Location"].startswith(
            "/oauth2/start?rd="
        )  # not signed in
        assert "evil" not in body
        status, body, _ = fetch("/api/me", {**spoof, "Cookie": "session=alice"})
        assert (status, body) == (200, "email=alice@example.com")  # the spoofed value never arrives
        status, body, _ = fetch("/oauth2/start", {})
        assert (status, body) == (200, "oauth2-proxy page")
    finally:
        for proc in procs:
            proc.terminate()
