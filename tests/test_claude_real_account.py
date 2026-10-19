"""Real-account checks: run by hand only.

``uv run pytest -m real_claude`` runs every recording sequence that needs a
logged-in account, plus the one that submits an invalid sign-in code, against
the installed `claude` under the account of the run (the folder
``CLAUDE_CONFIG_DIR`` names where it is set, otherwise the default Claude
configuration), and compares the screens with the fixtures. Each sequence that
brings a server to ready creates one Remote Control session in that account's
claude.ai/code list each run, as ``devtools/record_claude.py`` names them;
archive them afterwards.
"""

import shutil

import pytest

from devtools import record_claude as rec

pytestmark = pytest.mark.real_claude

REAL = shutil.which("claude")
ACCOUNT = [s for s in rec.SEQUENCES if (s.needs_account or s.submits_code) and not s.needs_human]


def test_account_screens_match_the_fixtures(tmp_path):
    assert REAL is not None, "claude is not on PATH"
    assert ACCOUNT  # non-vacuity
    recorded = rec.record(
        REAL, ACCOUNT, account_config_dir=rec.account_config_dir(), workdir=tmp_path
    )
    names = {c for s in ACCOUNT for c in s.captures}
    expected = {c: f for c, f in rec.load_fixtures().items() if c in names}
    assert set(recorded) == set(expected)
    assert rec.compare(recorded, expected) == []


def test_every_option_rcman_passes_is_in_help_when_logged_in(tmp_path):
    assert REAL is not None, "claude is not on PATH"
    missing = {}
    env = rec.account_env(tmp_path / "home")  # the run's account, in a home of its own
    for command, options in rec.options_used().items():
        text = rec.help_text(REAL, command, env)
        assert not rec.help_refused_logged_out(text), "the run's account is not logged in"
        absent = sorted(o for o in options if o not in text)
        if absent:
            missing[" ".join(command) or "(top level)"] = absent
    assert missing == {}
