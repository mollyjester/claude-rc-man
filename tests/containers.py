"""The containers a run of the tests starts itself on the tests' engine (tests/podman_service.py),
labelled as the run's, so that the run removes them when it ends.

Not a test module. ``engine`` below is the argv that runs podman on the engine (its ``argv()``).
Each run holds a folder of its own, its holder, made with devtools/held_folders.py, labels each
container it starts with the holder's path (labels()), and removes the containers labelled with it
when it ends (holder()), with their anonymous volumes, never a named one. A run killed by a signal
leaves its engine, and the next run that starts one removes it whole, its containers and volumes
with it (tests/podman_service.py), so nothing here sweeps for another run's. The rules are in
project-knowledge/tests.md, "What a killed run leaves".
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

from devtools import held_folders as hf

# What a holder's name begins with.
HOLDERS = "rcct-"
HOLDER = "rcman.test.holder"
# Seconds a removed container may take to be gone.
GONE_WITHIN = 60


# The image a FROM line names, but for its options, when a digest pins it.
_FROM_BY_DIGEST = re.compile(r"FROM\s+(?:--\S+\s+)*(\S+@sha256:[0-9a-f]{64})(?:\s|\Z)")


def digest_references(containerfile: Path) -> list[str]:
    """The images the Containerfile starts from that a digest pins, each once, in order."""
    found: list[str] = []
    for line in containerfile.read_text().splitlines():
        named = _FROM_BY_DIGEST.match(line)
        if named and named.group(1) not in found:
            found.append(named.group(1))
    return found


# An apt-get install command's words, up to the end of its command, its continued lines with it.
_APT_INSTALL = re.compile(r"apt-get\s+install\s+((?:\\\n|[^&;|\n])*)")


def package_lines(text: str) -> list[str]:
    """The lines of the apt-get install commands of a Containerfile's ``text`` that ask for a
    package, and not only options, as they are written but for the continuation."""
    return [
        line
        for command in _APT_INSTALL.findall(text)
        for line in command.split("\\\n")
        if any(not word.startswith("-") for word in line.split())
    ]


def asked_packages(text: str) -> list[str]:
    """The packages the apt-get install commands of a Containerfile's ``text`` ask for, in
    order: the words of their lines but the options."""
    return [w for line in package_lines(text) for w in line.split() if not w.startswith("-")]


# A command a text names: a lowercase word alone between backquotes, as `gh` is and neither
# `gh auth status` nor `/rcman/work` nor `HOME` is.
_NAMED_COMMAND = re.compile(r"`([a-z][a-z0-9+-]*)`")


def named_commands(text: str) -> list[str]:
    """The commands ``text`` names, each once, in order."""
    return list(dict.fromkeys(_NAMED_COMMAND.findall(text)))


def labels(holder: Path) -> list[str]:
    """podman run's arguments that label a container as the run holding ``holder``'s."""
    return ["--label", f"{HOLDER}={holder}"]


def _listed(engine: list[str], *label: str) -> list[str]:
    """Every container, running or not, with ``label``; a podman ps that fails raises."""
    filters = [arg for name in label for arg in ("--filter", f"label={name}")]
    listed = subprocess.run(
        [*engine, "ps", "-a", "-q", "--no-trunc", *filters],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if listed.returncode != 0:
        raise RuntimeError(f"podman ps failed: {listed.stderr}")
    return listed.stdout.split()


def _labelled(engine: list[str], *label: str) -> dict[str, dict[str, str]]:
    """{container: its labels} for every container, running or not, with ``label``."""
    ids = _listed(engine, *label)
    if not ids:
        return {}
    inspected = subprocess.run(
        [*engine, "inspect", "--format", "{{.Id}} {{json .Config.Labels}}", *ids],
        capture_output=True,
        text=True,
        timeout=60,
    )
    found = {}
    # One gone meanwhile is not listed, or listed as an empty line.
    for line in inspected.stdout.splitlines():
        container, _, text = line.partition(" ")
        if container and text:
            found[container] = json.loads(text) or {}
    return found


def _remove(engine: list[str], containers: list[str]) -> None:
    """Remove ``containers``, with their anonymous volumes, and wait until they are gone:
    another run removing one of them at the same time leaves it listed, its removal in
    progress, for a while."""
    if not containers:
        return
    subprocess.run([*engine, "rm", "-f", "-v", *containers], capture_output=True, timeout=120)
    deadline = time.monotonic() + GONE_WITHIN
    while left := set(containers) & set(_listed(engine)):
        if time.monotonic() > deadline:
            raise RuntimeError(f"containers not removed: {sorted(left)}")
        time.sleep(0.2)


@contextlib.contextmanager
def holder(engine: list[str]) -> Iterator[Path]:
    """This run's holder; afterwards the containers labelled with it are removed, and then the
    holder."""
    with hf.held(HOLDERS) as folder:
        try:
            yield folder
        finally:
            _remove(engine, list(_labelled(engine, f"{HOLDER}={folder}")))
