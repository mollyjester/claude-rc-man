#!/usr/bin/python3
"""The whole of rcman in one container of the tests' engine, for the browser tests and the
checks of the whole stack.

On the host, ``launched`` starts it (``a_stack`` in tests/container/conftest.py): a container of the
system image (tests/container/system/) on the tests' engine (tests/podman_service.py) with no
network but its own loopback, which the stack's workspaces and the browser's container join
(``container:<its name>``). In it, as processes of their own, run:

- rcman's app, made as tests/e2e_server.py makes one but with the Podman runtime at the suites'
  sizes (tests/container/sizes.py) on the engine, whose socket it is given; alice@example.com its
  first administrator, bob and carol let in; the fake claude in the store, which the browser tests
  sign in to on the Claude page; its listeners the unix sockets the installer gives it, its
  own and its forwarding server's;
- oauth2-proxy (from the installer's toolchain) with the installer's own configuration, its Google
  endpoints pointed at tests/container/oidc_stub.py;
- Caddy with the installer's own site block for rcman.test and its forwarded ports, its own
  certificate authority, its admin API off and on-demand certificates asked of a site on its
  loopback (``global_options``), in front of rcman.test, auth.test (the stub) and github.com: the
  stand-in GitHub (tests/github_server.py through tests/container/github_stand_in.py ``serve``,
  with a public and a private repository, and a public one whose devcontainer.json forwards a
  port) under a certificate of an authority of its own, which the workspaces' image, derived
  from the default image, trusts, and where its git finds github.com;
- the fake claude's releases, served, as the address rcman downloads Claude Code from;
- ``systemctl`` the stand-in of tests/container/stand_ins.py, which rcman's limits reach.

Run as ``python3 tests/container/stack.py <verb>`` in the container, with ``RCMAN_STACK_CONFIG``
naming the stack's configuration:

- ``up`` starts all of it and returns once everything answers;
- ``restart`` ends rcman's app and starts it again;
- ``caddy [<file> [<ask address>]]`` ends Caddy and starts it again with the sites in the file
  after the stack's own, or with the stack's own only, its global options asking the address
  given or the stack's own ask site, failing with Caddy's version and its words where Caddy does
  not load the result;
- ``request <json>`` makes one request to rcman's socket as Caddy passes one on, and
  ``identity`` signs in through Caddy, oauth2-proxy and the stand-in for Google as a browser
  does, and ``cookies`` signs in the same way one redirect at a time and then out, each printing
  what it was answered;
- ``repository <workspace id>``, which a browser test runs through the relay the browser's
  container has (tests/container/stack_exec.mjs), makes a git repository in that workspace's work
  folder, with a bare origin of its own in the workspace, one commit pushed, and changes left to
  stage; and ``serve-port <workspace id> <port>``, run the same way, starts an app on that port
  in the workspace (``PORT_APP``) and returns once it listens;
- ``check``, after the browser tests, checks what they left: carol, whom they deleted, has nothing
  labelled hers left, the changes they staged, committed and pushed from the page are in the
  origin as the person set as the author, with an audit row for each step and none that holds what
  was written; the workspaces they made by cloning are ready with their repository's commit; and
  the GitHub token they stored is in no audit row, workspace row or line of rcman's output.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE.parent.parent

DOMAIN = "rcman.test"
AUTH = "auth.test"
ADMIN = "alice@example.com"
OTHERS = ("bob@example.com", "carol@example.com")
# The loopback ports of the stack's own network, which nothing else shares.
STUB_PORT, GITHUB_PORT, RELEASES_PORT, PROXY_PORT = 9100, 9200, 9300, 4180
# Where Caddy asks whether it may get a certificate for a host on demand (on_demand_tls), which
# only a site served on that loopback port answers.
ASK_PORT = 9400
ASK_PATH = "/.rcman/ask"
ASK_URL = f"http://127.0.0.1:{ASK_PORT}{ASK_PATH}"


def global_options(ask: str = ASK_URL) -> str:
    """The stack's own global options for Caddy: no admin API, certificates from its own
    authority, which is not installed in the system's trust, and on-demand certificates only
    once ``ask`` allows one; with no ``ask``, no ``on_demand_tls`` at all, as in a Caddyfile
    that lacks the block the installer prints."""
    on_demand = f"\ton_demand_tls {{\n\t\task {ask}\n\t}}\n" if ask else ""
    return "{\n\tadmin off\n\tlocal_certs\n\tskip_install_trust\n" + on_demand + "}\n"


# rcman's sockets and its allowed list, where the installer puts them.
SOCKET = Path("/run/rcman/rcman.sock")
FORWARD_SOCKET = Path("/run/rcman/forward.sock")
ALLOWED = Path("/var/lib/rcman/allowed-emails")
# The browser's image: Playwright's, at the version frontend/package-lock.json pins, by digest.
PLAYWRIGHT_VERSION = "1.63.0"
PLAYWRIGHT_IMAGE = (
    f"mcr.microsoft.com/playwright:v{PLAYWRIGHT_VERSION}-noble"
    "@sha256:eff16c30e6f3f4af0a03fa4b706120d5e9b0891c344a27d64559aff5900a4a27"
)


def playwright_problem(image: str, lock: dict) -> str | None:
    """Why ``image`` is not Playwright's image at the version ``lock`` (frontend/package-lock.json)
    pins, or None: the browser and the tests' own Playwright must be one release."""
    pinned = lock["packages"]["node_modules/playwright"]["version"]
    found = re.search(r":v(\d+\.\d+\.\d+)-", image)
    if found is None or found[1] != pinned:
        return f"{image} is not Playwright {pinned}, which frontend/package-lock.json pins"
    return None


# The verbs the browser tests may have the suite run in the stack's container, each with how
# many arguments it takes, every one decimal digits, and nothing else.
RELAYED = ("python3", "tests/container/stack.py")
RELAYED_VERBS = {"repository": 1, "serve-port": 2}


def relayed(argv: list[str]) -> bool:
    """Whether ``argv`` is a command the relay runs: one of RELAYED_VERBS with its numbers."""
    if not isinstance(argv, list) or tuple(argv[: len(RELAYED)]) != RELAYED:
        return False
    verb, *numbers = argv[len(RELAYED) :] or [None]
    return (
        isinstance(verb, str)
        and len(numbers) == RELAYED_VERBS.get(verb, -1)
        and all(isinstance(n, str) and re.fullmatch(r"[0-9]+", n) for n in numbers)
    )


@contextlib.contextmanager
def command_relay(run, folder: Path):
    """A unix socket in ``folder`` (a held folder, its owner's alone) on which the browser's
    container asks for a command (tests/container/stack_exec.mjs): ``run(argv)``, giving the exit
    code and what it printed, for the command ``relayed`` takes, and code 2 for anything else.
    The socket is its owner's alone too: the browser's container runs as that owner. The
    socket's path."""
    import threading

    os.chmod(folder, 0o700)
    path = folder / "s"
    listening = socket.socket(socket.AF_UNIX)
    listening.bind(str(path))
    os.chmod(path, 0o600)
    listening.listen(4)

    def serve() -> None:
        while True:
            try:
                client, _ = listening.accept()
            except OSError:
                return
            with client:
                parts = []
                while part := client.recv(65536):
                    parts.append(part)
                try:
                    asked = json.loads(b"".join(parts) or b"[]")
                except ValueError:
                    asked = None
                if relayed(asked):
                    code, output = run(asked)
                else:
                    code, output = 2, f"not a command of the stack's: {asked}\n"
                client.sendall(json.dumps({"code": code, "output": output}).encode())

    threading.Thread(target=serve, daemon=True).start()
    try:
        yield path
    finally:
        listening.close()


# Where the backend's packages and the built frontend are in the stack's container.
VENV = "/opt/rcman-test-venv"
DIST = "/opt/rcman-test-dist"
# Which workspace got a repository, and what the browser tests are to have made of it.
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
# Seconds the stack may take to answer, and rcman to answer again after a restart.
ANSWERS_WITHIN = 180
# Seconds a process of the stack is given to end when asked, and again once killed.
ENDS_WITHIN = 60

# --- in the stack's container --------------------------------------------------------------


def config() -> dict:
    return json.loads(Path(os.environ["RCMAN_STACK_CONFIG"]).read_text())


def state() -> Path:
    return Path(config()["state"])


def podman(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """podman on the engine, through its socket."""
    proc = subprocess.run(
        ["podman", "--remote", "--url", f"unix://{config()['engine_socket']}", *args],
        capture_output=True, text=True, errors="replace", timeout=300,
    )  # fmt: skip
    assert proc.returncode == 0 or not check, proc.stdout + proc.stderr
    return proc


def in_workspace(workspace_id: int, script: str) -> str:
    """A script run by the workspace's busybox shell; what it printed."""
    found = podman("exec", f"rcman-ws-{workspace_id}", "/opt/rcman/bin/busybox", "sh", "-c", script)
    return found.stdout


def database() -> Path:
    return state() / "rcman.db"


def people() -> dict[str, int]:
    con = sqlite3.connect(f"file:{database()}?mode=ro", uri=True)
    try:
        return dict(con.execute("select email, id from people"))
    finally:
        con.close()


def settings():
    """rcman's settings in the stack: its state in the stack's folder, which the engine sees at
    the same path (the canary's staging folder is in it), the allowed list where oauth2-proxy's
    configuration reads it, and the tools, the store and the frontend at their paths."""
    from rcman.settings import Settings

    held = config()
    return Settings(
        public_origin=f"https://{DOMAIN}",
        database=database(),
        allowed_emails=ALLOWED,
        podman_socket=Path(held["engine_socket"]),
        workspace_image=held["image"],
        builder_image=held["builder_image"],
        tools_dir=Path(held["tools"]),
        claude_store=Path(held["store"]),
        claude_download=f"http://127.0.0.1:{RELEASES_PORT}",
        forward_socket=FORWARD_SOCKET,
        docs_dir=SRC / "docs",
        frontend_dir=Path(DIST),
    )


def create_app():
    """rcman's app, as tests/e2e_server.py makes one but on the engine: the people of the stack,
    made once, and the Podman runtime at the suites' sizes."""
    sys.path.insert(0, str(SRC))
    from rcman import db
    from rcman import people as rcman_people
    from rcman.main import create_app as rcman_app

    from tests.container import sizes

    made = settings()
    new = not made.database.exists()
    db.migrate(made.database)
    if new:
        con = db.connect(made.database)
        try:
            rcman_people.add(con, ADMIN, True, last_slot=sizes.PEOPLE_SLOTS[-1])
            for email in OTHERS:
                rcman_people.add(con, email, False, last_slot=sizes.PEOPLE_SLOTS[-1])
        finally:
            con.close()
    return rcman_app(made, runtime=runtime_of(made), builder_slot=sizes.BUILDER_SLOT)


def runtime_of(made):
    """The Podman runtime rcman runs with in the stack, at the suites' sizes."""
    from rcman import runtime as rt

    from tests.container import sizes

    return rt.PodmanRuntime(
        rt.PodmanConfig(
            socket=made.podman_socket,
            image=made.workspace_image,
            builder_image=made.builder_image,
            scratch_dir=made.database.parent,
            tools_dir=made.tools_dir,
            claude_store=made.claude_store,
            range=sizes.TEST_RANGE,
            network=config()["network"],
            slices=False,
        )
    )


def admin_delete(email: str) -> None:
    """``rcman-admin delete <email>``: its own code (``admin_delete_here``), in a process of its
    own beside the service, run by the interpreter and in the environment the service runs in
    (``rcman_env``), which has the project's dependencies; this verb's ``python3`` has not."""
    done = subprocess.run(
        [f"{VENV}/bin/python", str(HERE / "stack.py"), "admin-delete-here", email],
        env=rcman_env(), cwd=str(SRC), check=False,
    )  # fmt: skip
    assert done.returncode == 0, done.returncode


def admin_delete_here(email: str) -> None:
    """``admin_delete``'s process: ``rcman-admin delete <email>`` with the stack's settings and
    runtime in place of an environment file (it runs as the stack's own account, so it does not
    run itself again as another)."""
    sys.path.insert(0, str(SRC))
    from rcman import admin

    args = admin._parser().parse_args(["delete", email])
    made = settings()
    code = admin._run(args, made, runtime_of(made))
    assert code == 0, code
    print("DELETED", flush=True)


def started(name: str, argv: list[str], **more) -> None:
    """``argv`` as a process of its own, its output in the stack's logs, its id noted."""
    logs = state() / "logs"
    logs.mkdir(exist_ok=True)
    with open(logs / f"{name}.log", "ab") as log:
        child = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
            **more,
        )  # fmt: skip
    (logs / f"{name}.pid").write_text(str(child.pid))


def group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    return True


def ended(name: str) -> None:
    """The process ``name`` and its group ended: asked to end, then killed once
    ``ENDS_WITHIN`` has passed; one whose group is still there after that ends the verb."""
    pid_file = state() / "logs" / f"{name}.pid"
    pid = int(pid_file.read_text())
    for sent in (signal.SIGTERM, signal.SIGKILL):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, sent)
        deadline = time.monotonic() + ENDS_WITHIN
        while group_alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        if not group_alive(pid):
            break
    else:
        sys.exit(f"{name} (process group {pid}) did not end")
    pid_file.unlink()


def running(pid: int) -> bool:
    """Whether the process ``pid`` runs: is there and is not a zombie."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rpartition(")")[2].split()[0] != "Z"


def answers(probe) -> bool:
    try:
        return probe()
    except OSError:
        return False


def socket_status(url: str) -> int:
    import http.client

    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.settimeout(10)
            self.sock.connect(str(SOCKET))

    connection = Connection("rcman")
    connection.request("GET", url)
    return connection.getresponse().status


def tcp_answers(port: int) -> bool:
    socket.create_connection(("127.0.0.1", port), timeout=1).close()
    return True


def wait(what: str, probe, within: float = ANSWERS_WITHIN) -> None:
    deadline = time.monotonic() + within
    while not answers(probe):
        if time.monotonic() > deadline:
            logs = "".join(
                f"\n--- {p.name}\n{p.read_text(errors='replace')[-4000:]}"
                for p in sorted((state() / "logs").glob("*.log"))
            )
            sys.exit(f"{what} does not answer{logs}")
        time.sleep(0.5)


def rcman_env() -> dict[str, str]:
    """The environment rcman's own code runs in, in the stack: the project's virtual
    environment, with the checkout's backend and tests on the path."""
    return {
        "PATH": f"{VENV}/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "HOME": str(state()),
        "PYTHONPATH": f"{SRC / 'backend'}:{SRC}",
        "PYTHONDONTWRITEBYTECODE": "1",
        "RCMAN_STACK_CONFIG": os.environ["RCMAN_STACK_CONFIG"],
    }


def rcman_started() -> None:
    SOCKET.parent.mkdir(parents=True, exist_ok=True)
    SOCKET.unlink(missing_ok=True)
    started(
        "rcman",
        [f"{VENV}/bin/python", "-m", "uvicorn", "--factory", "tests.container.stack:create_app",
         "--ws", "websockets-sansio", "--uds", str(SOCKET)],
        env=rcman_env(), cwd=str(SRC),
    )  # fmt: skip
    wait("rcman", lambda: socket_status("/api/health") == 200)


def install_settings() -> dict[str, str]:
    """What the installer's templates are rendered with: the stack's own."""
    import secrets

    return {
        "DOMAIN": DOMAIN,
        "GOOGLE_CLIENT_ID": "test-client-id",
        "GOOGLE_CLIENT_SECRET": "test-client-secret",
        "COOKIE_SECRET": secrets.token_urlsafe(24),
        "PROXY_PORT": str(PROXY_PORT),
        "ASK_PORT": str(ASK_PORT),
    }


def caddy_env() -> dict[str, str]:
    data = state() / "caddy"
    return {"PATH": "/usr/bin:/bin", "HOME": str(data), "XDG_DATA_HOME": str(data / "data"),
            "XDG_CONFIG_HOME": str(data / "config")}  # fmt: skip


def caddy_certificates(folder: Path) -> Path:
    """The folder of the certificates Caddy's own authority issued, one folder per host, in its
    data folder in the stack's folder ``folder``."""
    return folder / "caddy" / "data" / "caddy" / "certificates" / "local"


def caddy_root(folder: Path) -> Path:
    """The root certificate of the certificate authority of the stack's Caddy, in its data
    folder in the stack's folder ``folder``."""
    return folder / "caddy" / "data" / "caddy" / "pki" / "authorities" / "local" / "root.crt"


def caddy_started(rendered: dict[str, str], sites: str = "", ask: str = ASK_URL) -> None:
    """Caddy with the stack's global block asking ``ask``, the installer's site block rendered
    with ``rendered``, the stack's own sites and then ``sites``, once it has loaded the file and
    answers. A file it does not load ends the verb with Caddy's version and its words."""
    sys.path.insert(0, str(SRC))
    from installer import install as ins

    pki = Path(config()["pki"])
    Path("/var/log/caddy").mkdir(parents=True, exist_ok=True)
    caddyfile = state() / "Caddyfile"
    caddyfile.write_text(
        global_options(ask)
        + ins.render("Caddyfile", rendered)
        + f"\n{AUTH} {{\n\treverse_proxy 127.0.0.1:{STUB_PORT}\n}}\n"
        + f"\ngithub.com {{\n\ttls {pki / 'leaf.crt'} {pki / 'leaf.key'}\n"
        + f"\treverse_proxy 127.0.0.1:{GITHUB_PORT}\n}}\n"
        + sites
    )
    (state() / "caddy").mkdir(exist_ok=True)
    loaded = ["--adapter", "caddyfile", "--config", str(caddyfile)]
    checked = subprocess.run(
        ["caddy", "validate", *loaded], env=caddy_env(), capture_output=True, text=True
    )
    if checked.returncode != 0:
        version = subprocess.run(["caddy", "version"], capture_output=True, text=True).stdout
        sys.exit(f"Caddy {version.strip()} does not load {caddyfile}:\n{checked.stderr}")
    started("caddy", ["caddy", "run", *loaded], env=caddy_env())
    probe = ["curl", "-sk", "-o", "/dev/null", "--resolve", f"{DOMAIN}:443:127.0.0.1"]
    wait("caddy", lambda: subprocess.run([*probe, f"https://{DOMAIN}/"]).returncode == 0)
    # What answered is the Caddy just started, not one an earlier start left running.
    pid = int((state() / "logs" / "caddy.pid").read_text())
    if not running(pid):
        log = (state() / "logs" / "caddy.log").read_text(errors="replace")[-4000:]
        sys.exit(f"the Caddy started ({pid}) is not running, yet Caddy answers:\n{log}")


def up() -> None:
    sys.path.insert(0, str(SRC))
    from installer import install as ins
    from tests import claude_download
    from tests.container import github_stand_in as stand_in

    version = (SRC / "helpers" / "claude-version").read_text().strip()
    releases = state() / "releases"
    claude_download.release(releases, version)
    started("releases", [sys.executable, "-m", "http.server", str(RELEASES_PORT), "--bind",
                         "127.0.0.1", "--directory", str(releases)])  # fmt: skip
    started("oidc", [sys.executable, str(HERE / "oidc_stub.py"), str(STUB_PORT)])
    started("github", [sys.executable, str(HERE / "github_stand_in.py"), "serve",
                       str(state() / "github"), "127.0.0.1", str(GITHUB_PORT)])  # fmt: skip
    rcman_started()
    rendered = install_settings()
    ins.ETC.mkdir(parents=True, exist_ok=True)
    proxy_config = ins.ETC / "oauth2-proxy.cfg"
    proxy_config.write_text(
        ins.render("oauth2-proxy.cfg", rendered)
        + "# The whole stack's stand-in for Google:\n"
        + f'login_url = "https://{AUTH}/authorize"\n'
        + f'redeem_url = "http://127.0.0.1:{STUB_PORT}/token"\n'
        + f'validate_url = "http://127.0.0.1:{STUB_PORT}/tokeninfo"\n'
    )
    started("oauth2-proxy", [str(ins.oauth2_proxy_binary()), "--config", str(proxy_config)])
    wait("oauth2-proxy", lambda: tcp_answers(PROXY_PORT))
    caddy_started(rendered)
    github = ["curl", "-sk", "-o", "/dev/null", "-w", "%{http_code}", "--resolve",
              "github.com:443:127.0.0.1",
              f"https://github.com/{stand_in.STACK_OWNER}/{stand_in.STACK_PUBLIC}/info/refs"
              "?service=git-upload-pack"]  # fmt: skip
    wait("the stand-in GitHub", lambda: subprocess.run(
        github, capture_output=True, text=True).stdout == "200")  # fmt: skip
    (state() / "people.json").write_text(json.dumps(people()))
    print("READY", flush=True)


def caddy(sites: str = "", ask: str = ASK_URL) -> None:
    """Caddy ended and started again, with the sites in the file ``sites`` after the stack's
    own, or with the stack's own only, its global options asking ``ask``, or asking nothing
    where ``ask`` is empty. Where Caddy does not
    load the result, the verb fails and Caddy is left ended. The settings are rendered again,
    with a fresh cookie secret that only oauth2-proxy's configuration, which is not written
    again, would hold: the Caddyfile has none."""
    ended("caddy")
    caddy_started(install_settings(), Path(sites).read_text() if sites else "", ask)
    print("CADDY", flush=True)


def restart() -> None:
    """rcman's app ended and started again, on the same state."""
    ended("rcman")
    rcman_started()
    print("RESTARTED", flush=True)


def request(text: str) -> None:
    """One request to rcman's socket, as Caddy passes one on: ``text`` is the JSON of the
    method, the path, the headers and the body; what is printed, the JSON of the status and the
    body."""
    import http.client

    method, url, headers, body = json.loads(text)

    class Connection(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX)
            self.sock.settimeout(120)
            self.sock.connect(str(SOCKET))

    connection = Connection("rcman")
    connection.request(method, url, body=body, headers=headers)
    answer = connection.getresponse()
    print(json.dumps([answer.status, answer.read().decode(errors="replace")]))


class Browser:
    """Requests to the stack's sites through Caddy, as a browser makes them, through one of
    three openers: ``plain`` keeps no cookies and answers redirects as they are; ``follows``
    keeps the cookies it is given in ``jar`` and follows redirects; ``steps`` keeps them in the
    same ``jar`` and answers redirects as they are. ``fetch`` answers the status, the body and
    the headers as a list of pairs."""

    def __init__(self):
        import http.cookiejar
        import ssl
        import urllib.request

        context = ssl.create_default_context()
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE  # Caddy's own authority
        self.jar = http.cookiejar.CookieJar()

        class Kept(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args):
                return None

        https = urllib.request.HTTPSHandler(context=context)
        cookie_handler = urllib.request.HTTPCookieProcessor(self.jar)
        self.plain = urllib.request.build_opener(https, Kept())
        self.follows = urllib.request.build_opener(https, cookie_handler)
        self.steps = urllib.request.build_opener(https, cookie_handler, Kept())

    @staticmethod
    def fetch(opener, url, headers=None):
        import urllib.error
        import urllib.request

        try:
            with opener.open(urllib.request.Request(url, headers=headers or {}), timeout=30) as r:
                return r.status, r.read().decode(errors="replace"), r.headers.items()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace"), e.headers.items()


def identity() -> None:
    """Through Caddy and oauth2-proxy, as a browser: a request that sets the identity header
    itself and has no session is sent to sign in, and once signed in through the stand-in for
    Google its header is not what rcman is told. What is printed, the JSON of what was seen."""
    import urllib.parse

    browser = Browser()
    spoof = {"X-Forwarded-Email": "evil@example.com"}
    status, _, headers = browser.fetch(browser.plain, f"https://{DOMAIN}/api/me", spoof)
    seen = {"unsigned": [status, dict(headers)]}
    status, page, _ = browser.fetch(browser.follows, f"https://{DOMAIN}/oauth2/start?rd=/")
    form = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', page))
    query = urllib.parse.urlencode({**form, "email": ADMIN})
    status, _, _ = browser.fetch(browser.follows, f"https://{AUTH}/authorize?{query}")
    seen["signed_in"] = status  # back through the callback
    seen["me"] = browser.fetch(browser.follows, f"https://{DOMAIN}/api/me", spoof)[:2]
    print(json.dumps(seen))


def set_cookies_of(headers) -> list[list]:
    """The ``Set-Cookie`` headers among ``headers``, each with its value left out, as
    ``[name; attributes, whether the value was empty]``."""
    found = []
    for key, value in headers:
        if key.lower() == "set-cookie":
            pair, _, attributes = value.partition(";")
            name, _, value = pair.partition("=")
            found.append([name + ";" + attributes, value.strip().strip('"') == ""])
    return found


def cookies() -> None:
    """A sign-in through Caddy, oauth2-proxy and the stand-in for Google, one redirect at a
    time, as a browser makes it, then a sign-out. What is printed, the JSON of every
    ``Set-Cookie`` the sign-in's responses from rcman's host carried, and the sign-out's (see
    ``set_cookies_of``), and what ``/api/me`` answered after each."""
    import urllib.parse

    browser = Browser()
    set_cookies = []
    url = f"https://{DOMAIN}/oauth2/start?rd=/"
    for _ in range(10):
        status, page, headers = browser.fetch(browser.steps, url)
        if urllib.parse.urlsplit(url).hostname == DOMAIN:
            set_cookies += set_cookies_of(headers)
        location = dict((k.lower(), v) for k, v in headers).get("location")
        if status not in (301, 302, 303, 307) or not location:
            break
        url = urllib.parse.urljoin(url, location)
        if urllib.parse.urlsplit(url).path == "/authorize" and "email=" not in url:
            status, page, _ = browser.fetch(browser.steps, url)  # the stand-in's form
            form = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', page))
            url = f"https://{AUTH}/authorize?" + urllib.parse.urlencode({**form, "email": ADMIN})
    me = browser.fetch(browser.steps, f"https://{DOMAIN}/api/me")[:2]
    _, _, headers = browser.fetch(browser.steps, f"https://{DOMAIN}/oauth2/sign_out")
    signed_out = set_cookies_of(headers)
    after, _, headers = browser.fetch(browser.steps, f"https://{DOMAIN}/api/me")
    location = dict((k.lower(), v) for k, v in headers).get("location", "")
    print(json.dumps({"set_cookies": set_cookies, "last": [status, url], "me": me,
                      "signed_out": signed_out, "me_after": [after, location]}))  # fmt: skip


def repository(workspace_id: str) -> None:
    """A repository in the workspace, with a bare origin in it, and changes to stage."""
    in_workspace(int(workspace_id), MAKE_REPOSITORY)
    (state() / "repository.json").write_text(json.dumps({"workspace": int(workspace_id)}))
    print("REPOSITORY", flush=True)


# Run by the workspace's python3, on its 127.0.0.1 at the port it is given: the app of a
# forwarded port the browser tests reach. A page that shows the path and query it was asked for
# (``#seen``), and a WebSocket at any path that sends back every message it is sent.
PORT_APP = r"""
import base64, hashlib, html, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.headers.get("Upgrade", "").lower() == "websocket":
            key = self.headers["Sec-WebSocket-Key"] + GUID
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            accept = base64.b64encode(hashlib.sha1(key.encode()).digest()).decode()
            self.send_header("Sec-WebSocket-Accept", accept)
            self.end_headers()
            self.close_connection = True
            self.echo()
            return
        body = ("<!doctype html><title>forwarded</title><p id=seen>" + html.escape(self.path)
                + "</p>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def echo(self):
        read = self.rfile.read
        while len(head := read(2)) == 2:
            opcode, length = head[0] & 0x0F, head[1] & 0x7F
            if length == 126:
                length = int.from_bytes(read(2), "big")
            elif length == 127:
                length = int.from_bytes(read(8), "big")
            mask = read(4) if head[1] & 0x80 else bytes(4)
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(read(length)))
            if opcode == 8:
                self.wfile.write(bytes([0x88, len(data)]) + data)
                return
            opcode = 10 if opcode == 9 else opcode
            n = len(data)
            size = bytes([n]) if n < 126 else (bytes([126]) + n.to_bytes(2, "big") if n < 65536
                                               else bytes([127]) + n.to_bytes(8, "big"))
            self.wfile.write(bytes([0x80 | opcode]) + size + data)

ThreadingHTTPServer(("127.0.0.1", int(sys.argv[1])), Handler).serve_forever()
"""
# Run by the workspace's python3: whether something listens on its 127.0.0.1 at the port.
PORT_OPEN = "import socket, sys; socket.create_connection(('127.0.0.1', int(sys.argv[1])), 2)"


def serve_port(workspace_id: str, port: str) -> None:
    """PORT_APP started in the workspace on ``port``, once it listens."""
    container = f"rcman-ws-{int(workspace_id)}"
    podman("exec", "--detach", container, "python3", "-c", PORT_APP, str(int(port)))
    deadline = time.monotonic() + ANSWERS_WITHIN
    while podman("exec", container, "python3", "-c", PORT_OPEN, str(int(port)),
                 check=False).returncode != 0:  # fmt: skip
        if time.monotonic() > deadline:
            sys.exit(f"nothing listens on port {port} in {container}")
        time.sleep(0.5)
    print("SERVING", flush=True)


def check() -> None:
    """What the browser tests left: nothing of carol's, whom they deleted."""
    carol = json.loads((state() / "people.json").read_text())["carol@example.com"]
    label = f"label=rcman.person={carol}"
    left = podman("ps", "--all", "--quiet", "--filter", label).stdout.split()
    volumes = podman("volume", "ls", "--quiet", "--filter", label).stdout.split()
    assert left == [] and volumes == [], (left, volumes)
    assert "carol@example.com" not in people()
    assert "carol@example.com" not in ALLOWED.read_text()
    pushed()
    cloned()
    print("CHECKED", flush=True)


def pushed() -> None:
    """The changes the browser tests staged, committed and pushed are in the origin as the
    person's commit, the workspace has nothing left to commit, and the audit has a row for
    each step but none holding the message."""
    workspace = json.loads((state() / "repository.json").read_text())["workspace"]
    log = in_workspace(workspace, f"git -C {ORIGIN} log --format='%an|%ae|%s' main").splitlines()
    assert log == [f"{AUTHOR[0]}|{AUTHOR[1]}|{MESSAGE}", f"{SETUP[0]}|{SETUP[1]}|First commit"], log
    assert in_workspace(workspace, f"git -C {ORIGIN} show main:notes.txt") == "new\n"
    assert in_workspace(workspace, f"git -C {ORIGIN} show main:hello.txt") == "one\ntwo\n"
    assert in_workspace(workspace, f"git -C {WORK} status --porcelain") == ""
    here = in_workspace(workspace, f"git -C {WORK} rev-parse HEAD")
    assert here == in_workspace(workspace, f"git -C {ORIGIN} rev-parse main")  # nothing left

    con = sqlite3.connect(f"file:{database()}?mode=ro", uri=True)
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


def cloned() -> None:
    """The workspaces the browser tests made by cloning from the stand-in GitHub: each is
    ready, holds its repository's commit, and was made by an audited ``git.clone`` that
    names the address; the token the browser tests stored is in rcman's database and in no
    audit row, workspace row or line of rcman's output."""
    sys.path.insert(0, str(SRC))
    from tests.container import github_stand_in as stand_in

    public = f"https://github.com/{stand_in.STACK_OWNER}/{stand_in.STACK_PUBLIC}"
    private = f"https://github.com/{stand_in.STACK_OWNER}/{stand_in.STACK_PRIVATE}"
    con = sqlite3.connect(f"file:{database()}?mode=ro", uri=True)
    try:
        clones = con.execute(
            "select target, result from audit where email = ? and action = 'git.clone' order by id",
            (ADMIN,),
        ).fetchall()
        workspaces = con.execute(
            "select id, name, state, source, error, log from workspaces where person = "
            "(select id from people where email = ?) and source is not null order by id",
            (ADMIN,),
        ).fetchall()
        stored = con.execute("select token from github_tokens").fetchall()
        everything = "\n".join(
            " ".join(map(str, row))
            for table in ("audit", "workspaces", "user_settings")
            for row in con.execute(f"select * from {table}")
        )
    finally:
        con.close()
    # The public one, the private one that failed without a token, the private one again.
    assert clones == [(a, "ok") for a in (public, private, private)], clones
    assert [(w[1], w[2], w[3], w[4], w[5]) for w in workspaces] == [
        (stand_in.STACK_PUBLIC, "ready", public, None, None),
        (stand_in.STACK_PRIVATE, "ready", private, None, None),
    ], workspaces  # the failed one was deleted
    for workspace, name, *_ in workspaces:
        log = in_workspace(workspace, f"git -C {WORK} log --format=%s").splitlines()
        assert log == [f"First commit of {name}"], (name, log)
    assert [row[0] for row in stored] == [stand_in.STACK_TOKEN], "the token was not stored"
    assert stand_in.STACK_TOKEN not in everything
    output = (state() / "logs" / "rcman.log").read_text(errors="replace")
    assert public in output and "git.clone" in output  # positive: the audit is in its output
    assert stand_in.STACK_TOKEN not in output


# --- on the host ----------------------------------------------------------------------------


class Stack:
    """The stack's container on the engine, and its verbs."""

    def __init__(self, engine, name: str, folder: Path):
        self.engine, self.name, self.folder = engine, name, folder

    @property
    def network(self) -> str:
        return f"container:{self.name}"

    def exec(self, *argv: str, stdin: bytes | None = None, timeout: float = 600):
        return self.engine.cli(
            "exec", "-i", "-w", str(SRC_IN_CONTAINER), "-e",
            f"RCMAN_STACK_CONFIG={self.folder / 'config.json'}", "-e",
            "PYTHONDONTWRITEBYTECODE=1", self.name, *argv, input=stdin, timeout=timeout,
        )  # fmt: skip

    def request(self, method: str, url: str, who: str | None = ADMIN, body=None):
        """A request to rcman as Caddy passes it on for ``who`` (none: no identity): the status
        and the answer, as JSON where it is JSON."""
        headers = {"X-Forwarded-Email": who} if who else {}
        if method != "GET":
            headers |= {"Origin": f"https://{DOMAIN}", "Content-Type": "application/json"}
        sent = json.dumps([method, url, headers, None if body is None else json.dumps(body)])
        status, text = json.loads(self.verb("request", sent).strip().splitlines()[-1])
        try:
            return status, json.loads(text)
        except ValueError:
            return status, text

    def verb(self, *args: str, timeout: float = 600) -> str:
        """``stack.py`` with ``args`` in the container: what it printed; it must succeed."""
        done = self.exec("python3", "tests/container/stack.py", *args, timeout=timeout)
        output = (done.stdout + done.stderr).decode(errors="replace")
        assert done.returncode == 0, output[-12000:]
        return output


# The checkout's files in the stack's container.
SRC_IN_CONTAINER = Path("/src")


@contextlib.contextmanager
def launched(engine, *, name: str, system_image: str, default_image: str, builder_image: str,
             tools: Path, store: Path, prepared: Path, toolchain: Path, source: Path,
             labelled: list[str]):  # fmt: skip
    """The stack, up, in a container called ``name``: its workspaces' image derived from
    ``default_image``, the backend's packages and the built frontend from ``prepared`` (a folder
    with ``venv`` and ``dist``), the tools, the store and its own state at the same paths on the
    host and in the container, as the engine resolves what the stack's rcman asks it to mount."""
    from devtools import held_folders as hf
    from tests.container import github_stand_in as stand_in

    with hf.held("rcstack-") as folder:
        os.chmod(folder, 0o755)
        pki = stand_in.mint(folder / "pki")
        (folder / "github").mkdir()
        image_name = f"localhost/rcman-test-stack:{os.getpid()}"
        context = folder / "image"
        context.mkdir()
        shutil.copy(pki / "ca.crt", context / "ca.crt")
        (context / "Containerfile").write_text(stand_in.dockerfile(default_image))
        built = engine.cli(
            "build",
            "-q",
            "--pull=never",
            "-t",
            image_name,
            "-f",
            str(context / "Containerfile"),
            str(context),
            timeout=1800,
        )
        assert built.returncode == 0, built.stderr.decode(errors="replace")
        (folder / "config.json").write_text(json.dumps({
            "engine_socket": str(engine.socket), "image": image_name,
            "builder_image": builder_image, "tools": str(tools), "store": str(store),
            "state": str(folder), "pki": str(pki), "network": f"container:{name}",
        }))  # fmt: skip
        stand_ins = source / "tests" / "container" / "stand_ins.py"
        made = engine.cli(
            "run", "-d", "--init", "--pull=never", "--network=none", f"--hostname={DOMAIN}",
            *(f"--add-host={host}:127.0.0.1" for host in (DOMAIN, AUTH, "github.com")), *labelled,
            "--name", name, "-v", f"{source}:{SRC_IN_CONTAINER}:ro",
            "-v", f"{toolchain}:/opt/rcman/toolchain:ro", "-v", f"{prepared / 'venv'}:{VENV}:ro",
            "-v", f"{prepared / 'dist'}:{DIST}:ro", "-v", f"{tools}:{tools}:ro",
            "-v", f"{store}:{store}", "-v", f"{folder}:{folder}",
            "-v", f"{engine.socket}:{engine.socket}", "-v", f"{stand_ins}:/usr/bin/systemctl:ro",
            system_image, "sleep", "infinity", timeout=300,
        )  # fmt: skip
        assert made.returncode == 0, made.stderr.decode(errors="replace")
        stack = Stack(engine, name, folder)
        try:
            stack.verb("up", timeout=900)
            yield stack
        finally:
            for kind in ("ps", "volume"):
                listed = engine.cli(
                    *(["ps", "--all"] if kind == "ps" else ["volume", "ls"]), "--quiet",
                    "--filter", "label=rcman.person",
                )  # fmt: skip
                for found in listed.stdout.decode().split():
                    engine.cli(*(["rm"] if kind == "ps" else ["volume", "rm"]), "--force", found)
            engine.cli("rm", "--force", "--time=0", name, timeout=300)
            engine.cli("rmi", "--force", image_name, timeout=300)


if __name__ == "__main__":
    verb, *arguments = sys.argv[1:]
    sys.path.insert(0, str(SRC / "backend"))
    run = {"up": up, "restart": restart, "caddy": caddy, "check": check, "repository": repository,
           "serve-port": serve_port,
           "request": request, "identity": identity, "cookies": cookies,
           "admin-delete": admin_delete, "admin-delete-here": admin_delete_here}[verb]  # fmt: skip
    run(*arguments)
