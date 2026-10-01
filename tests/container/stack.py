#!/usr/bin/python3
"""The whole of rcman in one container, for the browser test on the host.

Installs rcman from a clone as install.sh does, adds people with rcman-user,
points oauth2-proxy's Google provider at a stand-in (tests/container/oidc_stub.py),
and puts Caddy, with its own certificate authority, in front of rcman.test and
auth.test. Prints READY and keeps everything running.

The only change to what the installer wrote is the three Google endpoints in
oauth2-proxy's configuration.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from clone import CLONE, SRC, make_clone, sh  # noqa: E402

DOMAIN = "rcman.test"
AUTH = "auth.test"
STUB_PORT = 9100
PEOPLE = {  # linux user: (email, administrator)
    "alice": ("alice@example.com", True),
    "bob": ("bob@example.com", False),
    "carol": ("carol@example.com", False),
    "dave": ("dave@example.com", False),
}
GOOD_CODE = "GOOD-CODE"
INSTALLER = SRC / "tests" / "container" / "fake-installer"
ENV = {
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "RC_ADMIN_INSTALLER": str(INSTALLER),
}


def main() -> None:
    make_clone()
    install = subprocess.run(
        [str(CLONE / "install.sh")],
        env={
            **ENV,
            "SUDO_USER": "dev",
            "RCMAN_INSTALL_DOMAIN": DOMAIN,
            "RCMAN_INSTALL_GOOGLE_CLIENT_ID": "test-client-id",
            "RCMAN_INSTALL_GOOGLE_CLIENT_SECRET": "test-client-secret",
            "RCMAN_INSTALL_ADMIN_EMAIL": PEOPLE["alice"][0],
            "RCMAN_INSTALL_ADMIN_LINUX": "alice",
        },
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    if install.returncode != 0:
        sys.exit(f"install.sh failed:\n{install.stdout}{install.stderr}")

    for linux, (email, admin) in PEOPLE.items():
        if linux != "alice":
            sh(
                "/opt/rcman/current/helpers/rcman-user",
                "add",
                linux,
                email,
                *(["--admin"] if admin else []),
                env=ENV,
            )
        home = Path(f"/home/{linux}")
        demo = home / "projects" / f"{linux}-demo"
        demo.mkdir(parents=True, exist_ok=True)
        state = home / ".claude-fake" / "fake-state.json"
        state.parent.mkdir(exist_ok=True)
        state.write_text(
            json.dumps(
                {"logged_in": False, "trusted": [], "servers": {}, "accept_codes": [GOOD_CODE]}
            )
        )
        sh("chown", "-R", f"{linux}:{linux}", str(demo.parent), str(state.parent))

    cfg = Path("/etc/rcman/oauth2-proxy.cfg")
    cfg.write_text(
        cfg.read_text()
        + "# The full-stack test's stand-in for Google:\n"
        + f'login_url = "https://{AUTH}/authorize"\n'
        + f'redeem_url = "http://127.0.0.1:{STUB_PORT}/token"\n'
        + f'validate_url = "http://127.0.0.1:{STUB_PORT}/tokeninfo"\n'
    )
    subprocess.Popen(
        [
            "runuser",
            "-u",
            "nobody",
            "--",
            sys.executable,
            str(SRC / "tests/container/oidc_stub.py"),
            str(STUB_PORT),
        ]
    )
    sh("systemctl", "restart", "rcman-oauth2-proxy.service")

    caddyfile = Path("/etc/caddy/Caddyfile")
    caddyfile.parent.mkdir(parents=True, exist_ok=True)
    caddyfile.write_text(
        "{\n\tadmin off\n\tlocal_certs\n\tskip_install_trust\n}\n"
        + Path("/etc/rcman/Caddyfile.snippet").read_text()
        + f"\n{AUTH} {{\n\treverse_proxy 127.0.0.1:{STUB_PORT}\n}}\n"
    )
    caddy = subprocess.Popen(["caddy", "run", "--adapter", "caddyfile", "--config", str(caddyfile)])
    time.sleep(3)
    if caddy.poll() is not None:
        sys.exit("caddy did not start")
    print("READY", flush=True)
    caddy.wait()


if __name__ == "__main__":
    os.environ.update(ENV)
    main()
