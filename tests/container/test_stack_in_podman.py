"""The whole of rcman in one container of the tests' engine (tests/container/stack.py): what an
installation does once people and workspaces are there, through rcman's own socket as Caddy
passes requests on, and through Caddy and oauth2-proxy; the network the stack's workspaces
share; and what the stack's Caddy does for a forwarded port: certificates on demand, asked of a
site on its loopback, and for any name when nothing is asked; a log without cookies or query
strings; and the installer's own site deleting the identity headers. The browser tests run against
a stack of their own (tests/container/test_stack_in_a_browser.py).

The stack's workspaces run on the engine (tests/podman_service.py) at the suites' sizes
(tests/container/sizes.py), in the default image with the stand-in GitHub's authority trusted, and
join the stack's own network, which has no way out. What needs systemd, the journal or the
kernel's limits is checked by hand (project-knowledge/real-claude-checklist.md).
"""

from __future__ import annotations

import json
import os
import re
import secrets
import socket
import time
import urllib.parse
from pathlib import Path

import pytest
from rcman import limits
from rcman import runtime as rt

from devtools.fake_claude import ACCEPTED_CODE
from installer import install as ins
from tests.container import github_stand_in as stand_in
from tests.container import network
from tests.container import stack as st
from tests.container.conftest import a_stack

BOB = "bob@example.com"
EVE = "eve@example.com"  # added by a test
# Seconds a workspace, or a server, may take to be what a test waits for.
COMES_WITHIN = 300
REPO = Path(__file__).resolve().parent.parent.parent


@pytest.fixture(scope="module")
def stack(podman_service, system_image, default_image, builder_image, engine_tools,
          stack_prepared, toolchain, source, labelled):  # fmt: skip
    with a_stack(
        podman_service, f"rcman-test-stack-{os.getpid()}", system_image, default_image,
        builder_image, engine_tools, stack_prepared, toolchain, source, labelled,
    ) as found:  # fmt: skip
        yield found


# --- what the tests ask the stack -----------------------------------------------------------


def until(what, within: float = COMES_WITHIN, every: float = 1.0):
    deadline = time.monotonic() + within
    while not (found := what()):
        assert time.monotonic() < deadline, f"never: {what.__doc__ or what}"
        time.sleep(every)
    return found


def podman(stack, *args: str) -> str:
    done = stack.engine.cli(*args, timeout=300)
    assert done.returncode == 0, done.stderr
    return done.stdout.decode()


def in_workspace(stack, workspace_id: int, script: str, check: bool = True) -> str:
    done = stack.engine.cli(
        "exec", f"rcman-ws-{workspace_id}", "/opt/rcman/bin/busybox", "sh", "-c", script
    )
    assert done.returncode == 0 or not check, done.stderr
    return done.stdout.decode()


def workspace_of(stack, name: str, who: str = st.ADMIN) -> dict | None:
    status, listed = stack.request("GET", "/api/workspaces", who)
    assert status == 200, listed
    return next((w for w in listed["workspaces"] if w["name"] == name), None)


def made(stack, name: str, who: str = st.ADMIN) -> dict:
    status, answer = stack.request("POST", "/api/workspaces", who, {"name": name})
    assert status == 202, answer

    def ready():
        found = workspace_of(stack, name, who)
        return found if found and found["state"] != "creating" else None

    found = until(ready)
    assert found["state"] == "ready", found
    return found


def server_state(stack, name: str, who: str = st.ADMIN) -> str | None:
    found = workspace_of(stack, name, who)
    return found and found["server"] and found["server"]["state"]


def claude_processes(stack, workspace_id: int) -> list[int]:
    """The ids, in the workspace, of every process of the fake claude there. The pattern is
    written so that it does not find the shell that looks for it."""
    out = in_workspace(
        stack,
        workspace_id,
        "for f in /proc/[0-9]*/cmdline; do "
        'if /opt/rcman/bin/busybox tr "\\0" " " < "$f" 2>/dev/null | '
        "/opt/rcman/bin/busybox grep -q 'fakes/clau[d]e'; then "
        'p="${f#/proc/}"; echo "${p%/cmdline}"; fi; done',
    )
    return sorted(int(pid) for pid in out.split())


def ready_server(stack, name: str) -> None:
    found = workspace_of(stack, name)
    status, answer = stack.request("POST", "/api/servers", body={"workspace": found["id"]})
    assert status == 200 and answer["server"]["state"] == "trust_prompt", answer
    status, answer = stack.request(
        "POST", "/api/servers/confirm", body={"workspace": found["id"], "answer": "y"}
    )
    assert status == 200, answer
    until(lambda: server_state(stack, name) == "ready")
    assert claude_processes(stack, found["id"])


def person_id(stack, email: str = st.ADMIN) -> int:
    status, everyone = stack.request("GET", "/api/people")
    assert status == 200, everyone
    return next(p["id"] for p in everyone["people"] if p["email"] == email)


def in_stack(stack, *argv: str) -> str:
    done = stack.exec(*argv)
    assert done.returncode == 0, done.stderr
    return done.stdout.decode()


def held_properties(stack) -> dict:
    """What the systemctl stand-in holds of each slice (tests/container/stand_ins.py)."""
    return json.loads(in_stack(stack, "cat", "/var/lib/rcman-test-stand-ins/slices.json"))


def bound(stack) -> set[tuple[str, int]]:
    """Every listening TCP address (host, port) of the stack's network, IPv4 and IPv6, as the
    kernel's tables have it: one on every address is ``0.0.0.0`` or ``::``."""
    found = set()
    for table, family in (("/proc/net/tcp", socket.AF_INET), ("/proc/net/tcp6", socket.AF_INET6)):
        for line in in_stack(stack, "cat", table).splitlines()[1:]:
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
            found.add((host, int(port, 16)))
    return found


def listeners(stack) -> set[tuple[str, int]]:
    """Every listening TCP address (host, port) of the stack's network, IPv4 and IPv6; one on
    every address is taken at the loopback's."""
    loopback = {"0.0.0.0": "127.0.0.1", "::": "::1"}
    return {(loopback.get(host, host), port) for host, port in bound(stack)}


# --- what is there once it runs --------------------------------------------------------------


def test_services_answer(stack):
    assert stack.request("GET", "/api/health", None) == (200, {"status": "ok"})
    proxy = in_stack(stack, "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
                     f"http://127.0.0.1:{st.PROXY_PORT}/ping")  # fmt: skip
    assert proxy == "200"
    status, body = stack.request("GET", "/", None)
    assert status == 200 and '<div id="root">' in body


def test_the_stacks_administrator_is_an_administrator(stack):
    """alice, whom the stack's app adds as an administrator (tests/container/stack.py), is one to
    rcman; the installer's first administrator is the stubbed installation's
    (tests/container/test_install_in_container.py)."""
    status, me = stack.request("GET", "/api/me")
    assert status == 200 and me["admin"] is True and me["email"] == st.ADMIN


def test_no_listener_takes_an_identity_a_request_gives_itself(stack):
    """Whatever listens on the stack's network, a request that sets the identity headers itself
    is not taken for that person there: rcman's listeners are its sockets, which only Caddy's
    group may enter on an installation (checked by hand, and by the installation's checks)."""
    spoof = {"X-Forwarded-Email": st.ADMIN, "X-Auth-Request-Email": st.ADMIN}
    header = [arg for key, value in spoof.items() for arg in ("-H", f"{key}: {value}")]
    tcp = listeners(stack)
    assert ("127.0.0.1", st.PROXY_PORT) in tcp and {("127.0.0.1", 443), ("::1", 443)} & tcp
    for host, port in sorted(tcp):
        where = f"[{host}]" if ":" in host else host
        out = stack.exec("curl", "-sk", "-m", "10", *header, f"http://{where}:{port}/api/me")
        text = out.stdout.decode(errors="replace")
        assert '"admin":true' not in text and f'"email":"{st.ADMIN}"' not in text, (port, text)
    # oauth2-proxy without its cookie names no one.
    asked = in_stack(stack, "curl", "-s", "-D", "-", "-o", "/dev/null", *header,
                     f"http://127.0.0.1:{st.PROXY_PORT}/oauth2/auth")  # fmt: skip
    assert asked.split()[1] == "401" and "X-Auth-Request-Email" not in asked
    # Positive control: through the socket, as Caddy passes it on, the header is believed.
    status, me = stack.request("GET", "/api/me", st.ADMIN)
    assert status == 200 and me["email"] == st.ADMIN and me["admin"] is True


def test_caddy_lets_only_oauth2_proxy_set_the_identity(stack):
    """A request that names someone itself, with no session, is sent to sign in; signed in as
    alice through the stand-in for Google, the name the request gives is not what rcman hears."""
    seen = json.loads(stack.verb("identity").strip().splitlines()[-1])
    status, headers = seen["unsigned"]
    assert status == 302 and headers["Location"].startswith("/oauth2/start?rd="), seen
    assert seen["signed_in"] == 200
    status, body = seen["me"]
    assert status == 200 and json.loads(body)["email"] == st.ADMIN, seen
    assert "evil" not in body


def cookie_attributes(line: str) -> tuple[str, dict[str, str]]:
    """A ``Set-Cookie`` as ``stack.py cookies`` prints it, ``name; attributes``: its name and its
    attributes, by their lowercase names, with their values (``""`` for a flag)."""
    name, *attributes = (part.strip() for part in line.split(";"))
    found = {}
    for attribute in filter(None, attributes):
        key, _, value = attribute.partition("=")
        found[key.strip().lower()] = value.strip()
    return name, found


def clears(attributes: dict[str, str]) -> bool:
    """Whether a cookie with ``attributes`` is one a browser deletes: Max-Age 0 or less, or an
    Expires in the past."""
    import email.utils

    if "max-age" in attributes:
        return int(attributes["max-age"]) <= 0
    if "expires" in attributes:
        return email.utils.parsedate_to_datetime(attributes["expires"]).timestamp() < time.time()
    return False


def host_prefixed(name: str, attributes: dict[str, str]) -> None:
    """A cookie a browser keeps as a ``__Host-rcman-auth`` one: Secure, Path=/ and no Domain."""
    assert name.startswith("__Host-rcman-auth"), (name, attributes)
    assert "secure" in attributes, (name, attributes)
    assert attributes.get("path") == "/", (name, attributes)
    assert "domain" not in attributes, (name, attributes)


def test_sign_in_sets_host_prefixed_cookies_secure_path_root_without_domain(stack):
    """Every cookie oauth2-proxy, the pinned one in the installer's toolchain, sets while a person
    signs in (its CSRF cookie, the session, the session's chunks where it has them, and the
    clearing of the CSRF cookie), and the clearing of the session when they sign out, is a
    ``__Host-rcman-auth`` one: Secure, Path=/ and no Domain, so a forwarded port's subdomain of
    rcman's host can neither set nor replace it, and a browser does delete it at sign-out (it
    ignores a ``__Host-`` cookie without them, the clearing one too)."""
    # Planted positives: the parse sees a Domain, a missing Secure and another path; a clearing
    # cookie is told from one that is kept.
    name, planted = cookie_attributes("__Host-rcman-auth; Path=/x; Domain=rcman.test; HttpOnly")
    assert name == "__Host-rcman-auth" and planted["domain"] == "rcman.test", planted
    assert planted["path"] == "/x" and "secure" not in planted and "httponly" in planted, planted
    assert clears({"max-age": "0"}) and clears({"expires": "Thu, 01 Jan 1970 00:00:00 GMT"})
    assert not clears({"max-age": "60"}) and not clears({})
    assert not clears({"expires": "Fri, 01 Jan 2100 00:00:00 GMT"})
    seen = json.loads(stack.verb("cookies").strip().splitlines()[-1])
    status, body = seen["me"]
    assert status == 200 and json.loads(body)["email"] == st.ADMIN, seen  # signed in
    cookies = [(*cookie_attributes(line), empty) for line, empty in seen["set_cookies"]]
    names = [name for name, _, _ in cookies]
    session = re.compile(r"__Host-rcman-auth(_\d+)?")
    assert any(session.fullmatch(name) and not empty for name, _, empty in cookies), cookies
    assert "__Host-rcman-auth_csrf" in names, names
    for name, attributes, _ in cookies:
        host_prefixed(name, attributes)
    # Signed out: the session's cookie cleared, as a __Host- one, and rcman sends them to sign in.
    cleared = [(*cookie_attributes(line), empty) for line, empty in seen["signed_out"]]
    ended = [name for name, attributes, _ in cleared if clears(attributes)]
    assert any(session.fullmatch(name) for name in ended), cleared
    for name, attributes, _ in cleared:
        host_prefixed(name, attributes)
    status, location = seen["me_after"]
    assert status == 302 and location.startswith("/oauth2/start"), seen


# --- people and workspaces -------------------------------------------------------------------


def test_a_person_is_added_and_can_come_in(stack):
    status, answer = stack.request("POST", "/api/people", body={"email": EVE})
    assert status == 200, answer
    assert EVE in in_stack(stack, "cat", str(st.ALLOWED))
    assert stack.request("GET", "/api/me", EVE)[0] == 200


def test_a_workspace_is_made_and_its_server_is_refused_until_claude_is_signed_in(stack):
    alpha = made(stack, "alpha")
    status, answer = stack.request("POST", "/api/servers", body={"workspace": alpha["id"]})
    assert (status, answer["code"]) == (409, "claude_not_logged_in"), answer


def test_signing_in_once_lets_workspaces_made_before_and_after_reach_ready(stack):
    """One sign-in, through a sign-in container that is gone afterwards, reaches alpha, made
    before it, and gamma, made after it."""
    status, answer = stack.request("POST", "/api/claude/login", body={})
    assert status == 200 and answer["url"].startswith("https://claude.com/"), answer
    status, answer = stack.request("POST", "/api/claude/code", body={"code": ACCEPTED_CODE})
    assert status == 200 and answer["connection"] == "connected", answer
    assert stack.request("GET", "/api/claude")[1]["account"]["email"] == st.ADMIN
    label = f"label=rcman.person={person_id(stack)}"
    containers = podman(stack, "ps", "--all", "--format", "{{.Names}}", "--filter", label).split()
    assert containers and not [c for c in containers if "sign-in" in c], containers
    ready_server(stack, "alpha")
    made(stack, "gamma")
    ready_server(stack, "gamma")
    gamma = workspace_of(stack, "gamma")
    status, answer = stack.request("POST", "/api/servers/stop", body={"workspace": gamma["id"]})
    assert status == 200, answer


def test_restarting_rcman_leaves_the_server_running_as_it_was(stack):
    alpha = workspace_of(stack, "alpha")
    before = claude_processes(stack, alpha["id"])
    assert before  # positive control
    stack.verb("restart")
    until(lambda: server_state(stack, "alpha") == "ready", within=60)
    assert claude_processes(stack, alpha["id"]) == before


def test_a_stop_leaves_no_claude_process_in_the_workspace(stack):
    alpha = workspace_of(stack, "alpha")
    assert claude_processes(stack, alpha["id"])  # positive control
    status, answer = stack.request("POST", "/api/servers/stop", body={"workspace": alpha["id"]})
    assert status == 200 and answer["server"]["state"] == "stopped", answer
    assert claude_processes(stack, alpha["id"]) == []
    status, answer = stack.request("POST", "/api/servers/resume", body={"workspace": alpha["id"]})
    assert status == 200, answer
    until(lambda: server_state(stack, "alpha") == "ready")


def test_one_person_cannot_reach_anothers_workspace(stack):
    alpha = workspace_of(stack, "alpha")
    bobs = made(stack, "beta", BOB)
    assert workspace_of(stack, "beta") is None  # not in the administrator's own list
    for method, url, body in (
        ("POST", "/api/servers", {"workspace": bobs["id"]}),
        ("GET", f"/api/servers/output?workspace={bobs['id']}", None),
        ("DELETE", f"/api/workspaces/{bobs['id']}", None),
    ):
        status, answer = stack.request(method, url, body=body)
        assert (status, answer["code"]) == (404, "workspace_not_found"), (url, answer)
    mappings, tmps = {}, set()
    for workspace in (alpha, bobs):
        name = f"rcman-ws-{workspace['id']}"
        found = json.loads(podman(stack, "container", "inspect", name))[0]
        volumes = [m for m in found["Mounts"] if m.get("Type") == "volume"]
        named = {m.get("Name") for m in volumes if m.get("Destination") != rt.TMP_MOUNT}
        assert named == {name}, volumes  # its own volume, and no one else's
        (tmp,) = [m for m in volumes if m.get("Destination") == rt.TMP_MOUNT]
        assert rt.ANONYMOUS_VOLUME.match(tmp["Name"]), tmp  # and its /tmp, of its own
        tmps.add(tmp["Name"])
        person = found["Config"]["Labels"]["rcman.person"]
        own = podman(stack, "volume", "inspect", "--format", "{{.Mountpoint}}",
                     f"rcman-person-{person}").strip()  # fmt: skip
        binds = [m["Source"] for m in found["Mounts"] if m.get("Type") == "bind"]
        assert f"{own}/.credentials.json" in binds, binds  # their own sign-in
        mappings[name] = in_workspace(stack, workspace["id"], "cat /proc/self/uid_map").split()
    assert len(tmps) == 2  # no two the same
    first, second = mappings.values()
    assert first[0] == second[0] == "0" and first[1] != second[1]  # ranges of their own
    for path in (str(st.SOCKET), str(stack.engine.socket), str(stack.folder)):
        assert in_workspace(stack, alpha["id"], f"test -e {path} && echo there", False) == ""
    assert in_stack(stack, "test", "-S", str(st.SOCKET)) == ""  # the control: the stack has it


# The stack has no way out to a registry, so no Dockerfile can be built in it: a rebuild is held
# instead by a create command that runs long, in the new container, from an image the stack's
# Podman has - the one the workspace was made from.
SLOW_COMMAND = "sleep 600"


def rebuild_shown(stack, who: str, workspace_id: int) -> str:
    """What a failing wait says: the workspace as the listing shows it, its rebuild's steps and
    error among it, and every container the stack's Podman has."""
    status, listed = stack.request("GET", "/api/workspaces", who)
    found = [w for w in listed.get("workspaces", []) if w["id"] == workspace_id]
    return f"{status} {json.dumps(found, indent=1)}\n{podman(stack, 'ps', '--all')}"


def rebuilding_with_a_slow_command(stack, who: str) -> dict:
    """A workspace of ``who``'s cloned from the stand-in GitHub, its devcontainer.json changed
    in it to one with a create command that runs long, and rebuilt: once that command runs in
    its new container, the rebuild in its switch."""
    url = f"https://github.com/{stand_in.STACK_OWNER}/{stand_in.STACK_PUBLIC}"
    status, answer = stack.request("POST", "/api/git/clone", who, {"url": url})
    assert status == 202, answer
    workspace_id = answer["workspace"]["id"]

    def listed():
        status, answer = stack.request("GET", "/api/workspaces", who)
        return next(w for w in answer["workspaces"] if w["id"] == workspace_id)

    assert until(lambda: listed()["state"] != "creating") and listed()["state"] == "ready"
    name = f"rcman-ws-{workspace_id}"
    image = podman(stack, "container", "inspect", "--format", "{{.ImageName}}", name).strip()
    config = json.dumps({"image": image, "postCreateCommand": SLOW_COMMAND})
    folder = f"{st.WORK}/.devcontainer"
    in_workspace(
        stack,
        workspace_id,
        f"mkdir -p '{folder}' && printf '%s' '{config}' > '{folder}/devcontainer.json'",
    )
    status, answer = stack.request(
        "POST", f"/api/workspaces/{workspace_id}/rebuild", who, {"confirm": True}
    )
    assert status == 202, answer
    deadline = time.monotonic() + COMES_WITHIN
    while True:
        rebuild = listed()["rebuild"]
        running = podman(stack, "ps", "--quiet", "--filter", f"name=^{name}$").split()
        if (
            rebuild["state"] == "switching"
            and running
            and SLOW_COMMAND in podman(stack, "top", name, "args")
        ):
            return answer["workspace"]
        assert time.monotonic() < deadline, rebuild_shown(stack, who, workspace_id)
        time.sleep(1)


def test_deleting_a_person_leaves_nothing_labelled_theirs(stack):
    """Through the People page, and through ``rcman-admin`` in a process of its own while a
    rebuild of theirs replaces a container: nothing labelled theirs is left either way."""
    bob = person_id(stack, BOB)
    label = f"label=rcman.person={bob}"
    made(stack, "bobs-own", BOB)  # whatever the tests before made of his, or did not
    assert podman(stack, "ps", "--all", "--quiet", "--filter", label).split()  # positive control
    volumes = podman(stack, "volume", "ls", "--quiet", "--filter", label).split()
    assert f"rcman-person-{bob}" in volumes  # positive control: his sign-in's
    status, answer = stack.request("DELETE", f"/api/people/{bob}")
    assert status == 200, answer
    assert podman(stack, "ps", "--all", "--quiet", "--filter", label).split() == []
    assert podman(stack, "volume", "ls", "--quiet", "--filter", label).split() == []

    status, answer = stack.request("POST", "/api/people", body={"email": BOB})
    assert status == 200, answer
    bob = person_id(stack, BOB)
    label = f"label=rcman.person={bob}"
    rebuilding = rebuilding_with_a_slow_command(stack, BOB)
    assert podman(stack, "ps", "--all", "--quiet", "--filter", label).split()  # it is there
    stack.verb("admin-delete", BOB, timeout=900)
    left = podman(stack, "ps", "--all", "--quiet", "--filter", label).split()
    assert left == [], rebuild_shown(stack, st.ADMIN, rebuilding["id"])
    assert podman(stack, "volume", "ls", "--quiet", "--filter", label).split() == []
    assert stack.request("GET", "/api/me", BOB)[0] == 403
    assert BOB not in in_stack(stack, "cat", str(st.ALLOWED))
    assert stack.request("GET", "/api/me", BOB)[0] == 403


def test_an_administrators_limits_reach_systemctl_and_are_read_back(stack):
    """A person's and a workspace's, through the API, reaching the user manager (the stand-in,
    which keeps them as it does); the workspace's server runs on, and both are read back.
    ``rcman-admin``, which runs itself again as rcman's account, is the installation's
    (tests/container/test_install_in_container.py)."""
    alpha = workspace_of(stack, "alpha")
    me = person_id(stack)
    before = claude_processes(stack, alpha["id"])
    assert before  # positive control
    for url, held, expected in (
        (f"/api/people/{me}/limits", {"memory": "4G", "cpu": "3", "pids": "6000"},
         (limits.person_slice(me), 4096, "3s", "6000")),
        (f"/api/people/{me}/workspaces/{alpha['id']}/limits",
         {"memory": "2G", "cpu": "1.5", "pids": "3000"},
         (limits.workspace_slice(me, alpha["id"]), 2048, "1.5s", "3000")),
    ):  # fmt: skip
        status, answer = stack.request("PUT", url, body=held)
        assert status == 200, answer
        unit, memory, cpu, pids = expected
        assert held_properties(stack)[unit] == {
            "MemoryMax": str(memory * limits.MEBIBYTE),
            "MemorySwapMax": "0",
            "CPUQuotaPerSecUSec": cpu,
            "TasksMax": pids,
        }
    status, report = stack.request("GET", f"/api/people/{me}/limits")
    assert status == 200, report
    assert report["person"]["limits"] == {"memory": "4G", "cpu": "3", "pids": "6000"}
    (own,) = [w for w in report["workspaces"] if w["id"] == alpha["id"]]
    assert own["limits"] == {"memory": "2G", "cpu": "1.5", "pids": "3000"}
    assert (
        claude_processes(stack, alpha["id"]) == before and server_state(stack, "alpha") == "ready"
    )


def test_the_monitor_reads_a_running_workspaces_slice_through_systemctl(stack):
    """The usage round's one show of every slice reaches the user manager (the stand-in) and is
    read slice by slice: a running workspace's memory is known. The shortest usage interval is
    saved for the test, so that a round comes soon, and the defaults put back after."""
    from rcman import monitor

    alpha = workspace_of(stack, "alpha")
    shortest = {
        **monitor.DEFAULTS.model_dump(),
        "usage_interval_seconds": monitor.USAGE_INTERVAL_MIN,
    }
    status, answer = stack.request("PUT", "/api/monitor/settings", body=shortest)
    assert status == 200, answer
    try:
        deadline = time.monotonic() + 6 * monitor.USAGE_INTERVAL_MIN
        while True:
            status, snap = stack.request("GET", "/api/monitor")
            assert status == 200, snap
            (row,) = [c for c in snap["containers"] if c["id"] == alpha["id"]]
            if row["memory_bytes"] is not None:
                break
            assert time.monotonic() < deadline, snap
            time.sleep(1)
        assert snap["usage"]["error"] is None
        mine = [p for p in snap["people"] if p["id"] == person_id(stack)]
        assert mine and mine[0]["memory_bytes"] is not None
    finally:
        stack.request("PUT", "/api/monitor/settings", body=monitor.DEFAULTS.model_dump())


# --- the stack's network ----------------------------------------------------------------------


def test_a_workspace_is_in_the_stacks_network_with_a_hostname_and_a_hosts_file_of_its_own(stack):
    """Gate: the workspaces join the stack's network, and keep their own hostname and hosts
    file; git there finds the stand-in GitHub at github.com through the image, not the hosts
    file."""
    alpha = workspace_of(stack, "alpha")
    stacks = network.namespace_of(stack.engine, stack.name)
    assert in_workspace(stack, alpha["id"], "readlink /proc/self/ns/net").strip() == stacks
    assert os.readlink("/proc/self/ns/net") != stacks  # the control: this process's is another
    hostname = in_workspace(stack, alpha["id"], "cat /proc/sys/kernel/hostname").strip()
    assert hostname == f"rcman-ws-{alpha['id']}"
    assert in_stack(stack, "cat", "/proc/sys/kernel/hostname").strip() == st.DOMAIN  # control
    theirs = in_workspace(stack, alpha["id"], "cat /etc/hosts")
    assert "github.com" not in theirs and "auth.test" not in theirs
    assert "github.com" in in_stack(stack, "cat", "/etc/hosts")  # the control: the stack's has it
    resolve = in_workspace(stack, alpha["id"], "git config --system --get http.curloptResolve")
    assert resolve.strip() == f"github.com:{stand_in.PORT}:{stand_in.ADDRESS}"
    public = f"https://github.com/{stand_in.STACK_OWNER}/{stand_in.STACK_PUBLIC}"
    assert "refs/heads/main" in in_workspace(stack, alpha["id"], f"git ls-remote {public}")


# --- a forwarded port's certificate, asked of rcman -------------------------------------------

# The hosts a forwarded port is reached at, under a name of the gate's own beside the installer's
# ``ws``: one the stand-in for rcman's ask allows, one it refuses, and a site of the same upstream
# that logs everything, the twin of the filtered log. The gate's ask site has a port of its own,
# beside the installer's, and the global options ask it for the gate.
ALLOWED = f"6080-1.gate.{st.DOMAIN}"
REFUSED = f"6081-1.gate.{st.DOMAIN}"
TWIN = f"twin.{st.DOMAIN}"
GATE_ASK_PORT = st.ASK_PORT + 1
GATE_ASK_URL = f"http://127.0.0.1:{GATE_ASK_PORT}{st.ASK_PATH}"
# What a request carries, and its upstream answers, that the forward site's log must not keep.
COOKIE, QUERY, REFERRED = "cookie-c0ffee", "query-5eed", "referer-feed"
SET, LOCATED = "set-cookie-be11", "location-query-d00d"
# Paths of the ask site that are not exactly the ask's, each answered 404 and passed to no one;
# one that begins with the site's own address is sent as an absolute-form request target.
NOT_THE_ASK = ["/.rcman/ask/", "/.rcman/askx", "/.rcman/ask/x", "/.RCMAN/ASK", "/.rcman/Ask",
               "/.rcman/", "/", "/x/.rcman/ask", "/.rcman/ask.json", "//.rcman/ask",
               "/.rcman/./ask", "/.rcman/%61sk", "/.rcman/ask%3F", "/.rcman/ask%2F",
               f"http://127.0.0.1:{GATE_ASK_PORT}{st.ASK_PATH}"]  # fmt: skip
# Run by the stack's python3, in the stack's container: the stand-in for rcman's ask on
# ask.sock, answering 200 for the one host it allows and 404 for any other, and the upstream of
# the forwarded port on forward.sock, answering every request with a cookie and a Location
# holding a query string; each records what it was asked in a JSON line. Its id is written
# last, once both sockets listen.
STAND_IN = r"""
import json, os, socketserver, sys, threading, urllib.parse
from http.server import BaseHTTPRequestHandler
folder, allowed, set_cookie, located = sys.argv[1:5]
lock = threading.Lock()

def record(name, entry):
    with lock, open(os.path.join(folder, name), "a") as f:
        f.write(json.dumps(entry) + "\n")

class Quiet(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

class Ask(Quiet):
    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        record("asked.jsonl", {"path": self.path, "host": self.headers.get("Host")})
        self.send_response(200 if query.get("domain") == [allowed] else 404)
        self.send_header("Content-Length", "0")
        self.end_headers()

class Upstream(Quiet):
    def do_GET(self):
        record("upstream.jsonl", {"path": self.path, "headers": dict(self.headers)})
        body = b"upstream"
        self.send_response(200)
        self.send_header("Set-Cookie", "up=" + set_cookie)
        self.send_header("Location", "/next?" + located)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

class Server(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True

for name, handler in (("ask.sock", Ask), ("forward.sock", Upstream)):
    path = os.path.join(folder, name)
    server = Server(path, handler)
    os.chmod(path, 0o666)
    threading.Thread(target=server.serve_forever, daemon=True).start()
with open(os.path.join(folder, "stand-in.pid.part"), "w") as f:
    f.write(str(os.getpid()))
os.rename(os.path.join(folder, "stand-in.pid.part"), os.path.join(folder, "stand-in.pid"))
threading.Event().wait()
"""


def forward_sites(folder: Path) -> str:
    """The sites a forwarded port needs, after the stack's own: every host under ``ws.`` on a
    certificate got on demand, passed to forward.sock and logged with no cookie and no query
    string; its twin, logged as Caddy logs by default; and the ask's site, bound to the
    loopback, passing only the ask's exact path to ask.sock: the request's target as it came,
    neither cleaned nor decoded, is the path, alone or before a query."""
    target = "{http.request.orig_uri}"
    exact = f"{target} == '{st.ASK_PATH}' || {target}.startsWith('{st.ASK_PATH}?')"
    return f"""
*.gate.{st.DOMAIN} {{
    tls {{
        on_demand
    }}
    log {{
        output file {folder}/forward-access.log
        format filter {{
            wrap json
            fields {{
                request>headers>Cookie delete
                request>headers>Referer delete
                resp_headers>Set-Cookie delete
                resp_headers>Location delete
                request>uri regexp [?].* ""
            }}
        }}
    }}
    reverse_proxy unix/{folder}/forward.sock
}}

{TWIN} {{
    log {{
        output file {folder}/twin-access.log
    }}
    reverse_proxy unix/{folder}/forward.sock
}}

http://127.0.0.1:{GATE_ASK_PORT} {{
    bind 127.0.0.1
    @ask expression "{exact}"
    handle @ask {{
        reverse_proxy unix/{folder}/ask.sock
    }}
    handle {{
        respond 404
    }}
}}
"""


def recorded(path: Path) -> list[dict]:
    """The JSON lines of a file, none while there is no file."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def through_caddy(stack, host: str, path: str, *more: str):
    """curl in the stack's container, to ``host`` on Caddy's port 443."""
    return stack.exec("curl", "-sS", "-m", "60", "--resolve", f"{host}:443:127.0.0.1", *more,
                      f"https://{host}{path}")  # fmt: skip


def asked_domains(folder: Path) -> list[str]:
    return [
        urllib.parse.parse_qs(urllib.parse.urlsplit(entry["path"]).query).get("domain", [""])[0]
        for entry in recorded(folder / "asked.jsonl")
    ]


# Run by the stack's python3: the subject alternative names of the certificate served for a
# host on Caddy's port 443, verified against the authority given, as JSON.
SERVED_NAMES = r"""
import json, socket, ssl, sys
host, authority = sys.argv[1:3]
context = ssl.create_default_context(cafile=authority)
with socket.create_connection(("127.0.0.1", 443), timeout=30) as raw:
    with context.wrap_socket(raw, server_hostname=host) as tls:
        print(json.dumps(tls.getpeercert()["subjectAltName"]))
"""


def served_names(stack, host: str, authority: str) -> list:
    return json.loads(in_stack(stack, "python3", "-c", SERVED_NAMES, host, authority))


def ask_status(stack, path: str, probe: str) -> str:
    """The status the ask's site answers ``path`` with, asked for the allowed host and marked
    with ``probe``."""
    site, query = f"http://127.0.0.1:{GATE_ASK_PORT}", f"?domain={ALLOWED}&probe={probe}"
    if path.startswith(site):  # the whole address as the request's target
        target = ["--request-target", path + query]
        url = site + "/"
    else:
        target, url = [], site + path + query
    return in_stack(
        stack, "curl", "-s", "--path-as-is", *target, "-o", "/dev/null", "-w", "%{http_code}",
        url,
    )  # fmt: skip


def test_ubuntu_caddy_on_demand_asks_rcman_over_a_bound_loopback_site(stack):
    """Gate: with ``on_demand_tls`` asking http://127.0.0.1:<ask port>/.rcman/ask in the stack's
    own global block (tests/container/stack.py ``global_options``), the Ubuntu package's Caddy loads
    the forward site, the ask's site and a twin; the ask's site listens on 127.0.0.1 alone; a
    host the ask refuses gets no certificate while the host it allows gets one of the stack's
    authority and reaches the upstream on its unix socket; only the ask's exact path passes;
    and the forward site's log keeps no cookie and no query string, which its twin's keeps."""
    version = in_stack(stack, "caddy", "version").strip()
    package = in_stack(stack, "dpkg-query", "-W", "-f", "${Version}", "caddy").strip()
    caddy = f"Caddy {version}, Ubuntu's caddy {package}"
    folder = stack.folder / "forward-gate"
    folder.mkdir()
    made = stack.engine.cli("exec", "-d", stack.name, "python3", "-c", STAND_IN, str(folder),
                            ALLOWED, SET, LOCATED)  # fmt: skip
    assert made.returncode == 0, made.stderr
    until(lambda: (folder / "stand-in.pid").exists(), within=60, every=0.2)
    (folder / "sites").write_text(forward_sites(folder))
    try:
        # Loaded, or Caddy's version and its words.
        stack.verb("caddy", str(folder / "sites"), GATE_ASK_URL)

        # The ask's site on the loopback alone; the control: one on every address is seen so.
        tcp = bound(stack)
        assert {host for host, port in tcp if port == GATE_ASK_PORT} == {"127.0.0.1"}, (caddy, tcp)
        assert {host for host, port in tcp if port == st.ASK_PORT} == {"127.0.0.1"}, tcp  # rcman's
        assert {("0.0.0.0", 443), ("::", 443)} & tcp, tcp

        # A refused host: Caddy asked, was told no, and gave no certificate at all.
        refused = through_caddy(stack, REFUSED, "/", "-k")
        assert refused.returncode == 35, (caddy, refused.returncode, refused.stderr)
        assert REFUSED in asked_domains(folder)

        # The allowed host: a certificate of the stack's authority, and the upstream reached.
        root = str(st.caddy_root(stack.folder))
        headers = [
            "-H",
            f"Cookie: gate={COOKIE}",
            "-H",
            f"Referer: https://elsewhere.test/?{REFERRED}=1",
        ]
        page = f"/page?{QUERY}=1"
        allowed = through_caddy(stack, ALLOWED, page, "--cacert", root, *headers)
        assert allowed.returncode == 0, (caddy, allowed.returncode, allowed.stderr)
        assert allowed.stdout == b"upstream"
        asked = recorded(folder / "asked.jsonl")
        assert {
            "path": f"{st.ASK_PATH}?domain={ALLOWED}",
            "host": f"127.0.0.1:{GATE_ASK_PORT}",
        } in asked, asked
        # The control: verified against another authority, the same certificate is refused.
        other = through_caddy(stack, ALLOWED, "/", "--cacert", str(stack.folder / "pki" / "ca.crt"))
        assert other.returncode == 60, (other.returncode, other.stderr)
        upstream = recorded(folder / "upstream.jsonl")
        assert [r["path"] for r in upstream if r["headers"].get("Host") == ALLOWED][:1] == [page]
        assert not [r for r in upstream if r["headers"].get("Host") == REFUSED], upstream
        # The certificate served names the allowed host alone, and Caddy's storage has one for it
        # and none for the refused host.
        assert served_names(stack, ALLOWED, root) == [["DNS", ALLOWED]], caddy
        issued = {p.name for p in st.caddy_certificates(stack.folder).iterdir()}
        assert ALLOWED in issued and REFUSED not in issued, issued

        # Only the ask's exact path passes, and nothing else reaches the stand-in.
        for n, path in enumerate(NOT_THE_ASK):
            assert ask_status(stack, path, str(n)) == "404", path
        assert ask_status(stack, st.ASK_PATH, "exact") == "200"  # the planted positive
        probes = {
            urllib.parse.parse_qs(urllib.parse.urlsplit(entry["path"]).query).get("probe", [""])[0]
            for entry in recorded(folder / "asked.jsonl")
        } - {""}
        assert probes == {"exact"}, probes

        # The forward site's log keeps no cookie and no query string; its twin's, the same
        # request's, keeps both, and the upstream saw them through both.
        twin = through_caddy(stack, TWIN, page, "--cacert", root, *headers)
        assert twin.returncode == 0 and twin.stdout == b"upstream", (twin.returncode, twin.stderr)

        def entries(name: str, host: str) -> list[dict]:
            return [e for e in recorded(folder / name) if e["request"]["host"] == host]

        kept = until(lambda: entries("twin-access.log", TWIN), within=60, every=0.2)
        filtered = until(lambda: entries("forward-access.log", ALLOWED), within=60, every=0.2)
        [kept] = [e for e in kept if e["request"]["uri"].startswith("/page")]
        assert kept["request"]["uri"] == page, kept
        assert "Cookie" in kept["request"]["headers"], kept
        assert REFERRED in json.dumps(kept["request"]["headers"]["Referer"]), kept
        assert "Set-Cookie" in kept["resp_headers"], kept
        assert LOCATED in json.dumps(kept["resp_headers"]["Location"]), kept
        [logged] = [e for e in filtered if e["request"]["uri"].startswith("/page")]
        assert logged["request"]["uri"] == "/page", logged
        assert "Cookie" not in logged["request"]["headers"], logged
        assert "Referer" not in logged["request"]["headers"], logged
        assert "Set-Cookie" not in logged["resp_headers"], logged
        text = (folder / "forward-access.log").read_text()
        assert "Location" not in logged["resp_headers"], logged
        for secret in (COOKIE, QUERY, REFERRED, SET, LOCATED):
            assert secret not in text, (secret, text)
        seen = [r for r in recorded(folder / "upstream.jsonl") if r["path"] == page]
        assert {r["headers"]["Host"] for r in seen} == {ALLOWED, TWIN}, seen
        assert all(COOKIE in r["headers"]["Cookie"] for r in seen), seen
    finally:
        try:
            stack.verb("caddy")
        finally:
            in_stack(stack, "kill", (folder / "stand-in.pid").read_text())


def started_stand_in(stack, folder: Path, allowed: str) -> None:
    """STAND_IN in the stack, in ``folder``, which it makes, allowing ``allowed`` alone."""
    folder.mkdir()
    made = stack.engine.cli("exec", "-d", stack.name, "python3", "-c", STAND_IN, str(folder),
                            allowed, SET, LOCATED)  # fmt: skip
    assert made.returncode == 0, made.stderr
    until(lambda: (folder / "stand-in.pid").exists(), within=60, every=0.2)


def caddy_named(stack) -> str:
    version = in_stack(stack, "caddy", "version").strip()
    package = in_stack(stack, "dpkg-query", "-W", "-f", "${Version}", "caddy").strip()
    return f"Caddy {version}, Ubuntu's caddy {package}"


def test_ubuntu_caddy_without_the_global_ask_gives_any_host_under_the_wildcard_a_certificate(
    stack,
):
    """What the installer's on_demand_tls in the global options is for: with the forward site's
    ``on_demand`` and no ``on_demand_tls`` in the global options, the Ubuntu package's Caddy loads
    the file and gets a certificate for any name under the wildcard that a client asks for, a
    random one, asking no one. The control: the same Caddy, its global options asking, refuses
    another random name."""
    caddy = caddy_named(stack)
    folder = stack.folder / "no-ask-gate"
    started_stand_in(stack, folder, ALLOWED)
    (folder / "sites").write_text(forward_sites(folder))
    root = str(st.caddy_root(stack.folder))
    try:
        stack.verb("caddy", str(folder / "sites"), "")  # loaded, or Caddy's version and words
        anyone = f"{secrets.token_hex(6)}.gate.{st.DOMAIN}"
        got = through_caddy(stack, anyone, "/", "--cacert", root)
        assert got.returncode == 0, (caddy, got.returncode, got.stderr)
        assert got.stdout == b"upstream", (caddy, got.stdout)
        assert served_names(stack, anyone, root) == [["DNS", anyone]], caddy
        assert recorded(folder / "asked.jsonl") == [], caddy  # no one was asked

        stack.verb("caddy", str(folder / "sites"), GATE_ASK_URL)
        other = f"{secrets.token_hex(6)}.gate.{st.DOMAIN}"
        refused = through_caddy(stack, other, "/", "-k")
        assert refused.returncode == 35, (caddy, refused.returncode, refused.stderr)
        assert other in asked_domains(folder)
    finally:
        try:
            stack.verb("caddy")
        finally:
            in_stack(stack, "kill", (folder / "stand-in.pid").read_text())


# The installer's site for forwarded ports, rendered for a domain of its own beside the stack's,
# and a host of it the stand-in's ask allows.
SNIPPET_DOMAIN = f"snip.{st.DOMAIN}"
SNIPPET_HOST = f"6080-1.ws.{SNIPPET_DOMAIN}"
# What a browser may send that says who it is, each of a name the installer's site deletes, and
# a header it passes on.
IDENTITY_SENT = {
    "X-Forwarded-Email": "evil@example.com",
    "X-Forwarded-User": "evil",
    "X-Auth-Request-Email": "evil@example.com",
    "X-Auth-Request-User": "evil",
    "X-Auth-Request-Groups": "admins",
}
PASSED = ("X-Planted-Passed", "passed")


def installers_forward_site(folder: Path) -> str:
    """The installer's own site for forwarded ports, as the installer renders it for
    SNIPPET_DOMAIN, with its upstream the stand-in's socket in ``folder`` and its log a file
    there: the rest of it, its deletions of identity headers among them, as it is."""
    snippet = "\n" + ins.render("Caddyfile", {**st.install_settings(), "DOMAIN": SNIPPET_DOMAIN})
    start = snippet.index(f"\n*.ws.{SNIPPET_DOMAIN} {{\n") + 1
    site = snippet[start : snippet.index("\n}\n", start) + 3]
    for old, new in (
        (f"unix/{ins.FORWARD_SOCKET}", f"unix/{folder}/forward.sock"),
        ("/var/log/caddy/rcman-forward-access.log", f"{folder}/snippet-access.log"),
    ):
        assert site.count(old) == 1, (old, site)  # the plant took
        site = site.replace(old, new)
    return "\n" + site


def test_the_installers_forward_site_deletes_identity_headers_before_the_forwarding_server(stack):
    """The installer's own site for forwarded ports passes on none of the identity headers a
    browser sends itself, ``X-Auth-Request-*`` by its wildcard among them, while another header
    passes. The planted positive: the same request through the twin, which deletes nothing,
    reaches the same upstream with every one of them."""
    caddy = caddy_named(stack)
    folder = stack.folder / "snippet-gate"
    started_stand_in(stack, folder, SNIPPET_HOST)
    (folder / "sites").write_text(forward_sites(folder) + installers_forward_site(folder))
    root = str(st.caddy_root(stack.folder))
    sent = [part for name, value in [*IDENTITY_SENT.items(), PASSED]
            for part in ("-H", f"{name}: {value}")]  # fmt: skip
    try:
        stack.verb("caddy", str(folder / "sites"), GATE_ASK_URL)
        for host in (SNIPPET_HOST, TWIN):
            got = through_caddy(stack, host, "/who", "--cacert", root, *sent)
            assert got.returncode == 0 and got.stdout == b"upstream", (caddy, host, got.stderr)
        seen = {
            r["headers"].get("Host"): {name.lower() for name in r["headers"]}
            for r in recorded(folder / "upstream.jsonl")
            if r["path"] == "/who"
        }
        identity = {name.lower() for name in IDENTITY_SENT}
        assert identity <= seen[TWIN] and PASSED[0].lower() in seen[TWIN], seen
        assert not identity & seen[SNIPPET_HOST], (caddy, seen)
        assert PASSED[0].lower() in seen[SNIPPET_HOST], seen
    finally:
        try:
            stack.verb("caddy")
        finally:
            in_stack(stack, "kill", (folder / "stand-in.pid").read_text())
