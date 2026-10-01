"""Install or upgrade rcman on this machine. Run through ``install.sh``, as root.

``install.sh`` updates the clone first; this module does everything else:
settings, system users and groups, the toolchain, a release built from the
clone's commit, configuration, sudoers rules, systemd services, the first
administrator, and a health check. Every step leaves things as they are when
they are already right, so running it again changes nothing.

Python standard library only: it runs under the system interpreter.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import pwd
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

ROOT = Path("/opt/rcman")
CURRENT = ROOT / "current"
TOOLCHAIN = ROOT / "toolchain"
ETC = Path("/etc/rcman")
STATE = Path("/var/lib/rcman")
SETTINGS_FILE = ETC / "install.env"
SYSTEMD = Path("/etc/systemd/system")
SUDOERS = Path("/etc/sudoers.d/rcman")
CADDY_SNIPPET = ETC / "Caddyfile.snippet"

APP_USER = "rcman"
PROXY_USER = "rcman-proxy"
USER_GROUP = "rcusers"
COMPLETE = ".complete"  # marks a release that was fully built
UNITS = ("rcman.service", "rcman-oauth2-proxy.service")
TEMPLATES = (
    "rcman.env",
    "oauth2-proxy.cfg",
    "rcman.service",
    "rcman-oauth2-proxy.service",
    "Caddyfile",
)

UV_VERSION = "0.12.21"
OAUTH2_PROXY_VERSION = "v7.15.5"
HEALTH_TIMEOUT = 60.0

LABEL = r"(?!-)[A-Za-z0-9-]+(?<!-)"  # a host name label: no leading or trailing hyphen
HOSTNAME = re.compile(LABEL + r"(?:\." + LABEL + r")+")
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+")
LINUX_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
PORT = re.compile(r"[1-9][0-9]{1,4}")


class InstallError(Exception):
    pass


# --- settings -----------------------------------------------------------------------

# key: (question, secret, default, pattern). A default of None means it must be given.
FIELDS = {
    "DOMAIN": ("Public host name of rcman (such as rcman.example.com)", False, None, HOSTNAME),
    "GOOGLE_CLIENT_ID": ("Google OAuth client ID", False, None, None),
    "GOOGLE_CLIENT_SECRET": ("Google OAuth client secret", True, None, None),
    "ADMIN_EMAIL": ("Google email address of the first administrator", False, None, EMAIL),
    "ADMIN_LINUX": ("Linux user of the first administrator", False, "", LINUX_NAME),
    "APP_PORT": ("Local port for rcman", False, "8001", PORT),
    "PROXY_PORT": ("Local port for oauth2-proxy", False, "4180", PORT),
}


def load_settings(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip() and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value
    return values


def save_settings(path: Path, values: dict[str, str]) -> bool:
    text = "# Answers install.sh saved; it reads them on every run.\n"
    text += "".join(f"{key}={value}\n" for key, value in sorted(values.items()))
    path.parent.mkdir(parents=True, exist_ok=True)
    return write_if_changed(path, text, 0o600)


def gather(
    saved: Path, environ: dict[str, str], flags: dict[str, str | None], ask, sudo_user: str
) -> dict[str, str]:
    """Settings from flags, then RCMAN_INSTALL_* variables, then the saved file, then questions."""
    values = load_settings(saved)
    for key in (*FIELDS, "COOKIE_SECRET"):
        if environ.get(f"RCMAN_INSTALL_{key}"):
            values[key] = environ[f"RCMAN_INSTALL_{key}"]
        if flags.get(key.lower()):
            values[key] = flags[key.lower()]
    missing = []
    for key, (question, secret, default, _pattern) in FIELDS.items():
        if values.get(key):
            continue
        if default is not None:
            values[key] = default or sudo_user
        elif ask is not None:
            values[key] = ask(key, question, secret)
        else:
            missing.append(key)
    if missing:
        raise InstallError(
            "missing settings (give them as --options or RCMAN_INSTALL_* variables, "
            "or run from a terminal to be asked): " + ", ".join(missing)
        )
    values.setdefault("COOKIE_SECRET", secrets.token_urlsafe(24))
    for key, (_question, _secret, _default, pattern) in FIELDS.items():
        if not values[key] or (pattern and not pattern.fullmatch(values[key])):
            raise InstallError(f"invalid {key}: {values[key]!r}")
    return values


def _ask_terminal(key: str, question: str, secret: bool) -> str:
    import getpass

    return (getpass.getpass if secret else input)(f"{question}: ").strip()


# --- rendering ------------------------------------------------------------------------


def _paths() -> dict[str, str]:
    return {
        "CURRENT": str(CURRENT),
        "ETC": str(ETC),
        "STATE": str(STATE),
        "OAUTH2_PROXY": str(oauth2_proxy_binary()),
    }


def render(name: str, settings: dict[str, str]) -> str:
    text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
    values = {**settings, **_paths()}

    def replace(match: re.Match) -> str:
        return values[match.group(1)]

    return re.sub(r"@([A-Z0-9_]+)@", replace, text)


def sudoers() -> str:
    parts = []
    for template, placeholder, helper in (
        ("sudoers.rc-helper", "@HELPER@", "rc-helper"),
        ("sudoers.rc-admin", "@ADMIN@", "rc-admin"),
    ):
        text = (REPO / "helpers" / template).read_text(encoding="utf-8")
        parts.append(
            text.replace(placeholder, str(CURRENT / "helpers" / helper)).replace(
                "@APP_USER@", APP_USER
            )
        )
    return "# Written by install.sh.\n" + "\n".join(parts)


def write_if_changed(path: Path, text: str, mode: int, gid: int | None = None) -> bool:
    """Write ``text`` atomically unless it is already there; True when something changed."""
    changed = not path.exists() or path.read_text(encoding="utf-8") != text
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    st = path.stat()
    if (st.st_mode & 0o7777) != mode:
        os.chmod(path, mode)
        changed = True
    if gid is not None and st.st_gid != gid:
        os.chown(path, 0, gid)
        changed = True
    return changed


# --- releases ---------------------------------------------------------------------------


def _link(root: Path, name: str) -> str | None:
    path = root / name
    return os.readlink(path) if path.is_symlink() else None


def _set_link(root: Path, name: str, target: str) -> None:
    tmp = root / f".{name}.new"
    tmp.unlink(missing_ok=True)
    os.symlink(target, tmp)
    os.replace(tmp, root / name)


def switch(root: Path, release: str) -> bool:
    """Make ``release`` current and the old current previous; remove the old previous."""
    target = f"releases/{release}"
    if not (root / target / COMPLETE).exists():
        raise InstallError(f"release {release} is incomplete")
    current, old_previous = _link(root, "current"), _link(root, "previous")
    if current == target:
        return False
    if current:
        _set_link(root, "previous", current)
    _set_link(root, "current", target)
    if old_previous and old_previous not in (current, target):
        shutil.rmtree(root / old_previous, ignore_errors=True)  # it fell off the end
    return True


def rollback(root: Path) -> None:
    current, previous = _link(root, "current"), _link(root, "previous")
    if not previous:
        raise InstallError("there is no previous release to roll back to")
    _set_link(root, "current", previous)
    _set_link(root, "previous", current)


# --- system ---------------------------------------------------------------------------


def run(argv: list[str], user: str | None = None, **kw) -> subprocess.CompletedProcess:
    if user:
        argv = ["runuser", "-u", user, "--", *argv]
    kw.setdefault("text", True)
    proc = subprocess.run(argv, capture_output=True, **kw)
    if proc.returncode != 0:
        output = [
            o.decode(errors="replace") if isinstance(o, bytes) else o
            for o in (proc.stdout, proc.stderr)
        ]
        raise InstallError(f"{' '.join(argv)} failed:\n{''.join(output)}")
    return proc


def _exists(kind: str, name: str) -> bool:
    return subprocess.run(["getent", kind, name], capture_output=True).returncode == 0


def ensure_accounts() -> list[str]:
    changes = []
    if not _exists("group", USER_GROUP):
        run(["groupadd", USER_GROUP])
        changes.append(f"group {USER_GROUP}")
    if not _exists("passwd", APP_USER):
        run(
            [
                "useradd",
                "--system",
                "--user-group",
                "--home-dir",
                str(STATE),
                "--shell",
                "/usr/sbin/nologin",
                APP_USER,
            ]
        )
        changes.append(f"user {APP_USER}")
    if not _exists("passwd", PROXY_USER):
        run(
            [
                "useradd",
                "--system",
                "--user-group",
                "--no-create-home",
                "--shell",
                "/usr/sbin/nologin",
                PROXY_USER,
            ]
        )
        changes.append(f"user {PROXY_USER}")
    proxy_groups = run(["id", "-nG", PROXY_USER]).stdout.split()
    if APP_USER not in proxy_groups:  # to read the allowlist, which belongs to group rcman
        run(["usermod", "--append", "--groups", APP_USER, PROXY_USER])
        changes.append(f"{PROXY_USER} in group {APP_USER}")
    app = pwd.getpwnam(APP_USER)
    STATE.mkdir(parents=True, exist_ok=True)
    if STATE.stat().st_uid != app.pw_uid or (STATE.stat().st_mode & 0o777) != 0o750:
        os.chown(STATE, app.pw_uid, app.pw_gid)
        os.chmod(STATE, 0o750)
        changes.append(str(STATE))
    ETC.mkdir(parents=True, exist_ok=True)
    return changes


# --- toolchain ---------------------------------------------------------------------------


def _arch() -> str:
    machine = platform.machine()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("aarch64", "arm64"):
        return "arm64"
    raise InstallError(f"unsupported machine: {machine}")


def _download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=300) as response:
        return response.read()


def _verified(url: str, expected: str) -> bytes:
    data = _download(url)
    actual = hashlib.sha256(data).hexdigest()
    if actual != expected:
        raise InstallError(f"checksum mismatch for {url}")
    return data


def node_version() -> str:
    return (REPO / "frontend" / ".nvmrc").read_text(encoding="utf-8").strip()


def node_bin(toolchain: Path = TOOLCHAIN) -> Path:
    return toolchain / f"node-v{node_version()}" / "bin"


def uv_binary(toolchain: Path = TOOLCHAIN) -> Path:
    return toolchain / f"uv-{UV_VERSION}" / "uv"


def oauth2_proxy_binary(toolchain: Path = TOOLCHAIN) -> Path:
    return toolchain / f"oauth2-proxy-{OAUTH2_PROXY_VERSION}" / "oauth2-proxy"


def _unpack(
    data: bytes, dest: Path, mode: str, strip: int, members: list[str] | None = None
) -> None:
    tmp = Path(tempfile.mkdtemp(dir=dest.parent, prefix=f".{dest.name}."))
    try:
        with tempfile.TemporaryFile() as archive:
            archive.write(data)
            archive.seek(0)
            with tarfile.open(fileobj=archive, mode=mode) as tar:
                for member in tar.getmembers():
                    parts = Path(member.name).parts[strip:]
                    if not parts or (members and parts[-1] not in members):
                        continue
                    member.name = str(Path(*parts))
                    tar.extract(member, tmp, filter="data")
        os.chmod(tmp, 0o755)  # mkdtemp makes it private; the services' users must reach it
        if dest.exists():
            shutil.rmtree(dest)
        tmp.rename(dest)
    finally:
        if tmp.exists():
            shutil.rmtree(tmp)


def open_up(root: Path) -> None:
    """Make every directory under ``root`` reachable by the services' users."""
    for path in [root, *root.rglob("*")]:
        if path.is_dir() and not path.is_symlink() and (path.stat().st_mode & 0o755) != 0o755:
            os.chmod(path, path.stat().st_mode | 0o755)


def ensure_toolchain(toolchain: Path = TOOLCHAIN) -> list[str]:
    """Node.js (to build the frontend), uv and oauth2-proxy, each checksum-verified."""
    toolchain.mkdir(parents=True, exist_ok=True)
    changes = []
    arch = _arch()
    if not (node_bin(toolchain) / "node").exists():
        version = node_version()
        name = f"node-v{version}-linux-{arch}.tar.xz"
        base = f"https://nodejs.org/dist/v{version}"
        sums = _download(f"{base}/SHASUMS256.txt").decode()
        expected = next(line.split()[0] for line in sums.splitlines() if line.endswith(f"  {name}"))
        _unpack(_verified(f"{base}/{name}", expected), node_bin(toolchain).parent, "r:xz", strip=1)
        changes.append(f"node {version}")
    if not uv_binary(toolchain).exists():
        triple = {"x64": "x86_64", "arm64": "aarch64"}[arch] + "-unknown-linux-gnu"
        url = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-{triple}.tar.gz"
        expected = _download(url + ".sha256").decode().split()[0]
        _unpack(
            _verified(url, expected), uv_binary(toolchain).parent, "r:gz", strip=1, members=["uv"]
        )
        changes.append(f"uv {UV_VERSION}")
    if not oauth2_proxy_binary(toolchain).exists():
        goarch = {"x64": "amd64", "arm64": "arm64"}[arch]
        name = f"oauth2-proxy-{OAUTH2_PROXY_VERSION}.linux-{goarch}.tar.gz"
        base = (
            f"https://github.com/oauth2-proxy/oauth2-proxy/releases/download/{OAUTH2_PROXY_VERSION}"
        )
        expected = _download(f"{base}/{name}-sha256sum.txt").decode().split()[0]
        _unpack(
            _verified(f"{base}/{name}", expected),
            oauth2_proxy_binary(toolchain).parent,
            "r:gz",
            strip=1,
            members=["oauth2-proxy"],
        )
        changes.append(f"oauth2-proxy {OAUTH2_PROXY_VERSION}")
    open_up(toolchain)
    return changes


# --- building a release ---------------------------------------------------------------


def build_release(repo: Path, owner: str) -> tuple[str, bool]:
    """The release for the clone's commit, built when it does not exist yet."""
    commit = run(["git", "-C", str(repo), "rev-parse", "HEAD"], user=owner).stdout.strip()
    name = commit[:12]
    release = ROOT / "releases" / name
    if (release / COMPLETE).exists():
        return name, False
    (ROOT / "releases").mkdir(parents=True, exist_ok=True)
    # Built where it will stay: the virtualenv's scripts name their own path.
    # Until .complete is written, it is never switched to and is rebuilt.
    work = release
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    try:
        archive = run(
            ["git", "-C", str(repo), "archive", "--format=tar", "HEAD"], user=owner, text=False
        )
        with tempfile.TemporaryFile() as handle:
            handle.write(archive.stdout)
            handle.seek(0)
            with tarfile.open(fileobj=handle) as tar:
                tar.extractall(work, filter="data")
        path = f"{node_bin()}:/usr/local/bin:/usr/bin:/bin"
        frontend = repo / "frontend"
        env = ["/usr/bin/env", f"PATH={path}", f"HOME={pwd.getpwnam(owner).pw_dir}"]
        run([*env, "npm", "ci", "--no-audit", "--no-fund"], user=owner, cwd=frontend)
        run([*env, "npm", "run", "build"], user=owner, cwd=frontend)
        shutil.copytree(frontend / "dist", work / "frontend" / "dist")
        uv_env = {
            **os.environ,
            "UV_PROJECT_ENVIRONMENT": str(work / ".venv"),
            "UV_PYTHON": "/usr/bin/python3",
        }
        run(
            [
                str(uv_binary()),
                "sync",
                "--frozen",
                "--no-dev",
                "--no-editable",
                "--python",
                "/usr/bin/python3",
            ],
            cwd=work,
            env=uv_env,
        )
        for path_ in [work, *work.rglob("*")]:
            if not path_.is_symlink():
                os.chown(path_, 0, 0)
                os.chmod(
                    path_, (path_.stat().st_mode & 0o755) | (0o755 if path_.is_dir() else 0o644)
                )
        (work / COMPLETE).write_text(commit + "\n")
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    return name, True


# --- configuration, services, first administrator ------------------------------------


def _gid(group: str) -> int:
    return int(run(["getent", "group", group]).stdout.split(":")[2])


def configure(settings: dict[str, str]) -> list[str]:
    changes = []
    app_gid, proxy_gid = _gid(APP_USER), _gid(PROXY_USER)
    files = [
        (ETC / "rcman.env", render("rcman.env", settings), 0o640, app_gid),
        (ETC / "oauth2-proxy.cfg", render("oauth2-proxy.cfg", settings), 0o640, proxy_gid),
        (CADDY_SNIPPET, render("Caddyfile", settings), 0o644, None),
    ]
    for unit in UNITS:
        files.append((SYSTEMD / unit, render(unit, settings), 0o644, None))
    for path, text, mode, gid in files:
        if write_if_changed(path, text, mode, gid):
            changes.append(str(path))
    allowlist = ETC / "allowed-emails"
    if not allowlist.exists():  # rc-admin regenerates it; oauth2-proxy needs it to exist
        write_if_changed(allowlist, "", 0o640, app_gid)
        changes.append(str(allowlist))
    text = sudoers()
    current = SUDOERS.read_text(encoding="utf-8") if SUDOERS.exists() else None
    if current != text:
        with tempfile.NamedTemporaryFile("w", suffix=".sudoers", delete=False) as tmp:
            tmp.write(text)
        try:
            run(["visudo", "-c", "-f", tmp.name])
        finally:
            os.unlink(tmp.name)
        write_if_changed(SUDOERS, text, 0o440)
        changes.append(str(SUDOERS))
    elif (SUDOERS.stat().st_mode & 0o777) != 0o440:
        os.chmod(SUDOERS, 0o440)
        changes.append(str(SUDOERS))
    return changes


def start_services(restart: bool) -> None:
    run(["systemctl", "daemon-reload"])
    for unit in UNITS:
        run(["systemctl", "enable", unit])
        active = subprocess.run(["systemctl", "is-active", "--quiet", unit]).returncode == 0
        if restart or not active:
            run(["systemctl", "restart", unit])


def ensure_admin(settings: dict[str, str]) -> None:
    run(
        [
            str(CURRENT / "helpers" / "rc-admin"),
            "add",
            settings["ADMIN_LINUX"],
            settings["ADMIN_EMAIL"],
            "--admin",
            "--adopt",
        ]
    )


def health(settings: dict[str, str]) -> None:
    checks = {
        "rcman": f"http://127.0.0.1:{settings['APP_PORT']}/api/health",
        "oauth2-proxy": f"http://127.0.0.1:{settings['PROXY_PORT']}/ping",
    }
    deadline = time.monotonic() + HEALTH_TIMEOUT
    pending = dict(checks)
    while pending:
        for name, url in list(pending.items()):
            try:
                with urllib.request.urlopen(url, timeout=5) as response:
                    if response.status == 200:
                        del pending[name]
            except OSError:
                pass
        if pending and time.monotonic() > deadline:
            raise InstallError("not healthy: " + ", ".join(pending))
        if pending:
            time.sleep(1)


# --- command line -----------------------------------------------------------------------


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="install.sh", description="Install or upgrade rcman.")
    for key, (question, _secret, _default, _pattern) in FIELDS.items():
        parser.add_argument("--" + key.lower().replace("_", "-"), dest=key.lower(), help=question)
    parser.add_argument(
        "--rollback", action="store_true", help="switch back to the previous release"
    )
    args = parser.parse_args(argv)
    try:
        if os.geteuid() != 0:
            raise InstallError("run install.sh with sudo")
        if args.rollback:
            rollback(ROOT)
            start_services(restart=True)
            print(f"Rolled back to {os.readlink(CURRENT)}.")
            return 0
        owner = os.environ.get("SUDO_USER") or ""
        if not owner or owner == "root":
            raise InstallError("run install.sh with sudo from the account that owns the clone")
        ask = _ask_terminal if sys.stdin.isatty() else None
        settings = gather(SETTINGS_FILE, dict(os.environ), vars(args), ask, owner)
        changes = []
        if save_settings(SETTINGS_FILE, settings):
            changes.append(str(SETTINGS_FILE))
        changes += ensure_accounts()
        changes += ensure_toolchain()
        release, built = build_release(REPO, owner)
        switched = switch(ROOT, release)
        if built or switched:
            changes.append(f"release {release}")
        changes += configure(settings)
        start_services(restart=bool(changes))
        ensure_admin(settings)
        health(settings)
        print(json.dumps({"release": release, "changed": changes}))
        print(f"\nAdd this to your Caddyfile ({CADDY_SNIPPET}):\n")
        print(CADDY_SNIPPET.read_text(encoding="utf-8"))
        return 0
    except InstallError as error:
        print(f"install.sh: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
