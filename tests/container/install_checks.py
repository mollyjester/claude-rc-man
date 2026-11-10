"""The checks of the stubbed installation: rcman's real installer, run as root in a container
of the system image on the tests' engine, with no systemd (tests/container/installed.py).

Not a test module. tests/container/test_install_in_container.py runs ``first`` once, as the
installation, and then its one check by its name, as root in the container:
``python3 tests/container/install_checks.py <name>`` from /src, which exits 0 when the check
passes and prints what failed otherwise. The check is what its test of that name asserts.
"""

import hashlib
import json
import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import installed  # noqa: E402
from clone import SRC, sh  # noqa: E402

sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "backend"))

from installer import install as ins  # noqa: E402


def first() -> None:
    """The first installation."""
    proc = installed.first()
    assert proc.returncode == 0, (
        proc.stdout
        + proc.stderr
        + installed.journal()
        + installed.journal("rcman-oauth2-proxy.service")
    )


def test_the_first_installation_puts_rcman_in_caddy_and_restarts_it():
    """The first installation edits the package's Caddyfile, which it keeps as its backup: the
    Caddyfile imports rcman's site block, and ``caddy adapt``, run as Caddy's account, reads its
    global options as asking rcman before an on-demand certificate; and Caddy was restarted."""
    assert f"import {ins.CADDY_SNIPPET}\n" in ins.CADDYFILE.read_text()
    port = ins.gather(ins.SETTINGS_FILE, {}, {}, None)["ASK_PORT"]
    argv = ["caddy", "adapt", "--adapter", "caddyfile", "--config", str(ins.CADDYFILE)]
    config = json.loads(sh(*argv, user="caddy").stdout)
    assert config["apps"]["tls"]["automation"]["on_demand"]["ask"] == ins.ask_url(port)
    conffiles = sh("dpkg-query", "-W", "-f=${Conffiles}", "caddy").stdout.split("\n")
    [md5] = [f.split()[1] for f in conffiles if f.split()[:1] == [str(ins.CADDYFILE)]]
    assert hashlib.md5(ins.CADDY_BACKUP.read_bytes()).hexdigest() == md5
    assert installed.stood_in("systemctl", ["restart", "caddy.service"])


def main(name: str) -> int:
    check = globals()[name]
    try:
        check()
    except Exception:  # noqa: BLE001 - told to the test that ran it
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
