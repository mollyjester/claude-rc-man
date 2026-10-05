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


SOCKET = "/run/rcman/rcman.sock"
# One HTTP request over a unix socket, run as another user; prints status, headers, body.
UDS_CLIENT = """
import http.client, json, socket, sys
path, method, url, headers, body = json.loads(sys.argv[1])
class C(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(10)
        self.sock.connect(path)
try:
    c = C("rcman")
    c.request(method, url, body=body, headers=headers)
    r = c.getresponse()
    print(json.dumps([r.status, dict(r.headers), r.read().decode(errors="replace")]))
except OSError as e:
    print(json.dumps([None, {}, type(e).__name__ + ": " + str(e)]))
except Exception as e:
    print(json.dumps([None, {}, "not HTTP: " + type(e).__name__]))
"""


def app(url, headers=None, method="GET", body=None, user="caddy", path=SOCKET):
    """A request to rcman over a unix socket as ``user``: (status, body); status None if refused."""
    request = json.dumps([path, method, url, headers or {}, body])
    proc = sh("runuser", "-u", user, "--", sys.executable, "-c", UDS_CLIENT, request)
    status, _headers, text = json.loads(proc.stdout)
    return status, text


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


def root_owned(directory: Path) -> list[str]:
    return sh("find", str(directory), "-user", "root").stdout.split("\n")[:-1]


def test_installing_leaves_nothing_owned_by_root_in_the_clone(first_install, tmp_path):
    """install.sh runs as root from someone's clone; it must not leave root's files there."""
    planted = tmp_path / "planted"
    planted.write_text("x")  # this suite runs as root
    assert str(planted) in root_owned(tmp_path)  # positive control: the check finds root's file
    assert sh("find", str(CLONE), "-type", "f").stdout  # non-vacuity: the clone has files
    assert root_owned(CLONE) == []


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
    assert app("/api/health") == (200, '{"status":"ok"}')
    assert get("http://127.0.0.1:4180/ping")[0] == 200
    status, body = app("/")
    assert status == 200 and '<div id="root">' in body


def test_the_app_reaches_the_helpers_through_sudo(first_install):
    me = {"X-Forwarded-Email": "dev@example.com"}
    status, body = app("/api/me", me)
    assert status == 200 and json.loads(body)["admin"] is True
    status, body = app("/api/folders", me)  # rcman -> sudo -u dev rc-helper
    assert status == 200, body
    # dev's own home: the clone and the projects folder rc-admin created
    assert [e["path"] for e in json.loads(body)["entries"]] == ["projects", "rcman"]
    status, body = app("/api/users", me)  # rcman -> sudo rc-admin
    assert status == 200, body


def test_an_administrator_runs_rcman_user_by_name_through_sudo(first_install):
    """As the guides say: `sudo rcman-user`, found on sudo's own secure path."""
    proc = sh("sudo", "rcman-user", "list", check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "dev@example.com" in proc.stdout  # the first administrator, added by the install


def test_the_socket_directory_admits_only_its_group(first_install):
    import grp

    run = Path("/run/rcman")
    st = run.stat()
    assert pwd.getpwuid(st.st_uid).pw_name == "rcman"
    assert grp.getgrgid(st.st_gid).gr_name == "rcman-web"
    assert stat.S_IMODE(st.st_mode) == 0o750  # the socket itself is 0666; this is what guards it
    assert grp.getgrnam("rcman-web").gr_mem == ["caddy"]
    tmpfiles = Path("/etc/tmpfiles.d/rcman.conf").read_text()
    assert tmpfiles == "d /run/rcman 0750 rcman rcman-web -\n"
    assert app("/api/health", user="caddy")[0] == 200  # positive control
    status, text = app("/api/health", user="dev")
    assert status is None and text.startswith("PermissionError"), text
    for other in ("rcman-proxy", "nobody"):
        assert app("/api/health", user=other)[0] is None, other


def test_the_socket_group_is_kept_to_caddy_alone(first_install):
    import grp

    sh("usermod", "--append", "--groups", "rcman-web", "dev")
    assert "dev" in grp.getgrnam("rcman-web").gr_mem  # the plant took effect
    proc = install()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert grp.getgrnam("rcman-web").gr_mem == ["caddy"]
    assert "dev removed from group rcman-web" in changes(proc)["changed"]


SLOW = """#!/usr/bin/python3
import os, sys, time
open(sys.argv[1], "w").write(str(os.getpid()))
time.sleep(120)
"""
BOUNDED = """
import subprocess, sys
from rcman.helpers_client import run_bounded
runner = run_bounded if sys.argv[1] == "bounded" else subprocess.run
try:
    runner(sys.argv[2:], input=None, capture_output=True, text=True, timeout=2)
except subprocess.TimeoutExpired:
    print("timed out")
"""


def slow_survives(how, tmp_path):
    """Run a slow command through real sudo, as rcman runs a helper, and time it out."""
    pid_file = Path(f"/tmp/slow-{how}.pid")
    pid_file.unlink(missing_ok=True)
    out = sh(
        "runuser", "-u", "rcman", "--", str(ROOT / "current" / ".venv" / "bin" / "python"),
        "-c", BOUNDED, how, "sudo", "-n", "-u", "dev", "/tmp/slow-helper", str(pid_file),
    ).stdout  # fmt: skip
    assert out.strip() == "timed out"
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            state = Path(f"/proc/{pid}/stat").read_text().split(")")[-1].split()[0]
        except FileNotFoundError:
            return False
        if state == "Z":
            return False
        time.sleep(0.1)
    os.kill(pid, 9)
    return True


def test_a_timed_out_helper_call_ends_the_helper_behind_sudo(first_install, tmp_path):
    slow = Path("/tmp/slow-helper")
    slow.write_text(SLOW)
    slow.chmod(0o755)
    rule = Path("/etc/sudoers.d/zz-slow-helper")
    rule.write_text("rcman ALL=(dev) NOPASSWD: /tmp/slow-helper\n")
    rule.chmod(0o440)
    try:
        assert slow_survives("plain", tmp_path)  # planted twin: killing sudo leaves it running
        assert not slow_survives("bounded", tmp_path)
    finally:
        rule.unlink()


STUBBORN = """#!/usr/bin/python3
import os, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open(sys.argv[1], "w").write(str(os.getpid()))
time.sleep(120)
"""


def test_a_helper_ignoring_sigterm_behind_sudo_does_not_hold_the_call(first_install):
    """It is left behind, but the call still ends soon after its timeout."""
    stubborn = Path("/tmp/stubborn-helper")
    stubborn.write_text(STUBBORN)
    stubborn.chmod(0o755)
    rule = Path("/etc/sudoers.d/zz-stubborn-helper")
    rule.write_text("rcman ALL=(dev) NOPASSWD: /tmp/stubborn-helper\n")
    rule.chmod(0o440)
    pid_file = Path("/tmp/stubborn.pid")
    pid_file.unlink(missing_ok=True)
    python = str(ROOT / "current" / ".venv" / "bin" / "python")
    grace = float(
        sh("runuser", "-u", "rcman", "--", python, "-c",
           "from rcman.helpers_client import TERMINATE_GRACE_SECONDS as g; print(g)").stdout
    )  # fmt: skip
    started = time.monotonic()
    try:
        out = sh(
            "runuser", "-u", "rcman", "--", "setsid", python, "-c", BOUNDED, "bounded",
            "sudo", "-n", "-u", "dev", str(stubborn), str(pid_file),
            stdin=subprocess.DEVNULL, timeout=120,
        ).stdout  # fmt: skip
        elapsed = time.monotonic() - started
    finally:
        rule.unlink()
        if pid_file.exists() and pid_file.read_text():
            sh("kill", "-9", pid_file.read_text(), check=False)
    assert out.strip() == "timed out"
    assert elapsed < 2 + grace + 5, elapsed  # the call's timeout, the grace period, a margin


def listeners():
    """Every listening TCP address (host, port) and unix socket path in the container."""
    tcp = set()
    for table, family in (("/proc/net/tcp", socket.AF_INET), ("/proc/net/tcp6", socket.AF_INET6)):
        for line in Path(table).read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] != "0A":  # LISTEN
                continue
            address, port = fields[1].split(":")
            raw = bytes.fromhex(address)
            if family == socket.AF_INET:
                host = socket.inet_ntop(family, raw[::-1])
            else:
                host = socket.inet_ntop(
                    family, b"".join(raw[i : i + 4][::-1] for i in range(0, 16, 4))
                )
            if host in ("0.0.0.0", "::"):
                host = "127.0.0.1" if family == socket.AF_INET else "::1"
            tcp.add((host, int(port, 16)))
    unix = set()
    for line in Path("/proc/net/unix").read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 8 and fields[5] == "01" and int(fields[3], 16) & 0x10000:
            if not fields[7].startswith("@"):  # abstract names are not files
                unix.add(fields[7])
    return tcp, unix


PROBE = """
import json, sys, urllib.request, urllib.error
req = urllib.request.Request(sys.argv[1], headers=json.loads(sys.argv[2]))
try:
    r = urllib.request.urlopen(req, timeout=10)
    print(r.status, r.read().decode(errors="replace"))
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode(errors="replace"))
except Exception as e:
    print("refused", e)
"""


def test_no_loopback_listener_lets_a_local_user_impersonate(first_install):
    """Whatever listens here, a local user cannot use it to be someone in rcman."""
    admin = "dev@example.com"
    spoof = {"X-Forwarded-Email": admin, "X-Auth-Request-Email": admin}
    tcp, unix = listeners()
    assert ("127.0.0.1", 4180) in tcp and SOCKET in unix  # non-vacuity: both are found
    for host, port in sorted(tcp):
        url = f"http://{'[' + host + ']' if ':' in host else host}:{port}/api/me"
        out = sh(
            "runuser", "-u", "dev", "--", sys.executable, "-c", PROBE, url, json.dumps(spoof)
        ).stdout
        assert '"admin":true' not in out and f'"email":"{admin}"' not in out, (host, port, out)
    for path in sorted(unix):
        status, text = app("/api/me", spoof, user="dev", path=path)
        assert '"admin":true' not in text and f'"email":"{admin}"' not in text, (path, text)
    # oauth2-proxy without its cookie names no one.
    request = urllib.request.Request("http://127.0.0.1:4180/oauth2/auth", headers=spoof)
    try:
        urllib.request.urlopen(request, timeout=10)
        raise AssertionError("oauth2-proxy accepted a request without its cookie")
    except urllib.error.HTTPError as error:
        assert error.code == 401
        assert "X-Auth-Request-Email" not in error.headers
    # Positive control: through the socket, as Caddy, the header is believed.
    status, text = app("/api/me", spoof, user="caddy")
    assert status == 200 and json.loads(text)["email"] == admin
    assert '"admin":true' in text


def test_the_installer_warns_about_a_caddy_admin_api_on_loopback(first_install):
    """Planted twin: a Caddy whose admin API is on loopback, as by default, is warned about."""
    sys.path.insert(0, str(SRC))
    from installer import install as ins

    assert ins.CADDY_ADMIN_WARNING not in first_install.stdout + first_install.stderr
    caddy = subprocess.Popen(
        ["caddy", "run"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )  # no configuration: the admin API on localhost:2019
    try:
        deadline = time.monotonic() + 20
        while ("127.0.0.1", 2019) not in listeners()[0] and time.monotonic() < deadline:
            time.sleep(0.2)
        assert ("127.0.0.1", 2019) in listeners()[0]
        proc = install()
        assert proc.returncode == 0, proc.stdout + proc.stderr  # a warning, not a refusal
        assert ins.CADDY_ADMIN_WARNING in proc.stdout + proc.stderr
    finally:
        caddy.terminate()
        caddy.wait(timeout=10)


def test_audit_events_reach_the_journal(first_install):
    """The service's output is what systemd sends to the journal; an audit event is a line in it."""
    log = Path("/run/fake-systemd/rcman.service.log")
    before = log.read_text(errors="replace").splitlines()
    status, text = app(
        "/api/me/settings",
        {
            "X-Forwarded-Email": "dev@example.com",
            "Origin": f"https://{DOMAIN}",
            "Content-Type": "application/json",
        },
        method="PATCH",
        body=json.dumps({"prefix": "journal"}),
    )
    assert status == 200, text
    deadline = time.monotonic() + 10
    while True:  # the event is written as the request finishes, which may follow the response
        after = log.read_text(errors="replace").splitlines()[len(before) :]
        events = []
        for line in after:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("action") == "settings.update":
                events.append(event)
        if events or time.monotonic() > deadline:
            break
        time.sleep(0.2)
    assert any("PATCH /api/me/settings" in line for line in after), after  # positive control
    assert len(events) == 1, after
    assert (events[0]["target"], events[0]["result"]) == ('{"prefix": "journal"}', "ok")


def test_the_caddy_snippet_is_printed_and_valid(first_install):
    assert "Add this to your Caddyfile" in first_install.stdout
    assert "The Caddy site block changed" in first_install.stdout
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
    assert "The Caddy site block changed" not in proc.stdout
    assert "restart Caddy" not in proc.stdout
    assert os.readlink(ROOT / "current") == release
    assert app("/api/health")[0] == 200


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
    assert app("/api/health")[0] == 200


def test_rollback_returns_to_the_previous_release(first_install):
    current, previous = os.readlink(ROOT / "current"), os.readlink(ROOT / "previous")
    proc = subprocess.run([str(CLONE / "install.sh"), "--rollback"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert os.readlink(ROOT / "current") == previous
    assert os.readlink(ROOT / "previous") == current
    command = Path("/usr/local/sbin/rcman-user").resolve()
    assert command == (ROOT / previous / "helpers" / "rcman-user").resolve()
    assert sh("sudo", "rcman-user", "list", check=False).returncode == 0
    deadline = time.monotonic() + 30
    while app("/api/health")[0] != 200 and time.monotonic() < deadline:
        time.sleep(0.5)
    assert app("/api/health")[0] == 200


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
import http.server, socketserver, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(("email=" + str(self.headers.get("X-Forwarded-Email"))).encode())
    def log_message(self, *a): pass
    def address_string(self): return "unix"
socketserver.UnixStreamServer(sys.argv[1], H).serve_forever()
"""


def test_caddy_lets_only_oauth2_proxy_set_the_identity(tmp_path):
    sys.path.insert(0, str(SRC))
    from installer import install as ins

    auth, site = free_port(), free_port()
    fake_socket = tmp_path / "app.sock"
    rendered = ins.render(
        "Caddyfile", {"DOMAIN": f"http://127.0.0.1:{site}", "PROXY_PORT": str(auth)}
    )
    assert f"unix/{ins.SOCKET}" in rendered
    snippet = rendered.replace(str(ins.SOCKET), str(fake_socket))
    config = tmp_path / "Caddyfile"
    config.write_text("{\n\tadmin off\n\tauto_https off\n}\n" + snippet)
    procs = [
        subprocess.Popen([sys.executable, "-c", FAKE_AUTH, str(auth)]),
        subprocess.Popen([sys.executable, "-c", FAKE_APP, str(fake_socket)]),
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
