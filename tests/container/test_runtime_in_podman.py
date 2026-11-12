"""The Podman runtime (backend/rcman/runtime.py) against real rootless Podman: the tests'
engine (tests/podman_service.py), with the suites' sizes (tests/container/sizes.py), the tools
as the installer writes them, an empty Claude store and the default workspace image
(tests/container/conftest.py). The basic cases (``BASIC``) of the contract the host runtime
keeps (tests/runtime_contract.py) run here, and a workspace on the product's own network,
``rt.NETWORK``.

No test here holds the recordings of Podman the host tests read (tests/fakes/podman-monitor/,
tests/fakes/podman-images/, tests/fakes/podman-seccomp.json) to the real one: they are copies
of one point in time, recorded again when the product starts failing against real Podman.
"""

import os
import socket

import pytest
from rcman import runtime as rt

from installer import install as ins
from tests import runtime_contract
from tests.container import sizes
from tests.container.conftest import engine_runtime
from tests.container.test_nest_in_podman import tarball
from tests.hang_guard import CONTAINER_HANG_GUARD

# The workspace ids this module's own workspaces have, clear of the other modules'.
FIRST_WORKSPACE = 500


def spec(n: int) -> rt.Spec:
    """The n-th workspace a test makes, of a person of its own numbered as the workspace is, in
    one of the people's slots in turn: the engine holds a few slots, not every one rcman has."""
    slots = sizes.PEOPLE_SLOTS
    slot = slots[n % len(slots)]
    workspace = FIRST_WORKSPACE + n
    return rt.Spec(
        name=rt.container_name(workspace), slot=slot, workspace=workspace, person=workspace
    )


@pytest.fixture(scope="module", autouse=True)
def nothing_left(podman):
    """What the module made goes when it ends: the engine is the session's, and another module
    may give the same slot to a person of the same number. The contract's cases remove their
    workspaces but leave a person's account, as a person's deletion is what removes it."""
    yield
    for name, labels in podman.labelled().items():
        if int(labels[rt.LABEL_WORKSPACE]) >= FIRST_WORKSPACE:
            podman.remove(name)
    for n in range(1, len(runtime_contract.CASES) + 100):
        podman.remove_account(spec(n).person)
        podman.remove_account(spec(n).person + 100)  # the contract's other person of a slot


# The core: a workspace is made, runs and is removed, an exec answers, and its files outlive a stop.
BASIC = [
    runtime_contract.a_workspace_that_is_made_exists_runs_and_is_labelled,
    runtime_contract.an_exec_gets_its_input_and_answers_its_streams_and_code,
    runtime_contract.a_workspaces_files_outlive_a_stop,
]


@pytest.mark.parametrize("case", BASIC, ids=lambda case: case.__name__)
def test_podman_keeps_the_contract(podman, case):
    case(podman, spec)


# --- the product's network --------------------------------------------------------------------

# A connection from the workspace to the machine through the address pasta maps to the machine's
# own (--map-guest-addr, host.containers.internal) on each port it is given: "open" or "refused".
REACH = """
import socket, sys
for port in sys.argv[1:]:
    try:
        socket.create_connection(("169.254.1.2", int(port))).close()
        print("open")
    except ConnectionRefusedError:
        print("refused")
"""


def listener(host: str) -> socket.socket:
    found = socket.socket()
    found.bind((host, 0))
    found.listen()
    return found


def test_a_workspace_on_the_products_network_reaches_the_machine_but_not_its_loopback(
    podman_service, default_image, engine_tools, engine_store, engine_scratch
):
    """Its own network namespace; the machine's other addresses answer and its loopback refuses
    at once; and a container nested in it gets a network of its own from pasta."""
    runtime = engine_runtime(
        podman_service, default_image, engine_tools, engine_store, engine_scratch,
        network=rt.NETWORK,
    )  # fmt: skip
    made = spec(len(runtime_contract.CASES) + 50)
    anywhere, loopback = listener("0.0.0.0"), listener("127.0.0.1")
    try:
        runtime.create(made)
        work = runtime.layout(made.name).work
        own = runtime.exec(made.name, ["readlink", "/proc/self/ns/net"], cwd=work)
        assert own.code == 0 and own.out.decode().strip() != os.readlink("/proc/self/ns/net")
        ports = [str(s.getsockname()[1]) for s in (anywhere, loopback)]
        reached = runtime.exec(made.name, ["python3", "-c", REACH, *ports], cwd=work)
        assert (reached.code, reached.out.decode().split()) == (0, ["open", "refused"]), reached
        wrapper = f"{rt.TOOLS_MOUNT}/{ins.PODMAN_TOOLS}/bin/podman"
        image = "localhost/rcman-test-net"
        imported = runtime.exec(made.name, [wrapper, "import", "-", image], stdin=tarball(),
                                cwd=work, timeout=CONTAINER_HANG_GUARD)  # fmt: skip
        assert imported.code == 0, imported
        nested = runtime.exec(
            made.name,
            [wrapper, "run", "--rm", "--pull=never", "--network=pasta", image, "busybox", "true"],
            cwd=work, timeout=CONTAINER_HANG_GUARD,
        )  # fmt: skip
        assert nested.code == 0, nested  # it runs only once pasta has started
    finally:
        runtime.remove(made.name)
        runtime.remove_account(made.person)
        anywhere.close()
        loopback.close()
