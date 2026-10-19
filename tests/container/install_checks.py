"""The checks of the stubbed installation: rcman's real installer, run as root in a container
of the system image on the tests' engine, with no systemd (tests/container/installed.py).

Not a test module. tests/container/test_install_in_container.py runs ``first`` once, as the
installation, and then each check by its name, as root in the container, in the order they are
here: ``python3 tests/container/install_checks.py <name>`` from /src, which exits 0 when the check
passes and prints what failed otherwise. Each check is what its test of that name asserts. The
checks share one installation and run in order, as the installations they run again build on
each other.
"""

import grp
import json
import os
import pwd
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import installed  # noqa: E402
from clone import CLONE, ORIGIN, SRC, git, sh  # noqa: E402
from installed import (  # noqa: E402
    ADMIN,
    DOMAIN,
    ETC,
    ROOT,
    SETTINGS,
    STATE,
    app,
    changes,
    healthy,
    install,
)

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


def root_owned(directory: Path) -> list[str]:
    return sh("find", str(directory), "-user", "root").stdout.split("\n")[:-1]


def listeners():
    """Every listening TCP address (host, port) and unix socket path in the container."""
    import socket

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


def commit_in_origin(path, text, message):
    work = Path("/tmp/origin-work")
    if work.exists():
        shutil.rmtree(work)
    sh("git", "clone", "-q", str(ORIGIN), str(work), user="dev")
    target = work / path
    target.write_text(target.read_text() + text)
    git("commit", "-qam", message, cwd=work)
    git("push", "-q", "origin", "main", cwd=work)


def test_installing_leaves_nothing_owned_by_root_in_the_clone():
    """install.sh runs as root from someone's clone; it must not leave root's files there."""
    tmp_path = Path(tempfile.mkdtemp(prefix="rcman-check-"))
    planted = tmp_path / "planted"
    planted.write_text("x")  # this suite runs as root
    assert str(planted) in root_owned(tmp_path)  # positive control: the check finds root's file
    assert sh("find", str(CLONE), "-type", "f").stdout  # non-vacuity: the clone has files
    assert root_owned(CLONE) == []


def test_installs_the_clones_commit_as_the_current_release():
    commit = git("rev-parse", "HEAD", cwd=CLONE)
    assert os.readlink(ROOT / "current") == f"releases/{commit[:12]}"
    release = ROOT / "current"
    assert (release / ".complete").read_text().strip() == commit
    assert (release / "frontend" / "dist" / "index.html").is_file()
    for script in ("uvicorn", "rcman-admin"):
        assert (release / ".venv" / "bin" / script).is_file(), script


def test_the_release_holds_only_runtime_files_owned_by_root():
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


def test_accounts_exist():
    assert sh("id", "-nG", "rcman-proxy").stdout.split().count("rcman") == 1
    assert grp.getgrnam("rcman-web").gr_mem == ["caddy"]
    state = Path("/var/lib/rcman")
    assert state.stat().st_uid == pwd.getpwnam("rcman").pw_uid
    assert stat.S_IMODE(state.stat().st_mode) == 0o750


def test_rcmans_subordinate_ids_and_linger_are_written():
    """What the installer writes for rcman's rootless Podman; that Podman runs under systemd is
    checked by hand (project-knowledge/real-claude-checklist.md)."""
    for path in ins.SUBORDINATE_FILES:
        mine = [line.split(":") for line in path.read_text().splitlines()]
        assert sum(int(f[2]) for f in mine if f[0] == "rcman") >= ins.SUBORDINATE_COUNT, path
    assert (ins.LINGER / "rcman").exists()
    assert installed.stood_in("loginctl", ["enable-linger", "rcman"])  # as the installer asked


def test_the_limits_drop_ins_are_written_with_the_installers_text():
    """The drop-ins and their owners; what systemd and the kernel make of them is checked by
    hand."""
    uid = installed.rcman_uid()
    slice_file = ins.SYSTEMD / f"user-{uid}.slice.d" / "rcman.conf"
    user_file = STATE / ".config" / "systemd" / "user.conf.d" / "rcman.conf"
    assert slice_file.read_text() == ins.USER_SLICE_DROP_IN
    assert slice_file.stat().st_uid == 0
    assert user_file.read_text() == ins.USER_MANAGER_DROP_IN
    assert pwd.getpwuid(user_file.stat().st_uid).pw_name == "rcman"


def test_the_unit_runs_rcman_with_no_new_privileges_and_its_podman_socket():
    """The unit's file; what systemd shows of the running unit is checked by hand."""
    unit = (ins.SYSTEMD / "rcman.service").read_text().splitlines()
    assert "NoNewPrivileges=yes" in unit
    assert f"Environment=XDG_RUNTIME_DIR=/run/user/{installed.rcman_uid()}" in unit


def test_the_tools_are_static_and_root_s():
    for name in ("tmux", "busybox"):
        program = ins.TOOLS / "bin" / name
        assert ins.is_static(program), name
        assert program.stat().st_uid == 0 and stat.S_IMODE(program.stat().st_mode) == 0o755
    assert os.readlink(ins.TOOLS / "bin" / "sh") == "busybox"
    for entry in ins.TERMINFO:
        assert (ins.TOOLS / "terminfo" / entry).is_file(), entry
    conf = (ROOT / "current" / "backend" / "rcman" / "tmux.conf").read_text()
    assert (ins.TOOLS / "tmux.conf").read_text() == conf


def test_the_tools_are_outside_rcmans_home_and_root_leaves_nothing_there_of_its_own():
    """The tools are root's in a folder only root can write; nothing of building them is in
    rcman's home, and what is in the home is rcman's alone (root made none of it)."""
    assert STATE not in ins.TOOLS.parents and ins.TOOLS.parent == ROOT
    assert ins.TOOLS.stat().st_uid == 0
    assert list(STATE.glob(".tmux-build.*")) == []
    assert f"RCMAN_TOOLS_DIR={ins.TOOLS}\n" in (ETC / "rcman.env").read_text()
    # positive control: the same find lists root's files where there are some
    assert sh("find", str(ROOT), "-maxdepth", "1", "-user", "root").stdout.strip()
    assert sh("find", str(STATE), "-maxdepth", "4", "-user", "root").stdout.strip() == ""


def test_the_default_and_builder_images_are_built_and_named_in_rcmans_settings():
    """Built by rcman's Podman (the stand-in, which notes what it builds) under the names
    rcman's settings give; that the real images are there and the CLI runs in the builder's is
    checked by hand."""
    env = (ETC / "rcman.env").read_text()
    built = json.loads((installed.STAND_INS / "built").read_text())
    for key, image in (("RCMAN_WORKSPACE_IMAGE", ins.workspace_image()),
                       ("RCMAN_BUILDER_IMAGE", ins.devcontainer_builder_image())):  # fmt: skip
        assert f"{key}={image}\n" in env, key
        assert image in built, image


def test_rcmans_subordinate_range_covers_the_builders_slot():
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


def test_the_claude_store_holds_the_tested_version():
    version = (SRC / "helpers" / "claude-version").read_text().strip()
    store = ins.CLAUDE_STORE
    assert os.readlink(store / "current") == f"versions/{version}"
    assert os.readlink(store / "bin" / "claude") == "../current"
    assert stat.S_IMODE((store / "versions" / version).stat().st_mode) == 0o755


def test_the_claude_store_is_rcmans_and_readable_by_every_workspace():
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


def test_installing_again_leaves_current_on_the_version_rcman_approved():
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


def test_settings_are_saved_privately():
    saved = ETC / "install.env"
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    text = saved.read_text()
    assert f"DOMAIN={DOMAIN}\n" in text and "COOKIE_SECRET=" in text
    assert "CLAUDE_DOWNLOAD=" in text


def test_configuration_files_and_their_owners():
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
    for database in STATE.glob("rcman.db*"):  # oauth2-proxy shares rcman's group
        assert stat.S_IMODE(database.stat().st_mode) == 0o600, database


def test_the_first_administrator_is_there():
    """The address given to the installer, an administrator in rcman, as Caddy passes a request
    on for them."""
    status, me = installed.api("GET", "/api/me")
    assert status == 200 and me["email"] == ADMIN and me["admin"] is True


def test_an_administrator_runs_rcman_admin_by_name_as_root():
    """As the guides say: `sudo rcman-admin`, found on root's path, run as rcman."""
    proc = sh("rcman-admin", "list", check=False)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert ADMIN in proc.stdout


def test_the_socket_directory_admits_only_its_group():
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


def test_the_socket_group_is_kept_to_caddy_alone():
    sh("usermod", "--append", "--groups", "rcman-web", "dev")
    assert "dev" in grp.getgrnam("rcman-web").gr_mem  # the plant took effect
    proc = install()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert grp.getgrnam("rcman-web").gr_mem == ["caddy"]
    assert "dev removed from group rcman-web" in changes(proc)["changed"]


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


def test_no_listener_lets_a_local_user_impersonate():
    """Whatever listens here, a local user cannot use it to be someone in rcman: rcman and
    oauth2-proxy run as their accounts, and every TCP listener and unix socket is tried as dev."""
    spoof = {"X-Forwarded-Email": ADMIN, "X-Auth-Request-Email": ADMIN}
    tcp, unix = listeners()
    assert ("127.0.0.1", 4180) in tcp and installed.SOCKET in unix  # non-vacuity: both found
    for host, port in sorted(tcp):
        url = f"http://{'[' + host + ']' if ':' in host else host}:{port}/api/me"
        out = sh(
            "runuser", "-u", "dev", "--", sys.executable, "-c", PROBE, url, json.dumps(spoof)
        ).stdout
        assert '"admin":true' not in out and f'"email":"{ADMIN}"' not in out, (host, port, out)
    for path in sorted(unix):
        _, text = app("/api/me", spoof, user="dev", path=path)
        assert '"admin":true' not in text and f'"email":"{ADMIN}"' not in text, (path, text)
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
    assert status == 200 and json.loads(text)["email"] == ADMIN and '"admin":true' in text


def test_the_installer_warns_about_a_caddy_admin_api_on_loopback():
    """Planted twin: a Caddy whose admin API is on loopback, as by default, is warned about."""
    first_install = installed.first_install()
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


def test_the_caddy_snippet_is_printed_and_valid():
    first_install = installed.first_install()
    assert "Add this to your Caddyfile" in first_install.stdout
    assert "The Caddy site block changed" in first_install.stdout
    snippet = (ETC / "Caddyfile.snippet").read_text()
    assert snippet in first_install.stdout
    path = Path("/tmp/Caddyfile")
    path.write_text(snippet)
    check = sh("caddy", "validate", "--adapter", "caddyfile", "--config", str(path), check=False)
    assert check.returncode == 0, check.stdout + check.stderr


UPGRADE_UNITS = [ins.SYSTEMD / unit for unit in ins.UPGRADE_UNITS]
UPGRADE_STATUS = ins.UPGRADE_DIR / "status.json"


def upgrade_unit() -> dict[str, list[str]]:
    """The upgrade service's keys and their values, as the installer wrote it."""
    found: dict[str, list[str]] = {}
    for line in (ins.SYSTEMD / "rcman-upgrade.service").read_text().splitlines():
        if "=" in line and not line.lstrip().startswith(("#", "[")):
            key, _, value = line.partition("=")
            found.setdefault(key.strip(), []).append(value.strip())
    return found


def run_upgrade_unit(key: str, **environ: str) -> subprocess.CompletedProcess:
    """The upgrade service's ``ExecStart`` or ``ExecStopPost``, as root, as systemd runs it:
    with the unit's environment alone, no terminal and no SUDO_USER, and for ``ExecStopPost``
    the result systemd gives it."""
    unit = upgrade_unit()
    env = dict(item.split("=", 1) for value in unit["Environment"] for item in shlex.split(value))
    return subprocess.run(
        shlex.split(unit[key][0]), env={**env, **environ}, stdin=subprocess.DEVNULL, cwd="/",
        capture_output=True, text=True, errors="replace", timeout=2400, start_new_session=True,
    )  # fmt: skip


def upgrade_status() -> dict:
    found = UPGRADE_STATUS.stat()
    assert (found.st_uid, grp.getgrgid(found.st_gid).gr_name) == (0, "rcman")
    assert stat.S_IMODE(found.st_mode) == 0o640
    return json.loads(UPGRADE_STATUS.read_text())


def test_with_web_upgrade_off_no_upgrade_unit_exists():
    assert (ins.SYSTEMD / "rcman.service").is_file()  # known positive: the units are written
    assert "WEB_UPGRADE=no\n" in (ETC / "install.env").read_text()
    for path in UPGRADE_UNITS:
        assert not path.exists(), path
    assert not ins.UPGRADE_WRAPPER.exists()
    assert "RCMAN_WEB_UPGRADE=false\n" in (ETC / "rcman.env").read_text()
    calls = (installed.STAND_INS / "calls").read_text()
    assert "rcman-upgrade" not in calls and "rcman.service" in calls


def test_web_upgrade_turned_on_enables_its_path_unit_and_installs_the_wrapper():
    proc = install(env={"RCMAN_INSTALL_WEB_UPGRADE": "yes"})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    for path in UPGRADE_UNITS:
        found = path.stat()
        assert found.st_uid == 0 and stat.S_IMODE(found.st_mode) == 0o644, path
    assert installed.stood_in("systemctl", ["enable", "--now", "rcman-upgrade.path"])
    for line in (installed.STAND_INS / "calls").read_text().splitlines():
        call = json.loads(line)
        if call["program"] == "systemctl" and "rcman-upgrade.service" in call["args"]:
            assert not {"start", "restart"} & set(call["args"]), call  # never started by it
    wrapper = ins.UPGRADE_WRAPPER.stat()
    assert wrapper.st_uid == 0 and stat.S_IMODE(wrapper.st_mode) == 0o755
    assert ins.UPGRADE_WRAPPER.read_bytes() == (CLONE / "installer" / "upgrade.py").read_bytes()
    folder = ins.UPGRADE_DIR.stat()
    assert (folder.st_uid, grp.getgrgid(folder.st_gid).gr_name) == (0, "rcman")
    assert stat.S_IMODE(folder.st_mode) == 0o750
    assert "WEB_UPGRADE=yes\n" in (ETC / "install.env").read_text()
    assert "RCMAN_WEB_UPGRADE=true\n" in (ETC / "rcman.env").read_text()
    assert stat.S_IMODE((ROOT / "current" / ".complete").stat().st_mode) == 0o644
    healthy()


def test_a_wrapper_run_with_no_sudo_user_writes_the_status():
    commit = git("rev-parse", "HEAD", cwd=CLONE)
    proc = run_upgrade_unit("ExecStart")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    found = upgrade_status()
    assert found["state"] == "succeeded", found
    assert found["from"] == commit and found["to"] == commit
    assert found["boot_id"] == Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    assert found["started"] <= found["finished"] and found["error"] is None
    assert isinstance(found["notices"], list)
    done = run_upgrade_unit("ExecStopPost", SERVICE_RESULT="success", EXIT_STATUS="0")
    assert done.returncode == 0, done.stderr
    assert upgrade_status() == found  # a run that ended by itself is left as it wrote it
    healthy()


def test_a_dirty_clone_gives_a_failed_status():
    readme = CLONE / "README.md"
    original = readme.read_text()
    readme.write_text(original + "local change\n")
    try:
        proc = run_upgrade_unit("ExecStart")
        assert proc.returncode == 1, proc.stdout + proc.stderr
        found = upgrade_status()
        assert found["state"] == "failed" and "uncommitted" in found["error"], found
        assert "\n" not in found["error"]  # the last error line only
        done = run_upgrade_unit("ExecStopPost", SERVICE_RESULT="exit-code", EXIT_STATUS="1")
        assert done.returncode == 0, done.stderr
        assert upgrade_status() == found
    finally:
        readme.write_text(original)
    healthy()


def test_running_again_changes_nothing():
    release = os.readlink(ROOT / "current")
    proc = install()
    assert proc.returncode == 0, proc.stderr
    assert changes(proc)["changed"] == []
    assert "The Caddy site block changed" not in proc.stdout
    assert "restart Caddy" not in proc.stdout
    assert os.readlink(ROOT / "current") == release
    assert app("/api/health")[0] == 200


def test_saved_settings_need_no_answers():
    proc = install(env={k: "" for k in SETTINGS})
    assert proc.returncode == 0, proc.stderr


def test_a_dirty_clone_is_refused():
    readme = CLONE / "README.md"
    original = readme.read_text()
    readme.write_text(original + "local change\n")
    try:
        proc = install()
        assert proc.returncode == 1 and "uncommitted" in proc.stderr
    finally:
        readme.write_text(original)


def test_an_update_of_install_sh_restarts_it_and_builds_a_new_release():
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


def test_rollback_returns_to_the_previous_release():
    current, previous = os.readlink(ROOT / "current"), os.readlink(ROOT / "previous")
    proc = subprocess.run(
        [str(CLONE / "install.sh"), "--rollback"], capture_output=True, text=True, errors="replace"
    )
    assert proc.returncode == 0, proc.stderr
    assert os.readlink(ROOT / "current") == previous
    assert os.readlink(ROOT / "previous") == current
    command = Path("/usr/local/sbin/rcman-admin").resolve()
    assert command == (ROOT / previous / ".venv" / "bin" / "rcman-admin").resolve()
    assert sh("rcman-admin", "list", check=False).returncode == 0
    healthy()


def test_a_rollback_to_a_release_older_than_the_database_is_refused():
    """The database is made newer than the release rolled back to reads; the rollback says so
    and switches nothing. The version is put back, so that rcman starts again."""
    from rcman import db

    database = ins.DATABASE
    ask = ["python3", "-I", "-c"]
    script = "import sqlite3,sys;c=sqlite3.connect(sys.argv[1]);{}"

    def version() -> int:
        read = script.format("print(c.execute('pragma user_version').fetchone()[0])")
        return int(sh(*ask, read, str(database), user="rcman").stdout)

    def set_version(number: int) -> None:
        write = script.format(f"c.execute('pragma user_version = {number}');c.commit()")
        sh(*ask, write, str(database), user="rcman")

    have = version()
    assert have == len(db.MIGRATIONS)
    current, previous = os.readlink(ROOT / "current"), os.readlink(ROOT / "previous")
    set_version(have + 1)
    try:
        proc = subprocess.run(
            [str(CLONE / "install.sh"), "--rollback"],
            capture_output=True,
            text=True,
            errors="replace",
        )
        assert proc.returncode == 1 and "schema version" in proc.stderr, proc.stderr
        assert (os.readlink(ROOT / "current"), os.readlink(ROOT / "previous")) == (
            current,
            previous,
        )
    finally:
        set_version(have)
    assert version() == have


def test_a_clone_that_cannot_fast_forward_is_refused():
    commit_in_origin("README.md", "upstream change\n", "upstream")
    readme = CLONE / "README.md"
    readme.write_text(readme.read_text() + "local commit\n")
    git("commit", "-qam", "local", cwd=CLONE)
    proc = install()
    assert proc.returncode == 1 and "fast-forward" in proc.stderr


def main(name: str) -> int:
    check = globals()[name]
    try:
        if name != "first":
            installed.first_install()  # the settings it was given, for an installation again
        check()
    except Exception:  # noqa: BLE001 - told to the test that ran it
        traceback.print_exc()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
