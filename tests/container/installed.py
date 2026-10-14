"""rcman installed for real in the podman host, and the ways the suites there reach it.

Not a test module. Used as root inside the podman host (tests/container/podman/),
where systemd runs: by test_install_in_container.py, test_restored_in_container.py
and stack.py. The checkout's files at /src become a git origin, a user ``dev``
clones it and runs ``install.sh`` as an administrator would (tests/container/clone.py),
with the fake claude as the Claude Code release it downloads (tests/claude_download.py),
served by a service of its own.
"""

from __future__ import annotations

import json
import pwd
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from clone import CLONE, SRC, sh  # noqa: E402

ROOT = Path("/opt/rcman")
ETC = Path("/etc/rcman")
STATE = Path("/var/lib/rcman")
SOCKET = "/run/rcman/rcman.sock"
DOMAIN = "rcman.example.com"
ORIGIN = f"https://{DOMAIN}"
ADMIN = "dev@example.com"
SETTINGS = {
    "RCMAN_INSTALL_DOMAIN": DOMAIN,
    "RCMAN_INSTALL_GOOGLE_CLIENT_ID": "test-client-id",
    "RCMAN_INSTALL_GOOGLE_CLIENT_SECRET": "test-client-secret",
    "RCMAN_INSTALL_ADMIN_EMAIL": ADMIN,
}
# Where the fake Claude Code releases are served from, and by what.
RELEASES = Path("/var/tmp/rcman-claude-releases")
DOWNLOAD_UNIT = "rcman-test-claude-download.service"
# Seconds rcman may take to answer after it starts, and a workspace or server to come.
ANSWERS_WITHIN = 120
COMES_WITHIN = 300


def serve_releases() -> str:
    """The fake claude, as the tested version's release, served on a free port by a
    service of its own; its address."""
    sys.path.insert(0, str(SRC))
    from tests import claude_download

    version = (SRC / "helpers" / "claude-version").read_text().strip()
    claude_download.release(RELEASES, version)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    sh("systemd-run", f"--unit={DOWNLOAD_UNIT}", "python3", "-m", "http.server", str(port),
       "--bind", "127.0.0.1", "--directory", str(RELEASES))  # fmt: skip
    address = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return address
        except OSError:
            assert time.monotonic() < deadline, "the releases are not served"
            time.sleep(0.2)


def install(*args, env=None) -> subprocess.CompletedProcess:
    full = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "SUDO_USER": "dev",
        **SETTINGS,
        **(env or {}),
    }
    return subprocess.run(
        [str(CLONE / "install.sh"), *args],
        env=full,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=2400,
    )


def changes(proc) -> dict:
    line = next(line for line in proc.stdout.splitlines() if line.startswith("{"))
    return json.loads(line)


# One HTTP request over a unix socket, run as another user; prints status, headers, body.
UDS_CLIENT = """
import http.client, json, socket, sys
path, method, url, headers, body = json.loads(sys.argv[1])
class C(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(60)
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
    """A request to rcman over a unix socket as ``user``: (status, body); status None if
    refused."""
    request = json.dumps([path, method, url, headers or {}, body])
    proc = sh("runuser", "-u", user, "--", sys.executable, "-c", UDS_CLIENT, request)
    status, _headers, text = json.loads(proc.stdout)
    return status, text


def api(method: str, url: str, who: str = ADMIN, body=None) -> tuple[int, object]:
    """A request to rcman's API as ``who``, as Caddy passes it on: (status, its JSON)."""
    headers = {"X-Forwarded-Email": who}
    if method != "GET":
        headers |= {"Origin": ORIGIN, "Content-Type": "application/json"}
    status, text = app(url, headers, method, None if body is None else json.dumps(body))
    try:
        return status, json.loads(text)
    except ValueError:
        return status, text


def healthy(within: float = ANSWERS_WITHIN) -> None:
    deadline = time.monotonic() + within
    while app("/api/health")[0] != 200:
        assert time.monotonic() < deadline, "rcman does not answer"
        time.sleep(0.5)


def rcman_uid() -> int:
    return pwd.getpwnam("rcman").pw_uid


def podman(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """podman --remote as rcman, through its own socket."""
    uid = rcman_uid()
    proc = subprocess.run(
        ["runuser", "-u", "rcman", "--", "env", f"XDG_RUNTIME_DIR=/run/user/{uid}",
         f"HOME={STATE}", "podman", "--remote", *args],
        capture_output=True, text=True, cwd="/", timeout=300,
    )  # fmt: skip
    assert proc.returncode == 0 or not check, proc.stdout + proc.stderr
    return proc


def in_workspace(workspace_id: int, script: str, check: bool = True) -> str:
    """A script run by the workspace's busybox shell; what it printed."""
    found = podman(
        "exec", f"rcman-ws-{workspace_id}", "/opt/rcman/bin/busybox", "sh", "-c", script,
        check=check,
    )  # fmt: skip
    return found.stdout


def workspace_of(name: str, who: str = ADMIN) -> dict | None:
    status, listed = api("GET", "/api/workspaces", who)
    assert status == 200, listed
    return next((w for w in listed["workspaces"] if w["name"] == name), None)


def until(what, within: float = COMES_WITHIN, every: float = 1.0):
    """``what()`` once it is true: waited for until ``within`` seconds have passed."""
    deadline = time.monotonic() + within
    while True:
        found = what()
        if found:
            return found
        assert time.monotonic() < deadline, f"never: {what.__doc__ or what}"
        time.sleep(every)


def claude_processes(workspace_id: int) -> list[int]:
    """The ids, in the workspace, of every process of the fake claude there. The pattern
    is written so that it does not find the shell that looks for it."""
    out = in_workspace(
        workspace_id,
        "for f in /proc/[0-9]*/cmdline; do "
        'if /opt/rcman/bin/busybox tr "\\0" " " < "$f" 2>/dev/null | '
        "/opt/rcman/bin/busybox grep -q 'fakes/clau[d]e'; then "
        'p="${f#/proc/}"; echo "${p%/cmdline}"; fi; done',
    )
    return sorted(int(pid) for pid in out.split())
