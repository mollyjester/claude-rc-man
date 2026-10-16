"""Rolling out Claude Code versions (backend/rcman/claude_versions.py) against real rootless
Podman, with the fake `claude` as the workspaces' Claude.

``tests/test_container_suite.py`` runs this module in the podman host as the service account,
once the tools and a store holding the fake `claude` are made as root and the store is the
account's, as the installer leaves it. Anywhere else it skips. What the host-runtime tests could
only assume is checked here against the real thing: the canary, a throwaway container in the
builder's uid range with no network, a read-only root, no capabilities and only the candidate and
rcman's helpers mounted, run by ``podman --remote``; a server keeping the version it started
with through an approval and a restart of rcman; and a version leaving the store once the last
server that used it has stopped.

The workspaces run the default workspace image, built here from its build file in the account's
Podman; the releases are the fake as Claude Code versions, served from a folder.
"""

import hashlib
import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from rcman import claude_versions, db, people
from rcman import runtime as rt
from rcman.problems import Problem
from rcman.settings import Settings
from rcman.workspaces import Workspaces

from devtools import fake_claude as fake
from helpers import claude_checks
from installer import install as ins
from tests import claude_download, runtime_contract
from tests.container import test_github_in_podman as shared
from tests.container import test_runtime_in_podman as base
from tests.fake_store import FAKE_VERSION

pytestmark = pytest.mark.skipif(
    os.environ.get("RCMAN_IN_CONTAINER") != "1" or "RCMAN_PODMAN_IMAGE" not in os.environ,
    reason="runs only as the service account inside the podman host",
)

# The version the store holds when a test starts, and the ones released after it: good ones,
# one that is not Claude Code, and one whose checksum is not its release's.
V0, V1, V2 = FAKE_VERSION, "1.1.0", "1.2.0"
BROKEN, FAILING = "1.3.0", "1.4.0"
EMAIL, ADMIN = "bob@example.com", "admin@example.com"
# How long the canary container is waited for, and how long a slow candidate keeps it up: the
# candidate sleeps this long at every call, so the container is there to be looked at for as
# long as its checks take.
APPEARS_WITHIN = 60
SLOW_SECONDS = 1


@pytest.fixture(scope="module")
def image(tmp_path_factory) -> str:
    """The default workspace image, built in the account's Podman from the installer's build
    file."""
    name = f"localhost/rcman-test-default:{os.getpid()}"
    context = tmp_path_factory.mktemp("default-image")
    shutil.copy(ins.CONTAINERFILE, context / "Containerfile")
    built = subprocess.run(
        ["podman", "--remote", "--url", f"unix://{base.socket()}", "build", "-q", "-t", name,
         "-f", str(context / "Containerfile"), str(context)],
        capture_output=True,
        text=True, errors="replace",
        timeout=1200,
    )  # fmt: skip
    assert built.returncode == 0, built.stdout + built.stderr
    yield name
    base.podman_cli("rmi", "--force", name)


@pytest.fixture(scope="module")
def podman(image) -> rt.PodmanRuntime:
    return shared.runtime_of(image)


@pytest.fixture(scope="module")
def download(tmp_path_factory):
    """The releases, served: the address."""
    folder = tmp_path_factory.mktemp("releases")
    for version, kind in ((V1, "good"), (V2, "good"), (BROKEN, "broken"), (FAILING, "failing")):
        claude_download.release(folder, version, kind, latest=False)
    with claude_download.served(folder) as url:
        yield url


@pytest.fixture
def store(monkeypatch):
    """The store as the installer leaves it for rcman: the one version, which ``current`` names,
    before and after the test. Pruning ages nothing here: the files are new, and a version put
    in the store within the grace is never pruned (as in the host tests)."""
    monkeypatch.setattr(claude_versions, "PRUNE_GRACE_SECONDS", 0.0)

    def reset() -> None:
        versions = base.CLAUDE / "versions"
        for path in versions.iterdir():
            path.unlink()
        binary = versions / V0
        binary.write_bytes(claude_download.bundle(V0))
        binary.chmod(0o755)
        link = base.CLAUDE / ".current.new"
        link.unlink(missing_ok=True)
        link.symlink_to(f"versions/{V0}")
        os.replace(link, base.CLAUDE / "current")

    reset()
    yield base.CLAUDE
    reset()


@pytest.fixture
def person(tmp_path, podman):
    """A person of a database of its own, in a slot the podman host's id range holds; the
    person's volume is removed afterwards."""
    path = tmp_path / "rcman.db"
    db.migrate(path)
    con = db.connect(path)
    try:
        who = people.add(con, EMAIL, False)
    finally:
        con.close()
    yield who, path
    podman.remove_account(who.id)


@pytest.fixture
def service(person, podman, download, store):
    """rcman's workspaces service over the person's database, the real Podman, the store and
    the served releases."""
    made = Workspaces(
        Settings(
            public_origin="https://rcman.test",
            database=person[1],
            claude_store=base.CLAUDE,
            claude_download=download,
        ),
        podman,
    )
    yield made
    made.close()


@pytest.fixture
def workspace(service, person, podman):
    """A ready workspace of the service's person whose Claude is signed in, with the fake
    trusting its work folder; deleted afterwards, container and volume found gone."""
    made = service.create(person[0], "versions", None)
    done = shared.settled(service, made.id)
    assert (done.state, done.error) == ("ready", None), done
    layout = podman.layout(done.container)
    put = lambda path, text: podman.exec(  # noqa: E731
        done.container, [layout.shell, "-c", 'cat > "$1"', "sh", path], stdin=text.encode()
    )
    state = json.dumps({"trusted": [layout.work], "servers": {}})
    assert put(f"{layout.config}/{fake.STATE_FILE}", state).code == 0
    assert put(f"{layout.config}/{rt.CREDENTIALS}", json.dumps(fake.credentials())).code == 0
    con = db.connect(service.settings.database)
    try:
        people.set_claude_account(con, person[0].id, {"oauthAccount": fake.oauth_account(EMAIL)})
    finally:
        con.close()
    yield done
    try:
        service.stop_server(service.get(done.id))
    except Problem:
        pass  # not running: the test stopped it
    finally:
        service.delete(service.get(done.id))
        shared.gone(service, done.id)
        assert not shared.exists("container", done.container)
        assert not shared.exists("volume", done.container)


# --- what is there, looked at from outside rcman ------------------------------------------


def canary_containers() -> list[str]:
    found = base.podman_cli(
        "ps", "--all", f"--filter=name={rt.CANARY_NAME}", "--format", "{{.Names}}"
    )
    assert found.returncode == 0, found.stderr
    return found.stdout.split()


def containers() -> list[str]:
    found = base.podman_cli("ps", "--all", "--format", "{{.Names}}")
    assert found.returncode == 0, found.stderr
    return sorted(found.stdout.split())


def tree(root: Path) -> dict[str, str]:
    """Everything under ``root``, by path: what a file holds and its mode, where a link leads."""
    found = {}
    for path in sorted(root.rglob("*")):
        mode = oct(path.lstat().st_mode)
        if path.is_symlink():
            found[str(path.relative_to(root))] = f"-> {os.readlink(path)}"
        elif path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            found[str(path.relative_to(root))] = f"{digest} {mode}"
        else:
            found[str(path.relative_to(root))] = f"folder {mode}"
    return found


def running_versions(podman, container: str) -> set[str]:
    """The version every process of the fake claude in the container was started as: the
    ``FAKE_CLAUDE_VERSION`` in its environment, which its program bakes in
    (tests/claude_download.py) and which is the only thing that tells two versions of the fake
    apart once it runs."""
    busybox = f"{rt.TOOLS_MOUNT}/bin/busybox"
    script = (
        "for f in /proc/[0-9]*/cmdline; do "
        f'if {busybox} tr "\\0" " " < "$f" 2>/dev/null | '
        f"{busybox} grep -q 'fakes/clau[d]e'; then "
        f'p="${{f%/cmdline}}"; {busybox} tr "\\0" "\\n" < "$p/environ" | '
        f"{busybox} grep '^FAKE_CLAUDE_VERSION='; fi; done"
    )
    done = podman.exec(container, [podman.layout(container).shell, "-c", script])
    return {line.split("=", 1)[1] for line in done.out.decode().split()}


def started_with(service, workspace) -> str:
    """What the workspace's tmux session was told to run: the file of the store."""
    return service.server(service.get(workspace.id)).tmux.run(
        "list-panes", "-a", "-F", "#{pane_start_command}"
    )


def start(service, workspace, podman) -> None:
    answer = service.start_server(service.get(workspace.id), "dev")
    assert answer["server"]["state"] in ("ready", "connected"), answer
    assert running_versions(podman, workspace.container)  # positive control: it is read


# --- a server keeps its version -----------------------------------------------------------


def test_a_server_keeps_its_version_through_an_approval_and_a_restart_and_a_start_takes_the_new_one(
    service, workspace, podman
):
    versions = service.versions
    start(service, workspace, podman)
    assert versions.approved() == V0  # adopted from the store, as `current` named it
    assert running_versions(podman, workspace.container) == {V0}
    assert f"{rt.CLAUDE_MOUNT}/versions/{V0}" in started_with(service, workspace)
    assert service.get(workspace.id).claude_version == V0

    report = versions.upgrade(V1, ADMIN)  # a real canary container
    assert (report["approved"], report["previous"]) == (V1, V0)
    assert os.readlink(base.CLAUDE / "current") == f"versions/{V1}"
    assert canary_containers() == []
    assert list(versions.staging.iterdir()) == []

    # The approval touched nothing of the server.
    assert running_versions(podman, workspace.container) == {V0}
    assert service.get(workspace.id).claude_version == V0
    (drift,) = versions.report()["drift"]
    assert (drift["workspace"], drift["email"], drift["version"], drift["approved"]) == (
        workspace.id, EMAIL, V0, V1,
    )  # fmt: skip

    # rcman ending and starting again, on the same database and Podman: the server runs on.
    again = Workspaces(service.settings, podman)
    try:
        again.restore()
        assert running_versions(podman, workspace.container) == {V0}
        assert again.get(workspace.id).claude_version == V0
        assert [d["version"] for d in again.versions.report()["drift"]] == [V0]
        assert again.versions.approved() == V1
    finally:
        again.close()

    # Its person's stop and start: the approved version, by the exact file.
    service.stop_server(service.get(workspace.id))
    assert service.get(workspace.id).claude_version is None
    assert running_versions(podman, workspace.container) == set()
    start(service, workspace, podman)
    assert running_versions(podman, workspace.container) == {V1}
    assert f"{rt.CLAUDE_MOUNT}/versions/{V1}" in started_with(service, workspace)
    assert service.get(workspace.id).claude_version == V1
    assert versions.report()["drift"] == []


def test_a_version_a_server_runs_stays_in_the_store_until_the_server_stops(
    service, workspace, podman
):
    versions = service.versions
    start(service, workspace, podman)
    versions.upgrade(V1, ADMIN)
    report = versions.upgrade(V2, ADMIN)
    # V0 is neither the approved version nor the previous one, and a server runs it.
    assert (report["approved"], report["previous"]) == (V2, V1)
    assert versions.installed() == [V0, V1, V2]
    assert {i["version"]: i["in_use"] for i in report["installed"]} == {V0: 1, V1: 0, V2: 0}
    assert running_versions(podman, workspace.container) == {V0}
    assert (base.CLAUDE / "versions" / V0).is_file()

    service.stop_server(service.get(workspace.id))  # the last that used it
    assert versions.installed() == [V1, V2]  # V0 went with the stop; the previous one stays
    assert not (base.CLAUDE / "versions" / V0).exists()
    assert os.readlink(base.CLAUDE / "current") == f"versions/{V2}"
    assert (versions.approved(), versions.previous()) == (V2, V1)

    start(service, workspace, podman)  # and the next start is the approved one
    assert running_versions(podman, workspace.container) == {V2}


# --- a candidate that is no good leaves everything as it was ------------------------------


def test_a_broken_candidate_and_one_that_fails_its_checksum_change_nothing(service, store):
    versions = service.versions
    versions.reconcile()  # as rcman does when it starts: the store's version is the first approval
    versions.upgrade(V1, ADMIN)  # something to be left: two versions and two approvals
    before = (tree(base.CLAUDE), versions.approvals(), containers())

    with pytest.raises(Problem) as broken:
        versions.upgrade(BROKEN, ADMIN)
    assert broken.value.code == "claude_checks_failed"
    assert set(broken.value.extra["failed"]) == {name for name, _ in claude_checks.CHECKS}
    assert (tree(base.CLAUDE), versions.approvals(), containers()) == before
    assert list(versions.staging.iterdir()) == []

    with pytest.raises(Problem) as failing:
        versions.upgrade(FAILING, ADMIN)
    assert (failing.value.code, failing.value.extra["kind"]) == (
        "claude_install_failed",
        "checksum",
    )
    assert (tree(base.CLAUDE), versions.approvals(), containers()) == before
    assert list(versions.staging.iterdir()) == []

    # Positive control: all of that is what a good rollout changes.
    versions.upgrade(V2, ADMIN)
    assert tree(base.CLAUDE) != before[0] and len(versions.approvals()) == 3
    assert canary_containers() == []


# --- the canary container, as designed ----------------------------------------------------


def slow_candidate(folder: Path) -> None:
    """A good candidate that waits at every call, so that the canary container is up long
    enough to be looked at: it sleeps, then runs the good one beside it."""
    claude_download.candidate(folder, V1)
    real = folder / "real"
    (folder / claude_download.CANDIDATE).rename(real)
    wrapper = folder / claude_download.CANDIDATE
    wrapper.write_text(
        f'#!/bin/sh\nsleep {SLOW_SECONDS}\nexec {rt.CANARY_CANDIDATE_MOUNT}/real "$@"\n'
    )
    wrapper.chmod(0o755)


def in_canary(script: str) -> subprocess.CompletedProcess:
    return base.podman_cli("exec", rt.CANARY_NAME, "sh", "-c", script)


def test_the_canary_container_is_what_the_design_says(podman, image, tmp_path):
    folder = tmp_path / "candidate"
    slow_candidate(folder)
    # A canary a rollout that was cut short left, which has the name and not the label.
    stale = base.podman_cli("run", "-d", f"--name={rt.CANARY_NAME}", "--label=stale=1", image,
                            "sleep", "600")  # fmt: skip
    assert stale.returncode == 0, stale.stderr
    stale_id = stale.stdout.strip()
    result: dict = {}

    def run() -> None:
        try:
            result["failed"] = podman.run_canary(folder, rt.CANARY_NAME, V1)
        except BaseException as error:  # noqa: BLE001 - told to the test
            result["error"] = error

    thread = threading.Thread(target=run)
    thread.start()
    try:
        deadline = time.monotonic() + APPEARS_WITHIN
        label = f"--filter=label={rt.LABEL_CANARY}"
        while not base.podman_cli("ps", label, "--format", "{{.ID}}").stdout.split():
            assert "error" not in result, result
            assert time.monotonic() < deadline, "the canary container never appeared"
            time.sleep(0.1)
        found = json.loads(base.podman_cli("container", "inspect", rt.CANARY_NAME).stdout)[0]
        assert found["Id"] != stale_id and found["Config"]["Labels"].get("stale") is None
        # the stale one was removed first, before this one was made
        assert base.podman_cli("container", "exists", stale_id).returncode != 0
        host = found["HostConfig"]
        assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"] is True
        assert any(o.startswith("no-new-privileges") for o in host["SecurityOpt"])
        mounts = {m["Destination"]: m for m in found["Mounts"] if m["Type"] == "bind"}
        helpers = f"{rt.CANARY_HELPERS_MOUNT}/{rt.HELPERS_DIR.name}"
        assert set(mounts) == {rt.CANARY_CANDIDATE_MOUNT, helpers}
        assert not mounts[rt.CANARY_CANDIDATE_MOUNT]["RW"] and not mounts[helpers]["RW"]
        assert mounts[rt.CANARY_CANDIDATE_MOUNT]["Source"] == str(folder)

        # From inside: the builder's uid range, no capabilities, no new privileges.
        slot_base = str(people.BUILDER_SLOT * rt.RANGE)
        ids = in_canary("cat /proc/self/uid_map /proc/self/gid_map")
        assert ids.stdout.split() == ["0", slot_base, str(rt.RANGE)] * 2, ids
        status = in_canary("grep -E '^(CapEff|CapBnd|NoNewPrivs):' /proc/self/status")
        assert [line.split()[1] for line in status.stdout.splitlines()] == [
            "0000000000000000", "0000000000000000", "1",
        ], status  # fmt: skip
        assert in_canary("ls /sys/class/net").stdout.split() == ["lo"]  # no network
        assert in_canary("touch /x").returncode != 0  # the root is read-only
        assert in_canary(f"touch {rt.CANARY_CANDIDATE_MOUNT}/x").returncode != 0
        assert in_canary(f"touch {helpers}/x").returncode != 0
        assert in_canary(f"touch {rt.CANARY_TMP}/x").returncode == 0  # positive control
        # Readable and runnable by that range, which is not the files' owner and has no
        # capability to read what its mode does not allow it.
        owner = in_canary(f"stat -c %u {rt.CANARY_CANDIDATE_MOUNT}/{rt.CANDIDATE}")
        assert owner.stdout.strip() not in ("0", str(os.getuid())), owner
        assert in_canary(f"test -x {rt.CANARY_CANDIDATE_MOUNT}/{rt.CANDIDATE}").returncode == 0
        assert in_canary(f"ls {rt.CANARY_CANDIDATE_MOUNT}").stdout.split() == ["claude", "real"]
        # python3 is in the default image, and imports the helpers where it is told to.
        imported = in_canary(
            "python3 -c 'import helpers.claude_canary, sys; print(sys.version_info[0])'"
        )
        assert imported.stdout.strip() == "3", imported
    finally:
        thread.join(timeout=300)
        for left in (rt.CANARY_NAME, stale_id):
            if thread.is_alive() or left == stale_id:
                base.podman_cli("rm", "--force", "--ignore", "--time=0", left)
    assert not thread.is_alive()
    assert result.get("error") is None, result
    assert result["failed"] == {}  # the good candidate fails no check, in the real container
    assert canary_containers() == []  # removed after


def test_podman_remote_run_ends_a_container_at_its_timeout(image):
    """The canary's ``--timeout``: accepted by ``podman --remote run``, and it kills what runs
    past it."""
    name = "rcman-test-timeout"
    began = time.monotonic()
    try:
        ran = base.podman_cli("run", "--rm", f"--name={name}", "--timeout=3", image, "sleep", "600")
        took = time.monotonic() - began
        assert "unknown flag" not in ran.stderr and "no such" not in ran.stderr.lower(), ran.stderr
        assert ran.returncode != 0 and took < 120, (ran, took)
    finally:
        base.podman_cli("rm", "--force", "--ignore", "--time=0", name)
    assert base.podman_cli("container", "exists", name).returncode != 0


@pytest.mark.parametrize("case", runtime_contract.CANARY_CASES, ids=lambda case: case.__name__)
def test_podman_keeps_the_canary_contract(podman, case):
    """The canary cases every runtime keeps, which need python3 in the image as the default
    workspace image has it."""
    case(podman, base.spec)
    assert canary_containers() == []
