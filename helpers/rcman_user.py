"""rcman-user: add, remove and list rcman users from a terminal. Runs as root.

The same operations as ``rc-admin`` (``helpers/rc_admin.py``), with output in
words. Deleting without ``-d``/``--delete`` or ``--keep-home`` asks whether to
delete the home folder, defaulting to no, and refuses when there is no
terminal to ask on. The administrator running it through sudo (``SUDO_USER``)
cannot delete themselves.
"""

from __future__ import annotations

import argparse
import os
import sys

from helpers import rc_admin as ra


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        self.print_usage(sys.stderr)
        raise SystemExit(2)


def _size(path) -> str:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                pass
    for unit in ("B", "KB", "MB", "GB"):
        if total < 1024 or unit == "GB":
            return f"{total:.0f} {unit}" if unit == "B" else f"{total:.1f} {unit}"
        total /= 1024
    return ""


def main(
    argv, cfg=None, system=None, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr, env=None
) -> int:
    env = dict(os.environ) if env is None else env
    try:
        args = ra.parser("rcman-user", parser_class=_Parser).parse_args(argv)
    except SystemExit as exit_:
        return int(exit_.code or 0)
    try:
        if cfg is None:
            if os.geteuid() != 0:
                raise ra.AdminError("not_root")
            cfg = ra.config_from_env(env)
            system = ra.RealSystem(cfg.installer)
        if args.verb == "add":
            result = ra.add(cfg, system, args.linux_user, args.email, args.admin, args.adopt)
            user = result["user"]
            role = "administrator" if user["admin"] else "user"
            verb = "Added" if result["created"] else "Updated"
            print(f"{verb} {user['linux']} ({user['email']}) as {role}.", file=stdout)
        elif args.verb == "del":
            delete_home = ra.home_decision(args)
            if delete_home is None and stdin.isatty():
                home = ra.home_of(system, args.linux_user)
                size = f" ({_size(home)})" if home and home.exists() else ""
                print(
                    f"Delete home directory {home}{size}? [y/N] ", end="", file=stdout, flush=True
                )
                delete_home = stdin.readline().strip().lower() in ("y", "yes")
            result = ra.delete(cfg, system, args.linux_user, delete_home, env.get("SUDO_USER"))
            kept = "deleted" if result["home_deleted"] else "kept"
            print(f"Removed {result['deleted']}; home folder {kept}.", file=stdout)
        elif args.verb == "claude-status":
            status = ra.claude_status(cfg, system)
            print(
                f"Claude Code: approved {status['approved']}, tested {status['tested']}",
                file=stdout,
            )
            for u in status["users"]:
                installed = u["installed"] or "not installed"
                note = "" if u["installed"] == status["approved"] else "  (differs)"
                print(f"  {u['linux']:<16} {installed}{note}", file=stdout)
        elif args.verb == "claude-upgrade":
            result = ra.upgrade(cfg, system, args.version)
            print(f"Approved Claude Code {result['approved']}.", file=stdout)
            if result["approved"] != result["tested"]:
                key = [
                    tuple(int(n) for n in v.split("."))
                    for v in (result["approved"], result["tested"])
                ]
                relation = "newer" if key[0] > key[1] else "older"
                print(
                    f"Note: {result['approved']} is {relation} than the tested {result['tested']}; "
                    "screens that need a signed-in account were not checked.",
                    file=stdout,
                )
            for linux, outcome in sorted(result["users"].items()):
                print(f"  {linux}: {outcome}", file=stdout)
        else:
            users = ra.list_users(cfg, system)["users"]
            print(f"{'LINUX':<16} {'EMAIL':<40} {'ROLE':<6} ACCOUNT", file=stdout)
            for u in users:
                role = "admin" if u["admin"] else "user"
                account = "ok" if u["account"] else "missing"
                print(f"{u['linux']:<16} {u['email']:<40} {role:<6} {account}", file=stdout)
        return 0
    except ra.AdminError as error:
        detail = error.detail.get("detail") or error.detail.get("stderr")
        if "failed" in error.detail:
            detail = "failed: " + ", ".join(error.detail["failed"])
        print(ra.MESSAGES[error.code] + (f" ({detail})" if detail else ""), file=stderr)
        for name, entry in error.detail.get("report", {}).items():
            print(f"{name}:", file=stderr)
            for line in entry["lines"]:
                print(f"    {line}", file=stderr)
            if entry["more"]:
                print(f"    ({entry['more']} more lines not shown)", file=stderr)
        return 1
