"""Claude Code's releases: where they are, what a release says about itself, and a download
checked against its checksum.

A release is ``<download>/<version>/manifest.json``, which names a checksum for each
platform, and ``<download>/<version>/<platform>/claude``, one program; ``<download>/latest``
names the newest version. The installer fetches the version rcman was tested with, and rcman
fetches the version an administrator rolls out, with this one code.

A download is streamed to a partial file next to where it will be, hashed as it arrives and
held to a size, and only a whole file whose checksum is the manifest's is renamed into place;
whatever goes wrong, nothing is left. The checksum comes from the same place as the program:
it protects against a download that is damaged, not against a source that is not to be
trusted.

Python standard library and the helpers' own rules only: the installer runs under the system
interpreter.
"""

from __future__ import annotations

import hashlib
import http.client
import io
import json
import os
import platform
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import BinaryIO

from helpers import rules

DOWNLOAD = "https://downloads.claude.ai/claude-code-releases"
# An address releases are downloaded from: http or https, with no slash at its end.
DOWNLOAD_ADDRESS = re.compile(r"https?://[A-Za-z0-9.-]+(?::[0-9]+)?(?:/[A-Za-z0-9._~%-]+)*")
# How long one wait of a connection may take, and how long the one question that is only a
# suggestion may.
TIMEOUT_SECONDS = 300.0
LATEST_TIMEOUT_SECONDS = 10.0
# The most a program or an archive may be, and the most a text (a manifest, a list of
# checksums, the name of a version) may be.
MAX_BINARY_BYTES = 1024**3
MAX_TEXT_BYTES = 1024 * 1024
MAX_LATEST_BYTES = 64
CHUNK_BYTES = 1024 * 1024
# What a download is called while it is being written: its destination's name, with a dot
# before it and this after.
PARTIAL_SUFFIX = ".partial"
MODE = 0o755
CHECKSUM = re.compile(r"[0-9a-f]{64}")
# What a ReleaseError can be, as its ``kind``.
KINDS = ("no_such_version", "no_build", "checksum", "too_big", "network")
ARCHITECTURES = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}


class ReleaseError(Exception):
    """A release could not be had. ``kind`` is one of ``no_such_version``, ``no_build`` (the
    release has no program for this platform), ``checksum``, ``too_big`` and ``network``; the
    message says more, for the log, and never holds a secret."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


def architecture(machine: str | None = None) -> str:
    """The name Claude Code's releases give this machine's architecture."""
    machine = machine or platform.machine()
    if machine not in ARCHITECTURES:
        raise ReleaseError("no_build", f"Claude Code has no build for the machine {machine}")
    return ARCHITECTURES[machine]


def platform_key(machine: str | None = None) -> str:
    """The key of this machine's program in a release's manifest."""
    return f"linux-{architecture(machine)}"


def _open(url: str, timeout: float | None = None, missing: str = "no_such_version"):
    """The response to a request for ``url``; a 404 is ``missing``, anything else that goes
    wrong is the network."""
    if not url.startswith(("http://", "https://")):
        raise ReleaseError("network", f"{url} is not an http or https address")
    try:
        return urllib.request.urlopen(url, timeout=timeout or TIMEOUT_SECONDS)
    except urllib.error.HTTPError as answer:
        answer.close()
        kind = missing if answer.code == 404 else "network"
        raise ReleaseError(kind, f"{url}: the answer was {answer.code}") from None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as error:
        raise ReleaseError("network", f"{url}: {error}") from None


def _stream(
    url: str,
    sink: BinaryIO,
    expected: str | None,
    max_bytes: int,
    timeout: float | None,
    missing: str = "no_such_version",
) -> None:
    """Write what ``url`` answers to ``sink`` as it arrives, at most ``max_bytes`` of it, and
    refuse it if ``expected`` is a checksum it does not have."""
    response = _open(url, timeout, missing)
    try:
        length = response.headers.get("Content-Length")
        if length and length.isdigit() and int(length) > max_bytes:
            raise ReleaseError("too_big", f"{url} is bigger than {max_bytes} bytes")
        digest = hashlib.sha256()
        total = 0
        while True:
            try:
                chunk = response.read(CHUNK_BYTES)
            except (OSError, http.client.HTTPException) as error:
                raise ReleaseError("network", f"{url}: the download stopped: {error}") from None
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ReleaseError("too_big", f"{url} is bigger than {max_bytes} bytes")
            digest.update(chunk)
            sink.write(chunk)
    finally:
        response.close()
    if expected is not None:
        check(digest.hexdigest(), expected, url)


def check(actual: str, expected: str, url: str) -> None:
    """Refuse a download whose SHA-256 (hexadecimal) is not the one ``expected``."""
    if actual != expected:
        raise ReleaseError("checksum", f"checksum mismatch for {url}")


def read(url: str, max_bytes: int | None = None, timeout: float | None = None) -> bytes:
    """What ``url`` answers, in memory: at most ``max_bytes`` (``MAX_TEXT_BYTES``, enough for
    a manifest or a list of checksums) of it."""
    sink = io.BytesIO()
    _stream(url, sink, None, max_bytes or MAX_TEXT_BYTES, timeout)
    return sink.getvalue()


def save_verified(
    url: str,
    expected: str,
    destination: Path,
    max_bytes: int | None = None,
    missing: str = "no_such_version",
) -> None:
    """What ``url`` answers as the file ``destination``, runnable, if it is at most
    ``max_bytes`` (``MAX_BINARY_BYTES``) and has the checksum ``expected``. It is written to
    a partial file beside ``destination`` and renamed to it whole; on any failure the partial
    file is removed and ``destination`` is as it was."""
    partial = destination.with_name(f".{destination.name}{PARTIAL_SUFFIX}")
    partial.unlink(missing_ok=True)  # what an earlier download left
    try:
        with partial.open("wb") as sink:
            _stream(url, sink, expected, max_bytes or MAX_BINARY_BYTES, None, missing)
        partial.chmod(MODE)
        os.replace(partial, destination)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def _checked_version(version: str) -> str:
    if not rules.VERSION.fullmatch(version):
        raise ReleaseError("no_such_version", f"{version!r} is not a Claude Code version")
    return version


def manifest(download: str, version: str) -> dict:
    """The manifest of the release of ``version``."""
    url = f"{download.rstrip('/')}/{_checked_version(version)}/manifest.json"
    try:
        found = json.loads(read(url))
    except ValueError:
        raise ReleaseError("network", f"{url} is not a manifest") from None
    if not isinstance(found, dict):
        raise ReleaseError("network", f"{url} is not a manifest")
    return found


def checksum(found: dict, version: str, key: str | None = None) -> str:
    """The checksum the manifest ``found`` names for the program for ``key`` (this platform
    by default); the release has no build for a platform it names none for."""
    key = key or platform_key()
    try:
        expected = found["platforms"][key]["checksum"]
    except (KeyError, TypeError):
        expected = None
    if not isinstance(expected, str) or not CHECKSUM.fullmatch(expected):
        raise ReleaseError("no_build", f"Claude Code {version} has no build for {key}")
    return expected


def fetch(download: str, version: str, destination: Path) -> None:
    """The program of the release of ``version`` for this platform, as the file
    ``destination``, checked against its manifest."""
    key = platform_key()
    expected = checksum(manifest(download, version), version, key)
    url = f"{download.rstrip('/')}/{version}/{key}/claude"
    save_verified(url, expected, destination, missing="no_build")


def latest(download: str) -> str:
    """The version the release site names as the newest."""
    url = f"{download.rstrip('/')}/latest"
    text = read(url, MAX_LATEST_BYTES, LATEST_TIMEOUT_SECONDS).decode("utf-8", "replace").strip()
    if not rules.VERSION.fullmatch(text):
        raise ReleaseError("network", f"{url} does not name a version")
    return text
