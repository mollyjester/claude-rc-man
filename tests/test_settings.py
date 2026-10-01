"""The settings model, and the configuration guide that documents it."""

import re
from pathlib import Path

import pytest
from pydantic import ValidationError
from rcman.settings import Settings

REPO = Path(__file__).resolve().parent.parent


def test_public_origin_is_required(monkeypatch):
    monkeypatch.delenv("RCMAN_PUBLIC_ORIGIN", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_values_come_from_rcman_environment_variables(monkeypatch):
    monkeypatch.setenv("RCMAN_PUBLIC_ORIGIN", "https://x.example.com")
    monkeypatch.setenv("RCMAN_AUDIT_RETENTION_DAYS", "7")
    s = Settings()
    assert s.public_origin == "https://x.example.com"
    assert s.audit_retention_days == 7


@pytest.mark.parametrize(
    "origin", ["x.example.com", "https://x.example.com/", "https://x.example.com/path", "ftp://x"]
)
def test_public_origin_must_be_a_bare_origin(origin):
    with pytest.raises(ValidationError):
        Settings(public_origin=origin)


@pytest.mark.parametrize("field", ["audit_retention_days", "audit_max_rows"])
def test_retention_bounds_are_positive(field):
    with pytest.raises(ValidationError):
        Settings(public_origin="https://x.example.com", **{field: 0})


def test_every_setting_is_documented_and_every_documented_setting_exists():
    guide = (REPO / "docs" / "configuration.md").read_text(encoding="utf-8")
    documented = set(re.findall(r"`(RCMAN_[A-Z_]+)`", guide))
    fields = {f"RCMAN_{name.upper()}" for name in Settings.model_fields}
    assert fields  # non-vacuity
    assert documented == fields


def test_every_setting_has_a_description():
    assert all(f.description for f in Settings.model_fields.values())
