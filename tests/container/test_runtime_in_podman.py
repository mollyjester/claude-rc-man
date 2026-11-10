"""The Podman runtime (backend/rcman/runtime.py) against real rootless Podman: the tests'
engine (tests/podman_service.py), with the suites' sizes (tests/container/sizes.py), the tools
as the installer writes them, an empty Claude store and the default workspace image
(tests/container/conftest.py). The same contract the host runtime keeps
(tests/runtime_contract.py) runs here first. What image collection and the monitor read of
Podman's images is held here too, with the recordings the unit tests read of it.
"""

import pytest
from rcman import runtime as rt

from tests import runtime_contract
from tests.container import sizes

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


@pytest.fixture
def workspace(podman):
    made = spec(2)
    podman.create(made)
    try:
        yield made.name
    finally:
        podman.remove(made.name)
        podman.remove_account(made.person)


# --- a person's Claude sign-in --------------------------------------------------------


# --- a person's git account -----------------------------------------------------------


# --- what Claude is told: a file of the tools bound read-only at Claude Code's managed path ------
#
# These hold the bind create makes, of the file of the tools the workspace's image calls for: where
# the image has no /etc/claude-code, where the ids are swapped, over an image's own folder, and
# what a rename of the file on the host, as the installer writes it, comes to.


# --- what an image has at /etc/claude-code, as the image's probe reads it ------------------------


# --- what the monitor reads (backend/rcman/monitor.py) ---------------------------------------


# --- what image collection and the monitor's Storage read of Podman's images -----------------
