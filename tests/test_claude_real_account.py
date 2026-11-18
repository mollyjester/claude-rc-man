"""Real-account checks: run by hand only.

``uv run pytest -m real_claude`` drives the installed `claude` under the account of the run
(the folder ``CLAUDE_CONFIG_DIR`` names where it is set, otherwise the default Claude
configuration) as rcman does: a server is started by rcman's own ClaudeServer
(backend/rcman/claude_server.py) in a workspace of the host runtime, read with
helpers/screens.py, brought to ready and stopped; and every option rcman passes is checked
in its command's `--help` with that account signed in. The workspace's credentials file
is a link that leads to the account's, as the host runtime's lead to a person's; a refresh
Claude writes by replacing the file may replace the link instead and not reach the account's
own file, so check afterwards that the account is still signed in. Each run creates one
Remote Control session in that account's claude.ai/code list, its name starting with
``PREFIX``; archive it afterwards.
"""

import json
import os
import shutil
from pathlib import Path

import pytest
from rcman import claude_server as cs
from rcman import runtime as rt

from devtools import fake_claude as fake
from devtools.held_folders import tmux_sockets
from helpers import screens
from tests.hang_guard import HANG_GUARD
from tests.test_claude_contract import claude_env, help_gaps, options_used
from tests.until import until

pytestmark = pytest.mark.real_claude

REAL = shutil.which("claude")
REPO = Path(__file__).resolve().parent.parent
TMUX_CONF = REPO / "backend" / "rcman" / "tmux.conf"
NAME = rt.container_name(1)
PREFIX = "rcprobe"


def account_config_dir() -> Path | None:
    """The folder of the account this run was started with, where it names one: the
    CLAUDE_CONFIG_DIR of its environment, which holds its ``.claude.json`` too; None for the
    account in the home of the run."""
    named = os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(named) if named else None


def account_files() -> tuple[Path, Path]:
    """The account's credentials file and the ``.claude.json`` that names it."""
    config = account_config_dir()
    if config is not None:
        return config / fake.CREDENTIALS, config / fake.CONFIG_FILE
    return Path.home() / ".claude" / fake.CREDENTIALS, Path.home() / fake.CONFIG_FILE


def account_env(home: Path) -> dict[str, str]:
    """The environment that gives `claude` the account of this run, in ``home`` as a home of
    its own: its folder where the run names one, otherwise ``home`` with ``.claude`` and
    ``.claude.json`` leading to those of the home of the run."""
    config = account_config_dir()
    if config is None:
        home.mkdir(parents=True, exist_ok=True)
        for name in (".claude", fake.CONFIG_FILE):
            (home / name).symlink_to(Path.home() / name)
    return claude_env(config, home)


@pytest.fixture
def server(tmp_path):
    """rcman's server of a workspace of the host runtime, whose store's one version runs the
    installed `claude`, signed in as the account of the run as rcman signs a workspace in: its
    credentials file a link leading to the account's, and ``oauthAccount`` seeded."""
    assert REAL is not None, "claude is not on PATH"
    store = tmp_path / "claude"
    (store / "versions").mkdir(parents=True)
    (store / "bin").mkdir()
    version = store / "versions" / "real"
    version.write_text(f'#!/bin/sh\nexec {REAL} "$@"\n', encoding="utf-8")
    version.chmod(0o755)
    (store / "current").symlink_to("versions/real")
    (store / "bin" / "claude").symlink_to("../current")
    root = tmp_path / "workspaces"
    root.mkdir()
    credentials, config = account_files()
    with tmux_sockets("rcs-") as sockets:
        runtime = rt.HostRuntime(root, sockets, store, TMUX_CONF)
        runtime.create(rt.Spec(name=NAME, slot=1, workspace=1, person=1))
        person = root / rt.HostRuntime.ACCOUNTS / "1" / fake.CREDENTIALS
        person.unlink()
        person.symlink_to(credentials)
        layout = runtime.layout(NAME)
        named = json.loads(config.read_text(encoding="utf-8"))["oauthAccount"]
        Path(layout.config, fake.CONFIG_FILE).write_text(json.dumps({"oauthAccount": named}))
        channel = runtime.channel(NAME)

        def run(argv, timeout=HANG_GUARD):
            return channel.run(argv, timeout=timeout)

        try:
            yield cs.Server(layout, run, 1)
        finally:
            channel.close()
            runtime.remove(NAME)


def test_a_server_comes_to_ready_and_stops(server):
    assert server.signed_in() is True  # positive control: the account is the workspace's
    state = server.start(PREFIX)["server"]
    while state["state"] in screens.QUESTIONS:  # the folder's trust, and Remote Control's
        state = server.confirm("y")["server"]

    def ready() -> dict | None:
        found = server.status()
        drawn = found and found["session_name"] and found["environment_url"]
        return found if drawn and found["state"] == "ready" else None

    shown = until(ready)
    assert shown["session_name"].startswith(f"{PREFIX}-")
    assert shown["environment_url"].startswith(fake.ENVIRONMENT_URL_PREFIX)
    stopped = server.stop()["server"]
    assert (stopped["state"], stopped["exit_code"]) == ("stopped", 0)


def test_every_option_rcman_passes_is_in_help_when_logged_in(tmp_path):
    assert REAL is not None, "claude is not on PATH"
    env = account_env(tmp_path / "home")  # the run's account, in a home of its own
    assert help_gaps(REAL, env, options_used(), tmp_path) == {}
