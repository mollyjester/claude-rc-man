"""rcman installed by its real installer in the stubbed installation, and the ways the checks
there reach it.

Not a test module. Used as root inside a container of the system image
(tests/container/system/) on the tests' engine, with no network: by
tests/container/install_checks.py, which tests/container/test_install_in_container.py runs
there. systemd is not running there; ``systemctl``, ``loginctl`` and rcman's ``podman`` are the
recorded stand-ins of tests/container/stand_ins.py, mounted over the programs, so the installer's
units really run, as their accounts (see that module). The checkout's files at /src become a git
origin, a user ``dev`` clones it and runs ``install.sh`` as an administrator would
(tests/container/clone.py), with the fake claude as the Claude Code release it downloads
(tests/claude_download.py), served by a process of its own. The frontend's packages and the
backend's come from caches the suite filled beforehand (``/cache``): npm and uv run offline.
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

from clone import CLONE, SRC, make_clone, sh  # noqa: E402

sys.path.insert(0, str(SRC / "backend"))
sys.path.insert(0, str(SRC))

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
# Where the fake Claude Code releases are served from, and the stand-ins' folder, where they
# record and keep what they are asked (tests/container/stand_ins.py STATE_DIR).
RELEASES = Path("/var/tmp/rcman-claude-releases")
STAND_INS = Path("/var/lib/rcman-test-stand-ins")
# The caches npm and uv run offline from, which the suite filled beforehand.
CACHE = Path("/cache")
# What the first installation printed, for the checks that read it.
FIRST = Path("/var/tmp/rcman-first-install.json")
# Seconds rcman may take to answer after it starts.
ANSWERS_WITHIN = 120


def serve_releases() -> str:
    """The fake claude, as the tested version's release, served on a free loopback port by a
    process of its own; its address."""
    sys.path.insert(0, str(SRC))
    from tests import claude_download

    version = (SRC / "helpers" / "claude-version").read_text().strip()
    claude_download.release(RELEASES, version)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1",
         "--directory", str(RELEASES)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )  # fmt: skip
    address = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 30
    while True:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return address
        except OSError:
            assert time.monotonic() < deadline, "the releases are not served"
            time.sleep(0.2)


def offline() -> None:
    """npm, as the clone's owner runs it, from the cache alone."""
    npmrc = Path("/home/dev/.npmrc")
    npmrc.write_text(f"cache={CACHE / 'npm'}\noffline=true\n")
    sh("chown", "dev:dev", str(npmrc))


def install(*args, env=None) -> subprocess.CompletedProcess:
    full = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "SUDO_USER": "dev",
        # uv, as the installer runs it as root, from the cache alone
        "UV_CACHE_DIR": str(CACHE / "uv"),
        "UV_OFFLINE": "1",
        **SETTINGS,
        **(env or {}),
    }
    return subprocess.run(
        [str(CLONE / "install.sh"), *args],
        env=full,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=2400,
    )


def first() -> subprocess.CompletedProcess:
    """The first installation, from a fresh clone, with the releases served; what it printed is
    kept in FIRST."""
    SETTINGS["RCMAN_INSTALL_CLAUDE_DOWNLOAD"] = serve_releases()
    make_clone()
    offline()
    proc = install()
    FIRST.write_text(
        json.dumps({"code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr,
                    "download": SETTINGS["RCMAN_INSTALL_CLAUDE_DOWNLOAD"]})
    )  # fmt: skip
    return proc


def first_install() -> subprocess.CompletedProcess:
    """What the first installation printed; the settings it was given, the release address with
    them, for the installations a check runs again."""
    kept = json.loads(FIRST.read_text())
    SETTINGS["RCMAN_INSTALL_CLAUDE_DOWNLOAD"] = kept["download"]
    return subprocess.CompletedProcess([], kept["code"], kept["stdout"], kept["stderr"])


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


def stood_in(program: str, args: list[str]) -> bool:
    """Whether a stand-in (tests/container/stand_ins.py) was asked ``program`` with ``args``."""
    calls = (STAND_INS / "calls").read_text().splitlines()
    return any(json.loads(c)["program"] == program and json.loads(c)["args"] == args for c in calls)


def journal(unit: str = "rcman.service") -> str:
    """The end of what a unit the stand-in ran wrote, for failure messages."""
    try:
        text = (STAND_INS / f"{unit}.log").read_text(errors="replace")
    except OSError as error:
        text = str(error)
    return f"\n--- {unit}\n{text[-8000:]}"


def until(what, within: float = 300, every: float = 1.0):
    """``what()`` once it is true: waited for until ``within`` seconds have passed."""
    deadline = time.monotonic() + within
    while True:
        found = what()
        if found:
            return found
        assert time.monotonic() < deadline, f"never: {what.__doc__ or what}"
        time.sleep(every)
