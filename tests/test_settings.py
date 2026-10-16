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


def test_the_release_site_defaults_to_claude_codes_own():
    from helpers import claude_release

    assert (
        Settings(public_origin="https://x.example.com").claude_download == claude_release.DOWNLOAD
    )


@pytest.mark.parametrize(
    "address",
    ["https://downloads.example.com/releases", "http://127.0.0.1", "https://a.b/c/d"],
)
def test_a_release_site_is_an_http_or_https_address(address):
    assert (
        Settings(public_origin="https://x.example.com", claude_download=address).claude_download
        == address
    )


@pytest.mark.parametrize(
    "address",
    [
        "",
        "ftp://downloads.example.com",
        "file:///srv/releases",
        "https://x/",
        "https://x/a b",
        "x.example.com",
    ],
)
def test_an_address_that_is_not_one_releases_can_be_downloaded_from_is_refused(address):
    with pytest.raises(ValidationError):
        Settings(public_origin="https://x.example.com", claude_download=address)


def test_the_installer_and_rcman_refuse_the_same_addresses():
    from helpers import claude_release
    from installer import install as ins

    assert ins.DOWNLOAD_ADDRESS is claude_release.DOWNLOAD_ADDRESS
