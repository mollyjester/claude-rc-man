"""The one environment the tests run rc-helper with, and the check that its GitHub hook works.

Not a test module. helper_env always sets RC_HELPER_GITHUB, to the tests'
GitHub or to UNREACHABLE, so that no rc-helper a test runs can reach the real
GitHub; tests/test_helper_env.py holds every test to building its rc-helper
environment here. Python standard library and the helpers' own modules only.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from tests.github_server import GITHUB, UNREACHABLE

# A repository the hook check asks about; no test makes it.
PROBE = "rcman-hook-check/probe.git"


def helper_env(
    home: Path,
    claude: Path,
    tmux_socket: Path,
    github: str = UNREACHABLE,
    path: str = "/usr/bin:/bin",
) -> dict[str, str]:
    """rc-helper's environment for a test: its home, Claude binary and tmux socket, and
    GitHub at ``github``."""
    return {
        "PATH": path,
        "RC_HELPER_HOME": str(home),
        "RC_HELPER_CLAUDE": str(claude),
        "RC_HELPER_TMUX_SOCKET": str(tmux_socket),
        "RC_HELPER_GITHUB": github,
    }


def assert_github_hooked(scratch: Path, github: str, server=None) -> None:
    """With RC_HELPER_GITHUB at ``github``, git as rc-helper runs it sends GitHub there.

    First the address git would use for a GitHub repository, which git works out
    without reaching anything; only once that is ``github`` is anything fetched, and
    then ``server``, the tests' GitHub at that address, must log the request.
    """
    from helpers import git_run
    from helpers import rc_helper as h

    cfg = h.Config(home=scratch, claude=scratch / "claude", tmux_socket=None, github=github)
    env = h.git_env(cfg)
    url = subprocess.run(
        [git_run.find_git(), "ls-remote", "--get-url", f"{GITHUB}{PROBE}"],
        cwd=scratch,
        env=env,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert url == f"{github}{PROBE}", f"git would reach {url!r} for GitHub, not {github!r}"
    if server is None:
        return
    before = len(server.requests)
    try:
        deadline = time.monotonic() + 30
        git_run.Git(env).run(scratch, ["ls-remote", f"{GITHUB}{PROBE}"], deadline=deadline)
    except git_run.GitFailed:
        pass  # no such repository: what matters is where git asked
    asked = [path for _, path in server.requests[before:]]
    assert any(path.startswith(f"/{PROBE}") for path in asked), asked
