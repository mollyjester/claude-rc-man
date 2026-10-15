#!/usr/bin/python3
"""The whole of rcman in the podman host, for the browser tests on the host.

Run as root inside the podman host by tests/test_container_suite.py:

- with no argument, it installs rcman from a clone as install.sh does
  (tests/container/installed.py), with alice@example.com its first administrator,
  adds bob and carol with rcman-admin, points oauth2-proxy's Google provider at a
  stand-in (tests/container/oidc_stub.py), and puts Caddy, with its own certificate
  authority and its admin API off, running as its own account, in front of
  rcman.test and auth.test; each runs as a service of its own, so this returns once
  everything answers. The browser tests sign in to the fake claude on the Claude page;
- with ``repository <workspace id>``, which a browser test runs through the container, it
  makes a git repository in that workspace's work folder, with a bare origin of its own in
  the workspace (a local path, so no network and no GitHub), one commit pushed, and
  changes left to stage;
- with ``check``, after the browser tests, it checks what they left: carol, whom they
  deleted, has nothing labelled hers left, and the changes the browser tests staged,
  committed and pushed from the page are in the origin, as the person set as the author,
  with an audit row for each step and none that holds what was written.

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
    in_workspace,
    install,
    podman,
    serve_releases,
)

DOMAIN = "rcman.test"
AUTH = "auth.test"
STUB_PORT = 9100
ADMIN = "alice@example.com"
OTHERS = ("bob@example.com", "carol@example.com")
LEFT = Path("/var/tmp/rcman-stack.json")
# Which workspace got a repository, and what the browser tests are to have made of it.
REPOSITORY = Path("/var/tmp/rcman-stack-repository.json")
WORK, ORIGIN = "/rcman/work", "/rcman/home/origin.git"
AUTHOR = ("Alice", "alice@example.com")  # set on the Settings page; the address is the sign-in
SETUP = ("Setup", "setup@example.com")
MESSAGE = "Add notes"
MAKE_REPOSITORY = f"""
set -e
cd {WORK}
git init -q -b main
printf 'one\\n' > hello.txt
git add hello.txt
git -c user.name={SETUP[0]} -c user.email={SETUP[1]} commit -q -m 'First commit'
git init -q --bare -b main {ORIGIN}
git remote add origin {ORIGIN}
git push -q -u origin main
printf 'two\\n' >> hello.txt
printf 'new\\n' > notes.txt
"""


def people() -> dict[str, int]:
    con = sqlite3.connect(f"file:{STATE / 'rcman.db'}?mode=ro", uri=True)
    try:
        return dict(con.execute("select email, id from people"))
    finally:
        con.close()


def repository(workspace_id: str) -> None:
    """A repository in the workspace, with a bare origin in it, and changes to stage."""
    in_workspace(int(workspace_id), MAKE_REPOSITORY)
    REPOSITORY.write_text(json.dumps({"workspace": int(workspace_id)}))
    print("REPOSITORY", flush=True)


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
    pushed()
    print("CHECKED", flush=True)


def pushed() -> None:
    """The changes the browser tests staged, committed and pushed are in the origin as the
    person's commit, the workspace has nothing left to commit, and the audit has a row for
    each step but none holding the message."""
    workspace = json.loads(REPOSITORY.read_text())["workspace"]
    log = in_workspace(workspace, f"git -C {ORIGIN} log --format='%an|%ae|%s' main").splitlines()
    assert log == [f"{AUTHOR[0]}|{AUTHOR[1]}|{MESSAGE}", f"{SETUP[0]}|{SETUP[1]}|First commit"], log
    assert in_workspace(workspace, f"git -C {ORIGIN} show main:notes.txt") == "new\n"
    assert in_workspace(workspace, f"git -C {ORIGIN} show main:hello.txt") == "one\ntwo\n"
    assert in_workspace(workspace, f"git -C {WORK} status --porcelain") == ""
    here = in_workspace(workspace, f"git -C {WORK} rev-parse HEAD")
    assert here == in_workspace(workspace, f"git -C {ORIGIN} rev-parse main")  # nothing left

    con = sqlite3.connect(f"file:{STATE / 'rcman.db'}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select action, target, result from audit where email = ? and action like 'git.%' "
            "order by id",
            (ADMIN,),
        ).fetchall()
        name = con.execute("select name from workspaces where id = ?", (workspace,)).fetchone()[0]
        everything = " ".join(" ".join(map(str, row)) for row in con.execute("select * from audit"))
    finally:
        con.close()
    steps = ["git.stage", "git.commit", "git.push"]
    assert [row for row in rows if row[0] in steps][:3] == [(a, name, "ok") for a in steps], rows
    assert MESSAGE not in everything and "notes.txt" not in everything  # what was written


if __name__ == "__main__":
    verb, *arguments = sys.argv[1:] or [""]
    run = {"": main, "check": check, "repository": repository}[verb]
    run(*arguments)
