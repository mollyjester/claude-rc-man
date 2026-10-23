"""The monitor (backend/rcman/monitor.py): its readers, against recordings of the real programs
(tests/fakes/podman-monitor/, from Podman on the tests' engine), its two rounds on the host
runtime, what a round may cost on the Podman runtime, and its loops."""

import asyncio
import json
import logging
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

import pytest
from rcman import db, limits, monitor
from rcman import runtime as rt
from rcman.monitor import Monitor, MonitorSettings

from tests.conftest import ALICE, BOB, CAROL

RECORDED = Path(__file__).resolve().parent / "fakes" / "podman-monitor"
# The install label the recordings' containers carry (container-inspect.json).
RECORDED_INSTALL = "gate-install"
TMP_1 = "2fd7447977a57331e3248a5eb1bf73e0e0f2ab2f1618cae6e78ea026a96dd8ca"


def recorded(name: str) -> str:
    return (RECORDED / name).read_text()


# --- the host's own files -------------------------------------------------------------

STAT = (
    "cpu  100 0 50 800 50 0 0 0 0 0\n"
    "cpu0 50 0 25 400 25 0 0 0 0 0\n"
    "cpu1 50 0 25 400 25 0 0 0 0 0\n"
    "intr 12345\n"
    "ctxt 6789\n"
)
LATER = (
    "cpu  200 0 100 900 50 0 0 0 0 0\n"
    "cpu0 100 0 50 450 25 0 0 0 0 0\n"
    "cpu1 100 0 50 450 25 0 0 0 0 0\n"
)


def test_proc_stat_gives_the_busy_and_total_time_and_the_cpus():
    busy, total, cpus = monitor.read_stat(STAT)
    assert (busy, total, cpus) == (150, 1000, 2)  # idle and iowait are not busy


def test_host_cpu_is_a_percent_of_one_core_over_the_change():
    before, after = monitor.read_stat(STAT), monitor.read_stat(LATER)
    # 150 busy of 250 on two cores: 60% of the machine, 120% of one core
    assert monitor.host_cpu(before, after) == 120.0
    assert monitor.host_cpu(after, after) is None  # no time passed: not known
    assert monitor.host_cpu(after, before) is None  # the counters went back: not known


def test_a_proc_stat_without_its_cpu_line_is_refused():
    with pytest.raises(ValueError):
        monitor.read_stat("intr 1\nctxt 2\n")


def test_memory_in_use_is_the_total_less_what_is_available():
    text = "MemTotal:       16000 kB\nMemFree:         1000 kB\nMemAvailable:    6000 kB\n"
    assert monitor.read_meminfo(text) == (10000 * 1024, 16000 * 1024)
    with pytest.raises(ValueError):
        monitor.read_meminfo("MemTotal:       16000 kB\n")  # no MemAvailable: not known


def test_tasks_are_the_fourth_field_of_loadavg_which_counts_threads():
    assert monitor.read_loadavg("0.52 0.58 0.59 3/1290 12345\n") == 1290
    with pytest.raises(ValueError):
        monitor.read_loadavg("0.52 0.58 0.59\n")


def test_the_real_proc_files_are_read():
    """Positive control for the readers above: the machine's own files read without error."""
    busy, total, cpus = monitor.read_stat(Path("/proc/stat").read_text())
    assert 0 <= busy <= total and cpus >= 1
    used, whole = monitor.read_meminfo(Path("/proc/meminfo").read_text())
    assert 0 < used < whole
    assert monitor.read_loadavg(Path("/proc/loadavg").read_text()) >= 1


def test_folders_on_one_filesystem_are_one_entry_with_both_roles(tmp_path):
    inner = tmp_path / "inner"
    inner.mkdir()
    found = monitor.filesystems([("database", tmp_path), ("podman", inner)])
    assert len(found) == 1
    (one,) = found
    assert one["roles"] == ["database", "podman"] and one["path"] == str(tmp_path)
    assert 0 <= one["used_bytes"] <= one["size_bytes"] and one["available_bytes"] >= 0


def test_a_folder_that_is_not_there_is_listed_as_not_known(tmp_path):
    found = monitor.filesystems([("database", tmp_path), ("podman", tmp_path / "missing")])
    missing = next(f for f in found if f["roles"] == ["podman"])
    assert (missing["size_bytes"], missing["used_bytes"], missing["available_bytes"]) == (
        None,
        None,
        None,
    )


# --- the slices -------------------------------------------------------------------------


def shown(unit: str, **props: str) -> str:
    return "\n".join(f"{k}={v}" for k, v in {"Id": unit, **props}.items())


ACTIVE = {
    "ActiveState": "active",
    "MemoryCurrent": "1000",
    "MemorySwapCurrent": "24",
    "TasksCurrent": "7",
    "CPUUsageNSec": "5000000000",
}
INACTIVE = {
    "ActiveState": "inactive",
    "MemoryCurrent": limits.NOT_SET,
    "MemorySwapCurrent": limits.NOT_SET,
    "TasksCurrent": limits.NOT_SET,
    "CPUUsageNSec": limits.NOT_SET,
}


def test_blocks_are_matched_by_their_id_whatever_their_order():
    text = shown("b.slice", **INACTIVE) + "\n\n" + shown("a.slice", **ACTIVE) + "\n"
    found = monitor.read_units(text)
    assert found["a.slice"] == monitor.UnitReading(True, 1024, 7, 5_000_000_000)
    assert found["b.slice"] == monitor.UnitReading(False, 0, 0, None)


def test_a_malformed_block_makes_only_its_own_unit_unknown():
    bad = {**ACTIVE, "MemoryCurrent": "lots"}
    text = shown("a.slice", **ACTIVE) + "\n\n" + shown("b.slice", **bad) + "\n"
    found = monitor.read_units(text)
    assert found["a.slice"] is not None  # positive control
    assert found["b.slice"] is None
    no_state = {k: v for k, v in ACTIVE.items() if k != "ActiveState"}
    assert monitor.read_units(shown("c.slice", **no_state))["c.slice"] is None


def test_a_count_the_kernel_does_not_keep_is_not_known_and_not_zero():
    off = {**ACTIVE, "MemoryCurrent": limits.NOT_SET, "TasksCurrent": str(limits.LARGEST)}
    found = monitor.read_units(shown("a.slice", **off))["a.slice"]
    assert (found.memory_bytes, found.tasks, found.cpu_nsec) == (None, None, 5_000_000_000)


def reading(active=True, nsec=None):
    return monitor.UnitReading(active, 0, 0, nsec)


def test_unit_cpu_is_a_percent_of_one_core_over_the_seconds_between_rounds():
    assert (
        monitor.unit_cpu((reading(nsec=1_000_000_000), 10.0), (reading(nsec=4_000_000_000), 20.0))
        == 30.0
    )


@pytest.mark.parametrize(
    "before, after",
    [
        (None, reading(nsec=4)),  # the first round: nothing to compare with
        ((reading(nsec=9_000_000_000), 10.0), reading(nsec=1_000_000_000)),  # dropped
        ((reading(active=False), 10.0), reading(nsec=4_000_000_000)),  # was inactive
        ((reading(nsec=1), 10.0), reading(nsec=None)),  # not counted now
        ((reading(nsec=1), 10.0), reading(active=False)),  # stopped meanwhile
    ],
)
def test_unit_cpu_is_not_known_where_it_cannot_be_told(before, after):
    assert monitor.unit_cpu(before, (after, 20.0)) is None


def test_a_unit_inactive_in_both_rounds_used_no_cpu():
    assert monitor.unit_cpu((reading(active=False), 10.0), (reading(active=False), 20.0)) == 0.0


# --- podman system df -v ------------------------------------------------------------


@pytest.mark.parametrize(
    "text, size",
    [("26B", 26), ("0B", 0), ("11.39kB", 11390), ("8.104MB", 8_104_000), ("1MB", 1_000_000),
     ("161.4MB", 161_400_000), ("2.5GB", 2_500_000_000), ("1.2TB", 1_200_000_000_000)],
)  # fmt: skip
def test_sizes_are_decimal_si(text, size):
    assert monitor.parse_size(text) == size


@pytest.mark.parametrize("text", ["", "12", "1.5 MB", "1MiB", "MB", "-1B"])
def test_a_size_that_is_not_one_is_refused(text):
    with pytest.raises(ValueError):
        monitor.parse_size(text)


def test_the_recorded_disk_read_gives_each_container_and_volume_by_name():
    found = monitor.read_disk(recorded("df-v.txt"))
    assert found.containers == {"rcman-ws-1": 11_390}
    assert found.volumes == {"rcman-ws-1": 1_000_000, TMP_1: 26}


@pytest.mark.parametrize("name", ["df-v-two.txt", "df-v-100k.txt"])
def test_cells_with_spaces_are_cut_at_the_headers_offsets(name):
    """The second container's command holds spaces and is longer than the first's, and its
    CREATED holds spaces too: a split on spaces would take its size from the wrong column."""
    found = monitor.read_disk(recorded(name))
    assert set(found.containers) == {"rcman-ws-1", "rcman-ws-2"}
    assert found.containers["rcman-ws-2"] == 0 and found.containers["rcman-ws-1"] == 11_390
    assert found.volumes["rcman-ws-2"] == 0 and len(found.volumes) == 4


def test_a_disk_read_without_one_of_its_tables_is_refused():
    text = recorded("df-v.txt")
    cut = text.split("Local Volumes space usage:")[0]
    assert monitor.read_disk(text).volumes  # positive control
    with pytest.raises(ValueError):
        monitor.read_disk(cut)
    with pytest.raises(ValueError):
        monitor.read_disk("")


def test_a_table_whose_header_lacks_a_column_read_is_refused():
    text = recorded("df-v.txt").replace("NAMES", "LABELS")
    with pytest.raises(ValueError):
        monitor.read_disk(text)


def test_an_empty_table_is_no_rows():
    text = "Images space usage:\n\nContainers space usage:\n\nLocal Volumes space usage:\n\n"
    found = monitor.read_disk(text)
    assert found.containers == {} and found.volumes == {}


# --- podman container inspect -------------------------------------------------------


def test_the_recorded_inspect_gives_each_containers_labels_and_tmp_volume():
    found = monitor.read_inspected(recorded("container-inspect.json"))
    assert found == {
        "rcman-ws-1": monitor.Inspected(
            labels={
                "org.opencontainers.image.version": "24.04",
                rt.LABEL_INSTALL: RECORDED_INSTALL,
                rt.LABEL_PERSON: "1",
                rt.LABEL_WORKSPACE: "1",
            },
            tmp_volume=TMP_1,
        )
    }


def test_nothing_inspected_is_no_containers():
    assert monitor.read_inspected("") == {} and monitor.read_inspected("[]") == {}


# --- the settings -------------------------------------------------------------------


def test_the_settings_are_their_defaults_until_some_are_saved(settings):
    con = db.connect(settings.database)
    try:
        assert monitor.read_settings(con) == monitor.DEFAULTS
        saved = monitor.DEFAULTS.model_copy(update={"people": False, "usage_interval_seconds": 120})
        monitor.write_settings(con, saved)
        assert monitor.read_settings(con) == saved
        again = saved.model_copy(update={"people": True})
        monitor.write_settings(con, again)  # a second save replaces the first
        assert monitor.read_settings(con) == again
        assert con.execute("select count(*) from monitor_settings").fetchone()[0] == 1
    finally:
        con.close()


def test_the_defaults_are_within_their_bounds_and_every_scope_is_on():
    fields = MonitorSettings.model_fields
    for name in ("usage_interval_seconds", "disk_interval_seconds"):
        bounds = {type(m).__name__: m for m in fields[name].metadata}
        low, high = bounds["Ge"].ge, bounds["Le"].le
        assert low <= getattr(monitor.DEFAULTS, name) <= high
    assert monitor.DEFAULTS.host and monitor.DEFAULTS.people and monitor.DEFAULTS.containers


# --- the rounds, on the host runtime -----------------------------------------------


class Clock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now


def ids(settings) -> dict[str, int]:
    con = db.connect(settings.database)
    try:
        return {r["email"]: r["id"] for r in con.execute("select id, email from people")}
    finally:
        con.close()


def add_workspace(settings, person: int, name: str, state: str = "ready") -> int:
    con = db.connect(settings.database)
    try:
        cursor = con.execute(
            "insert into workspaces (person, name, state) values (?, ?, ?)", (person, name, state)
        )
        con.commit()
        return cursor.lastrowid
    finally:
        con.close()


def install_of(settings) -> str:
    con = db.connect(settings.database)
    try:
        return db.install_id(con)
    finally:
        con.close()


def save(settings, **changes) -> MonitorSettings:
    made = monitor.DEFAULTS.model_copy(update=changes)
    con = db.connect(settings.database)
    try:
        monitor.write_settings(con, made)
    finally:
        con.close()
    return made


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def made(settings, runtime, clock):
    return Monitor(runtime, settings.database, clock=clock, now=lambda: f"t{clock.now}")


def plant_usage(runtime, unit: str, nsec: int, memory: int = 1000, tasks: int = 3) -> None:
    runtime.slices_shown[unit] = {
        **ACTIVE,
        "MemoryCurrent": str(memory),
        "MemorySwapCurrent": "0",
        "TasksCurrent": str(tasks),
        "CPUUsageNSec": str(nsec),
    }


def by(rows: list[dict], key: str) -> dict:
    return {row[key]: row for row in rows}


def test_nothing_is_known_before_a_round(made, settings):
    snap = made.snapshot()
    assert snap["usage"] == {"collected_at": None, "error": None}
    assert snap["disk"] == {"collected_at": None, "error": None}
    assert snap["scopes"] == {"host": True, "people": True, "containers": True}
    assert snap["host"] == {
        "cpu_percent": None,
        "cpus": None,
        "memory_used_bytes": None,
        "memory_total_bytes": None,
        "tasks": None,
        "disks": [],
    }
    people = by(snap["people"], "email")
    assert set(people) == {ALICE, BOB, CAROL}
    assert people[ALICE] == {
        "id": ids(settings)[ALICE],
        "email": ALICE,
        "workspaces": 0,
        "cpu_percent": None,
        "memory_bytes": None,
        "tasks": None,
        "disk_bytes": None,
    }
    assert snap["containers"] == []


@pytest.fixture
def proc(tmp_path, monkeypatch):
    """The host's files, as the test writes them."""
    folder = tmp_path / "proc"
    folder.mkdir()
    (folder / "stat").write_text(STAT)
    (folder / "meminfo").write_text("MemTotal:  16000 kB\nMemAvailable:  6000 kB\n")
    (folder / "loadavg").write_text("0.52 0.58 0.59 3/1290 12345\n")
    monkeypatch.setattr(monitor, "PROC", folder)
    return folder


def test_a_usage_round_reads_the_host(made, clock, settings, runtime, proc):
    made.prepare()  # the loop's, before its rounds: where Podman keeps its storage
    made.usage_round()
    host = made.snapshot()["host"]
    assert host["cpu_percent"] is None  # one reading: nothing to compare with
    assert (host["cpus"], host["memory_used_bytes"], host["memory_total_bytes"]) == (
        2,
        10000 * 1024,
        16000 * 1024,
    )
    assert host["tasks"] == 1290
    roles = [role for disk in host["disks"] for role in disk["roles"]]
    assert sorted(roles) == ["database", "podman", "root"]  # the host runtime's folder for Podman's
    clock.now += 60
    (proc / "stat").write_text(LATER)
    made.usage_round()
    assert made.snapshot()["host"]["cpu_percent"] == 120.0


def test_host_cpu_is_not_known_again_after_the_host_is_switched_off_and_on(made, proc):
    made.usage_round()
    (proc / "stat").write_text(LATER)
    made.changed(made.settings().model_copy(update={"host": False}))
    made.changed(made.settings().model_copy(update={"host": True}))
    made.usage_round()
    assert made.snapshot()["host"]["cpu_percent"] is None
    (proc / "stat").write_text(STAT.replace("cpu  100 0 50 800", "cpu  300 0 150 1000"))
    made.usage_round()
    assert made.snapshot()["host"]["cpu_percent"] is not None  # positive control


def test_a_usage_round_reads_each_persons_slice_and_each_workspaces_in_one_call(
    made, clock, settings, runtime
):
    people = ids(settings)
    first = add_workspace(settings, people[BOB], "alpha")
    other = add_workspace(settings, people[CAROL], "beta")
    plant_usage(runtime, limits.person_slice(people[BOB]), 1_000_000_000, memory=5000, tasks=9)
    plant_usage(runtime, limits.workspace_slice(people[BOB], first), 1_000_000_000, memory=4000)
    made.usage_round()
    asked = [call for call in runtime.monitor_calls if call[0] == "show_slices"]
    assert len(asked) == 1
    assert set(asked[0][1]) == {
        *(limits.person_slice(p) for p in people.values()),
        limits.workspace_slice(people[BOB], first),
        limits.workspace_slice(people[CAROL], other),
    }
    plant_usage(runtime, limits.person_slice(people[BOB]), 4_000_000_000, memory=6000, tasks=9)
    plant_usage(runtime, limits.workspace_slice(people[BOB], first), 2_500_000_000, memory=4000)
    clock.now += 60
    made.usage_round()
    snap = made.snapshot()
    bob = by(snap["people"], "email")[BOB]
    assert (bob["cpu_percent"], bob["memory_bytes"], bob["tasks"], bob["workspaces"]) == (
        5.0,
        6000,
        9,
        1,
    )
    containers = by(snap["containers"], "id")
    assert containers[first]["cpu_percent"] == 2.5 and containers[first]["owner"] == BOB
    assert containers[first]["name"] == "alpha" and containers[first]["state"] == "ready"
    # a slice nothing ran in, in both rounds, used nothing
    assert containers[other]["cpu_percent"] == 0.0 and containers[other]["memory_bytes"] == 0
    assert snap["usage"]["error"] is None and snap["usage"]["collected_at"] is not None


@pytest.mark.parametrize("scope", ["people", "containers"])
def test_a_scope_that_is_off_is_not_read_and_not_shown(made, settings, runtime, scope):
    people = ids(settings)
    add_workspace(settings, people[BOB], "alpha")
    made.changed(made.settings().model_copy(update={scope: False}))
    made.usage_round()
    (asked,) = [call[1] for call in runtime.monitor_calls if call[0] == "show_slices"]
    persons = {limits.person_slice(p) for p in people.values()}
    if scope == "people":
        assert asked and not persons & set(asked)
    else:
        assert set(asked) == persons
    snap = made.snapshot()
    assert snap[scope] is None and snap["scopes"][scope] is False
    other = "containers" if scope == "people" else "people"
    assert snap[other]  # positive control


def test_with_every_scope_off_nothing_is_called(made, runtime):
    made.changed(
        made.settings().model_copy(update={"host": False, "people": False, "containers": False})
    )
    made.usage_round()
    made.disk_round()
    assert runtime.monitor_calls == []
    snap = made.snapshot()
    assert snap["host"] is None and snap["people"] is None and snap["containers"] is None
    assert snap["usage"]["collected_at"] is None and snap["disk"]["collected_at"] is None


def test_a_scope_switched_off_during_a_round_drops_what_the_round_read(made, settings, runtime):
    people = ids(settings)
    plant_usage(runtime, limits.person_slice(people[BOB]), 1)
    real = runtime.show_slices

    def switching(slices):
        made.changed(made.settings().model_copy(update={"people": False}))
        return real(slices)

    made.usage_round()  # positive control: people are known with the scope on
    assert by(made.snapshot()["people"], "email")[BOB]["memory_bytes"] == 1000
    runtime.show_slices = switching
    made.changed(made.settings().model_copy(update={"people": True}))
    made.usage_round()
    made.changed(made.settings().model_copy(update={"people": True}))
    # The round ran with people on and ended with them off: what it read of them is dropped,
    # and switching them back on shows nothing until a round reads them again.
    assert by(made.snapshot()["people"], "email")[BOB]["memory_bytes"] is None


def test_switching_a_scope_off_drops_what_was_read_of_it_at_once(made, settings, runtime):
    people = ids(settings)
    plant_usage(runtime, limits.person_slice(people[BOB]), 1)
    made.usage_round()
    assert by(made.snapshot()["people"], "email")[BOB]["memory_bytes"] == 1000
    made.changed(made.settings().model_copy(update={"people": False}))
    made.changed(made.settings().model_copy(update={"people": True}))
    assert by(made.snapshot()["people"], "email")[BOB]["memory_bytes"] is None


def plant_disk(runtime, settings) -> tuple[int, int, int]:
    """Two workspaces as the recordings hold them, ids 1 and 2, of a person with id 1 (alice):
    the recordings' labels and names. Returns alice's id and the two workspaces' ids."""
    people = ids(settings)
    assert people[ALICE] == 1  # the recordings' person
    first = add_workspace(settings, 1, "alpha")
    second = add_workspace(settings, 1, "beta")
    assert (first, second) == (1, 2)
    runtime.disk_report = recorded("df-v-two.txt")
    inspected = json.loads(recorded("container-inspect.json"))
    install = install_of(settings)
    for entry in inspected:
        entry["Config"]["Labels"][rt.LABEL_INSTALL] = install
    two = json.loads(json.dumps(inspected[0]))
    two["Name"] = "rcman-ws-2"
    two["Config"]["Labels"][rt.LABEL_WORKSPACE] = "2"
    two["Mounts"][0]["Name"] = "cd4c6df60da826565ecfe58ca9d1ce35f35be413effaf88376063e836f408117"
    runtime.inspect_report = json.dumps([*inspected, two])
    return 1, first, second


def test_a_disk_round_sums_each_workspaces_volume_tmp_and_container(made, settings, runtime):
    alice, first, second = plant_disk(runtime, settings)
    made.disk_round()
    snap = made.snapshot()
    containers = by(snap["containers"], "id")
    assert containers[first]["disk_bytes"] == 1_000_000 + 26 + 11_390
    assert containers[second]["disk_bytes"] == 0
    people = by(snap["people"], "email")
    assert people[ALICE]["disk_bytes"] == 1_000_000 + 26 + 11_390
    assert people[BOB]["disk_bytes"] == 0  # no workspace: nothing
    assert snap["disk"]["error"] is None and snap["disk"]["collected_at"] is not None
    calls = [call[0] for call in runtime.monitor_calls]
    assert calls == ["disk_usage", "inspect_containers"]
    asked = next(call[1] for call in runtime.monitor_calls if call[0] == "inspect_containers")
    assert list(asked) == [rt.container_name(first), rt.container_name(second)]


def test_a_container_of_another_install_or_workspace_is_not_known(made, settings, runtime):
    alice, first, second = plant_disk(runtime, settings)
    entries = json.loads(runtime.inspect_report)
    entries[0]["Config"]["Labels"][rt.LABEL_INSTALL] = "another"
    entries[1]["Config"]["Labels"][rt.LABEL_WORKSPACE] = "9"
    runtime.inspect_report = json.dumps(entries)
    made.disk_round()
    containers = by(made.snapshot()["containers"], "id")
    assert containers[first]["disk_bytes"] is None and containers[second]["disk_bytes"] is None
    assert by(made.snapshot()["people"], "email")[ALICE]["disk_bytes"] is None


def test_a_workspace_whose_container_is_gone_is_not_known_and_neither_is_its_persons_sum(
    made, settings, runtime
):
    alice, first, second = plant_disk(runtime, settings)
    runtime.inspect_report = json.dumps(json.loads(runtime.inspect_report)[:1])
    made.disk_round()
    containers = by(made.snapshot()["containers"], "id")
    assert containers[first]["disk_bytes"] == 1_011_416  # positive control
    assert containers[second]["disk_bytes"] is None
    assert by(made.snapshot()["people"], "email")[ALICE]["disk_bytes"] is None


def test_a_failed_disk_read_is_tried_once_more(made, settings, runtime):
    plant_disk(runtime, settings)
    runtime.disk_failures = [rt.RuntimeFailed(recorded("df-v-churn.stderr").strip())]
    made.disk_round()
    assert [c[0] for c in runtime.monitor_calls].count("disk_usage") == 2
    snap = made.snapshot()
    assert snap["disk"]["error"] is None and by(snap["containers"], "id")[1]["disk_bytes"]


def test_an_inspect_that_failed_is_tried_again_alone(made, settings, runtime):
    plant_disk(runtime, settings)
    real, failed = runtime.inspect_containers, []

    def failing_once(names):
        if not failed:
            failed.append(names)
            raise rt.RuntimeFailed("planted")
        return real(names)

    runtime.inspect_containers = failing_once
    made.disk_round()
    assert [c[0] for c in runtime.monitor_calls] == ["disk_usage", "inspect_containers"]
    assert failed  # the plant took effect
    assert made.snapshot()["disk"]["error"] is None


def test_a_disk_read_that_fails_twice_keeps_the_last_good_figures_and_says_why(
    made, settings, runtime, clock
):
    plant_disk(runtime, settings)
    made.disk_round()
    good = made.snapshot()
    said = recorded("df-v-bad.stderr").strip()
    runtime.disk_failures = [rt.RuntimeFailed(said), rt.RuntimeFailed(said)]
    clock.now += 3600
    made.disk_round()
    snap = made.snapshot()
    assert snap["disk"] == {"collected_at": good["disk"]["collected_at"], "error": said}
    assert snap["containers"] == good["containers"] and snap["people"] == good["people"]
    assert [c[0] for c in runtime.monitor_calls].count("disk_usage") == 3


def test_a_disk_round_runs_only_if_people_or_containers_is_on(made, settings, runtime):
    plant_disk(runtime, settings)
    made.changed(made.settings().model_copy(update={"people": False, "containers": False}))
    made.disk_round()
    assert runtime.monitor_calls == []
    made.changed(made.settings().model_copy(update={"containers": True}))
    made.disk_round()
    assert [c[0] for c in runtime.monitor_calls] == ["disk_usage", "inspect_containers"]


def test_a_failure_is_logged_when_it_starts_and_when_it_ends_and_not_in_between(
    made, settings, runtime, caplog
):
    people = ids(settings)
    plant_usage(runtime, limits.person_slice(people[BOB]), 1)
    runtime.limits_error = rt.RuntimeUnavailable("the user manager is not there")
    with caplog.at_level(logging.INFO, logger=monitor.log.name):
        made.usage_round()
        made.usage_round()
        assert made.snapshot()["usage"]["error"] == "the user manager is not there"
        runtime.limits_error = None
        made.usage_round()
        made.usage_round()
    said = [(r.levelname, r.getMessage()) for r in caplog.records if r.name == monitor.log.name]
    assert len(said) == 2, said
    assert said[0][0] == "ERROR" and "the user manager is not there" in said[0][1]
    assert said[1][0] == "INFO"
    assert made.snapshot()["usage"]["error"] is None


def test_a_failed_usage_round_keeps_the_last_good_figures(made, settings, runtime, clock):
    people = ids(settings)
    plant_usage(runtime, limits.person_slice(people[BOB]), 1)
    made.usage_round()
    good = made.snapshot()
    runtime.limits_error = rt.RuntimeFailed("no")
    clock.now += 60
    made.usage_round()
    snap = made.snapshot()
    assert snap["usage"] == {"collected_at": good["usage"]["collected_at"], "error": "no"}
    assert snap["people"] == good["people"] and snap["host"] == good["host"]


def test_the_storage_folder_is_asked_of_the_runtime_once(made, runtime):
    made.prepare()
    made.prepare()
    assert [c[0] for c in runtime.monitor_calls] == ["storage_root"]


# --- what a round costs, on the Podman runtime ----------------------------------------


class Programs:
    """Stands in for subprocess in the runtime: counts each program it is asked to run by its
    name and verb, and answers from the recordings."""

    def __init__(self, units: dict[str, dict[str, str]] | None = None, df_fails: int = 0):
        self.calls: list[list[str]] = []
        self.units = units or {}
        self.df_fails = df_fails

    def answer(self, argv: list[str]) -> tuple[int, bytes, bytes]:
        self.calls.append(argv)
        program = Path(argv[0]).name
        if program == "systemctl":
            asked = [a for a in argv if a.endswith(".slice")]
            blocks = [shown(u, **self.units.get(u, INACTIVE)) for u in asked]
            return 0, ("\n\n".join(blocks) + "\n").encode(), b""
        words = [a for a in argv[1:] if not a.startswith(("-", "unix:"))]
        if words[:2] == ["system", "df"]:
            if self.df_fails:
                self.df_fails -= 1
                return 125, b"", recorded("df-v-churn.stderr").encode()
            return 0, recorded("df-v.txt").encode(), b""
        if words[:2] == ["container", "inspect"]:
            return 0, recorded("container-inspect.json").encode(), b""
        if words[:1] == ["info"]:
            return 0, recorded("info.json").encode(), b""
        return 0, b"", b""

    def run(self, argv, *args, **kwargs):
        code, out, err = self.answer(list(argv))
        return subprocess.CompletedProcess(argv, code, out, err)

    def popen(self, argv, *args, **kwargs):
        programs = self

        class Process:
            def __init__(self):
                self.returncode, self.out, self.err = programs.answer(list(argv))
                self.pid = 0

            def communicate(self, input=None, timeout=None):
                return self.out, self.err

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

            def kill(self):
                pass

        return Process()

    def counted(self) -> dict[str, int]:
        """How many times each program ran: systemctl, and Podman by its verb."""
        found: dict[str, int] = {}
        for argv in self.calls:
            if Path(argv[0]).name == "systemctl":
                key = "systemctl"
            else:
                words = [a for a in argv[1:] if not a.startswith(("-", "unix:"))]
                key = "podman " + " ".join(words[:2])
            found[key] = found.get(key, 0) + 1
        return found


# What a round may cost: the most each program may run, and nothing else may.
USAGE_COST = {"systemctl": 1}
DISK_COST = {"podman system df": 1, "podman container inspect": 1}
DISK_COST_WITH_RETRY = {key: 2 * count for key, count in DISK_COST.items()}


def over_cost(counted: dict[str, int], allowed: dict[str, int]) -> dict[str, int]:
    """What ran more often than ``allowed`` lets it, or at all where it is not allowed."""
    return {key: n for key, n in counted.items() if n > allowed.get(key, 0)}


def podman_runtime(tmp_path, kind=rt.PodmanRuntime, **more) -> rt.PodmanRuntime:
    return kind(
        rt.PodmanConfig(
            socket=Path("/run/user/990/podman/podman.sock"),
            image="localhost/rcman-workspace:test",
            tools_dir=tmp_path / "tools",
            claude_store=tmp_path / "claude",
            **more,
        )
    )


def podman_monitor(tmp_path, settings, kind=rt.PodmanRuntime) -> Monitor:
    clock = Clock()
    return Monitor(
        podman_runtime(tmp_path, kind), settings.database, clock=clock, now=lambda: f"t{clock.now}"
    )


@pytest.fixture
def programs(monkeypatch):
    found = Programs()
    monkeypatch.setattr(subprocess, "run", found.run)
    monkeypatch.setattr(subprocess, "Popen", found.popen)
    return found


@pytest.fixture
def busy(settings):
    """Every person with two workspaces, so that a round that asks once per unit asks often."""
    for person in ids(settings).values():
        for name in ("one", "two"):
            add_workspace(settings, person, name)


def test_a_usage_round_runs_systemctl_once_and_podman_never(tmp_path, settings, programs, busy):
    made = podman_monitor(tmp_path, settings)
    made.usage_round()
    counted = programs.counted()
    assert counted == {"systemctl": 1}  # non-vacuity: it ran
    assert over_cost(counted, USAGE_COST) == {}
    assert made.snapshot()["usage"]["error"] is None


def test_a_disk_round_runs_one_df_and_one_inspect_and_no_systemctl(
    tmp_path, settings, programs, busy
):
    made = podman_monitor(tmp_path, settings)
    made.disk_round()
    counted = programs.counted()
    assert counted == {"podman system df": 1, "podman container inspect": 1}
    assert over_cost(counted, DISK_COST) == {}


def test_a_disk_round_that_tries_again_runs_at_most_one_more_of_each(
    tmp_path, settings, programs, busy
):
    programs.df_fails = 2
    made = podman_monitor(tmp_path, settings)
    made.disk_round()
    counted = programs.counted()
    assert counted == {"podman system df": 2}
    assert over_cost(counted, DISK_COST_WITH_RETRY) == {}
    assert made.snapshot()["disk"]["error"]


def test_with_every_scope_off_no_program_runs(tmp_path, settings, programs, busy):
    made = podman_monitor(tmp_path, settings)
    made.changed(
        made.settings().model_copy(update={"host": False, "people": False, "containers": False})
    )
    made.usage_round()
    made.disk_round()
    assert programs.calls == []
    made.changed(made.settings().model_copy(update={"people": True}))  # positive control
    made.usage_round()
    assert programs.counted() == {"systemctl": 1}


class OnePerUnit(rt.PodmanRuntime):
    """Planted: reads the slices with one systemctl each."""

    def show_slices(self, slices):
        return "\n\n".join(super(OnePerUnit, self).show_slices([unit]) for unit in slices)


def test_the_cost_guard_fails_a_round_that_runs_systemctl_once_per_unit(
    tmp_path, settings, programs, busy
):
    made = podman_monitor(tmp_path, settings, kind=OnePerUnit)
    made.usage_round()
    assert made.snapshot()["usage"]["error"] is None  # the plant reads as much as the real one
    assert programs.counted()["systemctl"] > 1  # the plant took effect
    assert over_cost(programs.counted(), USAGE_COST) == {
        "systemctl": programs.counted()["systemctl"]
    }


# --- the runtimes' readings -----------------------------------------------------------


def test_the_podman_runtime_asks_systemctl_for_every_slice_in_one_show(tmp_path, programs):
    runtime = podman_runtime(tmp_path)
    runtime.show_slices(["a.slice", "b.slice"])
    (argv,) = programs.calls
    assert argv[:3] == [runtime.config.systemctl, "--user", "show"]
    asked = [argv[i + 1] for i, a in enumerate(argv) if a == "-p"]
    assert asked == list(limits.MONITORED)
    assert argv[-2:] == ["a.slice", "b.slice"]


def test_the_podman_runtime_reads_the_storage_folder_from_podman_info(tmp_path, programs):
    runtime = podman_runtime(tmp_path)
    assert runtime.storage_root() == json.loads(recorded("info.json"))["store"]["graphRoot"]


def test_the_podman_runtime_takes_what_inspect_found_when_a_container_is_gone(
    tmp_path, monkeypatch
):
    def run(argv, *args, **kwargs):
        return subprocess.CompletedProcess(
            argv,
            125,
            recorded("container-inspect.json").encode(),
            b"Error: no such container rcman-ws-9\n",
        )

    monkeypatch.setattr(subprocess, "run", run)
    runtime = podman_runtime(tmp_path)
    found = monitor.read_inspected(runtime.inspect_containers(["rcman-ws-1", "rcman-ws-9"]))
    assert set(found) == {"rcman-ws-1"}


def test_the_podman_runtime_refuses_an_inspect_that_failed_otherwise(tmp_path, monkeypatch):
    def run(argv, *args, **kwargs):
        return subprocess.CompletedProcess(argv, 125, b"", b"Error: something else\n")

    monkeypatch.setattr(subprocess, "run", run)
    runtime = podman_runtime(tmp_path)
    with pytest.raises(rt.RuntimeFailed):
        runtime.inspect_containers(["rcman-ws-1"])


def test_the_podman_runtime_inspects_nothing_for_no_containers(tmp_path, programs):
    runtime = podman_runtime(tmp_path)
    assert monitor.read_inspected(runtime.inspect_containers([])) == {}
    assert programs.calls == []


def test_a_failed_disk_read_is_a_failure_with_what_podman_said(tmp_path, programs):
    programs.df_fails = 1
    runtime = podman_runtime(tmp_path)
    with pytest.raises(rt.RuntimeFailed, match="no such file or directory"):
        runtime.disk_usage(threading.Event())


def sleeping_podman(tmp_path: Path) -> tuple[Path, Path]:
    """A podman that writes its pid and then sleeps, as a ``system df`` that takes long."""
    pidfile = tmp_path / "df.pid"
    program = tmp_path / "podman"
    program.write_text(
        "#!/usr/bin/env python3\n"
        "import os\n"
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "os.execv('/bin/sleep', ['sleep', '600'])\n"
    )
    program.chmod(0o755)
    return program, pidfile


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except FileNotFoundError:
        return False
    return state != "Z"


def test_a_stop_kills_the_disk_read(tmp_path, settings):
    program, pidfile = sleeping_podman(tmp_path)
    runtime = podman_runtime(tmp_path, podman=str(program))
    made = Monitor(runtime, settings.database, clock=Clock())
    add_workspace(settings, ids(settings)[BOB], "alpha")
    worker = threading.Thread(target=made.disk_round)
    worker.start()
    deadline = time.monotonic() + 30
    while not (pidfile.exists() and pidfile.read_text()):
        assert time.monotonic() < deadline, "the disk read did not start"
        time.sleep(0.05)
    pid = int(pidfile.read_text())
    assert alive(pid)  # positive control
    started = time.monotonic()
    made.halt()
    worker.join(30)
    assert not worker.is_alive()
    assert time.monotonic() - started < rt.DISK_TIMEOUT / 4
    assert not alive(pid)
    assert pidfile.read_text() == str(pid)  # it was not run again after the stop


def test_a_disk_read_that_outlasts_its_time_is_killed_and_a_failure(tmp_path, monkeypatch):
    program, pidfile = sleeping_podman(tmp_path)
    monkeypatch.setattr(rt, "DISK_TIMEOUT", 0.5)
    runtime = podman_runtime(tmp_path, podman=str(program))
    with pytest.raises(rt.RuntimeFailed, match="in time"):
        runtime.disk_usage(threading.Event())
    assert not alive(int(pidfile.read_text()))


def test_the_host_runtime_records_what_it_is_asked_and_answers_what_is_planted(runtime):
    runtime.slices_shown["a.slice"] = ACTIVE
    found = monitor.read_units(runtime.show_slices(["a.slice", "b.slice"]))
    assert found["a.slice"].active and found["b.slice"] == monitor.UnitReading(False, 0, 0, None)
    runtime.disk_failures = [rt.RuntimeFailed("planted")]
    with pytest.raises(rt.RuntimeFailed, match="planted"):
        runtime.disk_usage(threading.Event())
    assert monitor.read_disk(runtime.disk_usage(threading.Event())).volumes == {}
    runtime.disk_report = recorded("df-v.txt")
    assert monitor.read_disk(runtime.disk_usage(threading.Event())).volumes  # what is planted
    assert runtime.storage_root() == str(runtime.root)
    assert [c[0] for c in runtime.monitor_calls] == [
        "show_slices",
        "disk_usage",
        "disk_usage",
        "disk_usage",
        "storage_root",
    ]


def test_the_host_runtimes_disk_read_is_made_from_its_workspaces(settings, runtime):
    """Where nothing is planted, the host runtime's disk read has each workspace's folder as its
    volume, in Podman's tables, so that the monitor shows the disk of the host runtime's
    workspaces (make e2e, the screenshots)."""
    alice = ids(settings)[ALICE]
    workspace = add_workspace(settings, alice, "alpha")
    name = rt.container_name(workspace)
    runtime.create(
        rt.Spec(name=name, slot=1, workspace=workspace, person=alice, install=install_of(settings))
    )
    held = Path(runtime.layout(name).work) / "data"
    held.write_bytes(b"x" * 5000)
    disk = monitor.read_disk(runtime.disk_usage(threading.Event()))
    assert disk.containers == {name: 0}
    assert disk.volumes[name] >= 5000 and disk.volumes[f"{name}-tmp"] == 0
    assert monitor.read_headers(runtime.disk_usage(threading.Event())) == monitor.read_headers(
        recorded("df-v.txt")
    )
    made = Monitor(runtime, settings.database)
    made.disk_round()
    (row,) = made.snapshot()["containers"]
    assert row["disk_bytes"] == disk.volumes[name]


@pytest.mark.parametrize("count, shown", [(26, "26B"), (999, "999B"), (11_390, "11.39kB"),
    (8_104_000, "8.104MB"), (999_999, "1MB"), (161_400_000, "161.4MB")])  # fmt: skip
def test_the_host_runtime_prints_sizes_as_podman_does(count, shown):
    assert rt.si_size(count) == shown
    assert monitor.parse_size(shown) == pytest.approx(count, rel=1e-3)


def test_a_loop_whose_settings_cannot_be_read_says_so_once(settings, runtime, caplog):
    made, times, rounds = looping(settings, runtime)
    restored = threading.Event()
    restored.set()

    def unreadable():
        raise sqlite3.OperationalError("planted")

    made.settings = unreadable

    async def scenario():
        made.start(restored)
        try:
            for _ in range(3):
                await parked(times)
                times.advance(monitor.RESTORED_POLL_SECONDS)
            await parked(times)
        finally:
            await made.stop()

    with caplog.at_level(logging.ERROR, logger=monitor.log.name):
        asyncio.run(scenario())
    said = [r for r in caplog.records if r.name == monitor.log.name]
    assert len([c for c in times.asked if c[1] == monitor.RESTORED_POLL_SECONDS]) >= 6
    assert len(said) == 2  # once for each loop
    assert rounds.ran == []


# --- the loops --------------------------------------------------------------------------


class Time:
    """Time as the loops see it, moved on only by the test: a clock and a wait that ends when
    its event is set or the clock passes its deadline. Each move (``advance``, ``poke``) is a
    new generation, and a loop is parked when it waits again having looked at the latest."""

    def __init__(self):
        self.now = 1000.0
        self.asked: list[tuple[asyncio.Event, float | None]] = []
        self._tick: asyncio.Event | None = None
        self.generation = 0
        self.seen: dict[int, int] = {}  # each waiting loop's task: the generation it looked at

    def clock(self) -> float:
        return self.now

    async def wait(self, event: asyncio.Event, timeout: float | None) -> None:
        self.asked.append((event, timeout))
        deadline = None if timeout is None else self.now + timeout
        me = id(asyncio.current_task())
        try:
            while True:
                self.seen[me] = self.generation
                if event.is_set() or (deadline is not None and self.now >= deadline):
                    return
                if self._tick is None:
                    self._tick = asyncio.Event()
                tick = self._tick
                waiting = [asyncio.create_task(event.wait()), asyncio.create_task(tick.wait())]
                try:
                    await asyncio.wait(waiting, return_when=asyncio.FIRST_COMPLETED)
                finally:
                    for task in waiting:
                        task.cancel()
        finally:
            self.seen.pop(me, None)

    def poke(self) -> None:
        """A new generation with no time passing: every waiting loop looks again."""
        self.generation += 1
        if self._tick is not None:
            self._tick.set()
            self._tick = None

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.poke()

    def parked(self, loops: int) -> bool:
        return len(self.seen) == loops and all(g == self.generation for g in self.seen.values())


async def until(condition, within: float = 10.0) -> None:
    deadline = time.monotonic() + within
    while not condition():
        assert time.monotonic() < deadline, "the condition did not come about"
        await asyncio.sleep(0.01)


async def parked(times: Time, loops: int = 2) -> None:
    """Until ``loops`` loops wait again having seen the latest move of time or the latest save:
    what they had to do then is done. A save wakes them by their events, so it is poked first."""
    times.poke()
    await until(lambda: times.parked(loops))


class Rounds:
    """Stands in for the two rounds: records when each ran, and holds one while asked to."""

    def __init__(self, times: Time):
        self.times = times
        self.ran: list[tuple[str, float]] = []
        self.hold = {"usage": threading.Event(), "disk": threading.Event()}
        for event in self.hold.values():
            event.set()
        self.running = {"usage": 0, "disk": 0}
        self.overlapped = False
        self._lock = threading.Lock()

    def make(self, which: str):
        def round_() -> None:
            with self._lock:
                self.running[which] += 1
                self.overlapped |= self.running[which] > 1
                self.ran.append((which, self.times.now))
            self.hold[which].wait(30)
            with self._lock:
                self.running[which] -= 1

        return round_

    def of(self, which: str) -> list[float]:
        return [at for name, at in self.ran if name == which]


def looping(settings, runtime, **changes):
    """A monitor on test time with stand-in rounds, its settings ``changes`` of the defaults."""
    save(settings, **changes)
    times = Time()
    made = Monitor(runtime, settings.database, clock=times.clock, wait=times.wait)
    rounds = Rounds(times)
    made.usage_round = rounds.make("usage")
    made.disk_round = rounds.make("disk")
    made.prepare = lambda: None
    return made, times, rounds


def test_the_first_usage_round_waits_for_the_restore_and_the_first_disk_round_a_whole_interval(
    settings, runtime
):
    made, times, rounds = looping(settings, runtime)
    restored = threading.Event()
    disk = monitor.DEFAULTS.disk_interval_seconds

    async def scenario():
        made.start(restored)
        try:
            for _ in range(3):
                times.advance(monitor.DEFAULTS.usage_interval_seconds)
                await parked(times)
            assert rounds.ran == []  # nothing before the restore
            restored.set()
            times.advance(1)
            await until(lambda: rounds.of("usage"))
            await parked(times)  # the round has ended: its interval counts from its end
            assert rounds.of("usage") == [times.now]
            assert rounds.of("disk") == []
            start = times.now
            times.advance(disk - 2)
            await parked(times)
            assert rounds.of("disk") == []
            times.advance(2)
            await until(lambda: rounds.of("disk"))
            assert rounds.of("disk")[0] >= start + disk - 1
        finally:
            await made.stop()

    asyncio.run(scenario())


def test_the_loops_run_apart_and_neither_overlaps_itself(settings, runtime):
    made, times, rounds = looping(settings, runtime, disk_interval_seconds=600)
    restored = threading.Event()
    restored.set()
    usage = monitor.DEFAULTS.usage_interval_seconds

    async def scenario():
        made.start(restored)
        try:
            await until(lambda: rounds.of("usage"))
            await parked(times)  # the round has ended: its interval counts from its end
            rounds.hold["usage"].clear()
            times.advance(usage)
            await until(lambda: len(rounds.of("usage")) == 2)  # this one is held
            for _ in range(4):  # time passes and the settings are saved: still the one round
                times.advance(usage)
                made.changed(made.settings())
                await parked(times, loops=1)  # the disk loop; the usage loop is in its round
            assert len(rounds.of("usage")) == 2
            times.advance(600)  # the disk loop runs while the usage loop is held
            await until(lambda: rounds.of("disk"))
            rounds.hold["usage"].set()
            await parked(times)
            assert len(rounds.of("usage")) == 2  # the next is due an interval after it ended
            times.advance(usage)
            await until(lambda: len(rounds.of("usage")) == 3)
            assert not rounds.overlapped
        finally:
            rounds.hold["usage"].set()
            await made.stop()

    asyncio.run(scenario())


def test_a_save_wakes_the_loops_to_the_new_interval_from_their_last_round(settings, runtime):
    made, times, rounds = looping(settings, runtime, usage_interval_seconds=3600)
    restored = threading.Event()
    restored.set()

    async def scenario():
        made.start(restored)
        try:
            await until(lambda: rounds.of("usage"))
            await parked(times)  # the round has ended: its interval counts from its end
            first = rounds.of("usage")[0]
            times.advance(100)
            await parked(times)
            assert len(rounds.of("usage")) == 1
            # 100 seconds after the last round, an interval of 60 is due at once: no time passes
            made.changed(made.settings().model_copy(update={"usage_interval_seconds": 60}))
            await until(lambda: len(rounds.of("usage")) == 2)
            assert rounds.of("usage")[1] == first + 100
        finally:
            await made.stop()

    asyncio.run(scenario())


def test_a_round_that_ends_late_counts_the_next_from_its_end_and_a_save_too(settings, runtime):
    """The interleaving the save test above once met by chance, held here on purpose: time passes
    while a round runs, so its interval, and a new one saved after, count from when it ended,
    not from when it started."""
    made, times, rounds = looping(settings, runtime, usage_interval_seconds=3600)
    restored = threading.Event()
    restored.set()
    rounds.hold["usage"].clear()

    async def scenario():
        made.start(restored)
        try:
            await until(lambda: rounds.of("usage"))  # under way, and held
            started = rounds.of("usage")[0]
            times.advance(100)
            await parked(times, loops=1)  # the disk loop; the usage loop is in its round
            rounds.hold["usage"].set()
            await parked(times)
            ended = times.now
            assert ended == started + 100
            made.changed(made.settings().model_copy(update={"usage_interval_seconds": 60}))
            await parked(times)
            assert len(rounds.of("usage")) == 1  # 60 from its start has passed, not from its end
            times.advance(60)
            await until(lambda: len(rounds.of("usage")) == 2)
            assert rounds.of("usage")[1] == ended + 60
        finally:
            rounds.hold["usage"].set()
            await made.stop()

    asyncio.run(scenario())


def test_switching_a_scope_does_not_move_when_the_next_round_is_due(settings, runtime):
    made, times, rounds = looping(settings, runtime)
    restored = threading.Event()
    restored.set()
    usage = monitor.DEFAULTS.usage_interval_seconds

    async def scenario():
        made.start(restored)
        try:
            await until(lambda: rounds.of("usage"))
            await parked(times)  # the round has ended: its interval counts from its end
            first = rounds.of("usage")[0]
            times.advance(usage / 2)
            made.changed(made.settings().model_copy(update={"host": False}))
            await parked(times)
            made.changed(made.settings().model_copy(update={"host": True}))
            await parked(times)
            assert len(rounds.of("usage")) == 1
            times.advance(usage / 2)
            await until(lambda: len(rounds.of("usage")) == 2)
            assert rounds.of("usage")[1] == first + usage
        finally:
            await made.stop()

    asyncio.run(scenario())


def test_with_every_scope_off_the_loops_wait_without_a_deadline(settings, runtime):
    made, times, rounds = looping(settings, runtime, host=False, people=False, containers=False)
    restored = threading.Event()
    restored.set()

    async def scenario():
        made.start(restored)
        try:
            await parked(times)
            times.advance(10 * monitor.DEFAULTS.disk_interval_seconds)
            await parked(times)
            assert rounds.ran == []
            assert all(timeout is None for _, timeout in times.asked[-2:])
            made.changed(made.settings().model_copy(update={"host": True}))
            await until(lambda: rounds.of("usage"))  # the usage loop's time had long come
            assert rounds.of("disk") == []  # host alone: no disk round
        finally:
            await made.stop()

    asyncio.run(scenario())


def test_a_stop_ends_both_loops_and_waits_for_a_round_under_way(settings, runtime):
    made, times, rounds = looping(settings, runtime)
    restored = threading.Event()
    restored.set()
    finished = []

    async def scenario():
        made.start(restored)
        rounds.hold["usage"].clear()
        await until(lambda: rounds.of("usage"))

        async def release():
            await asyncio.sleep(0.3)
            finished.append("released")
            rounds.hold["usage"].set()

        releasing = asyncio.create_task(release())
        await made.stop()
        finished.append("stopped")
        await releasing
        assert made.halted.is_set()
        assert all(task.done() for task in made.tasks)

    asyncio.run(scenario())
    assert finished == ["released", "stopped"]
    assert rounds.running == {"usage": 0, "disk": 0}


def test_a_round_that_raises_does_not_end_its_loop(settings, runtime, caplog):
    made, times, rounds = looping(settings, runtime)
    restored = threading.Event()
    restored.set()
    calls = []

    def failing():
        calls.append(times.now)
        raise RuntimeError("planted")

    made.usage_round = failing

    async def scenario():
        made.start(restored)
        try:
            await until(lambda: len(calls) == 1)
            times.advance(monitor.DEFAULTS.usage_interval_seconds)
            await until(lambda: len(calls) == 2)
        finally:
            await made.stop()

    with caplog.at_level(logging.ERROR, logger=monitor.log.name):
        asyncio.run(scenario())
    assert len(calls) == 2


# --- the snapshot is plain data -------------------------------------------------------


def test_the_snapshot_is_json(made, settings, runtime):
    plant_disk(runtime, settings)
    made.usage_round()
    made.disk_round()
    snap = made.snapshot()
    assert json.loads(json.dumps(snap)) == snap
    assert snap["host"]["disks"] and snap["containers"]  # non-vacuity
