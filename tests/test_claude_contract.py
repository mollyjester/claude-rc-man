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


# Commands whose `--help` is refused without a login; their options are checked
# by the real-account tests instead.
HELP_NEEDS_LOGIN = {("remote-control",)}


def test_every_option_rcman_passes_is_in_help(tmp_path):
    used = rec.options_used()
    assert used  # non-vacuity
    missing, gated = {}, set()
    for command, options in used.items():
        text = rec.help_text(REAL, command, rec.claude_env(tmp_path))
        if rec.help_refused_logged_out(text):
            gated.add(command)
            continue
        absent = sorted(o for o in options if o not in text)
        if absent:
            missing[" ".join(command) or "(top level)"] = absent
    assert missing == {}
    assert gated == HELP_NEEDS_LOGIN
