"""The sizes the container suites run with on the tests' own engine (tests/podman_service.py),
which the runtime is given as constructor parameters (rcman/runtime.py INJECTED): a range of ids
per slot small enough that every slot the suites use fits in one user's subordinate ids, people
in the first slots and the builder after them, and the engine's own network.

Not a test module. tests/container/test_engine_in_podman.py holds them to the engine's map."""

# The ids of one slot, as runtime.RANGE is the product's.
TEST_RANGE = 8192
# The slots people have in the suites, and the builder's after the last of them.
PEOPLE_SLOTS = range(1, 6)
BUILDER_SLOT = 6
# A workspace's network on the engine: the engine's own, as in a workspace no slirp4netns runs.
TEST_NETWORK = "host"
