"""The monitor (backend/rcman/monitor.py): its readers, against recordings of the real programs
(tests/fakes/podman-monitor/, from Podman on the tests' engine), its two rounds on the host
runtime, what a round may cost on the Podman runtime, and its loops."""

import asyncio
import json
import subprocess
import threading
from pathlib import Path

import pytest
from rcman import db, limits, monitor
from rcman import runtime as rt
from rcman.monitor import Monitor, MonitorSettings

from tests.conftest import ALICE, BOB, CAROL
from tests.time_doubles import Clock, Time
from tests.until import until_async

RECORDED = Path(__file__).resolve().parent / "fakes" / "podman-monitor"
# The install label the recordings' containers carry (container-inspect.json).
RECORDED_INSTALL = "gate-install"
TMP_1 = "68e71514a7e593fa2feb8c0eb263131212eb9de122d05f037e4764f2251ae01b"


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


def test_host_cpu_is_a_percent_of_one_core_over_the_change():
    before, after = monitor.read_stat(STAT), monitor.read_stat(LATER)
    # 150 busy of 250 on two cores: 60% of the machine, 120% of one core
    assert monitor.host_cpu(before, after) == 120.0


def test_memory_in_use_is_the_total_less_what_is_available():
    text = "MemTotal:       16000 kB\nMemFree:         1000 kB\nMemAvailable:    6000 kB\n"
    assert monitor.read_meminfo(text) == (10000 * 1024, 16000 * 1024)


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


def reading(nsec: int) -> monitor.UnitReading:
    return monitor.UnitReading(True, 0, 0, nsec)


def test_unit_cpu_is_a_percent_of_one_core_over_the_seconds_between_rounds():
    assert (
        monitor.unit_cpu((reading(nsec=1_000_000_000), 10.0), (reading(nsec=4_000_000_000), 20.0))
        == 30.0
    )


# --- podman system df -v ------------------------------------------------------------


def test_the_recorded_disk_read_gives_each_container_and_volume_by_name():
    found = monitor.read_disk(recorded("df-v.txt"))
    assert found.containers == {"rcman-ws-1": 11_310}
    assert found.volumes == {"rcman-ws-1": 1_000_000, TMP_1: 26}


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


# --- the rounds, on the host runtime -----------------------------------------------


def ids(settings) -> dict[str, int]:
    con = db.connect(settings.database)
    try:
        return {r["email"]: r["id"] for r in con.execute("select id, email from people")}
    finally:
        con.close()


def add_workspace(settings, person: int, name: str) -> int:
    con = db.connect(settings.database)
    try:
        cursor = con.execute(
            "insert into workspaces (person, name, state) values (?, ?, 'ready')", (person, name)
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


# What a disk round asks the runtime for rcman's own use, with Storage on, after its two reads.
STORAGE_CALLS = ["disk_summary", "claude_versions_bytes"]
STORAGE_PARTS = (
    "images_bytes",
    "workspace_volumes_bytes",
    "other_volumes_bytes",
    "claude_versions_bytes",
    "total_bytes",
)


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
    tmp = next(m for m in two["Mounts"] if m["Destination"] == rt.TMP_MOUNT)
    tmp["Name"] = "d115a20ba81bd01574fed823df56355d1084177687b8fdcba8ee35bb6ee13b67"
    runtime.inspect_report = json.dumps([*inspected, two])
    return 1, first, second


def test_a_disk_round_sums_each_workspaces_volume_tmp_and_container(made, settings, runtime):
    _, first, second = plant_disk(runtime, settings)
    made.disk_round()
    snap = made.snapshot()
    containers = by(snap["containers"], "id")
    assert containers[first]["disk_bytes"] == 1_000_000 + 26 + 11_310
    assert containers[second]["disk_bytes"] == 0
    people = by(snap["people"], "email")
    assert people[ALICE]["disk_bytes"] == 1_000_000 + 26 + 11_310
    assert people[BOB]["disk_bytes"] == 0  # no workspace: nothing
    assert snap["disk"]["error"] is None and snap["disk"]["collected_at"] is not None
    calls = [call[0] for call in runtime.monitor_calls]
    assert calls == ["disk_usage", "inspect_containers", *STORAGE_CALLS]
    asked = next(call[1] for call in runtime.monitor_calls if call[0] == "inspect_containers")
    assert list(asked) == [rt.container_name(first), rt.container_name(second)]


def with_volume(text: str, name: str, size: str) -> str:
    """A disk read with one more volume, its cells at the offsets of its table's header."""
    header = next(line for line in text.splitlines() if line.startswith("VOLUME NAME"))
    links, at = header.index("LINKS"), header.index("SIZE")
    return text.rstrip("\n") + f"\n{name:<{links}}{'1':<{at - links}}{size}\n"


def test_a_disk_round_reads_rcmans_own_use_by_part(made, settings, runtime):
    plant_disk(runtime, settings)
    runtime.disk_report = with_volume(runtime.disk_report, "a-persons-volume", "5MB")
    runtime.summary_report = recorded("df-summary.json")
    assert made.snapshot()["storage"] == dict.fromkeys(STORAGE_PARTS)  # before a round
    made.disk_round()
    storage = made.snapshot()["storage"]
    versions = storage["claude_versions_bytes"]
    assert storage == {
        "images_bytes": 88_807_392,  # the summary's Images RawSize
        # rcman-ws-1 and rcman-ws-2, and the volumes at their /tmp
        "workspace_volumes_bytes": 1_000_000 + 26 + 0 + 0,
        "other_volumes_bytes": 5_000_000,
        "claude_versions_bytes": versions,
        "total_bytes": 88_807_392 + 1_000_026 + 5_000_000 + versions,
    }
    planted = runtime.claude_store / "versions" / "planted"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_bytes(b"x" * 5000)
    made.disk_round()
    assert made.snapshot()["storage"]["claude_versions_bytes"] == versions + 5000
    # Storage off: none of it is read or shown, and the round reads the rest
    runtime.monitor_calls.clear()
    made.changed(made.settings().model_copy(update={"storage": False}))
    made.disk_round()
    snap = made.snapshot()
    assert snap["storage"] is None and snap["scopes"]["storage"] is False
    assert [c[0] for c in runtime.monitor_calls] == ["disk_usage", "inspect_containers"]
    assert by(snap["containers"], "id")[1]["disk_bytes"]  # positive control: the round ran


# --- what a round costs, on the Podman runtime ----------------------------------------


class Programs:
    """Stands in for subprocess in the runtime: counts each program it is asked to run by its
    name and verb, and answers from the recordings."""

    def __init__(self):
        self.calls: list[list[str]] = []

    def answer(self, argv: list[str]) -> tuple[int, bytes, bytes]:
        self.calls.append(argv)
        program = Path(argv[0]).name
        if program == "systemctl":
            asked = [a for a in argv if a.endswith(".slice")]
            blocks = [shown(u, **INACTIVE) for u in asked]
            return 0, ("\n\n".join(blocks) + "\n").encode(), b""
        words = [a for a in argv[1:] if not a.startswith(("-", "unix:"))]
        if words[:2] == ["system", "df"] and "--format" in argv:
            return 0, recorded("df-summary.json").encode(), b""
        if words[:2] == ["system", "df"]:
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
        """How many times each program ran: systemctl, and Podman by its verb, ``system df`` by
        whether it is the verbose read or the summary."""
        found: dict[str, int] = {}
        for argv in self.calls:
            if Path(argv[0]).name == "systemctl":
                key = "systemctl"
            else:
                words = [a for a in argv[1:] if not a.startswith(("-", "unix:"))]
                key = "podman " + " ".join(words[:2])
                if words[:2] == ["system", "df"]:
                    key += " --format" if "--format" in argv else " --verbose"
            found[key] = found.get(key, 0) + 1
        return found


# What a round may cost: the most each program may run, and nothing else may.
USAGE_COST = {"systemctl": 1}


DISK_COST = {
    "podman system df --verbose": 1,
    "podman container inspect": 1,
    "podman system df --format": 1,
}


def over_cost(counted: dict[str, int], allowed: dict[str, int]) -> dict[str, int]:
    """What ran more often than ``allowed`` lets it, or at all where it is not allowed."""
    return {key: n for key, n in counted.items() if n > allowed.get(key, 0)}


def podman_runtime(tmp_path, kind) -> rt.PodmanRuntime:
    return kind(
        rt.PodmanConfig(
            socket=Path("/run/user/990/podman/podman.sock"),
            image="localhost/rcman-workspace:test",
            tools_dir=tmp_path / "tools",
            claude_store=tmp_path / "claude",
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
    assert counted == DISK_COST  # non-vacuity: each ran
    assert over_cost(counted, DISK_COST) == {}
    assert made.snapshot()["storage"]["images_bytes"] == 88_807_392  # the summary was read


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


def test_the_podman_runtime_reads_the_storage_folder_from_podman_info(tmp_path, programs):
    runtime = podman_runtime(tmp_path, rt.PodmanRuntime)
    assert runtime.storage_root() == json.loads(recorded("info.json"))["store"]["graphRoot"]


# --- the loops --------------------------------------------------------------------------


async def parked(times: Time) -> None:
    """Until both loops wait again having seen the latest move of time or the latest save:
    what they had to do then is done. A save wakes them by their events, so it is poked first."""
    times.poke()
    await until_async(lambda: times.parked(2))


class Rounds:
    """Stands in for the two rounds: records when each ran."""

    def __init__(self, times: Time):
        self.times = times
        self.ran: list[tuple[str, float]] = []
        self._lock = threading.Lock()

    def make(self, which: str):
        def round_() -> None:
            with self._lock:
                self.ran.append((which, self.times.now))

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
    disk = monitor.DEFAULTS.disk_interval_seconds

    async def scenario():
        made.start()
        try:
            for _ in range(3):
                times.advance(monitor.DEFAULTS.usage_interval_seconds)
                await parked(times)
            assert rounds.ran == []  # nothing before the restore
            made.restored_now()
            await until_async(lambda: rounds.of("usage"))
            await parked(times)  # the round has ended: its interval counts from its end
            assert rounds.of("usage") == [times.now]
            assert rounds.of("disk") == []
            start = times.now
            times.advance(disk - 2)
            await parked(times)
            assert rounds.of("disk") == []
            times.advance(2)
            await until_async(lambda: rounds.of("disk"))
            assert rounds.of("disk")[0] >= start + disk - 1
        finally:
            await made.stop()

    asyncio.run(scenario())


def test_a_save_wakes_the_loops_to_the_new_interval_from_their_last_round(settings, runtime):
    made, times, rounds = looping(settings, runtime, usage_interval_seconds=3600)

    async def scenario():
        made.start()
        made.restored_now()
        try:
            await until_async(lambda: rounds.of("usage"))
            await parked(times)  # the round has ended: its interval counts from its end
            first = rounds.of("usage")[0]
            times.advance(100)
            await parked(times)
            assert len(rounds.of("usage")) == 1
            # 100 seconds after the last round, an interval of 60 is due at once: no time passes
            made.changed(made.settings().model_copy(update={"usage_interval_seconds": 60}))
            await until_async(lambda: len(rounds.of("usage")) == 2)
            assert rounds.of("usage")[1] == first + 100
        finally:
            await made.stop()

    asyncio.run(scenario())
