"""The whole of rcman in one container of the tests' engine (tests/container/stack.py): what an
installation does once people and workspaces are there, through rcman's own socket as Caddy
passes requests on, and through Caddy and oauth2-proxy; and the network the stack's workspaces
share. The browser tests run against a stack of their own
(tests/container/test_stack_in_a_browser.py).

The stack's workspaces run on the engine (tests/podman_service.py) at the suites' sizes
(tests/container/sizes.py), in the default image with the stand-in GitHub's authority trusted, and
join the stack's own network, which has no way out. What needs systemd, the journal or the
kernel's limits is checked by hand (project-knowledge/real-claude-checklist.md).
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import pytest
from rcman import limits

from devtools.fake_claude import ACCEPTED_CODE
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


def listeners(stack) -> set[tuple[str, int]]:
    """Every listening TCP address (host, port) of the stack's network, IPv4 and IPv6; one on
    every address is taken at the loopback's."""
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
            if host in ("0.0.0.0", "::"):
                host = "127.0.0.1" if family == socket.AF_INET else "::1"
            found.add((host, int(port, 16)))
    return found


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
    is not taken for that person there: rcman's only listener is its socket, which only Caddy's
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
    mappings = {}
    for workspace in (alpha, bobs):
        name = f"rcman-ws-{workspace['id']}"
        found = json.loads(podman(stack, "container", "inspect", name))[0]
        mounts = {m.get("Name") for m in found["Mounts"] if m.get("Type") == "volume"}
        assert mounts == {name}, mounts  # its own volume, and no one else's
        person = found["Config"]["Labels"]["rcman.person"]
        own = podman(stack, "volume", "inspect", "--format", "{{.Mountpoint}}",
                     f"rcman-person-{person}").strip()  # fmt: skip
        binds = [m["Source"] for m in found["Mounts"] if m.get("Type") == "bind"]
        assert f"{own}/.credentials.json" in binds, binds  # their own sign-in
        mappings[name] = in_workspace(stack, workspace["id"], "cat /proc/self/uid_map").split()
    first, second = mappings.values()
    assert first[0] == second[0] == "0" and first[1] != second[1]  # ranges of their own
    for path in (str(st.SOCKET), str(stack.engine.socket), str(stack.folder)):
        assert in_workspace(stack, alpha["id"], f"test -e {path} && echo there", False) == ""
    assert in_stack(stack, "test", "-S", str(st.SOCKET)) == ""  # the control: the stack has it


def test_deleting_a_person_leaves_nothing_labelled_theirs(stack):
    bob = person_id(stack, BOB)
    label = f"label=rcman.person={bob}"
    assert podman(stack, "ps", "--all", "--quiet", "--filter", label).split()  # positive control
    volumes = podman(stack, "volume", "ls", "--quiet", "--filter", label).split()
    assert f"rcman-person-{bob}" in volumes  # positive control: his sign-in's
    status, answer = stack.request("DELETE", f"/api/people/{bob}")
    assert status == 200, answer
    assert podman(stack, "ps", "--all", "--quiet", "--filter", label).split() == []
    assert podman(stack, "volume", "ls", "--quiet", "--filter", label).split() == []
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
