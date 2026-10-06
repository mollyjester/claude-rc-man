"""The containers a run of the tests starts, labelled so that a later run removes those a
run killed by a signal left running.

Not a test module. Each run holds a folder of its own, its holder, made with
devtools/held_folders.py, and labels each container it starts (labels()) with:

- HOLDER, the holder's absolute path;
- OWNER, the user's uid;
- WHERE, the boot and the folder the holder is made in (where()), so a container
  another boot, another machine or another /tmp started is never taken for one of
  this run's.

holder() takes the folder, removes first every container whose labels are all the
run's own kind, whose holder its own prefix, HOLDERS, names, and whose holder nothing
holds any longer (abandoned()), and removes
the run's own containers when it ends. The rules are in project-knowledge/tests.md,
"What a killed run leaves".
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

from devtools import held_folders as hf

# What a holder's name begins with.
HOLDERS = "rcct-"
HOLDER = "rcman.test.holder"
OWNER = "rcman.test.owner"
WHERE = "rcman.test.where"
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
# Seconds a removed container may take to be gone.
GONE_WITHIN = 60


def where(holder: Path) -> str:
    """The boot, and the device and inode of the folder ``holder`` is made in."""
    status = holder.parent.stat()
    return f"{BOOT_ID.read_text().strip()} {status.st_dev}:{status.st_ino}"


def labels(holder: Path) -> list[str]:
    """docker run's arguments that label a container as the run holding ``holder``'s."""
    return [
        "--label", f"{HOLDER}={holder}",
        "--label", f"{OWNER}={os.getuid()}",
        "--label", f"{WHERE}={where(holder)}",
    ]  # fmt: skip


def abandoned(found: dict[str, dict[str, str]]) -> list[str]:
    """Of ``found``, {container: its labels}, those a killed run of the current user's
    left here: owner, an absolute holder made with HOLDERS, as it is when this is
    called, and where all match, and nothing holds the holder any longer. As a held
    folder's sweep takes only its own prefix's folders, a holder of another prefix is
    another sweep's."""
    out = []
    for container, labelled in found.items():
        holder = labelled.get(HOLDER, "")
        if labelled.get(OWNER) != str(os.getuid()) or not os.path.isabs(holder):
            continue
        if not hf.made_with(HOLDERS)(Path(holder).name):
            continue
        try:
            here = where(Path(holder))
        except OSError:
            continue
        if labelled.get(WHERE) == here and hf.unheld(Path(holder)):
            out.append(container)
    return out


def _listed(docker: list[str], *label: str) -> list[str]:
    """Every container, running or not, with ``label``; a docker ps that fails raises."""
    filters = [arg for name in label for arg in ("--filter", f"label={name}")]
    listed = subprocess.run(
        [*docker, "ps", "-a", "-q", "--no-trunc", *filters],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if listed.returncode != 0:
        raise RuntimeError(f"docker ps failed: {listed.stderr}")
    return listed.stdout.split()


def _labelled(docker: list[str], *label: str) -> dict[str, dict[str, str]]:
    """{container: its labels} for every container, running or not, with ``label``."""
    ids = _listed(docker, *label)
    if not ids:
        return {}
    inspected = subprocess.run(
        [*docker, "inspect", "--format", "{{.Id}} {{json .Config.Labels}}", *ids],
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


def _remove(docker: list[str], containers: list[str]) -> None:
    """Remove ``containers``, and wait until they are gone: another run removing one of
    them at the same time leaves it listed, its removal in progress, for a while."""
    if not containers:
        return
    subprocess.run([*docker, "rm", "-f", *containers], capture_output=True, timeout=120)
    deadline = time.monotonic() + GONE_WITHIN
    while left := set(containers) & set(_listed(docker)):
        if time.monotonic() > deadline:
            raise RuntimeError(f"containers not removed: {sorted(left)}")
        time.sleep(0.2)


def clear_abandoned(docker: list[str]) -> None:
    """Remove every container a killed run of the current user's left here."""
    _remove(docker, abandoned(_labelled(docker, HOLDER)))


@contextlib.contextmanager
def holder(docker: list[str]) -> Iterator[Path]:
    """This run's holder, once the containers killed runs left are removed; afterwards
    the containers labelled with it are removed, and then the holder."""
    with hf.held(HOLDERS) as folder:
        clear_abandoned(docker)
        try:
            yield folder
        finally:
            _remove(docker, list(_labelled(docker, f"{HOLDER}={folder}")))
