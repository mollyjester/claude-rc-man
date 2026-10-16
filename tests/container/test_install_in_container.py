"""rcman installed for real, as root, in the podman host, where systemd runs.

``tests/test_container_suite.py`` runs this module in a fresh podman host. The
checkout's files at /src become a git origin (tests/container/clone.py); a user
``dev`` clones it and runs ``install.sh`` through the real path, with the fake claude
as the Claude Code release it downloads (tests/container/installed.py). The
toolchain comes from a cache the host fills with the installer's own download code;
the images Podman builds from come from their registry. The tests share one
installation and run in order; the last leaves a workspace whose server runs, for
tests/container/test_restored_in_container.py to find again once the whole host has
restarted.
"""

import grp
import json
import os
import pwd
import re
import shutil
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RCMAN_IN_CONTAINER") != "1" or os.geteuid() != 0,
    reason="runs only as root inside the podman host",
)

sys.path.insert(0, str(Path(__file__).resolve().parent))

from clone import CLONE, ORIGIN, SRC, git, make_clone, sh  # noqa: E402
from installed import (  # noqa: E402
    ADMIN,
    DOMAIN,
    ETC,
    MEBIBYTE,
    ROOT,
    SETTINGS,
    SOCKET,
    STATE,
    api,
    app,
    cgroup_of,
    changes,
    claude_processes,
    drop_in_times,
    healthy,
    in_workspace,
    install,
    limit_files,
    manager_pid,
    not_limited,
    person_id,
    podman,
    rcman_uid,
    serve_releases,
    slice_dir,
    until,
    user_slice,
    workspace_of,
)

sys.path.insert(0, str(SRC))
sys.path.insert(0, str(SRC / "backend"))
from rcman import limits  # noqa: E402

from installer import install as ins  # noqa: E402

# What test_restored_in_container.py reads, once the host has restarted.
LEFT_RUNNING = Path("/var/tmp/rcman-left-running.json")
BOB = "bob@example.com"


def get(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


@pytest.fixture(scope="module")
def first_install():
    download = serve_releases()
    SETTINGS["RCMAN_INSTALL_CLAUDE_DOWNLOAD"] = download
    make_clone()
    proc = install()
    assert proc.returncode == 0, proc.stdout + proc.stderr + journal()
    return proc


def journal(unit: str = "rcman.service") -> str:
    """The end of what a unit wrote, for failure messages."""
    found = subprocess.run(
        ["journalctl", "--no-pager", "-o", "cat", "-n", "200", "-u", unit],
        capture_output=True,
        text=True,
    )
    return f"\n--- {unit}\n{found.stdout}"


# --- the first installation ---------------------------------------------------------


def root_owned(directory: Path) -> list[str]:
    return sh("find", str(directory), "-user", "root").stdout.split("\n")[:-1]


def test_installing_leaves_nothing_owned_by_root_in_the_clone(first_install, tmp_path):
    """install.sh runs as root from someone's clone; it must not leave root's files there."""
    planted = tmp_path / "planted"
    planted.write_text("x")  # this suite runs as root
    assert str(planted) in root_owned(tmp_path)  # positive control: the check finds root's file
    assert sh("find", str(CLONE), "-type", "f").stdout  # non-vacuity: the clone has files
    assert root_owned(CLONE) == []


def test_installs_the_clones_commit_as_the_current_release(first_install):
    commit = git("rev-parse", "HEAD", cwd=CLONE)
    assert os.readlink(ROOT / "current") == f"releases/{commit[:12]}"
    release = ROOT / "current"
    assert (release / ".complete").read_text().strip() == commit
    assert (release / "frontend" / "dist" / "index.html").is_file()
    for script in ("uvicorn", "rcman-admin"):
        assert (release / ".venv" / "bin" / script).is_file(), script


def test_the_release_holds_only_runtime_files_owned_by_root(first_install):
    release = (ROOT / "current").resolve()
    for absent in (
        "tests",
        "devtools",
        "installer",
        "frontend/src",
        "install.sh",
        "project-knowledge",
    ):
        assert not (release / absent).exists(), absent
    for path in [release, *release.rglob("*")]:
        if path.is_symlink():
            continue
        st = path.lstat()
        assert st.st_uid == 0, path
        assert not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH), path


def test_accounts_exist(first_install):
    assert sh("id", "-nG", "rcman-proxy").stdout.split().count("rcman") == 1
    assert grp.getgrnam("rcman-web").gr_mem == ["caddy"]
    state = Path("/var/lib/rcman")
    assert state.stat().st_uid == pwd.getpwnam("rcman").pw_uid
    assert stat.S_IMODE(state.stat().st_mode) == 0o750


def test_rcman_has_rootless_podman_of_its_own(first_install):
    uid = rcman_uid()
    for path in ins.SUBORDINATE_FILES:
        mine = [line.split(":") for line in path.read_text().splitlines()]
        assert sum(int(f[2]) for f in mine if f[0] == "rcman") >= ins.SUBORDINATE_COUNT, path
    assert (ins.LINGER / "rcman").exists()
    manager = ["systemctl", "--user", "--machine", "rcman@"]
    assert sh(*manager, "is-active", "podman.socket").stdout.strip() == "active"
    assert ins.podman_socket(uid).is_socket()
    info = json.loads(podman("info", "--format", "json").stdout)
    assert info["host"]["security"]["rootless"] is True


def test_nothing_limits_processes_but_what_rcman_sets(first_install):
    """The drop-ins are there with the installer's text, systemd has read them, and the
    controllers limits need are the user manager's."""
    uid = rcman_uid()
    slice_file = ins.SYSTEMD / f"user-{uid}.slice.d" / "rcman.conf"
    user_file = STATE / ".config" / "systemd" / "user.conf.d" / "rcman.conf"
    assert slice_file.read_text() == ins.USER_SLICE_DROP_IN
    assert user_file.read_text() == ins.USER_MANAGER_DROP_IN
    assert pwd.getpwuid(user_file.stat().st_uid).pw_name == "rcman"
    assert ins.limits_problem(uid) is None  # the controllers limits need are the manager's
    assert (user_slice() / "pids.max").read_text().strip() == "max"
    manager = ["systemctl", "--user", "--machine", "rcman@"]
    shown = sh(*manager, "show", "--property", "DefaultTasksMax", "--value").stdout.strip()
    assert shown == limits.INFINITY


def test_rcman_runs_with_no_new_privileges_and_its_podman_socket(first_install):
    shown = sh("systemctl", "show", "rcman.service", "--property", "NoNewPrivileges,Environment")
    assert "NoNewPrivileges=yes" in shown.stdout
    assert f"XDG_RUNTIME_DIR=/run/user/{rcman_uid()}" in shown.stdout


def test_the_tools_are_static_and_root_s(first_install):
    for name in ("tmux", "busybox"):
        program = ins.TOOLS / "bin" / name
        assert ins.is_static(program), name
        assert program.stat().st_uid == 0 and stat.S_IMODE(program.stat().st_mode) == 0o755
    assert os.readlink(ins.TOOLS / "bin" / "sh") == "busybox"
    for entry in ins.TERMINFO:
        assert (ins.TOOLS / "terminfo" / entry).is_file(), entry
    conf = (ROOT / "current" / "backend" / "rcman" / "tmux.conf").read_text()
    assert (ins.TOOLS / "tmux.conf").read_text() == conf


def test_the_default_image_is_built_and_named_in_rcmans_settings(first_install):
    image = ins.workspace_image()
    assert podman("image", "exists", image, check=False).returncode == 0
    assert f"RCMAN_WORKSPACE_IMAGE={image}\n" in (ETC / "rcman.env").read_text()


def test_the_devcontainer_builder_image_is_built_and_named_in_rcmans_settings(first_install):
    image = ins.devcontainer_builder_image()
    assert podman("image", "exists", image, check=False).returncode == 0
    assert f"RCMAN_BUILDER_IMAGE={image}\n" in (ETC / "rcman.env").read_text()
    shown = podman("run", "--rm", "--pull=never", image, "devcontainer", "--version")
    assert re.fullmatch(r"\d+\.\d+\.\d+", shown.stdout.strip()), shown.stdout


def test_rcmans_subordinate_range_covers_the_builders_slot(first_install):
    python = ROOT / "current" / ".venv" / "bin" / "python"
    slot = int(
        sh(
            str(python),
            "-c",
            "from rcman import people; print(people.BUILDER_SLOT)",
            cwd=ROOT / "current" / "backend",
        ).stdout  # fmt: skip
    )
    for path in ins.SUBORDINATE_FILES:
        mine = [line.split(":") for line in path.read_text().splitlines()]
        granted = sum(int(f[2]) for f in mine if f[0] == "rcman")
        assert granted >= (slot + 1) * ins.RANGE, path


def test_the_claude_store_holds_the_tested_version(first_install):
    version = (SRC / "helpers" / "claude-version").read_text().strip()
    store = ins.CLAUDE_STORE
    assert os.readlink(store / "current") == f"versions/{version}"
    assert os.readlink(store / "bin" / "claude") == "../current"
    assert stat.S_IMODE((store / "versions" / version).stat().st_mode) == 0o755


def test_the_claude_store_is_rcmans_and_readable_by_every_workspace(first_install):
    """rcman writes the store from the first rollout on; a workspace, whose ids are not rcman's,
    reads it through the mode alone."""
    store = ins.CLAUDE_STORE
    rcman = pwd.getpwnam("rcman")
    everything = [store, *store.rglob("*")]
    assert len(everything) > 4  # non-vacuity: the folders, the version, the links
    for path in everything:
        found = path.lstat()
        assert (found.st_uid, found.st_gid) == (rcman.pw_uid, rcman.pw_gid), path
        if not path.is_symlink():
            assert found.st_mode & 0o055 == 0o055, path  # read, and for a folder enter
            assert not found.st_mode & 0o022, path  # written by its owner alone


def test_installing_again_leaves_current_on_the_version_rcman_approved(first_install):
    """``current`` is rcman's to switch once it runs: the installer sets it only where there is
    none, so an administrator's approval of another version outlasts an update."""
    store = ins.CLAUDE_STORE
    tested = (SRC / "helpers" / "claude-version").read_text().strip()
    approved = store / "versions" / "9.9.9"
    shutil.copy2(store / "versions" / tested, approved)
    link = store / ".current.new"

    def point(target: str) -> None:
        link.unlink(missing_ok=True)
        link.symlink_to(target)
        os.replace(link, store / "current")

    point("versions/9.9.9")
    try:
        proc = install()
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert changes(proc)["changed"] == []
        assert os.readlink(store / "current") == "versions/9.9.9"
        assert sorted(p.name for p in (store / "versions").iterdir()) == sorted([tested, "9.9.9"])
    finally:
        point(f"versions/{tested}")
        approved.unlink()


def test_settings_are_saved_privately(first_install):
    saved = ETC / "install.env"
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    text = saved.read_text()
    assert f"DOMAIN={DOMAIN}\n" in text and "COOKIE_SECRET=" in text
    assert "CLAUDE_DOWNLOAD=" in text


def test_configuration_files_and_their_owners(first_install):
    cfg = ETC / "oauth2-proxy.cfg"
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o640
    assert grp.getgrgid(cfg.stat().st_gid).gr_name == "rcman-proxy"
    env = ETC / "rcman.env"
    assert grp.getgrgid(env.stat().st_gid).gr_name == "rcman"
    assert f"RCMAN_PUBLIC_ORIGIN=https://{DOMAIN}" in env.read_text()
    allowed = STATE / "allowed-emails"
    assert allowed.read_text() == f"{ADMIN}\n"
    assert pwd.getpwuid(allowed.stat().st_uid).pw_name == "rcman"
    assert grp.getgrgid(allowed.stat().st_gid).gr_name == "rcman"
    assert stat.S_IMODE(allowed.stat().st_mode) == 0o640
    assert not Path("/etc/sudoers.d/rcman").exists()
    for database in STATE.glob("rcman.db*"):  # oauth2-proxy shares rcman's group
        assert stat.S_IMODE(database.stat().st_mode) == 0o600, database


def test_services_answer(first_install):
    assert app("/api/health") == (200, '{"status":"ok"}')
    assert get("http://127.0.0.1:4180/ping")[0] == 200
    status, body = app("/")
    assert status == 200 and '<div id="root">' in body


def test_the_first_administrator_is_there(first_install):
    status, me = api("GET", "/api/me")
    assert status == 200 and me["admin"] is True


def test_an_administrator_runs_rcman_admin_by_name_as_root(first_install):
    """As the guides say: `sudo rcman-admin`, found on root's path, run as rcman."""
    proc = sh("rcman-admin", "list", check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert ADMIN in proc.stdout


def test_the_socket_directory_admits_only_its_group(first_install):
    run = Path("/run/rcman")
    st = run.stat()
    assert pwd.getpwuid(st.st_uid).pw_name == "rcman"
    assert grp.getgrgid(st.st_gid).gr_name == "rcman-web"
    assert stat.S_IMODE(st.st_mode) == 0o750  # the socket itself is 0666; this is what guards it
    tmpfiles = Path("/etc/tmpfiles.d/rcman.conf").read_text()
    assert tmpfiles == "d /run/rcman 0750 rcman rcman-web -\n"
    assert app("/api/health", user="caddy")[0] == 200  # positive control
    status, text = app("/api/health", user="dev")
    assert status is None and text.startswith("PermissionError"), text
    for other in ("rcman-proxy", "nobody"):
        assert app("/api/health", user=other)[0] is None, other


def test_the_socket_group_is_kept_to_caddy_alone(first_install):
    sh("usermod", "--append", "--groups", "rcman-web", "dev")
    assert "dev" in grp.getgrnam("rcman-web").gr_mem  # the plant took effect
    proc = install()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert grp.getgrnam("rcman-web").gr_mem == ["caddy"]
    assert "dev removed from group rcman-web" in changes(proc)["changed"]


def listeners():
    """Every listening TCP address (host, port) and unix socket path in the container."""
    tcp = set()
    for table, family in (("/proc/net/tcp", socket.AF_INET), ("/proc/net/tcp6", socket.AF_INET6)):
        for line in Path(table).read_text().splitlines()[1:]:
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
            tcp.add((host, int(port, 16)))
    unix = set()
    for line in Path("/proc/net/unix").read_text().splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 8 and fields[5] == "01" and int(fields[3], 16) & 0x10000:
            if not fields[7].startswith("@"):  # abstract names are not files
                unix.add(fields[7])
    return tcp, unix


PROBE = """
import json, sys, urllib.request, urllib.error
req = urllib.request.Request(sys.argv[1], headers=json.loads(sys.argv[2]))
try:
    r = urllib.request.urlopen(req, timeout=10)
    print(r.status, r.read().decode(errors="replace"))
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode(errors="replace"))
except Exception as e:
    print("refused", e)
"""


def test_no_loopback_listener_lets_a_local_user_impersonate(first_install):
    """Whatever listens here, a local user cannot use it to be someone in rcman."""
    admin = "dev@example.com"
    spoof = {"X-Forwarded-Email": admin, "X-Auth-Request-Email": admin}
    tcp, unix = listeners()
    assert ("127.0.0.1", 4180) in tcp and SOCKET in unix  # non-vacuity: both are found
    for host, port in sorted(tcp):
        url = f"http://{'[' + host + ']' if ':' in host else host}:{port}/api/me"
        out = sh(
            "runuser", "-u", "dev", "--", sys.executable, "-c", PROBE, url, json.dumps(spoof)
        ).stdout
        assert '"admin":true' not in out and f'"email":"{admin}"' not in out, (host, port, out)
    for path in sorted(unix):
        status, text = app("/api/me", spoof, user="dev", path=path)
        assert '"admin":true' not in text and f'"email":"{admin}"' not in text, (path, text)
    # oauth2-proxy without its cookie names no one.
    request = urllib.request.Request("http://127.0.0.1:4180/oauth2/auth", headers=spoof)
    try:
        urllib.request.urlopen(request, timeout=10)
        raise AssertionError("oauth2-proxy accepted a request without its cookie")
    except urllib.error.HTTPError as error:
        assert error.code == 401
        assert "X-Auth-Request-Email" not in error.headers
    # Positive control: through the socket, as Caddy, the header is believed.
    status, text = app("/api/me", spoof, user="caddy")
    assert status == 200 and json.loads(text)["email"] == admin
    assert '"admin":true' in text


def test_the_installer_warns_about_a_caddy_admin_api_on_loopback(first_install):
    """Planted twin: a Caddy whose admin API is on loopback, as by default, is warned about."""
    sys.path.insert(0, str(SRC))
    from installer import install as ins

    assert ins.CADDY_ADMIN_WARNING not in first_install.stdout + first_install.stderr
    caddy = subprocess.Popen(
        ["caddy", "run"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )  # no configuration: the admin API on localhost:2019
    try:
        deadline = time.monotonic() + 20
        while ("127.0.0.1", 2019) not in listeners()[0] and time.monotonic() < deadline:
            time.sleep(0.2)
        assert ("127.0.0.1", 2019) in listeners()[0]
        proc = install()
        assert proc.returncode == 0, proc.stdout + proc.stderr  # a warning, not a refusal
        assert ins.CADDY_ADMIN_WARNING in proc.stdout + proc.stderr
    finally:
        caddy.terminate()
        caddy.wait(timeout=10)


def test_audit_events_reach_the_journal(first_install):
    """The service's output is what systemd sends to the journal; an audit event is a line in it."""
    cursor = sh("journalctl", "--show-cursor", "-n", "0", "-o", "cat").stdout.split()[-1]
    status, settings = api("PATCH", "/api/me/settings", body={"prefix": "journal"})
    assert status == 200, settings
    api("PATCH", "/api/me/settings", body={"prefix": ""})

    def events():
        lines = sh("journalctl", "--after-cursor", cursor, "-u", "rcman.service", "-o", "cat")
        found = []
        for line in lines.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("target") == '{"prefix": "journal"}':
                found.append(event)
        return found

    [event] = until(events, within=30)
    assert (event["action"], event["result"]) == ("settings.update", "ok")


def test_the_caddy_snippet_is_printed_and_valid(first_install):
    assert "Add this to your Caddyfile" in first_install.stdout
    assert "The Caddy site block changed" in first_install.stdout
    snippet = (ETC / "Caddyfile.snippet").read_text()
    assert snippet in first_install.stdout
    path = Path("/tmp/Caddyfile")
    path.write_text(snippet)
    check = sh("caddy", "validate", "--adapter", "caddyfile", "--config", str(path), check=False)
    assert check.returncode == 0, check.stdout + check.stderr


# --- running it again -----------------------------------------------------------------


def test_running_again_changes_nothing(first_install):
    release = os.readlink(ROOT / "current")
    proc = install()
    assert proc.returncode == 0, proc.stderr
    assert changes(proc)["changed"] == []
    assert "The Caddy site block changed" not in proc.stdout
    assert "restart Caddy" not in proc.stdout
    assert os.readlink(ROOT / "current") == release
    assert app("/api/health")[0] == 200


def test_installing_again_changes_nothing_and_ends_nothing_that_runs(first_install):
    """A workspace's container, with limits on its slices, runs through a second installation
    as it was: the same process, the same user manager, the same limits."""
    probe = made("limits-probe")
    me = person_id()
    held = {"memory": "2G", "cpu": "1.5", "pids": "3000"}
    status, answer = api("PUT", f"/api/people/{me}/workspaces/{probe['id']}/limits", body=held)
    assert status == 200, answer
    directory = slice_dir(me, probe["id"])
    files = limit_files(directory)
    assert files["memory.max"] == str(2048 * MEBIBYTE) and files["pids.max"] == "3000"

    def running() -> tuple:
        name = f"rcman-ws-{probe['id']}"
        state = json.loads(podman("container", "inspect", name).stdout)[0]["State"]
        assert state["Running"]
        return (
            state["Pid"],
            state["ConmonPid"],
            state["StartedAt"],
            manager_pid(),
            limit_files(directory),
        )

    before = running()
    assert before[3] != "0"
    proc = install()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert changes(proc)["changed"] == []
    assert running() == before
    assert app("/api/health")[0] == 200

    # Deleting the workspace takes its limits off the slice, which a later workspace of the
    # same id must not find.
    status, answer = api("DELETE", f"/api/workspaces/{probe['id']}")
    assert status == 200, answer
    until(lambda: workspace_of("limits-probe") is None)
    assert directory.is_dir()  # a slice stays once made, so what it holds is looked at
    assert not_limited(directory), limit_files(directory)


def test_saved_settings_need_no_answers(first_install):
    proc = install(env={k: "" for k in SETTINGS})
    assert proc.returncode == 0, proc.stderr


def test_a_dirty_clone_is_refused(first_install):
    readme = CLONE / "README.md"
    original = readme.read_text()
    readme.write_text(original + "local change\n")
    try:
        proc = install()
        assert proc.returncode == 1 and "uncommitted" in proc.stderr
    finally:
        readme.write_text(original)


def test_an_update_of_install_sh_restarts_it_and_builds_a_new_release(first_install):
    old = os.readlink(ROOT / "current")
    commit_in_origin("install.sh", "# a change to the installer\n", "change install.sh")
    proc = install()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "install.sh changed; starting it again." in proc.stdout
    head = git("rev-parse", "HEAD", cwd=CLONE)
    assert (
        head == sh("git", "--git-dir", str(ORIGIN), "rev-parse", "main", user="dev").stdout.strip()
    )
    assert os.readlink(ROOT / "current") == f"releases/{head[:12]}"
    assert os.readlink(ROOT / "previous") == old
    assert app("/api/health")[0] == 200


def test_rollback_returns_to_the_previous_release(first_install):
    current, previous = os.readlink(ROOT / "current"), os.readlink(ROOT / "previous")
    proc = subprocess.run([str(CLONE / "install.sh"), "--rollback"], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
    assert os.readlink(ROOT / "current") == previous
    assert os.readlink(ROOT / "previous") == current
    command = Path("/usr/local/sbin/rcman-admin").resolve()
    assert command == (ROOT / previous / ".venv" / "bin" / "rcman-admin").resolve()
    assert sh("rcman-admin", "list", check=False).returncode == 0
    healthy()


def test_a_clone_that_cannot_fast_forward_is_refused(first_install):
    commit_in_origin("README.md", "upstream change\n", "upstream")
    readme = CLONE / "README.md"
    readme.write_text(readme.read_text() + "local commit\n")
    git("commit", "-qam", "local", cwd=CLONE)
    proc = install()
    assert proc.returncode == 1 and "fast-forward" in proc.stderr


# --- people and workspaces, in the installation ----------------------------------------


def made(name: str, who: str = ADMIN) -> dict:
    status, answer = api("POST", "/api/workspaces", who, {"name": name})
    assert status == 202, answer

    def ready():
        found = workspace_of(name, who)
        return found if found and found["state"] != "creating" else None

    found = until(ready)
    assert found["state"] == "ready", found
    return found


def server_state(name: str, who: str = ADMIN) -> str | None:
    found = workspace_of(name, who)
    return found and found["server"] and found["server"]["state"]


def test_a_person_is_added_and_can_come_in(first_install):
    status, answer = api("POST", "/api/people", body={"email": BOB})
    assert status == 200, answer
    assert BOB in (STATE / "allowed-emails").read_text()
    assert api("GET", "/api/me", BOB)[0] == 200


def test_a_workspace_is_made_and_its_server_is_refused_until_claude_is_signed_in(first_install):
    alpha = made("alpha")
    status, answer = api("POST", "/api/servers", body={"workspace": alpha["id"]})
    assert (status, answer["code"]) == (409, "claude_not_logged_in"), answer


def ready_server(name: str) -> None:
    found = workspace_of(name)
    status, answer = api("POST", "/api/servers", body={"workspace": found["id"]})
    assert status == 200 and answer["server"]["state"] == "trust_prompt", answer
    status, answer = api(
        "POST", "/api/servers/confirm", body={"workspace": found["id"], "answer": "y"}
    )
    assert status == 200, answer
    until(lambda: server_state(name) == "ready")
    assert claude_processes(found["id"])


def test_signing_in_once_lets_workspaces_made_before_and_after_reach_ready(first_install):
    """One sign-in, through a sign-in container that is gone afterwards, reaches alpha,
    made before it, and gamma, made after it."""
    import sys

    sys.path.insert(0, str(SRC))
    from devtools.fake_claude import ACCEPTED_CODE

    status, answer = api("POST", "/api/claude/login", body={})
    assert status == 200 and answer["url"].startswith("https://claude.com/"), answer
    status, answer = api("POST", "/api/claude/code", body={"code": ACCEPTED_CODE})
    assert status == 200 and answer["connection"] == "connected", answer
    assert api("GET", "/api/claude")[1]["account"]["email"] == ADMIN
    me = next(p for p in api("GET", "/api/people")[1]["people"] if p["email"] == ADMIN)
    label = f"label=rcman.person={me['id']}"
    containers = podman("ps", "--all", "--format", "{{.Names}}", "--filter", label).stdout.split()
    assert containers and not [c for c in containers if "sign-in" in c], containers
    ready_server("alpha")
    made("gamma")
    ready_server("gamma")
    status, answer = api(
        "POST", "/api/servers/stop", body={"workspace": workspace_of("gamma")["id"]}
    )
    assert status == 200, answer


def test_a_servers_processes_run_outside_rcmans_service(first_install):
    alpha = workspace_of("alpha")
    found = json.loads(podman("container", "inspect", f"rcman-ws-{alpha['id']}").stdout)[0]
    manager = f"/user.slice/user-{rcman_uid()}.slice/user@{rcman_uid()}.service/"
    for pid in (found["State"]["Pid"], found["State"]["ConmonPid"]):
        cgroup = Path(f"/proc/{pid}/cgroup").read_text().strip().removeprefix("0::")
        assert cgroup.startswith(manager) and "rcman.service" not in cgroup, cgroup


def test_restarting_rcman_leaves_the_server_running_as_it_was(first_install):
    alpha = workspace_of("alpha")
    before = claude_processes(alpha["id"])
    assert before  # positive control
    sh("systemctl", "restart", "rcman.service")
    healthy()
    until(lambda: server_state("alpha") == "ready", within=60)
    assert claude_processes(alpha["id"]) == before


def test_a_stop_leaves_no_claude_process_in_the_workspace(first_install):
    alpha = workspace_of("alpha")
    assert claude_processes(alpha["id"])  # positive control
    status, answer = api("POST", "/api/servers/stop", body={"workspace": alpha["id"]})
    assert status == 200 and answer["server"]["state"] == "stopped", answer
    assert claude_processes(alpha["id"]) == []
    status, answer = api("POST", "/api/servers/resume", body={"workspace": alpha["id"]})
    assert status == 200, answer
    until(lambda: server_state("alpha") == "ready")


def test_one_person_cannot_reach_anothers_workspace(first_install):
    alpha = workspace_of("alpha")
    signed_in_bob = made("beta", BOB)
    assert workspace_of("beta") is None  # not in the administrator's own list
    for method, url, body in (
        ("POST", "/api/servers", {"workspace": signed_in_bob["id"]}),
        ("GET", f"/api/servers/output?workspace={signed_in_bob['id']}", None),
        ("DELETE", f"/api/workspaces/{signed_in_bob['id']}", None),
    ):
        status, answer = api(method, url, body=body)
        assert (status, answer["code"]) == (404, "workspace_not_found"), (url, answer)
    mappings = {}
    for workspace in (alpha, signed_in_bob):
        name = f"rcman-ws-{workspace['id']}"
        found = json.loads(podman("container", "inspect", name).stdout)[0]
        mounts = {m.get("Name") for m in found["Mounts"] if m.get("Type") == "volume"}
        assert mounts == {name}, mounts  # its own volume, and no one else's
        person = found["Config"]["Labels"]["rcman.person"]
        own = podman("volume", "inspect", "--format", "{{.Mountpoint}}", f"rcman-person-{person}")
        binds = [m["Source"] for m in found["Mounts"] if m.get("Type") == "bind"]
        assert f"{own.stdout.strip()}/.credentials.json" in binds, binds  # their own sign-in
        mappings[name] = in_workspace(workspace["id"], "cat /proc/self/uid_map").split()
    first, second = mappings.values()
    assert first[0] == second[0] == "0" and first[1] != second[1]  # ranges of their own
    for path in (SOCKET, f"/run/user/{rcman_uid()}/podman/podman.sock", str(STATE)):
        assert in_workspace(alpha["id"], f"test -e {path} && echo there", check=False) == ""


def test_deleting_a_person_leaves_nothing_labelled_theirs(first_install):
    status, everyone = api("GET", "/api/people")
    bob = next(p for p in everyone["people"] if p["email"] == BOB)
    label = f"label=rcman.person={bob['id']}"
    assert podman("ps", "--all", "--quiet", "--filter", label).stdout.split()  # positive control
    volumes = podman("volume", "ls", "--quiet", "--filter", label).stdout.split()
    assert f"rcman-person-{bob['id']}" in volumes  # positive control: his sign-in's
    status, answer = api("DELETE", f"/api/people/{bob['id']}")
    assert status == 200, answer
    assert podman("ps", "--all", "--quiet", "--filter", label).stdout.split() == []
    assert podman("volume", "ls", "--quiet", "--filter", label).stdout.split() == []
    assert BOB not in (STATE / "allowed-emails").read_text()
    assert api("GET", "/api/me", BOB)[0] == 403


def test_an_administrators_limits_reach_the_kernel_through_the_api_and_the_command(first_install):
    """A person's through the API, a workspace's through ``rcman-admin``; the workspace's server
    runs on, and both are read back with what is in use."""
    alpha = workspace_of("alpha")
    me = person_id()
    before = claude_processes(alpha["id"])
    assert before and not_limited(slice_dir(me, alpha["id"]))  # positive control
    held = {"memory": "4G", "cpu": "3", "pids": "6000"}
    status, answer = api("PUT", f"/api/people/{me}/limits", body=held)
    assert status == 200, answer
    assert limit_files(slice_dir(me)) == {
        "memory.max": str(4096 * MEBIBYTE),
        "memory.swap.max": "0",
        "cpu.max": "300000 100000",
        "pids.max": "6000",
    }
    done = sh("rcman-admin", "limits", ADMIN, "--workspace", "alpha", "--memory", "2G",
              "--cpu", "1.5", "--pids", "3000")  # fmt: skip
    assert "memory=2G cpu=1.5 pids=3000" in done.stdout, done.stdout
    assert limit_files(slice_dir(me, alpha["id"])) == {
        "memory.max": str(2048 * MEBIBYTE),
        "memory.swap.max": "0",
        "cpu.max": "150000 100000",
        "pids.max": "3000",
    }
    shown = sh("rcman-admin", "limits", ADMIN, "--workspace", "alpha").stdout
    assert "memory=2G cpu=1.5 pids=3000" in shown and "In use: memory=" in shown, shown
    status, report = api("GET", f"/api/people/{me}/limits")
    assert status == 200, report
    assert report["person"]["limits"] == held and report["person"]["usage"]["tasks"] > 0
    (own,) = [w for w in report["workspaces"] if w["id"] == alpha["id"]]
    assert own["usage"]["tasks"] > 0 and own["limits"]["pids"] == "3000"
    found = json.loads(podman("container", "inspect", f"rcman-ws-{alpha['id']}").stdout)[0]
    leaf = cgroup_of(found["State"]["Pid"])  # the cgroup inside the container's scope
    assert leaf.parent.parent == slice_dir(me, alpha["id"]) and leaf.parent.name.startswith(
        "libpod-"
    )
    assert claude_processes(alpha["id"]) == before and server_state("alpha") == "ready"


def test_the_server_is_left_running_for_the_restart(first_install):
    alpha = workspace_of("alpha")
    assert server_state("alpha") == "ready"
    marker = Path("/run/rcman-test-before-the-restart")
    marker.touch()
    me = person_id()
    left = {
        "workspace": alpha["id"],
        "name": "alpha",
        "marker": str(marker),
        "person": me,
        "drop_ins": drop_in_times(me, alpha["id"]),
    }
    LEFT_RUNNING.write_text(json.dumps(left))


# --- Caddy in front -------------------------------------------------------------------


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


FAKE_AUTH = """
import http.server, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/oauth2/auth"):
            if "session=alice" in (self.headers.get("Cookie") or ""):
                self.send_response(202)
                self.send_header("X-Auth-Request-Email", "alice@example.com")
            else:
                self.send_response(401)
            self.end_headers(); return
        self.send_response(200); self.end_headers(); self.wfile.write(b"oauth2-proxy page")
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
"""
FAKE_APP = """
import http.server, socketserver, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers()
        self.wfile.write(("email=" + str(self.headers.get("X-Forwarded-Email"))).encode())
    def log_message(self, *a): pass
    def address_string(self): return "unix"
socketserver.UnixStreamServer(sys.argv[1], H).serve_forever()
"""


# How long the fake servers and Caddy get to start listening, and then to stop answering a
# request with a gateway error because a server behind Caddy was not ready.
STARTUP_DEADLINE = 20


def wait_accepting(family, address, what):
    """Waits until something accepts connections at the address, or fails naming what."""
    deadline = time.monotonic() + STARTUP_DEADLINE
    while True:
        try:
            with socket.socket(family) as s:
                s.settimeout(1)
                s.connect(address)
            return
        except OSError as error:
            if time.monotonic() > deadline:
                raise AssertionError(f"{what} at {address} did not accept: {error}") from error
            time.sleep(0.1)


def test_caddy_lets_only_oauth2_proxy_set_the_identity(tmp_path):
    sys.path.insert(0, str(SRC))
    from installer import install as ins

    auth, site = free_port(), free_port()
    fake_socket = tmp_path / "app.sock"
    rendered = ins.render(
        "Caddyfile", {"DOMAIN": f"http://127.0.0.1:{site}", "PROXY_PORT": str(auth)}
    )
    assert f"unix/{ins.SOCKET}" in rendered
    snippet = rendered.replace(str(ins.SOCKET), str(fake_socket))
    config = tmp_path / "Caddyfile"
    config.write_text("{\n\tadmin off\n\tauto_https off\n}\n" + snippet)
    # Each fake server is accepting before Caddy starts, so that no request through
    # Caddy can reach one that is still starting.
    procs = [
        subprocess.Popen([sys.executable, "-c", FAKE_AUTH, str(auth)]),
        subprocess.Popen([sys.executable, "-c", FAKE_APP, str(fake_socket)]),
    ]
    try:
        wait_accepting(socket.AF_INET, ("127.0.0.1", auth), "the fake oauth2-proxy")
        wait_accepting(socket.AF_UNIX, str(fake_socket), "the fake app")
        procs.append(
            subprocess.Popen(
                ["caddy", "run", "--adapter", "caddyfile", "--config", str(config)],
                stderr=subprocess.DEVNULL,
            )
        )
        wait_accepting(socket.AF_INET, ("127.0.0.1", site), "Caddy")
        base = f"http://127.0.0.1:{site}"
        opener = urllib.request.build_opener(
            type(
                "NoRedirect",
                (urllib.request.HTTPRedirectHandler,),
                {"redirect_request": lambda *a: None},
            )()
        )

        def fetch(path, headers):
            try:
                with opener.open(
                    urllib.request.Request(base + path, headers=headers), timeout=10
                ) as r:
                    return r.status, r.read().decode(), dict(r.headers)
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode(), dict(e.headers)

        spoof = {"X-Forwarded-Email": "evil@example.com"}
        status, body, headers = fetch("/api/me", spoof)
        assert status == 302 and headers["Location"].startswith(
            "/oauth2/start?rd="
        )  # not signed in
        assert "evil" not in body
        status, body, _ = fetch("/api/me", {**spoof, "Cookie": "session=alice"})
        # The spoofed value never arrives.
        assert (status, body) == (200, "email=alice@example.com"), (status, body)
        status, body, _ = fetch("/oauth2/start", {})
        assert (status, body) == (200, "oauth2-proxy page")
    finally:
        for proc in procs:
            proc.terminate()


def commit_in_origin(path, text, message):
    work = Path("/tmp/origin-work")
    if work.exists():
        shutil.rmtree(work)
    sh("git", "clone", "-q", str(ORIGIN), str(work), user="dev")
    target = work / path
    target.write_text(target.read_text() + text)
    git("commit", "-qam", message, cwd=work)
    git("push", "-q", "origin", "main", cwd=work)
