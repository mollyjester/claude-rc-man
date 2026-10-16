"""Limits on memory, CPU and processes (backend/rcman/limits.py) against real rootless Podman and
the real systemd of the podman host.

``tests/test_container_suite.py`` runs this module there as the service account, once root has
made the tools and the store and has made what the installer makes for limits: the drop-ins
of ``ensure_limits`` (installer/install.py), read by systemd. Anywhere else it skips. What the
host-runtime tests and the stand-in systemctl could only assume is read here from the kernel's
own files: the cgroup of the workspace's container, and ``memory.max``, ``memory.swap.max``,
``cpu.max`` and ``pids.max`` of the person's slice, the workspace's slice, the container's scope
and the cgroup inside it.

The workspaces are rows made here and containers made by the runtime from the base image, so
that no image is built; nothing is made to run out of memory.
"""

import os
import subprocess
from pathlib import Path

import pytest
from rcman import db, limits, people
from rcman import runtime as rt
from rcman.limits import Limits, Usage
from rcman.problems import Problem
from rcman.settings import Settings
from rcman.workspaces import Workspaces

from installer import install as ins
from tests.container import test_runtime_in_podman as base

pytestmark = pytest.mark.skipif(
    os.environ.get("RCMAN_IN_CONTAINER") != "1" or "RCMAN_PODMAN_IMAGE" not in os.environ,
    reason="runs only as the service account inside the podman host",
)

EMAIL = "bob@example.com"
MEBIBYTE = 1024 * 1024
CGROUPS = Path("/sys/fs/cgroup")
# The files a limit is read from, in the order limits.PROPERTIES sets what they come from.
FILES = ("memory.max", "memory.swap.max", "cpu.max", "pids.max")
# What cpu.max holds for a quota of one whole core, and the period it is a quota of.
PERIOD = 100000
# What the kernel's memory and task counts may differ by between two reads of an idle container.
MEMORY_SLACK = 4 * MEBIBYTE
# Memory a test puts into a workspace's tmpfs, so that more is in use than any small limit.
FILL = 48 * MEBIBYTE


def uid() -> int:
    return os.getuid()


def manager() -> Path:
    """The cgroup of rcman's user manager, which every slice of limits.py is beneath."""
    return CGROUPS / "user.slice" / f"user-{uid()}.slice" / f"user@{uid()}.service"


def slice_dir(person: int, workspace: int | None = None) -> Path:
    """A person's slice beneath rcman's, or a workspace's beneath its person's."""
    root = manager() / limits.ROOT_SLICE / limits.person_slice(person)
    return root if workspace is None else root / limits.workspace_slice(person, workspace)


def read(directory: Path, name: str) -> str | None:
    """What a cgroup file holds, None for a file the cgroup does not have: cpu.max is there
    only while the CPU controller is on in the cgroup."""
    try:
        return (directory / name).read_text().strip()
    except FileNotFoundError:
        return None


def held(directory: Path) -> dict[str, str | None]:
    return {name: read(directory, name) for name in FILES}


def unlimited(directory: Path) -> bool:
    """Every limit file of the cgroup says there is no limit; a cpu.max the cgroup does not have
    says it too."""
    found = held(directory)
    cpu = found.pop("cpu.max")
    return all(v == "max" for v in found.values()) and (cpu is None or cpu.startswith("max "))


def cpu_max(percent: int) -> str:
    return f"{percent * PERIOD // limits.PERCENT_PER_CORE} {PERIOD}"


def expected(held_by: Limits) -> dict[str, str]:
    """What the four files hold for ``held_by``, a limit on memory coming with no swap."""
    assert held_by.limited
    return {
        "memory.max": "max" if held_by.memory_mib is None else str(held_by.memory_mib * MEBIBYTE),
        "memory.swap.max": "max" if held_by.memory_mib is None else "0",
        "cpu.max": "max 100000" if held_by.cpu_percent is None else cpu_max(held_by.cpu_percent),
        "pids.max": "max" if held_by.pids is None else str(held_by.pids),
    }


def cgroup_of(pid: int) -> Path:
    """The cgroup a process is in, as a path of the host's cgroup filesystem."""
    text = Path(f"/proc/{pid}/cgroup").read_text().strip().removeprefix("0::")
    return CGROUPS / text.lstrip("/")


def inspected(name: str) -> dict:
    return base.inspect(name)


def stored(service: Workspaces, who: people.Person) -> Limits:
    """The limits the table holds for the person."""
    con = db.connect(service.settings.database)
    try:
        return people.by_id(con, who.id).limits
    finally:
        con.close()


def process_cgroup(name: str) -> Path:
    return cgroup_of(inspected(name)["State"]["Pid"])


# --- the fixtures --------------------------------------------------------------------------


@pytest.fixture(scope="module")
def podman() -> rt.PodmanRuntime:
    return rt.PodmanRuntime(
        rt.PodmanConfig(
            socket=base.socket(),
            image=os.environ["RCMAN_PODMAN_IMAGE"],
            tools_dir=base.TOOLS,
            claude_store=base.CLAUDE,
        )
    )


@pytest.fixture(scope="module")
def person(tmp_path_factory) -> tuple[people.Person, Path]:
    """A person of a database of its own, in a slot the podman host's id range holds."""
    path = tmp_path_factory.mktemp("rcman") / "rcman.db"
    db.migrate(path)
    con = db.connect(path)
    try:
        return people.add(con, EMAIL, False), path
    finally:
        con.close()


@pytest.fixture(scope="module")
def service(person, podman):
    made = Workspaces(Settings(public_origin="https://rcman.test", database=person[1]), podman)
    yield made
    made.close()


def make_workspace(service: Workspaces, podman: rt.PodmanRuntime, who, name: str):
    """A ready workspace row, and its container made by the runtime in the workspace's slice."""
    con = db.connect(service.settings.database)
    try:
        cursor = con.execute(
            "insert into workspaces (person, name, state) values (?, ?, 'ready')", (who.id, name)
        )
        con.commit()
        row = cursor.lastrowid
    finally:
        con.close()
    made = service.get(row)
    podman.create(rt.Spec(made.container, who.slot, made.id, who.id))
    return made


@pytest.fixture(scope="module")
def workspaces(service, podman, person):
    """Two workspaces of the person, running; removed afterwards with their limits."""
    who = person[0]
    made = [make_workspace(service, podman, who, name) for name in ("first", "second")]
    try:
        yield made
    finally:
        for workspace in made:
            podman.remove(workspace.container)
        podman.remove_account(who.id)
        for workspace in made:
            podman.drop_limits(who.id, workspace.id)
        podman.drop_limits(who.id, None)


@pytest.fixture(autouse=True)
def nothing_limited(service, podman, person, workspaces):
    """Every test starts from, and leaves, no limit anywhere: on the slices and in the table."""
    yield
    who = person[0]
    for workspace in workspaces:
        podman.drop_limits(who.id, workspace.id)
        service._set(workspace.id, memory_mib=None, cpu_percent=None, pids=None)
    podman.drop_limits(who.id, None)
    con = db.connect(service.settings.database)
    try:
        people.set_limits(con, who.id, Limits())
    finally:
        con.close()


# --- no limit unless someone sets one ------------------------------------------------------


def test_a_workspaces_container_is_under_its_slices_and_nothing_in_the_chain_has_a_limit(
    workspaces, person
):
    """The installer's drop-ins are what make this so: without them the user slice and the
    scope both carry a limit on processes of systemd's own."""
    who = person[0]
    slice_file = ins.SYSTEMD / f"user-{uid()}.slice.d" / "rcman.conf"
    user_file = Path(ins.STATE) / ".config" / "systemd" / "user.conf.d" / "rcman.conf"
    assert slice_file.read_text() == ins.USER_SLICE_DROP_IN
    assert user_file.read_text() == ins.USER_MANAGER_DROP_IN
    shown = subprocess.run(
        ["systemctl", "--user", "show", "--property", "DefaultTasksMax", "--value"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert shown.stdout.strip() == limits.INFINITY
    assert read(CGROUPS / "user.slice" / f"user-{uid()}.slice", "pids.max") == "max"
    assert read(manager(), "pids.max") == "max"

    first = workspaces[0]
    leaf = process_cgroup(first.container)
    scope = leaf.parent
    assert leaf.name == "container"  # the cgroup Podman makes inside the scope
    assert scope.name == f"libpod-{inspected(first.container)['Id']}.scope"
    assert scope.parent == slice_dir(who.id, first.id)  # beneath the workspace's slice ...
    assert scope.parent.parent == slice_dir(who.id)  # ... beneath the person's ...
    assert scope.parent.parent.parent == manager() / limits.ROOT_SLICE  # ... beneath rcman's
    chain = [slice_dir(who.id), slice_dir(who.id, first.id), scope, leaf]
    for directory in chain:
        found = held(directory)
        assert found["memory.max"] == "max" == found["pids.max"], (directory, found)
        assert found["cpu.max"] == f"max {PERIOD}", (directory, found)
        assert unlimited(directory), (directory, found)


# --- limits set, changed and taken away while the container runs ----------------------------


def test_limits_set_on_a_workspace_and_a_person_change_the_kernels_files_at_once(
    service, podman, person, workspaces
):
    who = person[0]
    first = workspaces[0]
    own, theirs = slice_dir(who.id, first.id), slice_dir(who.id)
    scope = process_cgroup(first.container).parent
    pid = inspected(first.container)["State"]["Pid"]

    def still_running() -> None:
        assert podman.running(first.container)
        assert inspected(first.container)["State"]["Pid"] == pid

    assert unlimited(own) and unlimited(theirs)  # before
    first_set = Limits(memory_mib=512, cpu_percent=50, pids=400)
    service.set_limits(who, first.id, first_set)
    assert held(own) == expected(first_set)  # after set
    assert service.get(first.id).limits == first_set
    assert unlimited(theirs) and unlimited(scope) and unlimited(scope / "container")
    still_running()

    changed = Limits(memory_mib=768, cpu_percent=150, pids=600)
    service.set_limits(who, first.id, changed)
    assert held(own) == expected(changed)  # after change
    assert read(own, "cpu.max") == "150000 100000"
    still_running()

    persons = Limits(memory_mib=2048, cpu_percent=300, pids=2000)
    service.set_limits(who, None, persons)
    assert held(theirs) == expected(persons)
    assert held(own) == expected(changed)  # the person's limit is not copied down
    assert stored(service, who) == persons
    still_running()

    service.set_limits(who, first.id, Limits())  # after unset
    service.set_limits(who, None, Limits())
    assert unlimited(own) and unlimited(theirs)
    assert service.get(first.id).limits == Limits()
    still_running()

    # Only memory: the others are not touched, and a limit on memory is real memory.
    service.set_limits(who, first.id, Limits(memory_mib=1024))
    found = held(own)
    assert (found["memory.max"], found["memory.swap.max"], found["pids.max"]) == (
        str(1024 * MEBIBYTE), "0", "max"
    )  # fmt: skip
    assert found["cpu.max"] is None or found["cpu.max"].startswith("max ")
    still_running()


def test_dropping_the_limits_returns_a_slice_to_none_with_its_container_running(
    service, podman, person, workspaces
):
    """The runtime alone: it applies what it is given, and a revert takes it all off at once
    without ending what runs in the slice; a slice nothing ever configured reverts without a
    word."""
    who = person[0]
    first = workspaces[0]
    own = slice_dir(who.id, first.id)
    pid = inspected(first.container)["State"]["Pid"]
    given = Limits(memory_mib=512, cpu_percent=25, pids=300)
    podman.apply_limits(who.id, first.id, given)
    assert held(own) == expected(given)  # positive control: there is something to take away
    podman.drop_limits(who.id, first.id)
    assert unlimited(own)
    assert podman.running(first.container)
    assert inspected(first.container)["State"]["Pid"] == pid
    assert stored(service, who) == Limits()
    podman.drop_limits(who.id, 987)  # a workspace nothing ever configured
    podman.drop_limits(who.id, first.id)  # and one that is already clear


def test_usage_is_what_the_kernel_counts_for_a_slice_and_all_beneath_it(podman, person, workspaces):
    who = person[0]
    first, second = workspaces
    names = {
        "first": slice_dir(who.id, first.id),
        "second": slice_dir(who.id, second.id),
        "person": slice_dir(who.id),
    }

    def kernel(key: str) -> tuple[int, int]:
        directory = names[key]
        return int(read(directory, "memory.current")), int(read(directory, "pids.current"))

    asked = {
        "first": podman.usage(who.id, first.id),
        "second": podman.usage(who.id, second.id),
        "person": podman.usage(who.id, None),
    }
    for key, used in asked.items():
        memory, tasks = kernel(key)
        assert used.tasks == tasks, key
        assert abs(used.memory_bytes - memory) <= MEMORY_SLACK, key
        assert used.tasks > 0 and used.memory_bytes > 0  # non-vacuity: something runs there
    # A person's use includes their workspaces'.
    assert asked["person"].tasks == asked["first"].tasks + asked["second"].tasks
    assert (
        asked["person"].memory_bytes
        >= asked["first"].memory_bytes + asked["second"].memory_bytes - MEMORY_SLACK
    )
    # A slice nothing runs in, a configured one included, uses nothing.
    assert podman.usage(who.id, 988) == Usage()
    podman.apply_limits(who.id, 988, Limits(pids=limits.MIN_PIDS))
    try:
        assert read(slice_dir(who.id, 988), "pids.max") == str(limits.MIN_PIDS)
        assert podman.usage(who.id, 988) == Usage()
    finally:
        podman.drop_limits(who.id, 988)


def test_a_limit_below_what_is_in_use_is_refused_and_changes_nothing(
    service, podman, person, workspaces
):
    who = person[0]
    first = workspaces[0]
    own, theirs = slice_dir(who.id, first.id), slice_dir(who.id)
    layout = podman.layout(first.container)
    filled = podman.exec(
        first.container,
        [layout.shell, "-c", f"head -c {FILL} /dev/zero > {rt.RUN_MOUNT}/fill && sync"],
    )
    assert filled.code == 0, filled
    assert podman.usage(who.id, first.id).memory_bytes >= FILL  # the fill is counted
    start = Limits(memory_mib=1024, cpu_percent=50, pids=500)
    service.set_limits(who, first.id, start)
    service.set_limits(who, None, Limits(memory_mib=4096))
    before = (held(own), held(theirs), service.get(first.id).limits)

    def unchanged() -> None:
        assert (held(own), held(theirs), service.get(first.id).limits) == before
        assert stored(service, who) == Limits(memory_mib=4096)

    for target, held_by, what in (
        (first.id, Limits(memory_mib=16, cpu_percent=50, pids=500), "memory"),
        (None, Limits(memory_mib=16), "memory"),
        (first.id, Limits(memory_mib=1024, cpu_percent=50, pids=1), "pids"),
        (None, Limits(memory_mib=4096, pids=1), "pids"),
    ):
        with pytest.raises(Problem) as refused:
            service.set_limits(who, target, held_by)
        assert refused.value.code == "limit_below_use", (target, held_by)
        assert refused.value.extra["limit"] == what
        unchanged()
    # Positive control: what is refused is the lowering; raising is allowed.
    service.set_limits(who, first.id, Limits(memory_mib=2048, cpu_percent=50, pids=800))
    assert held(own)["memory.max"] == str(2048 * MEBIBYTE)


def test_deleting_a_workspace_takes_its_limits_off_its_slice(service, podman, person):
    """The slice stays once made, so a workspace of the same id made later must not find the
    limits of this one."""
    who = person[0]
    gone = make_workspace(service, podman, who, "doomed")
    own = slice_dir(who.id, gone.id)
    try:
        held_by = Limits(memory_mib=512, pids=300)
        service.set_limits(who, gone.id, held_by)
        assert held(own) == expected(held_by)  # positive control: there is something to take off
        service.delete(service.get(gone.id))
        assert service.get(gone.id) is None and not podman.exists(gone.container)
        assert own.is_dir() and unlimited(own)
    finally:
        podman.remove(gone.container)  # nothing, once it is deleted
        podman.drop_limits(who.id, gone.id)


# --- restore -------------------------------------------------------------------------------


def test_restore_applies_the_stored_limits_before_it_starts_a_container_in_the_slice(
    service, podman, person, workspaces, monkeypatch
):
    """The unit manager forgets a runtime property when the machine restarts (the install
    suite's restart checks that the real restart does); here the properties are taken off as
    that does, with the container stopped, and restore is run on the same table and Podman."""
    who = person[0]
    first = workspaces[0]
    own, theirs = slice_dir(who.id, first.id), slice_dir(who.id)
    first_set = Limits(memory_mib=640, cpu_percent=75, pids=300)
    persons = Limits(memory_mib=2048, cpu_percent=300, pids=2000)
    service.set_limits(who, first.id, first_set)
    service.set_limits(who, None, persons)
    assert held(own) == expected(first_set) and held(theirs) == expected(persons)
    podman.stop(first.container)
    podman.drop_limits(who.id, first.id)
    podman.drop_limits(who.id, None)
    assert unlimited(own) and unlimited(theirs)  # gone, as after a restart
    assert not podman.running(first.container)
    assert service.get(first.id).limits == first_set  # the table still holds them

    seen = []
    start = podman.start

    def watched(name: str) -> None:
        seen.append((name, held(own), held(theirs)))
        start(name)

    monkeypatch.setattr(podman, "start", watched)
    again = Workspaces(service.settings, podman)
    try:
        again.restore()
    finally:
        again.close()
    assert [name for name, _, _ in seen] == [first.container]  # the one that was stopped
    _, in_own, in_theirs = seen[0]
    assert in_own == expected(first_set)  # already in the kernel when its start was asked for
    assert in_theirs == expected(persons)
    assert podman.running(first.container)
    assert process_cgroup(first.container).parent.parent == own
    assert held(own) == expected(first_set) and held(theirs) == expected(persons)
