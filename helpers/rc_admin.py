"""rc-admin: add, remove and list the people who use rcman. Runs as root.

Two front ends share this module: ``rc-admin`` (JSON, never prompts; what the
admin page runs through sudo) and ``rcman-user`` (for an administrator over
SSH; see ``helpers/rcman_user.py``). Every change to the system goes through a
:class:`RealSystem`, so the decisions can be tested without root.

Two environment variables are test hooks — ``RC_ADMIN_ETC`` and
``RC_ADMIN_INSTALLER`` — and the sudoers rule resets the environment, so they
never reach rc-admin started through sudo.

Python standard library and the helpers' own modules only: it runs under the
system interpreter.
"""

from __future__ import annotations

import argparse
import grp
import json
import os
import pwd
import re
import shutil
import subprocess
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from helpers import claude_checks
from helpers import users_file as uf
from helpers.rules import Rule

VERBS = ("add", "del", "list", "claude-status", "claude-upgrade")
MIN_UID = 1000
CLAUDE_INSTALLER_URL = "https://claude.ai/install.sh"
VERSION_FILE = Path(__file__).resolve().parent / "claude-version"
VERSION = Rule(re.compile(r"[0-9]+\.[0-9]+\.[0-9]+"), "A Claude Code version looks like 1.2.3.")
PROCESS_EXIT_TIMEOUT = 10.0

# The rules rc-admin refuses a value by, each with the code it refuses it with;
# MESSAGES gives each code its rule's own sentence.
RULE_CODES = {
    "linux_invalid": uf.LINUX_NAME,
    "email_invalid": uf.EMAIL,
    "version_invalid": VERSION,
}
MESSAGES = {
    "usage": "The command line was not understood.",
    "internal_error": "rc-admin failed unexpectedly.",
    "not_root": "rc-admin must run as root.",
    **{code: rule.sentence for code, rule in RULE_CODES.items()},
    "email_taken": "That email address already belongs to another user.",
    "linux_taken": "That Linux user already belongs to another email address.",
    "user_exists": (
        "That Linux account already exists and is not managed by rcman; "
        "pass --adopt to take it over."
    ),
    "system_user": "That is a system account; rcman does not manage it.",
    "not_managed": "That user is not managed by rcman.",
    "cannot_delete_self": "You cannot delete yourself.",
    "last_admin": "That is the last administrator.",
    "home_decision_required": "Say whether to delete the home folder: --delete or --keep-home.",
    "processes_remain": "The user's processes did not stop.",
    "command_failed": "A system command failed.",
    "claude_install_failed": "Installing Claude Code failed.",
    "users_file_invalid": "The users file is invalid.",
    "claude_checks_failed": "That Claude Code version failed rcman's checks; nothing was changed.",
}


class AdminError(Exception):
    def __init__(self, code: str, detail: dict | None = None):
        super().__init__(code)
        self.code = code
        self.detail = detail or {}


@dataclass(frozen=True)
class Config:
    etc: Path
    user_group: str
    app_group: str
    claude_version: str  # the version rcman's recorded tests cover
    installer: Path | None  # None: the official Claude Code installer
    app_user: str = "rcman"
    canary_home: Path = Path("/var/lib/rcman/canary")  # where candidate versions are checked

    @property
    def users_file(self) -> Path:
        return self.etc / "users.toml"

    @property
    def allowlist(self) -> Path:
        return self.etc / "allowed-emails"

    @property
    def approved_file(self) -> Path:
        return self.etc / "claude-version"


def approved_claude_version() -> str:
    """The version rcman's recorded tests cover, shipped in helpers/claude-version."""
    return VERSION_FILE.read_text(encoding="utf-8").strip()


def config_from_env(env: dict[str, str]) -> Config:
    installer = env.get("RC_ADMIN_INSTALLER")
    return Config(
        etc=Path(env.get("RC_ADMIN_ETC") or "/etc/rcman"),
        user_group="rcusers",
        app_group="rcman",
        claude_version=approved_claude_version(),
        installer=Path(installer) if installer else None,
        canary_home=Path(env.get("RC_ADMIN_CANARY") or "/var/lib/rcman/canary"),
    )


def approved_version(cfg: Config) -> str:
    """The version rolled out: the last approved upgrade, else the tested version."""
    try:
        text = cfg.approved_file.read_text(encoding="utf-8").strip()
    except OSError:
        return cfg.claude_version
    return text if VERSION.fullmatch(text) else cfg.claude_version


@dataclass(frozen=True)
class UserInfo:
    name: str
    uid: int
    gid: int
    home: Path
    groups: list[str]


def user_argv(info: UserInfo, argv: list[str]) -> list[str]:
    """Run ``argv`` as the user, with only the variables listed here."""
    env = {
        "HOME": str(info.home),
        "USER": info.name,
        "LOGNAME": info.name,
        "PATH": f"{info.home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "DISABLE_AUTOUPDATER": "1",
    }
    return [
        "runuser",
        "-u",
        info.name,
        "--",
        "/usr/bin/env",
        "-i",
        *(f"{k}={v}" for k, v in env.items()),
        *argv,
    ]


class RealSystem:
    def __init__(self, installer: Path | None):
        self.installer = installer

    def user(self, name: str) -> UserInfo | None:
        try:
            entry = pwd.getpwnam(name)
        except KeyError:
            return None
        groups = [g.gr_name for g in grp.getgrall() if name in g.gr_mem]
        try:
            groups.append(grp.getgrgid(entry.pw_gid).gr_name)
        except KeyError:
            pass
        return UserInfo(name, entry.pw_uid, entry.pw_gid, Path(entry.pw_dir), sorted(set(groups)))

    def group_gid(self, name: str) -> int | None:
        try:
            return grp.getgrnam(name).gr_gid
        except KeyError:
            return None

    def run(self, argv: list[str], check: bool = True) -> int:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            if check:
                raise AdminError(
                    "command_failed", {"command": argv[0], "stderr": "not found"}
                ) from None
            return 127
        if check and proc.returncode != 0:
            raise AdminError("command_failed", {"command": argv[0], "stderr": proc.stderr.strip()})
        return proc.returncode

    def as_user(self, info: UserInfo, argv: list[str], check: bool = True) -> tuple[int, str]:
        proc = subprocess.run(
            user_argv(info, argv),
            cwd=info.home,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
        if check and proc.returncode != 0:
            raise AdminError("command_failed", {"command": argv[0], "stderr": proc.stderr.strip()})
        return proc.returncode, proc.stdout

    def processes_of(self, info: UserInfo) -> list[int]:
        """The user's live processes; zombies hold nothing and are left to be reaped."""
        proc = subprocess.run(
            ["ps", "-o", "pid=,stat=", "-u", str(info.uid)], capture_output=True, text=True
        )
        rows = (line.split() for line in proc.stdout.splitlines())
        return [int(pid) for pid, stat in rows if not stat.startswith("Z")]

    def chown(self, path: Path, uid: int, gid: int) -> None:
        os.chown(path, uid, gid)

    def check_candidate(self, cfg: Config, version: str) -> list[str]:
        """Install ``version`` in the canary home, as the app user, and run the checks."""
        app = pwd.getpwnam(cfg.app_user)
        home = cfg.canary_home
        if home.exists():
            shutil.rmtree(home)
        home.mkdir(parents=True, mode=0o700)
        os.chown(home, app.pw_uid, app.pw_gid)
        canary = UserInfo(cfg.app_user, app.pw_uid, app.pw_gid, home, [])
        try:
            self.install_claude(canary, version)
        except AdminError:
            return ["install"]
        env = {
            "HOME": str(home),
            "PATH": f"{home}/.local/bin:/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "TERM": "xterm-256color",
            "DISABLE_AUTOUPDATER": "1",
            "CLAUDE_CONFIG_DIR": str(home / "check-config"),
        }
        claude = str(_claude(canary))

        def run(args, until=None, timeout=claude_checks.TIMEOUT):
            argv = ["runuser", "-u", cfg.app_user, "--", "/usr/bin/env", "-i"]
            argv += [f"{k}={v}" for k, v in env.items()] + [claude, *args]
            return claude_checks.run_in_pty(
                argv, {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"}, home, until, timeout
            )

        return claude_checks.candidate_checks(run, version)

    def install_claude(self, info: UserInfo, version: str) -> None:
        if self.installer is not None:
            code, _ = self.as_user(info, [str(self.installer), version], check=False)
        else:
            with tempfile.NamedTemporaryFile("wb", suffix=".sh", delete=False) as script:
                with urllib.request.urlopen(CLAUDE_INSTALLER_URL, timeout=60) as response:
                    script.write(response.read())
            try:
                os.chmod(script.name, 0o644)
                code, _ = self.as_user(info, ["/bin/bash", script.name, version], check=False)
            finally:
                os.unlink(script.name)
        if code != 0:
            raise AdminError("claude_install_failed")


# --- operations -------------------------------------------------------------------


def _load(cfg: Config) -> list[uf.User]:
    try:
        return uf.load(cfg.users_file)
    except uf.UsersFileError as error:
        raise AdminError("users_file_invalid", {"detail": str(error)}) from None


def _save(cfg: Config, system, users: list[uf.User]) -> None:
    gid = system.group_gid(cfg.app_group)
    uf.save(cfg.users_file, users, gid=gid)
    uf.write_atomic(cfg.allowlist, uf.allowlist(users), gid=gid)


def _claude(info: UserInfo) -> Path:
    return info.home / ".local" / "bin" / "claude"


def _installed_version(system, info: UserInfo) -> str | None:
    code, out = system.as_user(info, [str(_claude(info)), "--version"], check=False)
    match = VERSION.regex.search(out) if code == 0 else None
    return match.group(0) if match else None


def _seed(system, info: UserInfo) -> None:
    claude_dir = info.home / ".claude"
    claude_dir.mkdir(exist_ok=True)
    settings_path = claude_dir / "settings.json"
    settings = (
        json.loads(settings_path.read_text(encoding="utf-8")) if settings_path.exists() else {}
    )
    settings.setdefault("env", {})["DISABLE_AUTOUPDATER"] = "1"
    settings_path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8")
    projects = info.home / "projects"
    projects.mkdir(exist_ok=True)
    for path in (claude_dir, settings_path, projects):
        system.chown(path, info.uid, info.gid)


def add(cfg: Config, system, linux: str, email: str, admin: bool, adopt: bool = False) -> dict:
    if not uf.valid_linux(linux):
        raise AdminError("linux_invalid")
    if not uf.valid_email(email):
        raise AdminError("email_invalid")
    email = email.lower()
    users = _load(cfg)
    owner_of_email, entry = uf.by_email(users, email), uf.by_linux(users, linux)
    if owner_of_email and owner_of_email.linux != linux:
        raise AdminError("email_taken")
    if entry and entry.email != email:
        raise AdminError("linux_taken")

    info = system.user(linux)
    created = info is None
    if info is None:
        system.run(
            [
                "useradd",
                "--create-home",
                "--user-group",
                "--groups",
                cfg.user_group,
                "--shell",
                "/bin/bash",
                linux,
            ]
        )
        info = system.user(linux)
    else:
        if info.uid < MIN_UID:
            raise AdminError("system_user")
        if entry is None and cfg.user_group not in info.groups and not adopt:
            raise AdminError("user_exists")
        if cfg.user_group not in info.groups:
            system.run(["usermod", "--append", "--groups", cfg.user_group, linux])
    os.chmod(info.home, 0o750)

    version = approved_version(cfg)
    if _installed_version(system, info) != version:
        system.install_claude(info, version)
    _seed(system, info)

    users = [u for u in users if u.linux != linux] + [
        uf.User(email=email, linux=linux, admin=admin)
    ]
    _save(cfg, system, users)
    return {
        "user": {"linux": linux, "email": email, "admin": admin},
        "created": created,
        "claude_version": version,
    }


def delete(
    cfg: Config, system, linux: str, delete_home: bool | None, requester: str | None
) -> dict:
    if not uf.valid_linux(linux):
        raise AdminError("linux_invalid")
    if delete_home is None:
        raise AdminError("home_decision_required")
    users = _load(cfg)
    entry = uf.by_linux(users, linux)
    info = system.user(linux)
    if info is not None and info.uid < MIN_UID:
        raise AdminError("system_user")
    if entry is None:
        raise AdminError("not_managed")
    if requester == linux:
        raise AdminError("cannot_delete_self")
    if entry.admin and sum(u.admin for u in users) == 1:
        raise AdminError("last_admin")
    remaining = [u for u in users if u.linux != linux]
    if info is None:
        _save(cfg, system, remaining)
        return {"deleted": linux, "home_deleted": False, "account_missing": True}
    if cfg.user_group not in info.groups:
        raise AdminError("not_managed")

    system.as_user(info, ["tmux", "-L", "rcman", "kill-server"], check=False)
    system.as_user(info, [str(_claude(info)), "auth", "logout"], check=False)
    system.run(["pkill", "--signal", "KILL", "--uid", str(info.uid)], check=False)
    deadline = time.monotonic() + PROCESS_EXIT_TIMEOUT
    while system.processes_of(info):
        if time.monotonic() > deadline:
            raise AdminError("processes_remain")
        time.sleep(0.1)
    system.run(["userdel", "--remove", linux] if delete_home else ["userdel", linux])
    _save(cfg, system, remaining)
    return {"deleted": linux, "home_deleted": delete_home}


def list_users(cfg: Config, system) -> dict:
    out = []
    for user in _load(cfg):
        info = system.user(user.linux)
        managed = info is not None and cfg.user_group in info.groups
        out.append(
            {
                "linux": user.linux,
                "email": user.email,
                "admin": user.admin,
                "account": managed,
                "home": str(info.home) if info else None,
                "claude_installed": managed and _claude(info).exists(),
            }
        )
    return {"users": out}


def claude_status(cfg: Config, system) -> dict:
    users = []
    for user in _load(cfg):
        info = system.user(user.linux)
        installed = _installed_version(system, info) if info else None
        users.append({"linux": user.linux, "installed": installed})
    return {"approved": approved_version(cfg), "tested": cfg.claude_version, "users": users}


def upgrade(cfg: Config, system, version: str) -> dict:
    """Check ``version``; only if it passes, approve it and install it for everyone."""
    if not VERSION.fullmatch(version):
        raise AdminError("version_invalid")
    failed = system.check_candidate(cfg, version)
    if failed:
        raise AdminError("claude_checks_failed", {"failed": failed})
    uf.write_atomic(cfg.approved_file, version + "\n", mode=0o644)
    results = {}
    for user in _load(cfg):
        info = system.user(user.linux)
        if info is None or cfg.user_group not in info.groups:
            results[user.linux] = "no_account"
        elif _installed_version(system, info) == version:
            results[user.linux] = "already"
        else:
            try:
                system.install_claude(info, version)
                results[user.linux] = "installed"
            except AdminError:
                results[user.linux] = "failed"
    return {"approved": version, "tested": cfg.claude_version, "users": results}


def home_of(system, linux: str) -> Path | None:
    info = system.user(linux)
    return info.home if info else None


# --- JSON command line -------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise AdminError("usage", {"detail": message})


def parser(prog: str, parser_class=_Parser, requester: bool = False) -> argparse.ArgumentParser:
    p = parser_class(prog=prog, description="Manage the people who use rcman.")
    verbs = p.add_subparsers(dest="verb", required=True, parser_class=parser_class)
    add_p = verbs.add_parser("add", help="add a person, or repair one already added")
    add_p.add_argument("linux_user")
    add_p.add_argument("email")
    add_p.add_argument("--admin", action="store_true", help="make them an administrator")
    add_p.add_argument("--adopt", action="store_true", help="take over an existing Linux account")
    del_p = verbs.add_parser("del", help="remove a person")
    del_p.add_argument("linux_user")
    home = del_p.add_mutually_exclusive_group()
    home.add_argument("-d", "--delete", action="store_true", help="also delete their home folder")
    home.add_argument("--keep-home", action="store_true", help="keep their home folder")
    if requester:
        del_p.add_argument("--requester", help="email of the administrator asking")
    verbs.add_parser("list", help="everyone and their account state")
    verbs.add_parser(
        "claude-status", help="the approved Claude Code version and everyone's installed one"
    )
    up = verbs.add_parser("claude-upgrade", help="check a Claude Code version, then roll it out")
    up.add_argument("version")
    return p


def home_decision(args: argparse.Namespace) -> bool | None:
    if args.delete:
        return True
    if args.keep_home:
        return False
    return None


def main_json(argv: list[str], cfg: Config | None = None, system=None) -> int:
    try:
        args = parser("rc-admin", requester=True).parse_args(argv)
        if cfg is None:
            if os.geteuid() != 0:
                raise AdminError("not_root")
            cfg = config_from_env(dict(os.environ))
            system = RealSystem(cfg.installer)
        if args.verb == "add":
            result = add(cfg, system, args.linux_user, args.email, args.admin, args.adopt)
        elif args.verb == "del":
            requester = uf.by_email(_load(cfg), args.requester) if args.requester else None
            result = delete(
                cfg,
                system,
                args.linux_user,
                home_decision(args),
                requester.linux if requester else None,
            )
        elif args.verb == "claude-status":
            result = claude_status(cfg, system)
        elif args.verb == "claude-upgrade":
            result = upgrade(cfg, system, args.version)
        else:
            result = list_users(cfg, system)
        print(json.dumps(result))
        return 0
    except AdminError as error:
        print(
            json.dumps(
                {"error": {"code": error.code, "message": MESSAGES[error.code], **error.detail}}
            )
        )
        return 2 if error.code == "usage" else 1
    except Exception:  # noqa: BLE001 - the contract is JSON on stdout, always
        print(
            json.dumps({"error": {"code": "internal_error", "message": MESSAGES["internal_error"]}})
        )
        return 1
