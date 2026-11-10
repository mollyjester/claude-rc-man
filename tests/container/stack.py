#!/usr/bin/python3
"""The whole of rcman in one container of the tests' engine, for the checks of the whole stack.

On the host, ``launched`` starts it (``a_stack`` in tests/container/conftest.py): a container of the
system image (tests/container/system/) on the tests' engine (tests/podman_service.py) with no
network but its own loopback, which the stack's workspaces join (``container:<its name>``). In
it, as processes of their own, run:

- rcman's app, made as devtools/world.py makes one but with the Podman runtime at the suites'
  sizes (tests/container/sizes.py) on the engine, whose socket it is given; alice@example.com its
  first administrator, bob let in; the fake claude in the store; its listeners the unix
  sockets the installer gives it, its own and its forwarding server's;
- oauth2-proxy (from the installer's toolchain) with the installer's own configuration, its Google
  endpoints pointed at tests/container/oidc_stub.py;
- Caddy with the installer's own site block for rcman.test and its forwarded ports, its own
  certificate authority, its admin API off and on-demand certificates asked of a site on its
  loopback (``global_options``), in front of rcman.test, auth.test (the stub) and github.com: the
  stand-in GitHub (tests/github_server.py through tests/container/github_stand_in.py ``serve``,
  with a public repository) under a certificate of an authority of its own, which the
  workspaces' image, derived from the default image, trusts, and where its git finds github.com;
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
  ``identity`` asks for the installed app's files and paths beside them without a sign-in, and
  signs in to a page asked for through Caddy, oauth2-proxy and the stand-in for Google as a
  browser does, and ``cookies`` signs in the same way one redirect at a time and then out, each
  printing what it was answered.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
from pathlib import Path

# Run in the stack's container from the checkout's files at /src, as well as imported by the
# tests: the checkout's root on the path, for the hang guard's values and until.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from tests.hang_guard import CONTAINER_HANG_GUARD, EXEC_GUARD, GUARD_VARIABLE  # noqa: E402
from tests.until import until  # noqa: E402

HERE = Path(__file__).resolve().parent
SRC = HERE.parent.parent

DOMAIN = "rcman.test"
AUTH = "auth.test"
ADMIN = "alice@example.com"
OTHER = "bob@example.com"
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
    that lacks the block the installer adds."""
    on_demand = f"\ton_demand_tls {{\n\t\task {ask}\n\t}}\n" if ask else ""
    return "{\n\tadmin off\n\tlocal_certs\n\tskip_install_trust\n" + on_demand + "}\n"


# rcman's sockets and its allowed list, where the installer puts them.
SOCKET = Path("/run/rcman/rcman.sock")
FORWARD_SOCKET = Path("/run/rcman/forward.sock")
ALLOWED = Path("/var/lib/rcman/allowed-emails")
# Where the backend's packages and the built frontend are in the stack's container.
VENV = "/opt/rcman-test-venv"
DIST = "/opt/rcman-test-dist"
# A workspace's work folder.
WORK = "/rcman/work"
# --- in the stack's container --------------------------------------------------------------


def config() -> dict:
    return json.loads(Path(os.environ["RCMAN_STACK_CONFIG"]).read_text())


def state() -> Path:
    return Path(config()["state"])


def podman(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    """podman on the engine, through its socket."""
    proc = subprocess.run(
        ["podman", "--remote", "--url", f"unix://{config()['engine_socket']}", *args],
        capture_output=True, text=True, errors="replace", timeout=CONTAINER_HANG_GUARD,
    )  # fmt: skip
    assert proc.returncode == 0 or not check, proc.stdout + proc.stderr
    return proc


def database() -> Path:
    return state() / "rcman.db"


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
    """rcman's app, as devtools/world.py makes one but on the engine: the people of the stack,
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
            rcman_people.add(con, OTHER, False, last_slot=sizes.PEOPLE_SLOTS[-1])
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
    """The process ``name`` and its group sent SIGTERM, and waited for until the group is gone.
    Nothing is killed: a group still there at the hang bound ends the verb, saying so."""
    pid_file = state() / "logs" / f"{name}.pid"
    pid = int(pid_file.read_text())
    with contextlib.suppress(ProcessLookupError):
        os.killpg(pid, signal.SIGTERM)
    try:
        until(lambda: not group_alive(pid))
    except AssertionError:
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
            self.sock.settimeout(CONTAINER_HANG_GUARD)
            self.sock.connect(str(SOCKET))

    connection = Connection("rcman")
    connection.request("GET", url)
    return connection.getresponse().status


def tcp_answers(port: int) -> bool:
    socket.create_connection(("127.0.0.1", port), timeout=CONTAINER_HANG_GUARD).close()
    return True


def wait(what: str, probe) -> None:
    """Until ``probe`` answers; at the hang bound the verb ends with the stack's logs."""
    try:
        until(lambda: answers(probe))
    except AssertionError:
        logs = "".join(
            f"\n--- {p.name}\n{p.read_text(errors='replace')[-4000:]}"
            for p in sorted((state() / "logs").glob("*.log"))
        )
        sys.exit(f"{what} does not answer{logs}")


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
            self.sock.settimeout(CONTAINER_HANG_GUARD)
            self.sock.connect(str(SOCKET))

    connection = Connection("rcman")
    connection.request(method, url, body=body, headers=headers)
    answer = connection.getresponse()
    print(json.dumps([answer.status, answer.read().decode(errors="replace")]))


class Browser:
    """Requests to the stack's sites through Caddy, as a browser makes them, through one of
    three openers: ``plain`` keeps no cookies and answers redirects as they are; ``follows``
    keeps the cookies it is given in ``jar`` and follows redirects; ``steps`` keeps them in the
    same ``jar`` and answers redirects as they are. ``fetch`` answers the status, the body, the
    headers as a list of pairs and the address it ended at."""

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
            request = urllib.request.Request(url, headers=headers or {})
            with opener.open(request, timeout=CONTAINER_HANG_GUARD) as r:
                return r.status, r.read().decode(errors="replace"), r.headers.items(), r.geturl()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(errors="replace"), e.headers.items(), e.geturl()


def app_files() -> list[str]:
    """The paths of the installed app's files in the stack's build: the manifest, each icon it
    names and each icon index.html links."""
    manifest = json.loads((Path(DIST) / "manifest.webmanifest").read_text())
    index = (Path(DIST) / "index.html").read_text()
    linked = re.findall(r'<link rel="(?:icon|apple-touch-icon)"[^>]*href="([^"]+)"', index)
    return ["/manifest.webmanifest", *(icon["src"] for icon in manifest["icons"]), *linked]


def identity() -> None:
    """Through Caddy and oauth2-proxy, as a browser: a request that sets the identity header
    itself and has no session is sent to sign in, as is one for a path beside the installed
    app's files, while each of those files is answered as it is; a page asked for without a
    session is signed in for through the stand-in for Google and is where the sign-in ends,
    and once signed in the request's own header is not what rcman is told. What is printed,
    the JSON of what was seen."""
    import urllib.parse

    browser = Browser()
    spoof = {"X-Forwarded-Email": "evil@example.com"}
    status, _, headers, _ = browser.fetch(browser.plain, f"https://{DOMAIN}/api/me", spoof)
    seen = {"unsigned": [status, dict(headers)]}
    exempt = app_files()
    seen["app_files"] = {}
    for path in exempt:
        status, body, _, _ = browser.fetch(browser.plain, f"https://{DOMAIN}{path}", spoof)
        seen["app_files"][path] = [status, body if path.endswith(".webmanifest") else body[:16]]
    icon = next(path for path in exempt if path.startswith("/icons/"))
    beside = ["/icons/made-up.png", icon.upper(), "/manifest.webmanifest/x"]
    seen["beside"] = {}
    for path in beside:
        status, _, headers, _ = browser.fetch(browser.plain, f"https://{DOMAIN}{path}", spoof)
        seen["beside"][path] = [status, dict(headers).get("Location", "")]
    asked = f"https://{DOMAIN}/settings?workspace=1"
    status, page, _, _ = browser.fetch(browser.follows, asked)
    form = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', page))
    query = urllib.parse.urlencode({**form, "email": ADMIN})
    status, _, _, landed = browser.fetch(browser.follows, f"https://{AUTH}/authorize?{query}")
    seen["signed_in"] = [status, landed]  # back through the callback
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
    ``set_cookies_of``) with its ``Date``, and what ``/api/me`` answered after each."""
    import urllib.parse

    browser = Browser()
    set_cookies = []
    url = f"https://{DOMAIN}/oauth2/start?rd=/"
    for _ in range(10):
        status, page, headers, _ = browser.fetch(browser.steps, url)
        if urllib.parse.urlsplit(url).hostname == DOMAIN:
            set_cookies += set_cookies_of(headers)
        location = dict((k.lower(), v) for k, v in headers).get("location")
        if status not in (301, 302, 303, 307) or not location:
            break
        url = urllib.parse.urljoin(url, location)
        if urllib.parse.urlsplit(url).path == "/authorize" and "email=" not in url:
            status, page, _, _ = browser.fetch(browser.steps, url)  # the stand-in's form
            form = dict(re.findall(r'name="([^"]+)" value="([^"]*)"', page))
            url = f"https://{AUTH}/authorize?" + urllib.parse.urlencode({**form, "email": ADMIN})
    me = browser.fetch(browser.steps, f"https://{DOMAIN}/api/me")[:2]
    _, _, headers, _ = browser.fetch(browser.steps, f"https://{DOMAIN}/oauth2/sign_out")
    signed_out = set_cookies_of(headers)
    signed_out_at = dict((k.lower(), v) for k, v in headers).get("date")
    after, _, headers, _ = browser.fetch(browser.steps, f"https://{DOMAIN}/api/me")
    location = dict((k.lower(), v) for k, v in headers).get("location", "")
    print(json.dumps({"set_cookies": set_cookies, "last": [status, url], "me": me,
                      "signed_out": signed_out, "signed_out_at": signed_out_at,
                      "me_after": [after, location]}))  # fmt: skip


# --- on the host ----------------------------------------------------------------------------


class Stack:
    """The stack's container on the engine, and its verbs."""

    def __init__(self, engine, name: str, folder: Path):
        self.engine, self.name, self.folder = engine, name, folder

    def exec(self, *argv: str, stdin: bytes | None = None):
        """``argv`` in the stack's container, handed its tier's hang guard (``until``'s
        bound there) and killed beyond it (EXEC_GUARD), so that a wait there that never ends
        says what it waited for."""
        return self.engine.cli(
            "exec", "-i", "-w", str(SRC_IN_CONTAINER), "-e",
            f"RCMAN_STACK_CONFIG={self.folder / 'config.json'}", "-e",
            f"{GUARD_VARIABLE}={CONTAINER_HANG_GUARD}", "-e", "PYTHONDONTWRITEBYTECODE=1",
            self.name, *argv, input=stdin, timeout=EXEC_GUARD,
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

    def verb(self, *args: str) -> str:
        """``stack.py`` with ``args`` in the container: what it printed; it must succeed."""
        done = self.exec("python3", "tests/container/stack.py", *args)
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
            timeout=CONTAINER_HANG_GUARD,
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
            system_image, "sleep", "infinity", timeout=CONTAINER_HANG_GUARD,
        )  # fmt: skip
        assert made.returncode == 0, made.stderr.decode(errors="replace")
        stack = Stack(engine, name, folder)
        try:
            stack.verb("up")
            yield stack
        finally:
            for kind in ("ps", "volume"):
                listed = engine.cli(
                    *(["ps", "--all"] if kind == "ps" else ["volume", "ls"]), "--quiet",
                    "--filter", "label=rcman.person",
                )  # fmt: skip
                for found in listed.stdout.decode().split():
                    engine.cli(*(["rm"] if kind == "ps" else ["volume", "rm"]), "--force", found)
            engine.cli("rm", "--force", "--time=0", name, timeout=CONTAINER_HANG_GUARD)
            engine.cli("rmi", "--force", image_name, timeout=CONTAINER_HANG_GUARD)


if __name__ == "__main__":
    verb, *arguments = sys.argv[1:]
    sys.path.insert(0, str(SRC / "backend"))
    run = {"up": up, "restart": restart, "caddy": caddy, "request": request, "identity": identity,
           "cookies": cookies, "admin-delete": admin_delete,
           "admin-delete-here": admin_delete_here}[verb]  # fmt: skip
    run(*arguments)
