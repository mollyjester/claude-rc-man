"""The settings model, and the configuration guide that documents it."""

import re
from pathlib import Path

from rcman.settings import Settings

REPO = Path(__file__).resolve().parent.parent


def test_values_come_from_rcman_environment_variables(monkeypatch):
    monkeypatch.setenv("RCMAN_PUBLIC_ORIGIN", "https://x.example.com")
    monkeypatch.setenv("RCMAN_AUDIT_RETENTION_DAYS", "7")
    s = Settings()
    assert s.public_origin == "https://x.example.com"
    assert s.audit_retention_days == 7


def documented(guide: str) -> set[str]:
    """The settings the configuration guide names."""
    return set(re.findall(r"`(RCMAN_[A-Z_]+)`", guide))


def test_every_setting_is_documented_and_every_documented_setting_exists():
    guide = (REPO / "docs" / "configuration.md").read_text(encoding="utf-8")
    fields = {f"RCMAN_{name.upper()}" for name in Settings.model_fields}
    assert fields  # non-vacuity
    assert documented(guide) == fields


def test_the_settings_check_fails_on_a_drifted_guide():
    """Planted twin: a guide missing a setting, or naming one that does not exist, is caught."""
    fields = {"RCMAN_ONE", "RCMAN_TWO"}
    right = "Set `RCMAN_ONE` and `RCMAN_TWO`.\n"
    assert documented(right) == fields
    assert documented(right.replace(" and `RCMAN_TWO`", "")) != fields
    assert documented(right + "And `RCMAN_GONE`.\n") != fields


def test_the_tools_default_to_where_the_installer_puts_them():
    from installer import install as ins

    assert Settings(public_origin="https://x.example.com").tools_dir == ins.TOOLS
