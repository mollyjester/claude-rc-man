"""What the host, each person and each workspace container use now, for administrators: the
Monitor page (routers/monitor.py).

Two rounds collect it, each on an interval of its own, from the settings (``MonitorSettings``,
kept in the table ``monitor_settings``, the defaults ``DEFAULTS`` where no row is):

- the *usage round* reads the host's own files (``/proc/stat``, ``/proc/meminfo``,
  ``/proc/loadavg``) and the filesystems of the database's folder, of ``/`` and of Podman's
  storage, and asks the runtime once for the slices (limits.py) of the people and of the
  workspaces, as far as each scope is on: one ``systemctl --user show``;
- the *disk round* asks the runtime once for ``podman system df -v`` and once for
  ``podman container inspect`` of the workspace containers, and runs only when people or
  containers are on. A workspace's disk is its volume, the anonymous volume at its /tmp and its
  container's own layer; a person's is the sum over their workspaces. A read that fails is tried
  once more; one that fails again keeps the figures of the last that did not, with their time.

Each figure that is not known is None, never 0. CPU is a percent of one core, the average since
the round before. Each round is stamped with the generation of the settings it ran under, and
what it read of a scope that is off by the time it ends is dropped. A round's failure is logged
only when it starts, changes or ends (``log``).

The loops (``Monitor.start``) are tasks of the service's event loop, one for each round, which
run their round in a thread and never two of it at once. The first usage round runs once the
workspaces are restored, the first disk round a whole disk interval after that. A save of the
settings wakes both, from the request's thread, to their new intervals counted from their last
round. ``Monitor.stop`` ends both and waits for a round under way, killing a disk read.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import os
import re
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field, StrictBool, StrictInt

from rcman import db, limits
from rcman import runtime as rt

log = logging.getLogger("rcman.monitor")
log.setLevel(logging.INFO)

# --- the settings -----------------------------------------------------------------------

USAGE_INTERVAL_DEFAULT, USAGE_INTERVAL_MIN, USAGE_INTERVAL_MAX = 60, 15, 3600
DISK_INTERVAL_DEFAULT, DISK_INTERVAL_MIN, DISK_INTERVAL_MAX = 3600, 600, 86400


class MonitorSettings(BaseModel):
    """How often each round runs, and which scopes are collected. Every one is sent on a save."""

    usage_interval_seconds: Annotated[
        StrictInt,
        Field(
            ge=USAGE_INTERVAL_MIN,
            le=USAGE_INTERVAL_MAX,
            description="Seconds between readings of CPU, memory and tasks.",
        ),
    ]
    disk_interval_seconds: Annotated[
        StrictInt,
        Field(
            ge=DISK_INTERVAL_MIN,
            le=DISK_INTERVAL_MAX,
            description="Seconds between readings of the disk the workspaces use.",
        ),
    ]
    host: Annotated[StrictBool, Field(description="Collect the host's figures.")]
    people: Annotated[StrictBool, Field(description="Collect each person's figures.")]
    containers: Annotated[
        StrictBool, Field(description="Collect each workspace container's figures.")
    ]

    @property
    def usage_on(self) -> bool:
        return self.host or self.people or self.containers

    @property
    def disk_on(self) -> bool:
        return self.people or self.containers


DEFAULTS = MonitorSettings(
    usage_interval_seconds=USAGE_INTERVAL_DEFAULT,
    disk_interval_seconds=DISK_INTERVAL_DEFAULT,
    host=True,
    people=True,
    containers=True,
)
SCOPES = ("host", "people", "containers")
# Column names are the model's own fields, never anything from a request.
COLUMNS = tuple(MonitorSettings.model_fields)


def read_settings(con: sqlite3.Connection) -> MonitorSettings:
    """The settings saved, or ``DEFAULTS`` where none are."""
    row = con.execute("select * from monitor_settings where id = 1").fetchone()
    if row is None:
        return DEFAULTS
    return MonitorSettings(
        **{
            name: bool(row[name]) if isinstance(getattr(DEFAULTS, name), bool) else row[name]
            for name in COLUMNS
        }
    )


def write_settings(con: sqlite3.Connection, settings: MonitorSettings) -> None:
    """Save the whole set, over any saved before."""
    values = [int(v) if isinstance(v, bool) else v for v in settings.model_dump().values()]
    con.execute(
        f"insert into monitor_settings (id, {', '.join(COLUMNS)}) "
        f"values (1, {', '.join('?' for _ in COLUMNS)}) "
        f"on conflict (id) do update set {', '.join(f'{c} = excluded.{c}' for c in COLUMNS)}",
        values,
    )
    con.commit()


# --- the host's own files ---------------------------------------------------------------

PROC = Path("/proc")
# The fields of a cpu line of /proc/stat that make its time: user, nice, system, idle, iowait,
# irq, softirq and steal; guest time is counted in user and nice already.
STAT_FIELDS = 8
IDLE_FIELDS = (3, 4)  # idle and iowait
CPU_LINE = re.compile(r"cpu[0-9]+\s")
KIB = 1024


def read_stat(text: str) -> tuple[int, int, int]:
    """From /proc/stat: the busy and the total time of every CPU together, in ticks, and how many
    CPUs there are."""
    lines = text.splitlines()
    first = lines[0].split() if lines else []
    if not first or first[0] != "cpu":
        raise ValueError("/proc/stat has no cpu line")
    values = [int(v) for v in first[1 : 1 + STAT_FIELDS]]
    if len(values) <= max(IDLE_FIELDS):
        raise ValueError("/proc/stat's cpu line is short")
    total = sum(values)
    idle = sum(values[i] for i in IDLE_FIELDS)
    cpus = sum(1 for line in lines if CPU_LINE.match(line))
    return total - idle, total, cpus


def host_cpu(before: tuple[int, int, int], after: tuple[int, int, int]) -> float | None:
    """The host's CPU between two readings of read_stat, as a percent of one core; None where it
    cannot be told."""
    busy, total = after[0] - before[0], after[1] - before[1]
    if total <= 0 or busy < 0:
        return None
    return round(busy / total * after[2] * limits.PERCENT_PER_CORE, 1)


def read_meminfo(text: str) -> tuple[int, int]:
    """From /proc/meminfo: the memory in use, the total less what is available, and the total,
    in bytes."""
    found = {}
    for line in text.splitlines():
        name, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            found[name] = int(parts[0]) * KIB
    if "MemTotal" not in found or "MemAvailable" not in found:
        raise ValueError("/proc/meminfo has no MemTotal or MemAvailable")
    return found["MemTotal"] - found["MemAvailable"], found["MemTotal"]


def read_loadavg(text: str) -> int:
    """From /proc/loadavg: the tasks there are, threads counting, which its fourth field counts
    (and which hidepid does not hide)."""
    fields = text.split()
    if len(fields) < 4 or "/" not in fields[3]:
        raise ValueError("/proc/loadavg has no fourth field")
    return int(fields[3].partition("/")[2])


def filesystems(folders: list[tuple[str, Path]]) -> list[dict]:
    """Each filesystem the folders are on, once, with the roles of the folders on it, the path of
    the first, and its size, what is used and what is available to rcman, in bytes. A folder that
    cannot be read is a filesystem of its own whose figures are not known."""
    found: dict[object, dict] = {}
    for role, folder in folders:
        try:
            device = os.stat(folder).st_dev
            stats = os.statvfs(folder)
        except OSError:
            found[(role, str(folder))] = _filesystem(role, folder, None)
            continue
        if device in found:
            found[device]["roles"].append(role)
        else:
            found[device] = _filesystem(role, folder, stats)
    return list(found.values())


def _filesystem(role: str, folder: Path, stats: os.statvfs_result | None) -> dict:
    if stats is None:
        size = used = available = None
    else:
        size = stats.f_blocks * stats.f_frsize
        used = (stats.f_blocks - stats.f_bfree) * stats.f_frsize
        available = stats.f_bavail * stats.f_frsize
    return {
        "path": str(folder),
        "roles": [role],
        "size_bytes": size,
        "used_bytes": used,
        "available_bytes": available,
    }


# --- the slices -------------------------------------------------------------------------

NANOSECONDS = 1_000_000_000
INACTIVE = ("inactive", "failed")


@dataclass(frozen=True)
class UnitReading:
    """What ``systemctl show`` said of a slice: whether it is active, its memory with swap in
    bytes, its tasks and the CPU time it used, in nanoseconds; None where it is not counted. A
    slice that is not active uses nothing, and its CPU time is not counted."""

    active: bool
    memory_bytes: int | None
    tasks: int | None
    cpu_nsec: int | None


def _counted(value: str | None, name: str) -> int | None:
    """A count of systemctl's: None where it is not counted (``limits.NOT_SET``, or the largest
    number, as older systemds say it)."""
    if value is None:
        raise ValueError(f"systemctl showed no {name}")
    if value == limits.NOT_SET:
        return None
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"systemctl showed {name}={value}")
    number = int(value)
    return None if number >= limits.LARGEST else number


def _unit(found: dict[str, str]) -> UnitReading:
    state = found.get("ActiveState")
    if not state:
        raise ValueError("systemctl showed no ActiveState")
    if state in INACTIVE:
        return UnitReading(False, 0, 0, None)
    memory = _counted(found.get("MemoryCurrent"), "MemoryCurrent")
    swap = _counted(found.get("MemorySwapCurrent", limits.NOT_SET), "MemorySwapCurrent")
    return UnitReading(
        True,
        None if memory is None else memory + (swap or 0),
        _counted(found.get("TasksCurrent"), "TasksCurrent"),
        _counted(found.get("CPUUsageNSec"), "CPUUsageNSec"),
    )


def _blocks(text: str) -> list[list[str]]:
    """The groups of lines that blank lines part."""
    blocks: list[list[str]] = [[]]
    for line in text.splitlines():
        if line.strip():
            blocks[-1].append(line)
        elif blocks[-1]:
            blocks.append([])
    return [block for block in blocks if block]


def read_units(shown: str) -> dict[str, UnitReading | None]:
    """Each slice ``systemctl show`` of several (``limits.MONITORED``) told of, by its ``Id``:
    None for one whose block cannot be read, which leaves the others known."""
    found: dict[str, UnitReading | None] = {}
    for block in _blocks(shown):
        properties = {}
        for line in block:
            key, _, value = line.partition("=")
            properties[key] = value.strip()
        unit = properties.get("Id")
        if not unit:
            continue
        try:
            found[unit] = _unit(properties)
        except ValueError:
            found[unit] = None
    return found


def unit_cpu(
    before: tuple[UnitReading, float] | None, after: tuple[UnitReading, float]
) -> float | None:
    """A slice's CPU between two readings, each with the clock's time of it, as a percent of one
    core: None where it cannot be told - there is no reading before, the slice was not active
    in it, stopped meanwhile, is not counted, or its count dropped (it was started again); 0
    where it was not active in either."""
    now, at = after
    if before is None:
        return None
    then, since = before
    if not now.active:
        return 0.0 if not then.active else None
    if not then.active or then.cpu_nsec is None or now.cpu_nsec is None:
        return None
    used, seconds = now.cpu_nsec - then.cpu_nsec, at - since
    if used < 0 or seconds <= 0:
        return None
    return round(used / (seconds * NANOSECONDS) * limits.PERCENT_PER_CORE, 1)


# --- podman system df -v --------------------------------------------------------------------

# The tables of ``system df -v`` by the line that names each, and the columns read of each.
TABLES = {"images": rt.DF_IMAGES, "containers": rt.DF_CONTAINERS, "volumes": rt.DF_VOLUMES}
READ_COLUMNS = {"containers": ("NAMES", "SIZE"), "volumes": ("VOLUME NAME", "SIZE")}
# A header's column names: words with single spaces between them; columns are two spaces apart
# or more.
COLUMN = re.compile(r"\S+(?: \S+)*")
SIZE = re.compile(rf"([0-9]+(?:\.[0-9]+)?)([{rt.SI_PREFIXES}]?)B")


def parse_size(text: str) -> int:
    """A size as Podman prints one, decimal SI with up to four significant digits, in bytes."""
    found = SIZE.fullmatch(text)
    if not found:
        raise ValueError(f"{text!r} is not a size")
    number, prefix = found.groups()
    power = rt.SI_PREFIXES.index(prefix) + 1 if prefix else 0
    return round(float(number) * rt.SI_STEP**power)


def _tables(text: str) -> dict[str, list[dict[str, str]]]:
    """Each table's rows, each a cell by its column's name; a row's cells are cut at the offsets
    of the header's columns, since a cell may hold spaces."""
    titles = {title: name for name, title in TABLES.items()}
    tables: dict[str, list[dict[str, str]]] = {}
    current, header = None, None
    for line in text.splitlines():
        if line.strip() in titles:
            current, header = titles[line.strip()], None
            tables[current] = []
            continue
        if current is None or not line.strip():
            continue
        if header is None:
            header = [(m.group(), m.start()) for m in COLUMN.finditer(line)]
            tables[current].append({"": ""})  # marks that the table has a header
            continue
        cells = {}
        for i, (name, start) in enumerate(header):
            end = header[i + 1][1] if i + 1 < len(header) else None
            cells[name] = line[start:end].strip()
        tables[current].append(cells)
    missing = set(TABLES) - set(tables)
    if missing:
        raise ValueError(f"the disk read has no table of {', '.join(sorted(missing))}")
    return tables


def read_headers(text: str) -> dict[str, list[str]]:
    """The columns of each table's header, in order: what a recording is held to."""
    _tables(text)  # every table is there
    titles = {title: name for name, title in TABLES.items()}
    found: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.strip() in titles:
            current = titles[line.strip()]
        elif current is not None and line.strip() and current not in found:
            found[current] = COLUMN.findall(line)
    return found


@dataclass(frozen=True)
class DiskReport:
    """The size of each container's own layer, by its name, and of each volume, by its name."""

    containers: dict[str, int]
    volumes: dict[str, int]


def read_disk(text: str) -> DiskReport:
    tables = _tables(text)
    read = {}
    for table, (key, size) in READ_COLUMNS.items():
        rows = tables[table]
        sizes = {}
        for row in rows:
            if row == {"": ""}:
                continue
            if key not in row or size not in row:
                raise ValueError(f"the disk read's {table} have no {key} or {size}")
            sizes[row[key]] = parse_size(row[size])
        read[table] = sizes
    return DiskReport(read["containers"], read["volumes"])


# --- podman container inspect -----------------------------------------------------------


@dataclass(frozen=True)
class Inspected:
    """What inspect said of a container: its labels, and the name of the volume at its /tmp."""

    labels: dict[str, str]
    tmp_volume: str | None


def read_inspected(text: str) -> dict[str, Inspected]:
    found = {}
    for entry in json.loads(text or "[]") or []:
        mounts = entry.get("Mounts") or []
        tmp = next(
            (
                m.get("Name")
                for m in mounts
                if m.get("Destination") == rt.TMP_MOUNT and m.get("Name")
            ),
            None,
        )
        labels = (entry.get("Config") or {}).get("Labels") or {}
        found[str(entry["Name"])] = Inspected(dict(labels), tmp)
    return found


def _ours(found: Inspected, workspace: int, person: int, install: str) -> bool:
    labels = found.labels
    return (
        labels.get(rt.LABEL_WORKSPACE) == str(workspace)
        and labels.get(rt.LABEL_PERSON) == str(person)
        and labels.get(rt.LABEL_INSTALL, install) == install
    )


def workspace_disk(
    workspace: int, person: int, install: str, disk: DiskReport, inspected: dict[str, Inspected]
) -> int | None:
    """A workspace's volume, the volume at its /tmp and its container's own layer, together;
    None where any of them is not known."""
    name = rt.container_name(workspace)
    found = inspected.get(name)
    if found is None or not _ours(found, workspace, person, install) or not found.tmp_volume:
        return None
    parts = (
        disk.volumes.get(name),
        disk.volumes.get(found.tmp_volume),
        disk.containers.get(name),
    )
    return None if None in parts else sum(parts)


# --- the collector ----------------------------------------------------------------------

# How often the loops look whether the workspaces are restored yet.
RESTORED_POLL_SECONDS = 1.0
# How many times a disk round reads: once, and once more where that failed.
DISK_ATTEMPTS = 2


async def wait_for(event: asyncio.Event, timeout: float | None) -> None:
    """Until ``event`` is set or ``timeout`` seconds have passed; None waits for the event."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(event.wait(), timeout)


def _utc_now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


@dataclass
class _Round:
    """A round's state: when it last collected, and why its last run failed, if it did."""

    collected_at: str | None = None
    error: str | None = None
    # What its figures are, by scope, as the last round that did not fail left them.
    figures: dict = field(default_factory=dict)


class Monitor:
    def __init__(
        self,
        runtime: rt.Runtime,
        database: Path,
        *,
        clock: Callable[[], float] = time.monotonic,
        wait: Callable[[asyncio.Event, float | None], Awaitable[None]] = wait_for,
        now: Callable[[], str] = _utc_now,
    ):
        self.runtime, self.database = runtime, database
        self.clock, self.wait, self.now = clock, wait, now
        self._lock = threading.Lock()
        # Held across a save's write and its taking up, so that two saves take effect in the order
        # they were stored.
        self._saving = threading.Lock()
        # What each loop last failed with outside its round, said once (``_looping``).
        self._loop_errors: dict[str, str | None] = {"usage": None, "disk": None}
        self._settings: MonitorSettings | None = None
        self.generation = 0
        # Set by ``halt``: a round under way ends, killing a disk read, and none starts.
        self.halted = threading.Event()
        self._storage_root: str | None = None
        self._storage_said = False
        self._usage = _Round()
        self._disk = _Round()
        # The readings the next usage round compares with: /proc/stat's, and each slice's, each
        # with the clock's time of it.
        self._stat: tuple[tuple[int, int, int], float] | None = None
        self._units: dict[str, tuple[UnitReading, float]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self.usage_wake: asyncio.Event | None = None
        self.disk_wake: asyncio.Event | None = None
        self.tasks: list[asyncio.Task] = []
        self._inflight: set[asyncio.Future] = set()

    # --- settings ---

    def settings(self) -> MonitorSettings:
        with self._lock:
            if self._settings is not None:
                return self._settings
        con = db.connect(self.database)
        try:
            read = read_settings(con)
        finally:
            con.close()
        with self._lock:
            if self._settings is None:
                self._settings = read
            return self._settings

    def save(self, settings: MonitorSettings) -> None:
        """Store the settings and take them up: ``changed``."""
        with self._saving:
            con = db.connect(self.database)
            try:
                write_settings(con, settings)
            finally:
                con.close()
            self.changed(settings)

    def changed(self, settings: MonitorSettings) -> None:
        """Take up new settings: a new generation, what was read of a scope now off dropped, and
        both loops woken to their new intervals. Any thread may call it."""
        with self._lock:
            self._settings = settings
            self.generation += 1
            self._drop_off(settings)
        if self._loop is not None:
            for event in (self.usage_wake, self.disk_wake):
                with contextlib.suppress(RuntimeError):  # the loop is closed: nothing to wake
                    self._loop.call_soon_threadsafe(event.set)

    def _drop_off(self, settings: MonitorSettings) -> None:
        """Drop what was read of a scope that is off, and the readings CPU is counted from, so
        that a scope switched on again knows no CPU until its second round; the lock is held."""
        for state in (self._usage, self._disk):
            for scope in SCOPES:
                if not getattr(settings, scope):
                    state.figures.pop(scope, None)
        if not settings.host:
            self._stat = None
        if not (settings.people or settings.containers):
            self._units = {}

    # --- reading ---

    def prepare(self) -> None:
        """Ask the runtime where Podman keeps its storage, once: the loop does, before a usage
        round, until it is known."""
        if self._storage_root is not None:
            return
        try:
            self._storage_root = self.runtime.storage_root()
        except Exception as error:  # noqa: BLE001 - asked again before the next round
            if not self._storage_said:
                log.warning("the monitor cannot tell where Podman's storage is: %s", error)
                self._storage_said = True

    def _rows(self) -> tuple[list[sqlite3.Row], list[sqlite3.Row], str]:
        con = db.connect(self.database)
        try:
            people = con.execute("select id, email from people order by email").fetchall()
            workspaces = con.execute(
                "select w.id, w.person, w.name, w.state, p.email from workspaces w "
                "join people p on p.id = w.person order by p.email, w.name, w.id"
            ).fetchall()
            return people, workspaces, db.install_id(con)
        finally:
            con.close()

    def _read_host(self) -> tuple[dict, tuple[tuple[int, int, int], float]]:
        stat = read_stat((PROC / "stat").read_text())
        at = self.clock()
        used, total = read_meminfo((PROC / "meminfo").read_text())
        tasks = read_loadavg((PROC / "loadavg").read_text())
        folders = [("database", self.database.parent), ("root", Path("/"))]
        if self._storage_root is not None:
            folders.append(("podman", Path(self._storage_root)))
        cpu = None if self._stat is None else host_cpu(self._stat[0], stat)
        return {
            "cpu_percent": cpu,
            "cpus": stat[2],
            "memory_used_bytes": used,
            "memory_total_bytes": total,
            "tasks": tasks,
            "disks": filesystems(folders),
        }, (stat, at)

    def usage_round(self) -> None:
        """Read the host and the slices of the scopes that are on, and keep what was read. Nothing
        is read with every scope off."""
        with self._lock:
            settings, generation = self._settings_now(), self.generation
        if not settings.usage_on or self.halted.is_set():
            return
        try:
            figures: dict = {}
            stat = None
            if settings.host:
                figures["host"], stat = self._read_host()
            units: dict[str, tuple[UnitReading, float]] = {}
            if settings.people or settings.containers:
                people, workspaces, _ = self._rows()
                slices = {}
                if settings.people:
                    slices.update(
                        {limits.person_slice(p["id"]): ("people", p["id"]) for p in people}
                    )
                if settings.containers:
                    slices.update(
                        {
                            limits.workspace_slice(w["person"], w["id"]): ("containers", w["id"])
                            for w in workspaces
                        }
                    )
                read = read_units(self.runtime.show_slices(list(slices))) if slices else {}
                at = self.clock()
                if settings.people:
                    figures["people"] = {}
                if settings.containers:
                    figures["containers"] = {}
                for unit, (scope, key) in slices.items():
                    reading = read.get(unit)
                    if reading is None:
                        figures[scope][key] = _usage_figures(None, None)
                        continue
                    figures[scope][key] = _usage_figures(
                        reading, unit_cpu(self._units.get(unit), (reading, at))
                    )
                    units[unit] = (reading, at)
        except Exception as error:  # noqa: BLE001 - kept, said once, and the next round tries
            self._failed("usage", error)
            return
        with self._lock:
            now = self._settings
            if stat is not None and now.host:
                self._stat = stat
            if (settings.people or settings.containers) and (now.people or now.containers):
                self._units = units
            self._keep(self._usage, figures, generation)
        self._succeeded("usage")

    def disk_round(self) -> None:
        """Read the disk of the workspaces, and keep what was read: once more where it fails, and
        the last figures read where it fails again. Nothing is read unless people or containers
        is on."""
        with self._lock:
            settings, generation = self._settings_now(), self.generation
        if not settings.disk_on or self.halted.is_set():
            return
        try:
            _, workspaces, install = self._rows()
            names = [rt.container_name(w["id"]) for w in workspaces]
            # Each of the two reads is tried once more where it failed; one that worked is kept.
            disk: DiskReport | None = None
            inspected: dict[str, Inspected] | None = None
            failure: Exception | None = None
            for _ in range(DISK_ATTEMPTS):
                if self.halted.is_set():
                    return
                failure = None
                try:
                    if disk is None:
                        disk = read_disk(self.runtime.disk_usage(self.halted))
                    if inspected is None:
                        inspected = read_inspected(self.runtime.inspect_containers(names))
                except Exception as error:  # noqa: BLE001 - tried once more
                    failure = error
                    continue
                break
            if self.halted.is_set():
                return
            if failure is not None:
                raise failure
            assert disk is not None and inspected is not None
            sizes = {
                w["id"]: workspace_disk(w["id"], w["person"], install, disk, inspected)
                for w in workspaces
            }
        except Exception as error:  # noqa: BLE001 - kept, said once, and the next round tries
            self._failed("disk", error)
            return
        figures = {scope: sizes for scope in ("people", "containers") if getattr(settings, scope)}
        with self._lock:
            self._keep(self._disk, figures, generation)
        self._succeeded("disk")

    def _settings_now(self) -> MonitorSettings:
        """The settings, read from the database the first time; the lock is held."""
        if self._settings is None:
            con = db.connect(self.database)
            try:
                self._settings = read_settings(con)
            finally:
                con.close()
        return self._settings

    def _keep(self, state: _Round, figures: dict, generation: int) -> None:
        """Keep a round's figures; the lock is held. Where the settings changed meanwhile, what
        it read of a scope that is now off is dropped."""
        if generation != self.generation:
            figures = {s: f for s, f in figures.items() if getattr(self._settings, s)}
        state.figures.update(figures)
        state.collected_at = self.now()

    def _failed(self, which: str, error: Exception) -> None:
        said = str(error) or type(error).__name__
        state = self._usage if which == "usage" else self._disk
        with self._lock:
            before, state.error = state.error, said
        if before != said:
            log.error("the monitor's %s round failed: %s", which, said)

    def _succeeded(self, which: str) -> None:
        state = self._usage if which == "usage" else self._disk
        with self._lock:
            before, state.error = state.error, None
        if before is not None:
            log.info("the monitor's %s round collected again", which)

    # --- the snapshot ---

    def snapshot(self) -> dict:
        """What is known now: the scopes, each round's time and failure, and the host, each
        person and each workspace container; a scope that is off is None."""
        people, workspaces, _ = self._rows()
        settings = self.settings()
        with self._lock:
            usage = {k: dict(v) for k, v in self._usage.figures.items()}
            disk = {k: dict(v) for k, v in self._disk.figures.items()}
            rounds = {
                name: {"collected_at": state.collected_at, "error": state.error}
                for name, state in (("usage", self._usage), ("disk", self._disk))
            }
        answer: dict = {"scopes": {s: getattr(settings, s) for s in SCOPES}, **rounds}
        answer["host"] = (usage.get("host") or _NO_HOST) if settings.host else None
        if settings.people:
            theirs: dict[int, list[int]] = {p["id"]: [] for p in people}
            for w in workspaces:
                theirs.setdefault(w["person"], []).append(w["id"])
            sizes = disk.get("people")
            answer["people"] = [
                {
                    "id": p["id"],
                    "email": p["email"],
                    "workspaces": len(theirs[p["id"]]),
                    **usage.get("people", {}).get(p["id"], _NO_USAGE),
                    "disk_bytes": _sum(theirs[p["id"]], sizes),
                }
                for p in people
            ]
        else:
            answer["people"] = None
        if settings.containers:
            sizes = disk.get("containers") or {}
            answer["containers"] = [
                {
                    "id": w["id"],
                    "name": w["name"],
                    "owner": w["email"],
                    "state": w["state"],
                    **usage.get("containers", {}).get(w["id"], _NO_USAGE),
                    "disk_bytes": sizes.get(w["id"]),
                }
                for w in workspaces
            ]
        else:
            answer["containers"] = None
        return answer

    # --- the loops ---

    def start(self, restored: threading.Event) -> None:
        """Start both loops on the running event loop: the usage round once ``restored`` is set,
        the disk round a whole disk interval after that."""
        self._loop = asyncio.get_running_loop()
        self.usage_wake, self.disk_wake = asyncio.Event(), asyncio.Event()
        self.tasks = [
            asyncio.create_task(self._looping("usage", self.usage_wake, restored)),
            asyncio.create_task(self._looping("disk", self.disk_wake, restored)),
        ]

    def halt(self) -> None:
        """From any thread: a round under way ends, killing a disk read, and none starts."""
        self.halted.set()

    async def stop(self) -> None:
        """Halt, end both loops, and wait for a round under way to end."""
        self.halt()
        for task in self.tasks:
            task.cancel()
        for task in self.tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._inflight:
            await asyncio.gather(*list(self._inflight), return_exceptions=True)

    @property
    def busy(self) -> bool:
        """Whether a round, or anything else a loop runs in a thread, is under way."""
        return bool(self._inflight)

    async def _in_thread(self, function: Callable[[], object]) -> object:
        future = asyncio.get_running_loop().run_in_executor(None, function)
        self._inflight.add(future)
        future.add_done_callback(self._inflight.discard)
        # A stop cancels the loop and not the round, which it waits for (``stop``).
        return await asyncio.shield(future)

    async def _looping(self, which: str, wake: asyncio.Event, restored: threading.Event) -> None:
        while not restored.is_set():
            await self.wait(wake, RESTORED_POLL_SECONDS)
            wake.clear()
        # The disk round's first is a whole interval away; the usage round's is now.
        last = self.clock() if which == "disk" else None
        while not self.halted.is_set():
            try:
                settings = await self._in_thread(self.settings)
                self._loop_errors[which] = None
                if which == "usage":
                    on, interval = settings.usage_on, settings.usage_interval_seconds
                else:
                    on, interval = settings.disk_on, settings.disk_interval_seconds
                timeout = None
                if on:
                    due = 0.0 if last is None else last + interval - self.clock()
                    if due <= 0:
                        if which == "usage":
                            if settings.host:  # where Podman's storage is: for the host's disks
                                await self._in_thread(self.prepare)
                            await self._in_thread(self.usage_round)
                        else:
                            await self._in_thread(self.disk_round)
                        last = self.clock()
                        continue
                    timeout = due
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - the loop goes on, and says it once
                said = f"{type(error).__name__}: {error}"
                if self._loop_errors[which] != said:
                    log.exception("the monitor's %s loop failed", which)
                    self._loop_errors[which] = said
                last = self.clock()
                timeout = RESTORED_POLL_SECONDS
            await self.wait(wake, timeout)
            wake.clear()


_NO_HOST = {
    "cpu_percent": None,
    "cpus": None,
    "memory_used_bytes": None,
    "memory_total_bytes": None,
    "tasks": None,
    "disks": [],
}
_NO_USAGE = {"cpu_percent": None, "memory_bytes": None, "tasks": None}


def _usage_figures(reading: UnitReading | None, cpu: float | None) -> dict:
    if reading is None:
        return dict(_NO_USAGE)
    return {"cpu_percent": cpu, "memory_bytes": reading.memory_bytes, "tasks": reading.tasks}


def _sum(workspaces: list[int], sizes: dict[int, int | None] | None) -> int | None:
    """A person's disk: the sum over their workspaces, None where any is not known; 0 for none."""
    if sizes is None:
        return None
    found = [sizes.get(w) for w in workspaces]
    return None if None in found else sum(found)
