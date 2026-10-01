"""Calling rc-helper and rc-admin, and turning their errors into problems.

In production rc-helper runs as the signed-in user through
``sudo -n -u <user>`` and rc-admin as root through ``sudo -n``; the sudoers
rules allow exactly that. Each answers with one JSON object.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

from rcman.identity import Identity
from rcman.problems import MESSAGES, Problem

HELPER_TIMEOUT = 120
ADMIN_TIMEOUT = 600

HELPER_STATUS = {
    "usage": 500,
    "internal_error": 500,
    "refused_as_root": 500,
    "path_invalid": 400,
    "path_is_home": 400,
    "path_outside_home": 400,
    "path_not_found": 404,
    "path_not_directory": 400,
    "depth_invalid": 400,
    "prefix_invalid": 400,
    "email_invalid": 400,
    "claude_not_installed": 409,
    "claude_not_logged_in": 409,
    "claude_unrecognised_output": 502,
    "already_running": 409,
    "not_running": 409,
    "not_stopped": 409,
    "still_running": 409,
    "unknown_server": 404,
    "nothing_to_confirm": 409,
    "confirm_not_accepted": 409,
    "timeout": 504,
    "no_login_in_progress": 409,
    "login_failed": 409,
}
ADMIN_STATUS = {
    "usage": 500,
    "internal_error": 500,
    "not_root": 500,
    "linux_invalid": 400,
    "email_invalid": 400,
    "email_taken": 409,
    "linux_taken": 409,
    "user_exists": 409,
    "system_user": 409,
    "not_managed": 404,
    "cannot_delete_self": 409,
    "last_admin": 409,
    "home_decision_required": 400,
    "processes_remain": 500,
    "command_failed": 500,
    "claude_install_failed": 502,
    "users_file_invalid": 500,
}

HelperRunner = Callable[..., tuple[int, dict]]
AdminRunner = Callable[[list[str]], tuple[int, dict]]


def _parse(proc: subprocess.CompletedProcess) -> tuple[int, dict]:
    try:
        return proc.returncode, json.loads(proc.stdout)
    except json.JSONDecodeError:
        error = {"code": "helper_unavailable", "message": MESSAGES["helper_unavailable"]}
        return proc.returncode or 1, {"error": error}


def sudo_helper(helpers_dir: Path, runner=subprocess.run) -> HelperRunner:
    def run(linux: str, args: list[str], stdin: str | None = None) -> tuple[int, dict]:
        argv = ["sudo", "-n", "-u", linux, str(helpers_dir / "rc-helper"), *args]
        return _parse(
            runner(argv, input=stdin, capture_output=True, text=True, timeout=HELPER_TIMEOUT)
        )

    return run


def sudo_admin(helpers_dir: Path, runner=subprocess.run) -> AdminRunner:
    def run(args: list[str]) -> tuple[int, dict]:
        argv = ["sudo", "-n", str(helpers_dir / "rc-admin"), *args]
        return _parse(runner(argv, capture_output=True, text=True, timeout=ADMIN_TIMEOUT))

    return run


def _raise(out: dict, statuses: dict[str, int]) -> None:
    error = out.get("error") or {}
    code = error.get("code", "helper_unavailable")
    status = statuses.get(code) or (502 if code == "helper_unavailable" else 500)
    extra = {k: v for k, v in error.items() if k not in ("code", "message")}
    raise Problem(code, status, error.get("message") or MESSAGES["internal_error"], **extra)


def call_helper(
    run: HelperRunner, identity: Identity, args: list[str], stdin: str | None = None
) -> dict:
    code, out = run(identity.linux, args, stdin)
    if code != 0:
        _raise(out, HELPER_STATUS)
    return out


def call_admin(run: AdminRunner, args: list[str]) -> dict:
    code, out = run(args)
    if code != 0:
        _raise(out, ADMIN_STATUS)
    return out
