"""The Podman runtime (backend/rcman/runtime.py) against real rootless Podman, in the
podman host, as the service account rcman runs as.

``tests/test_container_suite.py`` runs this module there as the account, once the
podman host is set up (tests/container/podman_host.py), with the static tools built
by the installer's own code in TOOLS, an empty Claude store in CLAUDE, and a base image
loaded and named in ``RCMAN_PODMAN_IMAGE``. Anywhere else it skips. The same contract
the host runtime keeps (tests/runtime_contract.py) runs here first.
"""

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from rcman import channel as ch
from rcman import runtime as rt

from tests import runtime_contract

pytestmark = pytest.mark.skipif(
    os.environ.get("RCMAN_IN_CONTAINER") != "1" or "RCMAN_PODMAN_IMAGE" not in os.environ,
    reason="runs only as the service account inside the podman host",
)

STATE = Path("/var/lib/rcman")
TOOLS = STATE / "tools"
CLAUDE = STATE / "claude"
# The most memory one channel's podman client may hold: one is open per workspace with a
# server, so it bounds what rcman's channels cost.
CLIENT_MAX_RSS_BYTES = 64 * 1024 * 1024


def socket() -> Path:
    return Path(f"/run/user/{os.getuid()}/podman/podman.sock")


@pytest.fixture(scope="module")
def podman():
    return rt.PodmanRuntime(
        rt.PodmanConfig(
            socket=socket(),
            image=os.environ["RCMAN_PODMAN_IMAGE"],
            tools_dir=TOOLS,
            claude_store=CLAUDE,
        )
    )


def spec(n: int) -> rt.Spec:
    """The n-th workspace a test makes, in slot n: the podman host's subordinate range
    (tests/container/podman_host.py) holds a few slots, not every one rcman has."""
    return rt.Spec(name=rt.container_name(500 + n), slot=n, workspace=500 + n, person=n)


@pytest.mark.parametrize("case", runtime_contract.CASES, ids=lambda case: case.__name__)
def test_podman_keeps_the_contract(podman, case):
    case(podman, spec)


@pytest.fixture
def workspace(podman):
    made = spec(2)
    podman.create(made)
    try:
        yield made.name
    finally:
        podman.remove(made.name)
        podman.remove_account(made.person)


def podman_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["podman", "--remote", "--url", f"unix://{socket()}", *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


def inspect(name: str) -> dict:
    found = podman_cli("container", "inspect", name)
    assert found.returncode == 0, found.stderr
    return json.loads(found.stdout)[0]


def in_it(podman, name: str, script: str) -> rt.Result:
    layout = podman.layout(name)
    return podman.exec(name, [layout.shell, "-c", script])


def test_a_workspace_runs_in_its_persons_uid_range(podman, workspace):
    found = (
        in_it(podman, workspace, "cat /proc/self/uid_map /proc/self/gid_map").out.decode().split()
    )
    base = str(spec(2).slot * rt.RANGE)
    assert found == ["0", base, str(rt.RANGE), "0", base, str(rt.RANGE)]


def test_its_volume_belongs_to_its_ranges_root_and_no_one_else(podman, workspace):
    mount = podman_cli("volume", "inspect", "--format", "{{.Mountpoint}}", workspace)
    owner = subprocess.run(
        ["podman", "unshare", "stat", "-c", "%u %g %a", mount.stdout.strip()],
        capture_output=True,
        text=True,
    )
    base = spec(2).slot * rt.RANGE
    assert owner.stdout.split() == [str(base), str(base), "700"], owner.stderr


def test_a_workspace_has_an_init_and_publishes_nothing(podman, workspace):
    found = inspect(workspace)
    assert found["HostConfig"]["Init"] is True
    assert not found["HostConfig"]["PortBindings"]
    assert podman_cli("port", workspace).stdout.strip() == ""
    first = in_it(podman, workspace, "cat /proc/1/cmdline").out
    assert b"init" in first.split(b"\0")[0]


def test_the_tools_and_the_claude_store_are_read_only_and_the_runtime_folder_a_tmpfs(
    podman, workspace
):
    for folder in (rt.TOOLS_MOUNT, rt.CLAUDE_MOUNT):
        assert in_it(podman, workspace, f"touch {folder}/x").code != 0, folder
    assert in_it(podman, workspace, f"touch {rt.VOLUME_MOUNT}/x").code == 0  # positive control
    mounts = in_it(podman, workspace, "cat /proc/mounts").out.decode().splitlines()
    assert any(line.split()[1:3] == [rt.RUN_MOUNT, "tmpfs"] for line in mounts), mounts


def test_the_hosts_loopback_is_not_reachable(podman, workspace):
    """slirp4netns's address for the host leads nowhere: nothing on the host's loopback
    answers a workspace."""
    import http.server
    import threading

    server = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        import urllib.request

        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as answer:
            assert answer.status == 200  # positive control: it answers on the host
        wget = " ".join(podman.layout(workspace).tool("wget"))
        tried = in_it(podman, workspace, f"{wget} -q -T 5 -O /dev/null http://10.0.2.2:{port}/")
        assert tried.code != 0
    finally:
        server.shutdown()


def test_an_unreachable_socket_is_unavailable_not_missing():
    nowhere = rt.PodmanRuntime(
        rt.PodmanConfig(
            socket=Path("/run/user/0/nowhere.sock"),
            image="x",
            tools_dir=TOOLS,
            claude_store=CLAUDE,
        )
    )
    with pytest.raises(rt.RuntimeUnavailable):
        nowhere.exists("rcman-ws-1")


def test_the_channels_latency_through_podman(podman, workspace):
    channel = podman.channel(workspace)
    try:
        true = podman.layout(workspace).tool("wc") + ["-c", "/dev/null"]
        channel.run(true)
        times = []
        for _ in range(200):
            started = time.perf_counter()
            channel.run(true)
            times.append(time.perf_counter() - started)
        times.sort()
        assert times[189] < ch.LATENCY_P95_SECONDS, times[189]
    finally:
        channel.close()


def test_a_channels_podman_client_holds_little_memory(podman, workspace):
    channel = podman.channel(workspace)
    try:
        channel.run(podman.layout(workspace).tool("wc") + ["-c", "/dev/null"])
        status = Path(f"/proc/{channel.process.pid}/status").read_text()
        line = next(line for line in status.splitlines() if line.startswith("VmRSS:"))
        rss = int(line.split()[1])
        assert 0 < rss * 1024 < CLIENT_MAX_RSS_BYTES, rss
    finally:
        channel.close()


# --- a person's Claude sign-in --------------------------------------------------------


def credentials_in(layout: rt.Layout) -> str:
    return f"{layout.config}/{rt.CREDENTIALS}"


def test_a_persons_credentials_file_is_their_ranges_roots_alone(podman, workspace):
    who = spec(2)
    mount = podman_cli("volume", "inspect", "--format", "{{.Mountpoint}}", rt.account_volume(2))
    assert mount.returncode == 0, mount.stderr
    owner = subprocess.run(
        ["podman", "unshare", "stat", "-c", "%u %g %a", f"{mount.stdout.strip()}/{rt.CREDENTIALS}"],
        capture_output=True,
        text=True,
    )
    base = who.slot * rt.RANGE
    assert owner.stdout.split() == [str(base), str(base), "600"], owner.stderr
    labels = podman_cli("volume", "inspect", "--format", "{{json .Labels}}", rt.account_volume(2))
    assert json.loads(labels.stdout) == {rt.LABEL_PERSON: str(who.person)}
    seen = in_it(podman, workspace, f"stat -c '%u %a' {credentials_in(podman.layout(workspace))}")
    assert seen.out.split() == [b"0", b"600"]  # root's, as the workspace sees it


def test_a_rename_over_the_bound_file_is_refused_busy_and_a_write_in_place_is_shared(
    podman, workspace
):
    """Why the fake claude, and the real one as recorded, write in place when a rename
    fails: the file is a bind mount, and a rename over a mount point is EBUSY."""
    target = credentials_in(podman.layout(workspace))
    renamed = in_it(podman, workspace, f"echo new > {target}.new && mv {target}.new {target}")
    assert renamed.code != 0 and b"busy" in renamed.err.lower(), renamed
    assert in_it(podman, workspace, f"printf kept > {target}").code == 0  # positive control
    assert podman.read_account(2, spec(2).slot) == b"kept"


def test_a_sign_in_container_removes_itself_when_its_time_is_up(podman):
    who = spec(3)
    name = podman.start_sign_in(who.person, who.slot, seconds=3)
    try:
        assert podman.exists(name)  # positive control
        deadline = time.monotonic() + 60
        while podman.exists(name):
            assert time.monotonic() < deadline, "the sign-in container is still there"
            time.sleep(0.5)
    finally:
        podman.end_sign_in(name)
        podman.remove_account(who.person)
