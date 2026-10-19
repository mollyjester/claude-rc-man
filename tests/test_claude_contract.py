"""Contract tests: the real `claude` still behaves as the fixtures say.

These need no Claude account. They run the no-account recording sequences
against the installed binary and compare the sanitised screens with the
fixtures, check the installed version is the one the fixtures were recorded
from, and check every option rcman passes is still in `--help`.

When `claude` is not installed these tests skip, unless
``RCMAN_REQUIRE_REAL_CLAUDE=1`` (set by ``make check``), in which case they fail.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from devtools import record_claude as rec

REAL = shutil.which("claude")
REQUIRED = os.environ.get("RCMAN_REQUIRE_REAL_CLAUDE") == "1"

pytestmark = pytest.mark.skipif(
    REAL is None and not REQUIRED,
    reason="claude is not installed (make check sets RCMAN_REQUIRE_REAL_CLAUDE=1 to fail instead)",
)

NO_ACCOUNT = [
    s for s in rec.SEQUENCES if not s.needs_account and not s.submits_code and not s.needs_human
]


def test_real_claude_is_installed():
    assert REAL is not None, "claude is not on PATH"


def test_installed_version_is_the_recorded_one():
    recorded = rec.load_manifest()["claude_version"]
    installed = rec.installed_version(REAL)
    assert installed == recorded, (
        f"claude {installed} is installed but the fixtures were recorded from {recorded}: "
        "re-record them (see project-knowledge/fake-claude.md) before relying on the fake"
    )


def test_no_account_screens_match_the_fixtures(tmp_path):
    assert NO_ACCOUNT  # non-vacuity
    recorded = rec.record(REAL, NO_ACCOUNT, account_config_dir=None, workdir=tmp_path)
    expected = {
        c: f
        for c, f in rec.load_fixtures().items()
        if c in {x for s in NO_ACCOUNT for x in s.captures}
    }
    assert set(recorded) == set(expected)
    assert rec.compare(recorded, expected) == []


def test_every_option_rcman_passes_is_in_help(tmp_path):
    used = rec.options_used()
    assert used  # non-vacuity
    missing, gated = {}, set()
    for command, options in used.items():
        text = rec.help_text(REAL, command, rec.claude_env(tmp_path, tmp_path / "home"))
        if rec.help_refused_logged_out(text):
            gated.add(command)
            continue
        absent = sorted(o for o in options if o not in text)
        if absent:
            missing[" ".join(command) or "(top level)"] = absent
    assert missing == {}
    # No command's help is refused without a login, so every option is checked here.
    assert gated == set()


# --- the credentials file rcman shares and empties (backend/rcman/accounts.py) ---------


def _claude(args: list[str], config_dir) -> subprocess.CompletedProcess:
    return subprocess.run(
        [REAL, *args],
        env=rec.claude_env(config_dir, Path(config_dir).parent / f"{Path(config_dir).name}-home"),
        cwd=config_dir,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )


@pytest.mark.parametrize("content", ["", "{}"])
def test_an_empty_credentials_file_is_signed_out_and_left_as_it_is(tmp_path, content):
    """rcman makes the file empty, and empties it to sign out: the real binary reads
    both an empty file and ``{}`` as no sign-in, and does not rewrite either."""
    import json

    from devtools import fake_claude as fake

    (tmp_path / fake.CREDENTIALS).write_text(content)
    (tmp_path / fake.CREDENTIALS).chmod(0o600)
    found = _claude(["auth", "status"], tmp_path)
    assert json.loads(found.stdout)["loggedIn"] is False
    assert found.returncode == rec.load_fixtures()["auth_status_logged_out"].exit_code
    assert (tmp_path / fake.CREDENTIALS).read_text() == content


def test_logout_removes_the_credentials_file(tmp_path):
    """What the fake's logout copies; over a bound file the removal is refused, and
    rcman empties the file in place afterwards."""
    from devtools import fake_claude as fake

    (tmp_path / fake.CREDENTIALS).write_text("{}")
    assert (tmp_path / fake.CREDENTIALS).exists()  # positive control
    found = _claude(["auth", "logout"], tmp_path)
    assert found.returncode == rec.load_fixtures()["auth_logout"].exit_code
    assert not (tmp_path / fake.CREDENTIALS).exists()
