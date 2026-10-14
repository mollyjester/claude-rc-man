#!/usr/bin/python3
"""The whole of rcman in the podman host, for the browser tests on the host.

Run as root inside the podman host by tests/test_container_suite.py:

- with no argument, it installs rcman from a clone as install.sh does
  (tests/container/installed.py), with alice@example.com its first administrator,
  adds bob and carol with rcman-admin, points oauth2-proxy's Google provider at a
  stand-in (tests/container/oidc_stub.py), and puts Caddy, with its own certificate
  authority and its admin API off, running as its own account, in front of
  rcman.test and auth.test; each runs as a service of its own, so this returns once
  everything answers. Signing in to Claude in a workspace is not there yet, so a
  service of the test's signs in the fake claude of every workspace whose name
  begins with SIGNED as soon as it is made;
- with ``check``, after the browser tests, it checks what they left: carol, whom they
  deleted, has nothing labelled hers left.

The only change to what the installer wrote is the three Google endpoints in
oauth2-proxy's configuration.
"""

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from clone import SRC, make_clone, sh  # noqa: E402
from installed import (  # noqa: E402
    SETTINGS,
    STATE,
    install,
    podman,
    serve_releases,
    signed_in,
)

DOMAIN = "rcman.test"
AUTH = "auth.test"
STUB_PORT = 9100
ADMIN = "alice@example.com"
OTHERS = ("bob@example.com", "carol@example.com")
# The names of the workspaces whose fake claude is signed in as they are made.
SIGNED = "signed-"
LEFT = Path("/var/tmp/rcman-stack.json")


def people() -> dict[str, int]:
    con = sqlite3.connect(f"file:{STATE / 'rcman.db'}?mode=ro", uri=True)
    try:
        return dict(con.execute("select email, id from people"))
    finally:
        con.close()


def sign_in_forever() -> None:
    """Sign in the fake claude of each workspace named SIGNED..., once it is ready."""
    done: set[int] = set()
    while True:
        try:
            con = sqlite3.connect(f"file:{STATE / 'rcman.db'}?mode=ro", uri=True)
            try:
                rows = con.execute(
                    "select id from workspaces where state = 'ready' and name like ?",
                    (f"{SIGNED}%",),
                ).fetchall()
            finally:
                con.close()
            for (workspace,) in rows:
                if workspace not in done:
                    signed_in(workspace)
                    done.add(workspace)
        except Exception as error:  # noqa: BLE001 - it tries again on its next round
            print(f"signing in: {error}", flush=True)
        time.sleep(0.5)


def main() -> None:
    SETTINGS["RCMAN_INSTALL_CLAUDE_DOWNLOAD"] = serve_releases()
    SETTINGS["RCMAN_INSTALL_DOMAIN"] = DOMAIN
    SETTINGS["RCMAN_INSTALL_ADMIN_EMAIL"] = ADMIN
    make_clone()
    proc = install()
    if proc.returncode != 0:
        sys.exit(f"install.sh failed:\n{proc.stdout}{proc.stderr}")
    for email in OTHERS:
        sh("rcman-admin", "add", "--", email)
    LEFT.write_text(json.dumps(people()))

    cfg = Path("/etc/rcman/oauth2-proxy.cfg")
    cfg.write_text(
        cfg.read_text()
        + "# The full-stack test's stand-in for Google:\n"
        + f'login_url = "https://{AUTH}/authorize"\n'
        + f'redeem_url = "http://127.0.0.1:{STUB_PORT}/token"\n'
        + f'validate_url = "http://127.0.0.1:{STUB_PORT}/tokeninfo"\n'
    )
    sh("systemd-run", "--unit=rcman-test-oidc", "--uid=nobody",
       sys.executable, str(SRC / "tests/container/oidc_stub.py"), str(STUB_PORT))  # fmt: skip
    sh("systemctl", "restart", "rcman-oauth2-proxy.service")
    sh("systemd-run", "--unit=rcman-test-sign-in", sys.executable, __file__, "sign-in")

    caddyfile = Path("/etc/caddy/Caddyfile")
    caddyfile.parent.mkdir(parents=True, exist_ok=True)
    caddyfile.write_text(
        "{\n\tadmin off\n\tlocal_certs\n\tskip_install_trust\n}\n"
        + Path("/etc/rcman/Caddyfile.snippet").read_text()
        + f"\n{AUTH} {{\n\treverse_proxy 127.0.0.1:{STUB_PORT}\n}}\n"
    )
    # Caddy as its own account, as on a real host: it reaches rcman's socket only through
    # the group the installer made it a member of.
    data = Path("/var/lib/caddy")
    data.mkdir(parents=True, exist_ok=True)
    sh("chown", "-R", "caddy:caddy", str(data))
    sh(
        "systemd-run", "--unit=rcman-test-caddy", "--uid=caddy", f"--setenv=HOME={data}",
        f"--setenv=XDG_DATA_HOME={data}/.local/share", f"--setenv=XDG_CONFIG_HOME={data}/.config",
        "--property=AmbientCapabilities=CAP_NET_BIND_SERVICE",
        "caddy", "run", "--adapter", "caddyfile", "--config", str(caddyfile),
    )  # fmt: skip
    deadline = time.monotonic() + 60
    probe = ["curl", "-sk", "-o", "/dev/null", "--resolve", f"{DOMAIN}:443:127.0.0.1"]
    while subprocess.run([*probe, f"https://{DOMAIN}/"], capture_output=True).returncode != 0:
        if time.monotonic() > deadline:
            journal = sh("journalctl", "--no-pager", "-u", "rcman-test-caddy", check=False)
            sys.exit(f"caddy does not answer:\n{journal.stdout}")
        time.sleep(1)
    print("READY", flush=True)


def check() -> None:
    """What the browser tests left: nothing of carol's, whom they deleted."""
    carol = json.loads(LEFT.read_text())["carol@example.com"]
    label = f"label=rcman.person={carol}"
    left = podman("ps", "--all", "--quiet", "--filter", label).stdout.split()
    volumes = podman("volume", "ls", "--quiet", "--filter", label).stdout.split()
    assert left == [] and volumes == [], (left, volumes)
    assert "carol@example.com" not in people()
    assert "carol@example.com" not in (STATE / "allowed-emails").read_text()
    print("CHECKED", flush=True)


if __name__ == "__main__":
    {"": main, "check": check, "sign-in": sign_in_forever}[(sys.argv[1:] or [""])[0]]()
