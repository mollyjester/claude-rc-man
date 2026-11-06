"""Upgrading rcman from its web interface, rcman's side: what it reads, and asking for one.

The upgrade itself is root's: ``rcman-upgrade.path`` watches ``REQUEST``, in rcman's home, and
starts ``rcman-upgrade.service``, which runs the installer's wrapper (installer/upgrade.py) as
root; the wrapper writes what happened to ``STATUS``. rcman only writes the request, reads the
status, and asks systemd, read-only, what the two units are doing (``show_units``). The paths and
the status's fields are the installer's, mirrored here, as the service imports nothing of it.

Whether an upgrade runs is what systemd shows of the service, not what the status says, since
a run that was cut short may leave a status that says it runs. A request that has not started
a run yet is ``requested`` for ``UPGRADE_START_GRACE``, and ``not_started`` after it. While an
upgrade is requested or runs (``under_way``), no workspace is made or cloned and no Claude Code
version is rolled out: an upgrade restarts rcman, which would cut either short.

The release rcman runs is read once, when it starts (``running_release``), from the mark of the
release its environment is in: an upgrade switches ``current`` while this process runs from the
release before.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from rcman.problems import UPGRADE_REFUSED_MESSAGES, Problem
from rcman.problems import body as problem_body
from rcman.settings import Settings

# The installer's paths (installer/upgrade.py STATUS, installer/install.py UPGRADE_REQUEST).
STATUS = Path("/opt/rcman/upgrade/status.json")
REQUEST = Path("/var/lib/rcman/upgrade.request")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
# The environment rcman runs in: the release's .venv, whose folder holds the release's mark.
PREFIX = Path(sys.prefix)
COMPLETE = ".complete"  # installer/install.py COMPLETE: the release's commit and a newline
COMMIT = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")

# The status's fields and states, and the longest error it holds (installer/upgrade.py).
FIELDS = ("state", "boot_id", "invocation", "started", "finished", "from", "to", "error")
# A key no longer in FIELDS that a status may still hold, dropped before it is checked: the first
# upgrade from the web interface onto this release is finished by the old wrapper, still in
# memory, which writes it; read with it, the status is not one, and the report says unknown.
LEFTOVER = "notices"
STATES = ("running", "succeeded", "failed")
ERROR_LENGTH = 500

# What systemd is asked, read-only, of the upgrade's units, with an argv and environment of its
# own; its answer is a block for each unit, parted by an empty line.
SYSTEMCTL = "/usr/bin/systemctl"
SERVICE, PATH_UNIT = "rcman-upgrade.service", "rcman-upgrade.path"
PROPERTIES = ("Id", "LoadState", "ActiveState", "InvocationID")
SHOW_ARGS = ("show", "--no-pager", f"--property={','.join(PROPERTIES)}", "--", SERVICE, PATH_UNIT)
SHOW_ENVIRONMENT = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "SYSTEMD_PAGER": ""}
SHOW_TIMEOUT = 10.0
# systemd's active states; a unit in one of RUNNING has a run under way (a oneshot's ExecStart is
# activating, its ExecStopPost deactivating).
ACTIVE_STATES = (
    "active", "reloading", "inactive", "failed", "activating", "deactivating", "maintenance",
    "refreshing",
)  # fmt: skip
RUNNING = ("activating", "active", "deactivating", "reloading")

# How long a request may wait for the path unit to start its run before it is not_started.
UPGRADE_START_GRACE = 120.0
# How long a request waits for the images' lock, held while images are collected.
ADMISSION_WAIT = 10.0
# A person's requests: a burst, then a steady rate (ratelimit.py).
REQUESTS_BURST = 3
REQUESTS_PER_MINUTE = 2.0

# The states the report gives: the status's, and requested, not_started and unknown (no status
# rcman can read).
UNDER_WAY = ("requested", "running")
# The status's fields the report gives, where there is no status of the state it gives.
SHOWN = ("started", "finished", "from", "to", "error")


def nothing() -> dict:
    return dict.fromkeys(SHOWN)


class ShowFailed(Exception):
    """systemd could not be asked, or refused."""


@dataclass(frozen=True)
class Unit:
    id: str
    load: str
    active: str
    invocation: str

    @property
    def running(self) -> bool:
        return self.active in RUNNING


@dataclass(frozen=True)
class Request:
    """A request admitted: when (monotonic seconds), and the invocations the service and the
    status named then, so that a run started since is one that names another."""

    at: float
    unit_invocation: str
    status_invocation: str | None


def running_release(prefix: Path | None = None) -> str | None:
    """The commit of the release whose environment is ``prefix`` (PREFIX), from its mark; None
    where there is none or it names no commit, as in a development run."""
    try:
        release = Path(prefix or PREFIX).resolve().parent
        text = (release / COMPLETE).read_text(encoding="utf-8").strip()
    except (OSError, ValueError):
        return None
    return text if COMMIT.fullmatch(text) else None


def _text(value) -> bool:
    return value is None or isinstance(value, str)


def read_status(path: Path | None = None) -> dict | None:
    """The status at ``path`` (STATUS), or None where there is none, rcman cannot read it, or it
    is not one: exactly FIELDS once LEFTOVER is dropped, a state of STATES, text or null where
    text is, and an error no longer than ERROR_LENGTH."""
    try:
        found = json.loads((path or STATUS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(found, dict):
        return None
    found.pop(LEFTOVER, None)
    if set(found) != set(FIELDS) or found["state"] not in STATES:
        return None
    if not isinstance(found["boot_id"], str):
        return None
    if not all(_text(found[k]) for k in ("invocation", "started", "finished", "from", "to")):
        return None
    if not _text(found["error"]) or len(found["error"] or "") > ERROR_LENGTH:
        return None
    return found


def boot_id() -> str | None:
    try:
        return BOOT_ID.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def show_units() -> str:
    """What systemd shows of the upgrade's units (SHOW_ARGS); ShowFailed where it cannot be
    asked, does not answer in time or refuses."""
    try:
        done = subprocess.run(
            [SYSTEMCTL, *SHOW_ARGS],
            capture_output=True,
            env=SHOW_ENVIRONMENT,
            stdin=subprocess.DEVNULL,
            timeout=SHOW_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as failed:
        raise ShowFailed(f"systemctl show could not be run: {failed}") from None
    if done.returncode != 0:
        said = done.stderr.decode("utf-8", "replace").strip()
        raise ShowFailed(said or f"systemctl show exited {done.returncode}")
    return done.stdout.decode("utf-8", "replace")


def read_units(shown: str) -> dict[str, Unit]:
    """The two units from ``shown``: a block of exactly PROPERTIES for each, and nothing else.
    Anything else is a ValueError, so that what cannot be read is never taken for idle."""
    blocks = shown.strip("\n").split("\n\n") if shown.strip() else []
    units: dict[str, Unit] = {}
    for block in blocks:
        found: dict[str, str] = {}
        for line in block.splitlines():
            key, sep, value = line.partition("=")
            if not sep or key not in PROPERTIES or key in found:
                raise ValueError(f"systemctl showed {line!r}")
            found[key] = value.strip()
        if set(found) != set(PROPERTIES) or found["ActiveState"] not in ACTIVE_STATES:
            raise ValueError(f"systemctl showed {sorted(found)}")
        unit = Unit(found["Id"], found["LoadState"], found["ActiveState"], found["InvocationID"])
        if unit.id in units:
            raise ValueError(f"systemctl showed {unit.id} twice")
        units[unit.id] = unit
    if set(units) != {SERVICE, PATH_UNIT}:
        raise ValueError(f"systemctl showed {sorted(units)}")
    return units


def _fields(status: dict | None) -> dict:
    if status is None:
        return nothing()
    return {key: status[key] for key in SHOWN}


class Upgrades:
    """The upgrade of the rcman this process is: its release, read once; the report; whether
    one is under way; and the admission of a request (``request``)."""

    def __init__(self, settings: Settings, workspaces):
        self.settings = settings
        self.workspaces = workspaces
        self.release = running_release()
        # Asks systemd; the tests give recorded answers in its place.
        self.show = show_units
        self._lock = threading.Lock()
        self._request: Request | None = None

    # --- reading -------------------------------------------------------------------------------

    def _units(self) -> dict[str, Unit]:
        try:
            return read_units(self.show())
        except (ShowFailed, ValueError) as failed:
            raise ShowFailed(str(failed)) from None

    @staticmethod
    def _written(request: Request, status: dict | None) -> bool:
        """Whether a run since ``request`` wrote its status: one naming another invocation."""
        seen = status["invocation"] if status else None
        return seen is not None and seen != request.status_invocation

    def _done_with(self, request: Request, status: dict | None, units) -> bool:
        """Whether ``request`` is no longer what says how the upgrade goes: its run wrote its
        status, which says so from then on, or systemd shows another invocation of the service
        than then that no longer runs, a run that ended before it wrote anything. A run that
        has started and not written yet still holds it, so that nothing is let in meanwhile."""
        if self._written(request, status):
            return True
        if units is None:
            return False
        service = units[SERVICE]
        return bool(service.invocation) and (
            service.invocation != request.unit_invocation and not service.running
        )

    def _state(self, status: dict | None, units: dict[str, Unit] | None) -> tuple[str, dict]:
        """The state and the status's fields that go with it, from the status, what systemd
        shows (None where it was not asked or could not be read) and the request held."""
        with self._lock:
            request = self._request
            if request is not None and self._done_with(request, status, units):
                self._request = request = None  # what the run says from now on
        if request is not None:
            if units is not None and units[SERVICE].running:
                return "running", nothing()  # started, and has written nothing yet
            waited = time.monotonic() - request.at
            return ("requested" if waited < UPGRADE_START_GRACE else "not_started"), nothing()
        if units is not None and units[SERVICE].running:
            own = status is not None and status["invocation"] == units[SERVICE].invocation
            return "running", _fields(status if own else None)
        if status is None:
            return "unknown", nothing()
        if status["state"] == "running":
            this_boot = boot_id()
            if (this_boot is not None and status["boot_id"] != this_boot) or units is not None:
                return "failed", _fields(status)  # another boot's, or the service no longer runs it
        return status["state"], _fields(status)

    def _reason(
        self, units, unreadable: bool, state: str, rollouts_held: bool = False
    ) -> str | None:
        """Why no upgrade may be asked for now, or None; the admission, which holds the
        roll-outs' lock itself (``rollouts_held``), adds ``images``."""
        if not self.settings.web_upgrade:
            return "disabled"
        if unreadable:
            return "units_unreadable"
        if units[PATH_UNIT].load != "loaded" or units[PATH_UNIT].active != "active":
            return "path_inactive"
        if state in UNDER_WAY:
            return "under_way"
        if not rollouts_held and self.workspaces.versions.rolling_out():
            return "rolling_out"
        if self.workspaces.any_creating():
            return "creating"
        if self.workspaces.any_rebuilding():
            return "rebuilding"
        return None

    def _read(self) -> tuple[dict | None, dict[str, Unit] | None, bool]:
        status = read_status()
        units, unreadable = None, False
        if self.settings.web_upgrade:
            try:
                units = self._units()
            except ShowFailed:
                unreadable = True
        return status, units, unreadable

    def _report(self, state: str, fields: dict, reason: str | None) -> dict:
        return {
            "release": self.release,
            "enabled": self.settings.web_upgrade,
            "available": reason is None,
            "reason": None if reason is None else refusal(reason).body(),
            "state": state,
            **fields,
        }

    def report(self) -> dict:
        """What GET /api/rcman/upgrade answers: the release rcman runs, whether the option is
        on, whether an upgrade may be asked for and, if not, why (the problem a request would
        be refused with), and the last upgrade's state with the fields of its status."""
        status, units, unreadable = self._read()
        state, fields = self._state(status, units)
        return self._report(state, fields, self._reason(units, unreadable, state))

    def under_way(self) -> bool:
        """Whether an upgrade is requested or runs. It asks systemd only where the status says
        a run of this boot runs, so that a listing of workspaces costs no more than a read."""
        if not self.settings.web_upgrade:
            return False
        status = read_status()
        with self._lock:
            request = self._request
        if request is not None and not self._written(request, status):
            if time.monotonic() - request.at < UPGRADE_START_GRACE:
                return True
            try:  # past the grace, only a run of the service holds it
                units = self._units()
            except ShowFailed:
                units = None
            return self._state(status, units)[0] in UNDER_WAY
        if status is None or status["state"] != "running" or status["boot_id"] != boot_id():
            return False
        try:
            units = self._units()
        except ShowFailed:
            return True  # what the status says, which nothing contradicts
        return self._state(status, units)[0] in UNDER_WAY

    # --- asking ---------------------------------------------------------------------------------

    def request(self) -> dict:
        """Admit a request for an upgrade, or refuse it with ``rcman_upgrade_refused`` and why.
        It takes the images' lock, waiting at most ADMISSION_WAIT, which every workspace is
        recorded under, then tries the roll-outs' lock without waiting, which a roll-out and
        its sweep hold; holding both it checks that nothing refuses it, records the request
        and writes the request file, so that a creation or a roll-out is either seen by its
        checks or comes after the request and is refused. The report as it is then."""
        if not self.settings.web_upgrade:
            raise refusal("disabled")
        with self.workspaces.recording_held(ADMISSION_WAIT) as recording:
            if not recording:
                raise refusal("images")
            with self.workspaces.versions.rollouts_held() as rollouts:
                if not rollouts:
                    raise refusal("rolling_out")
                status, units, unreadable = self._read()
                state, _ = self._state(status, units)
                if (why := self._reason(units, unreadable, state, True)) is not None:
                    raise refusal(why)
                with self._lock:
                    self._request = Request(
                        time.monotonic(),
                        units[SERVICE].invocation,
                        status["invocation"] if status else None,
                    )
                try:
                    write_request()
                except OSError:
                    with self._lock:
                        self._request = None
                    raise
        return self._report("requested", nothing(), "under_way")


def write_request(path: Path | None = None) -> None:
    """Write the request file, which the path unit watches: a write and a close, which
    PathModified= sees, of a file of rcman's own that is never followed through a link."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path or REQUEST, flags, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(dt.datetime.now(dt.UTC).isoformat(timespec="seconds") + "\n")


class Refused(Problem):
    """``rcman_upgrade_refused``, with why in plain words and as ``reason``, one of
    problems.UPGRADE_REFUSED_MESSAGES; the report gives the same details."""

    def __init__(self, reason: str):
        super().__init__(
            "rcman_upgrade_refused", title=UPGRADE_REFUSED_MESSAGES[reason], reason=reason
        )

    def body(self) -> dict:
        return problem_body(self.code, self.status, self.title, **self.extra)


refusal = Refused
