"""Limits on what a person's workspaces, and each workspace, may use: memory, CPU and processes.

A limit set is three numbers, each None where there is no limit. They are kept in SQLite (people.py,
workspaces.py) and applied as properties of systemd slices in rcman's user manager (runtime.py):
a slice for each person, ``person_slice``, and one for each of their workspaces beneath it,
``workspace_slice``; a workspace's container runs in the workspace's slice, so the kernel holds it
to its own limits and its person's. The slices are named by ids, never by slots: an id is never
given again, and a slot is.

Python standard library and the helpers only, so that the podman host's suite can import it with
the system interpreter. The values are read and refused with the rules of helpers/rules.py.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass

from helpers import rules
from helpers.rules import MIN_CPU_PERCENT, MIN_MEMORY_MIB, MIN_PIDS

__all__ = [
    "MIN_CPU_PERCENT",
    "MIN_MEMORY_MIB",
    "MIN_PIDS",
    "NONE",
    "PROPERTIES",
    "ROOT_SLICE",
    "Limits",
    "Refused",
    "Usage",
]

MEBIBYTE = 1024 * 1024
MIB_PER_GIB = 1024
PERCENT_PER_CORE = 100
# The word the command line uses for no limit.
NONE = "none"
# What a property is set to for no limit; systemd's own word for it.
INFINITY = "infinity"
# The slice every person's slice is beneath, because the dash in a slice's name puts the slice
# beneath the one named by the part before it.
ROOT_SLICE = "rcman.slice"
# The properties set on a slice, in the order they are set: always all of them, so that a limit
# taken away is taken away.
PROPERTIES = ("MemoryMax", "MemorySwapMax", "CPUQuota", "TasksMax")
# systemctl show prints this for a property of a unit that is not active.
NOT_SET = "[not set]"


class Refused(ValueError):
    """A value is not one a limit takes. ``field`` is memory, cpu or pids; the message is the
    sentence of its rule."""

    def __init__(self, field: str, sentence: str):
        super().__init__(sentence)
        self.field = field


# --- reading and showing ----------------------------------------------------------------


def parse_memory(text: str) -> int:
    """Memory as mebibytes, from a count followed by M or G (binary)."""
    if not rules.MEMORY.fullmatch(text):
        raise Refused("memory", rules.MEMORY.sentence)
    count = int(text[:-1])
    mebibytes = count * (MIB_PER_GIB if text[-1] in "Gg" else 1)
    if mebibytes < MIN_MEMORY_MIB:
        raise Refused("memory", rules.MEMORY.sentence)
    return mebibytes


def format_memory(mebibytes: int) -> str:
    """Memory in the largest unit that keeps it whole: what ``parse_memory`` reads back."""
    if mebibytes and mebibytes % MIB_PER_GIB == 0:
        return f"{mebibytes // MIB_PER_GIB}G"
    return f"{mebibytes}M"


def parse_cpu(text: str) -> int:
    """CPU as percent of one core (a whole core is ``PERCENT_PER_CORE``), from cores with up to
    ``rules.MAX_CPU_DECIMALS`` decimals."""
    if not rules.CPU.fullmatch(text):
        raise Refused("cpu", rules.CPU.sentence)
    whole, _, decimals = text.partition(".")
    places = len(str(PERCENT_PER_CORE)) - 1  # the decimals that make a percent
    percent = int(whole) * PERCENT_PER_CORE + int(decimals.ljust(places, "0") or 0)
    if percent < MIN_CPU_PERCENT:
        raise Refused("cpu", rules.CPU.sentence)
    return percent


def format_cpu(percent: int) -> str:
    """CPU as cores, without a trailing zero: what ``parse_cpu`` reads back."""
    whole, rest = divmod(percent, PERCENT_PER_CORE)
    places = len(str(PERCENT_PER_CORE)) - 1
    decimals = f"{rest:0{places}d}".rstrip("0")
    return f"{whole}.{decimals}" if decimals else str(whole)


def parse_pids(text: str) -> int:
    """Processes, threads counting too, as a whole number."""
    if not rules.PIDS.fullmatch(text):
        raise Refused("pids", rules.PIDS.sentence)
    count = int(text)
    if count < MIN_PIDS:
        raise Refused("pids", rules.PIDS.sentence)
    return count


def parse_option(text: str, parse: Callable[[str], int]) -> int | None:
    """A value of the command line: ``parse`` of it, or None for the word ``NONE``, in any case."""
    return None if text.lower() == NONE else parse(text)


@dataclass(frozen=True)
class Limits:
    """What may be used, None where nothing limits it: memory in mebibytes, CPU in percent of one
    core and processes (threads count)."""

    memory_mib: int | None = None
    cpu_percent: int | None = None
    pids: int | None = None

    @property
    def limited(self) -> bool:
        """Whether any of the three is a limit."""
        return not (self.memory_mib is None and self.cpu_percent is None and self.pids is None)

    def text(self) -> dict[str, str | None]:
        """The three as text the API shows and takes back, None for no limit."""
        return {
            "memory": None if self.memory_mib is None else format_memory(self.memory_mib),
            "cpu": None if self.cpu_percent is None else format_cpu(self.cpu_percent),
            "pids": None if self.pids is None else str(self.pids),
        }

    def describe(self) -> str:
        """The three in words, ``NONE`` for no limit: for the audit trail and the command line."""
        shown = {name: NONE if value is None else value for name, value in self.text().items()}
        return " ".join(f"{name}={value}" for name, value in shown.items())

    def properties(self) -> list[str]:
        """The ``NAME=value`` arguments of ``systemctl set-property``: always all of
        ``PROPERTIES``, each reset where there is no limit. A memory limit comes with no swap, so
        that it is real memory; CPUQuota is reset by an empty value."""
        memory = INFINITY if self.memory_mib is None else str(self.memory_mib * MEBIBYTE)
        return [
            f"MemoryMax={memory}",
            f"MemorySwapMax={INFINITY if self.memory_mib is None else 0}",
            f"CPUQuota={'' if self.cpu_percent is None else f'{self.cpu_percent}%'}",
            f"TasksMax={INFINITY if self.pids is None else self.pids}",
        ]


def parse(memory: str | None, cpu: str | None, pids: str | None) -> Limits:
    """A whole set from text, None for no limit; the first value that is wrong is a
    :class:`Refused` that names it."""
    return Limits(
        None if memory is None else parse_memory(memory),
        None if cpu is None else parse_cpu(cpu),
        None if pids is None else parse_pids(pids),
    )


# --- slices -----------------------------------------------------------------------------


def person_slice(person: int) -> str:
    """The slice of a person's workspaces, by the person's id, never their slot."""
    return f"{ROOT_SLICE.removesuffix('.slice')}-p{person}.slice"


def workspace_slice(person: int, workspace: int) -> str:
    """A workspace's slice, beneath its person's: the dash is what puts it there."""
    return f"{person_slice(person).removesuffix('.slice')}-w{workspace}.slice"


def slice_of(person: int, workspace: int | None) -> str:
    """The workspace's slice, or the person's where there is no workspace."""
    return person_slice(person) if workspace is None else workspace_slice(person, workspace)


# --- what is in use ---------------------------------------------------------------------


@dataclass(frozen=True)
class Usage:
    """What a slice and everything beneath it uses now: memory in bytes, swap included, and
    tasks. The memory is what the kernel counts for the slice, which includes the page cache,
    so it can be more than the programs in it hold. ``memory_known`` and ``tasks_known`` are
    False where the slice is active and the kernel counts no such thing (accounting is off):
    that is not none in use, and the numbers are then 0 and mean nothing."""

    memory_bytes: int = 0
    tasks: int = 0
    memory_known: bool = True
    tasks_known: bool = True

    def memory_text(self) -> str:
        """The memory in use in a unit a limit can be typed in, rounded up so that a limit typed
        from it is never below what is used."""
        return format_memory(math.ceil(self.memory_bytes / MEBIBYTE))


def _number(value: str, name: str) -> int:
    if not re.fullmatch(r"[0-9]+", value):
        raise ValueError(f"systemctl showed {name}={value}")
    return int(value)


def _shown(text: str) -> dict[str, str]:
    return {
        key: value.strip() for key, _, value in (line.partition("=") for line in text.splitlines())
    }


def read_usage(shown: str) -> Usage:
    """Usage from the output of ``systemctl show`` for ActiveState, MemoryCurrent,
    MemorySwapCurrent and TasksCurrent. A slice that is not active uses nothing; one that is
    active and shows ``NOT_SET`` is not counted, which is unknown and not zero. Output that
    lacks the state, the memory or the tasks, or holds something else, is a ValueError: usage
    not known is never taken for none."""
    found = _shown(shown)
    for name in ("ActiveState", "MemoryCurrent", "TasksCurrent"):
        if name not in found:
            raise ValueError(f"systemctl showed no {name}")
    if found["ActiveState"] in ("inactive", "failed"):
        return Usage()
    memory, tasks = found["MemoryCurrent"], found["TasksCurrent"]
    swap = found.get("MemorySwapCurrent", NOT_SET)
    return Usage(
        0
        if memory == NOT_SET
        else _number(memory, "MemoryCurrent")
        + (0 if swap == NOT_SET else _number(swap, "MemorySwapCurrent")),
        0 if tasks == NOT_SET else _number(tasks, "TasksCurrent"),
        memory_known=memory != NOT_SET,
        tasks_known=tasks != NOT_SET,
    )


@dataclass(frozen=True)
class Caps:
    """The caps a slice has now, as the unit manager holds them, which may not be the ones
    rcman's table holds: memory in bytes and tasks, None where there is no cap."""

    memory_bytes: int | None = None
    tasks: int | None = None


UNLIMITED = (INFINITY, NOT_SET)
# What the unit manager shows for a cap of the largest number there is (a 64-bit unsigned).
LARGEST = 2**64 - 1


def read_caps(shown: str) -> Caps:
    """Caps from the output of ``systemctl show`` for MemoryMax and TasksMax; ``infinity`` and
    ``NOT_SET`` are no cap. Output that lacks either or holds something else is a ValueError."""
    found = _shown(shown)
    values = []
    for name in ("MemoryMax", "TasksMax"):
        if name not in found:
            raise ValueError(f"systemctl showed no {name}")
        value = found[name]
        if value in UNLIMITED:
            values.append(None)
        else:
            number = _number(value, name)
            values.append(None if number >= LARGEST else number)
    return Caps(*values)


def lower_than(held: int, current: int | None) -> bool:
    """Whether a limit is lower than the cap there is; no cap is the highest."""
    return current is None or held < current


def memory_below(mebibytes: int, usage: Usage) -> bool:
    """Whether a memory limit is less than what is in use."""
    return mebibytes * MEBIBYTE < usage.memory_bytes


def pids_below(count: int, usage: Usage) -> bool:
    """Whether a limit on processes is less than what is in use."""
    return count < usage.tasks
