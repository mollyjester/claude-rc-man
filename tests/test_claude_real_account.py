"""Real-account checks: run by hand, never in CI.

``uv run pytest -m real_claude`` runs every recording sequence that needs a
logged-in account, plus the one that submits an invalid sign-in code, against
the installed `claude` under the default Claude configuration, and compares
the screens with the fixtures. The ``server`` sequence creates one Remote
Control session in that account's claude.ai/code list each run; archive it
afterwards.
"""

import shutil

import pytest

from devtools import record_claude as rec

pytestmark = pytest.mark.real_claude

REAL = shutil.which("claude")
ACCOUNT = [s for s in rec.SEQUENCES if s.needs_account or s.submits_code]


def test_account_screens_match_the_fixtures(tmp_path):
    assert REAL is not None, "claude is not on PATH"
    assert ACCOUNT  # non-vacuity
    recorded = rec.record(REAL, ACCOUNT, account_config_dir=None, workdir=tmp_path)
    names = {c for s in ACCOUNT for c in s.captures}
    expected = {c: f for c, f in rec.load_fixtures().items() if c in names}
    assert set(recorded) == set(expected)
    assert rec.compare(recorded, expected) == []


def test_every_option_rcman_passes_is_in_help_when_logged_in():
    assert REAL is not None, "claude is not on PATH"
    missing = {}
    for command, options in rec.options_used().items():
        text = rec.help_text(REAL, command, rec.claude_env(None))
        assert not rec.help_refused_logged_out(text), "the default configuration is not logged in"
        absent = sorted(o for o in options if o not in text)
        if absent:
            missing[" ".join(command) or "(top level)"] = absent
    assert missing == {}
