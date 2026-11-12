# Tests

How the test suite is organised and run. The rules the suite follows are in
[working-rules.md](working-rules.md).

## Running

| Command | What it does |
|---|---|
| `make setup` | Creates the development environment: `make frontend-setup`, then `uv sync` |
| `make test` | Runs every Python test that runs on the host, in parallel with pytest-xdist: all but those marked `container`, those marked `benchmark`, which `make benchmark` runs, and those marked `real_claude`, which no target runs. `TEST_WORKERS` (in the `Makefile`, which says why) is the number of workers; `--dist worksteal` gives an idle worker the queue of a busy one |
| `make test-container` | On demand: runs the Python tests marked `container`, in one process; the `Makefile` says why. It sets `RCMAN_REQUIRE_CONTAINER=1`, so they fail instead of skipping where the user's rootless Podman cannot run. When the working rules require it, and before the live reinstall |
| `make benchmark` | On demand, and not a check: runs the tests marked `benchmark`, which measure and report, in one process, printing what each reports; each fails only when it cannot measure. No other target runs them; see [The benchmark](#the-benchmark) |
| `make lint` | Checks formatting and lint: ruff for the Python code, Biome for the frontend, with the frontend's packages `make frontend-setup` installs |
| `make format` | Applies formatting and safe lint fixes: ruff for the Python code, Biome for the frontend |
| `make frontend-setup` | Installs the frontend's packages and Playwright's Chromium, which `make screenshot` drives |
| `make frontend-test` | Type-checks the frontend and runs its component tests |
| `make frontend-build` | Builds the frontend into `frontend/dist` |
| `make screenshot` | Not a test: builds the frontend and runs `devtools/screenshots.py`, which captures one page, `PAGE`, in the state `STATE` if one is given, at a phone's and a desktop's width, light and dark, into `frontend/test-results/screenshots/`, for a look at the design; see [Screenshots](#screenshots) |
| `make check` | The full check: lint, `make test`, the frontend tests and the frontend's build; no on-demand run. It sets `RCMAN_REQUIRE_REAL_CLAUDE=1`, so tests that would skip for want of the real `claude` fail instead |

`make check` is the gating check and the full check; `make test-container` is
the on-demand run. When each runs, and how a run is read, are working rules:
[the full check runs at the edges of a change](working-rules.md#tests-and-guards),
and the on-demand run happens once, at the end, when required. The pre-test
hook counts every one of these targets that runs tests as a test run (see
[Pre-test hook](#pre-test-hook)).

Run pytest from the repository root; `pyproject.toml` holds its configuration.
The Python tests are split by where they run, not by how long they take:
`make test` runs everything that runs on the host, slow tests included, and a
slow host test is made cheaper or split into cases that workers share rather
than moved out; the tests that need a container, marked `container` (see
below), run on demand: `make test-container` runs them.
`tests/test_make.py` holds the split: every test is in exactly one of
`make test`, `make test-container` and `make benchmark`, `make check` holds no
on-demand run, and no container test of `make test-container` can skip. To
work on what a container suite covers, run its module directly, as in
`RCMAN_REQUIRE_CONTAINER=1 uv run pytest tests/container/test_runtime_in_podman.py`.

What keeps `make test` fast, none of it losing an assertion: it runs
`TEST_WORKERS` workers with `--dist worksteal`; an autouse fixture in
`tests/conftest.py` opens every database the backend opens in the test process
with SQLite's `synchronous = off` (a backend started as a subprocess keeps the
default), since no test is about surviving a crash and production
keeps the default; the collections `tests/test_make.py` reads are made once per
run and shared by all the workers through pytest's basetemp
(`tests/collected.py`).
The suite needs `tmux`, `git`, procps' `kill` and a busybox with `nc` and `setsid`
on the host, as `busybox-static`, which `.devcontainer/Dockerfile` installs, has
them. Each test's tmux sockets are in a folder of its own, made under `/tmp` by
`tmux_sockets` in `devtools/held_folders.py`, because a socket's path has to stay
short, and removed, with every tmux server in it, when the test ends; one a killed
run left is removed by a later run (see
[What a killed run leaves](#what-a-killed-run-leaves)).

Workspaces in the backend's tests run on the host runtime (see
[runtime.md](runtime.md#the-runtime)): a folder and a tmux socket per workspace,
the fake `claude`, real tmux, and a real channel through the host's `sh`.
`tests/runtime_contract.py`, which is not a test module, holds the cases every
runtime keeps, which `tests/test_runtime.py` runs against the host runtime and
`tests/container/test_runtime_in_podman.py`, in three basic cases, against real Podman on the
tests' engine. Its case of `port_argv` reaches a listener
the workspace's `python3` runs, so the runtime's image needs python3, as the default image and
the host have it.

The forwarding server, the hand-off and the ports the workspaces' listing gives
(`backend/rcman/forward.py`, `routers/forward.py`, `routers/workspaces.py`) are
`tests/test_forward.py`'s, the ask telling which hosts were marked as wanted, on the host
runtime: its socket in a held folder (`rcfw-`), each request one Caddy would pass on, on a
connection kept open as Caddy pools one, to an app of the test's own on the host's loopback,
which the host runtime's relays reach, which echoes what it is sent and sets cookies of
rcman's names. `tests/test_main.py` holds the lifespan's start and end of it.

Forwarded ports' relays on the host runtime, whose relays are the host's processes, are
`tests/test_forwards.py`'s: a real relay through `HostRuntime.port_argv` to a listener that
holds every connection open, its client holding its input open, ended by each place a
workspace sweeps (a deletion, the start of a rebuild's switch, a restore that finds the
container running).

The tools' `podman` and `docker` wrapper's reading of a published port,
`backend/rcman/podman/publish.sh`, is tested on the host by `tests/test_podman_wrapper.py`,
sourced as the wrapper sources it, by the static busybox's shell; its lists of Podman's options
are held there to the help recorded in `tests/fakes/podman-help/`.

Limits (see [runtime.md](runtime.md#limits)) are tested on the host runtime and a stand-in for
`systemctl`, which records what it is asked: `tests/test_limits.py` the pure module `limits.py`
(reading, showing, the slices' names, the properties, usage), `tests/test_runtime_limits.py`
`PodmanRuntime`'s calls, `tests/test_workspace_limits.py` setting, refusing below use,
reporting, restoring and dropping in `Workspaces`, `tests/test_people.py` and
`tests/test_admin.py` the routes and `rcman-admin limits`, and `tests/test_rules.py` the
sentences of the three values' rules. On the tests' engine the limits reach the stand-in for
`systemctl` of `tests/container/stand_ins.py`, which keeps a slice's properties as the user
manager does until a restart, and answers the monitor's one show of many slices with a block for
each, nothing in use but CPU time that grows with the clock, so that wherever the stand-in runs
every person and workspace reads as one whole core busy; `tests/test_stand_ins.py` holds it to
what rcman's own `limits.py`, monitor and runtime read of it; what the kernel does with a slice
is checked by hand ([real-claude-checklist.md](real-claude-checklist.md)).

Everything git is tested against real git, never a fake: `tests/test_git_run.py` holds the
status parser of `helpers/git_run.py` to every kind of record git writes, and its refusal to
git's words and exit status, `tests/test_repository.py` every verb of `repository.py` in
host-runtime workspaces through a real channel and `exec`, `tests/test_devcontainer.py` the pure
module (`devcontainer.py`) and the guard that holds the administrators' devcontainer page equal
to its policy, `tests/test_devcontainer_flow.py` a workspace made from a repository's
devcontainer.json on a host runtime with planted images, builds on a host runtime that records
what it is asked (a Dockerfile and features built, the waiting line for builds, the restart
removing what no workspace uses), `tests/test_rebuild.py` rebuilding a workspace on its volume
on the same host runtime (telling that its devcontainer.json changed, the switch and putting the
old container back when a new command fails, a restart of rcman after the old container was
discarded, the refusal while rcman is upgraded, the guard that a request asks whether the
container is there before it takes the images' lock, the images' two names, a Start that makes a
lost container again on its volume, and the rebuild of a failed workspace that kept its volume),
`tests/test_builder.py` the pure module `builder.py` (its argument lists, the reading of the
builder's words, the pump, the copy of a repository with the host's own tar),
`tests/test_runtime_build.py` `PodmanRuntime.build_image` against a stand-in for podman (the
archive that is loaded, rcman's own names held across the load, the staging folder, a rebuild's
next name and its promotion to the built one), `tests/test_headers.py` the security headers of
every answer and the limit on a page's reports of failures, `tests/test_filelock.py` the lock
file the service shares with `rcman-admin`, `tests/test_clone.py` a workspace made by cloning (a
phase of `creating`: its log, a failure and its cause, the token in no trace, names numbered,
cancel by deletion, restart recovery) on a host runtime that runs real git and can answer a
clone with what git would say or hold it as a running git, `tests/test_git_account.py`
`gitaccount.py` on the host runtime (git's and gh's files, a save and a forget, and the
restore's writes of gh's files), and `tests/test_git.py` the git routes.
`tests/git_fixtures.py`, which is not a test module, makes real repositories, their origins and
their branches with the system's git, under a fixture configuration of its own, with a home,
`FIXTURE_HOME`, that does not exist, so the configuration git reads is only what a test plants;
fixture git refuses every transport but a local repository's, so an http, https, ssh, git or
scp-like address a test gives it is refused before anything is connected to. GitHub is
`tests/github_server.py`: the system's `git http-backend` behind a small server, which serves
anyone a read of a public repository, and takes a push only with its token as the password,
under any non-empty username, as GitHub takes a personal access token. Its `gate` event, when a
test clears it, makes every request wait, so that a clone is under way and stays so. Nothing a
host test drives reaches the real github.com: the autouse `github_stand_in` fixture in
`tests/conftest.py` gives every git command the host runtime runs whose subcommand may reach a
remote (`NETWORK_SUBCOMMANDS` in `tests/git_fixtures.py`) a folder in place of
`https://github.com/` and forbids the ssh and git transports (`through_stand_in`), and such a
command not given it is an `AssertionError` (`assert_through_stand_in`). Any other git command
is left as it is: `remote get-url --push` rewritten would name the folder, and a push to GitHub
would no longer be known as one. The git of an argv is the first token named `git` that is no
`NAME=value`, so a variable rcman passes before it is not taken for it. `tests/test_stand_in.py`
holds the rewrite of each subcommand, and plants one it missed to see the guard fire. The
fixture sees only `HostRuntime.exec`, not a channel's `run`: it covers rcman's git that reaches
a remote because `repository.py` runs every such call with `network=True`, through `exec`. Git
in a workspace of real Podman, against the same stand-in served as `https://github.com`, is the
container suites' (see [The container suites](#the-container-suites)).

The Terminal tab is tested on the host runtime, whose shells are the host's own processes, found
in the host's `/proc` by a `sleep` of a length no other test uses: `tests/test_terminals.py`
drives `terminals.py` without a WebSocket (what an end leaves and what it does not, a pidfile
that ends its session only while it names the leader, the restore's sweep, a rebuild's end, and
a session that ignores the hangup killed after its grace), and `tests/test_terminal.py` the
route through the test client's WebSocket (what is typed and shown and a resize, who may open
what, a replacement, what closes a terminal, the audit, nothing typed recorded or logged, the
protocol held to backend.md's table and to the page's, `frontend/src/terminalSocket.ts`, loaded
with Node) and, for a service that stops, through real uvicorn running the screenshots' backend
(`devtools/world.py`) with the options of the unit's `ExecStart`.
`tests/container/test_terminal_in_podman.py` runs on the tests' engine, with the default
workspace image built from the installer's build file and workspaces made through the app: what
is typed reaching the shell through `podman --remote exec` and coming back. A remote exec's
record stays after it ends until Podman's own cleanup removes it, once the exit delay conmon is
given has passed, so whether an exec runs is read from Podman's API. The runtime contract's
terminal case (`tests/runtime_contract.py`) runs on the host runtime. No test opens the tab in a
real browser ([What the tests no longer check](#what-the-tests-no-longer-check)). The page's
side of the protocol, its states and its fakes are in [frontend.md](frontend.md).

What needs a real Claude account, and what needs systemd, the kernel's limits or a real
machine, is checked by hand: [real-claude-checklist.md](real-claude-checklist.md). Tests marked `real_claude` are deselected by default
and run by hand with `uv run pytest -m real_claude`; what they need is in
[fake-claude.md](fake-claude.md).

### The benchmark

A test marked `benchmark` measures how long something takes and reports it; it asserts no
time, so no machine's speed decides a run, and it fails only when it cannot measure. Each
prints its figures, which `make benchmark` shows, and records them with pytest's
`record_property`. The benchmark tests stay in the modules of what they measure, and
`make benchmark` alone runs them: pyproject's `addopts` leaves them out of a bare `pytest`,
and each other target's own `-m` leaves them out too, as `tests/test_make.py` holds. They
are:

- the channel's 95th percentile against `LATENCY_P95_SECONDS` in `channel.py`, on the host
  (`tests/test_channel.py`), in up to `LATENCY_BATCHES` batches, as a busy host can push one
  batch over the bound and a slow channel is over it in every batch;
- the redaction of a screen (`tests/test_claude_checks.py`), the time each of its large
  cases takes against its size. `make test` holds the same cases ended, each run in a thread
  joined within `HANG_GUARD` and asserted to have ended with the right result, so that a
  redaction that never ends fails its test rather than hanging the run.

### The hang guard

A test that hangs is ended by pytest-timeout, the suite's one hang guard, with the hooks
`tests/conftest.py` adds, and every pytest run has it. It has a value per tier, both in
`pyproject.toml`'s `[tool.pytest.ini_options]`, where a comment says what each covers:
pytest-timeout's own `timeout`, which every test gets, and `container_timeout`, which
`tests/conftest.py` registers and gives, as a `timeout` marker derived alongside the container
marker (`tests/hang_guard.py`), to every test that carries the container marker, since the image
builds of their session fixtures are charged to the test that first asks for them. That
marker outranks `--timeout`, which changes only the other tests' value;
`-o container_timeout=N` changes the container tests'. Each value is far beyond any
plausible slowness of its tier, so the guard ends only a genuine hang and never decides a test
that would have finished. The guard counts from a test's setup to the end of its teardown, also
after its setup or call failed: pytest-timeout on its own stops the guard then, so a teardown
could hang unguarded, and `tests/conftest.py` takes that cancel, unless the run is to enter the
debugger (`--pdb`).

The method, pytest-timeout's `timeout_method` there, is `thread`: a timer thread, on expiry,
stops every process below the test's process (under pytest-xdist the worker's, never the
controller's or another worker's), prints what the test printed and the stacks of every thread
of the process, kills the processes it stopped, and ends the process with `os._exit`, unless a
debugger is attached, as pytest-timeout's own timer allows. Each step is guarded: a failure to
stop or kill them, or to report, is said on stderr, the processes stopped are killed, and the
test is ended all the same. They are stopped first so
that a test waiting on one is shown where it waits, and held by pidfds taken only while each
start time is still the one read, so that a pid given again is never signalled; the processes
below are looked at again until a look finds nothing new, within `KILL_PASSES` looks, so that
none forks past the kill; one the user may not signal is neither stopped nor killed, and said on
stderr (`stop_descendants` in `tests/hang_guard.py`). It ends a test whatever the test is
blocked in, a worker thread it waits for that never ends, or code where no signal reaches it,
which the `signal` method, raising in the test's main thread, does not: a non-daemon thread it
leaves waiting keeps the interpreter from exiting, and with SIGALRM, the signal method's signal,
masked, or a call that retries it, the signal never arrives. What ending the process costs: the
test's teardown does not run, so it leaves what a killed run leaves (see [What a killed run
leaves](#what-a-killed-run-leaves)); in `make test` pytest-xdist reports the worker as crashed
while running the test, fails that test and starts another worker, and the run goes on; in the
one-process run, `make test-container`, the run ends there, its last output
the guard's report with the test's frame in the stacks.

`HANG_GUARD` and `CONTAINER_HANG_GUARD` in `tests/hang_guard.py`, each half its tier's guard,
are what a test hands a product call as its timeout for something expected to happen:
`HANG_GUARD` in the host tests, `CONTAINER_HANG_GUARD` in the container tests. A call given it
early in a test ends, and the test fails as any other does, its teardown run, before the guard
of its tier would end its process; successive waits that add up to more than the guard do not,
and the guard ends the test. Code both tiers run (`tests/runtime_contract.py`,
`tests/podman_service.py`, `tests/containers.py`) reads `tier_guard()` when it waits, the tier of
the test that asks; a session or module fixture, which no test's tier covers, names its tier's
guard with `handed`, as the engine's `started(guard)` does for what runs while the engine lasts:
`podman_service`, the session's engine, is started with `CONTAINER_HANG_GUARD`, which it keeps
handed for the whole session, while a test's own tier still comes first in the test. The
programs the container suites run in a container from the checkout's files
(`install_checks.py` and `installed.py`) are handed it in
`RCMAN_HANG_GUARD` by the exec that runs them, which is how `until` there knows its bound; the
exec kills the program at `EXEC_GUARD`, beyond that bound and within the test's guard, so that a
wait there that never ends fails with what it waited for, not a bare timeout;
`tier_guard` looks for the doubles' registry without importing it, as such a program has no
pytest.
`tests/test_hang_guard.py` holds the guard to this, each case a
child pytest with the project's configuration, `tests/conftest.py` loaded as a plugin, and a
short guard: a test blocked in a worker thread where no signal reaches it is ended and named, in
one process and under pytest-xdist; a test blocked in its teardown after its call failed is
ended; the child a hung test waits on is ended with it; and a test that finishes under the
project's own guard passes. It also holds, by reading their source, that each exec that hands
`RCMAN_HANG_GUARD` hands `CONTAINER_HANG_GUARD` and kills at `EXEC_GUARD`, and that these nest
within the container tests' guard.
`tests/test_make.py` holds that every test of every target is under its tier's guard.

The frontend's component tests have hang guards of their own, a tier of their own, in
`frontend/src/test/hangGuard.ts`; every timeout a frontend test or its config gives is one of
them or 0, and `tests/test_hang_guard.py` holds their values
([frontend.md](frontend.md#limits-and-time-in-the-tests)).

### Time in tests

The product reads the time and waits through the standard library, beside the few callables
some parts take for it (forwarding's and the monitor's `clock`, the token buckets'),
and gains no seam for a test: what a test asserts of time is made certain from the test's side
alone. `tests/time_doubles.py`, a plugin `tests/conftest.py` loads, holds the doubles a test
puts in place of a product's own, and its module docstring is their rules:

- `FakeTime`, for a module's `time` (`monkeypatch.setattr(module, "time", fake)`): `monotonic`
  and `time` move only by `advance`. A sleep, polling by default, really sleeps at most
  `POLL_CAP` and moves no time; with `parking` set, or given a test of a sleep's seconds that
  picks it, it parks until an advance covers it. Each sleep is numbered as it begins and counted
  as it returns (`sleeps_begun`, `sleeps_returned`, `mark()`), so that `wait_sleep_begun(after=k)`
  waits for a look begun after a point the test saw, and `wait_sleeps_returned(after=k, n)` for
  `n` looks begun and ended after it. It is used only where the test owns every thread that
  reads that module's `time`.
- `WatchedCondition`, for a condition, `Workspaces._changed` among them, on a lock it is given
  or a plain one: its `wait_for` is its own, since the standard library's reckons its timeout
  from a clock no patch reaches. In virtual mode a timeout never runs out by itself, and
  `expire(match)` ends a wait as if it had.
- `ControlledEvent`, for an event, `Workspaces._closing` among them: a `wait` given seconds ends
  only on `set` or `expire(match)`.
- `Clock`, for a clock a product's seam reads, the monitor's among them:
  calling it gives `now`, which only `advance`, or the test setting it, moves.
- `Time`, for the monitor's loops, the `clock` and the `wait` it is given: a wait ends when its
  event is set or the clock passes its deadline; each `advance`, and each `poke`, which moves no
  time, is a new generation every waiting loop looks at again, and `parked(n)` tells when `n`
  loops wait again having looked at the latest.

A double is put in place before any thread that reads it starts: a module's `time` by
`monkeypatch` before the call, and a part of the app by a fixture that runs before `client`
starts the service. `tests/conftest.py`'s `changed` puts a virtual `WatchedCondition` into the
workspaces' service as its `_changed`, and `periodic` a `ControlledEvent` as its `_closing`, which
the periodic checks wait on between rounds; each goes into the first service made after it
(`into_the_first_service`), the app's where the test has one, so that rcman-admin's service in a
test of two processes keeps its own. They are the conftest's `SEAMS`, which `app` asks for first
wherever the test asks for them, itself or through another fixture. A module's own seam is
listed by the test before `client`; a module's `fake_time` puts a `FakeTime` on the module it
tests, and where the periodic checks read that module's `time` too it holds them (`periodic`),
so that the test owns every thread that reads it. `ended` waits, on the service's condition,
until no thread that makes or rebuilds the workspace it names is left.

Every wait on a double is recorded, with its thread, its seconds and an ordinal of the
double's, under the primitive's own lock and only once what it waits for has been seen not to
hold: a recorded waiter really waits, and what is told after the record reaches it. A test waits
for records through the double's own condition (`wait_waiting`, `wait_parked`, the sleeps'
waits), never by polling, and picks a wait out by a match: its thread, its seconds or
a predicate on the record, so that `_closing`'s periodic wait of `IDLE_CHECK_SECONDS` is told
from a person's deletion's look of `WORKING_POLL_SECONDS`. A `_changed` wait is matched by its
seconds only where they are exact: `close`'s `CLOSE_WITHIN_SECONDS`, handed as it is, and
`MADE_WITHIN_SECONDS` in a person's deletion with `FakeTime` on the module's `time`; elsewhere by
thread or by predicate.

**The hang bound.** A frozen wait that nothing ends within the double's hang bound, `HANG_GUARD`
for a host test and `CONTAINER_HANG_GUARD` for one that carries the container marker ([the hang
guard](#the-hang-guard)) unless the double is given its own, raises `DoubleHung` in its waiter,
and so does a polling `FakeTime` loop that goes as long with no advance. A thread's sleeps count
as one loop only while no more than `POLL_GAP` of real time passes between one's return and its
next: a longer gap starts the bound again, as a pooled thread may poll for one thing and later
for another. A test's own wait on a double fails as long after. `DoubleHung` is a BaseException,
which the product's `except Exception` lets by, and each is also recorded: the plugin's autouse
fixture fails the test at its teardown on any hang of any double the test made, so a hang in a
product thread whose caller never sees it still fails the test. A double is made in a test or a
fixture of the test's own (`DoubleOutsideTest` otherwise); at the test's end it is closed,
records no hang, and its waits go on until what they wait for ends them.
`tests/test_time_doubles.py` holds the doubles to this, the registry's own cases in child
pytests.

**The rules.** A test that waits on time keeps these:

- (a) A deadline meant to run out is put at 0 with `runs_out`, or at the floor the product
  refuses to go under, against a stand-in that never finishes, and covers nothing that must
  succeed; a control runs before it, or after the `monkeypatch.context()` it was given puts the
  original back.
- (b) A deadline over something that must happen is left as it is, or raised to `HANG_GUARD`
  before `client`.
- (c) To tell which of two causes ended something, never assert the time it took. Where the call
  runs in another thread, or the test waits on an event, the cause that must not act is pushed
  beyond twice `HANG_GUARD` and the other waited for within `HANG_GUARD`. Where the call is
  synchronous in the test's thread, the cause is not pushed past the hang bound, which would hang
  the worker until the guard ends it: either the deadline that must not act is set to
  `HANG_GUARD`, so that the wrong cause ends as another problem the test asserts against, or the
  call runs in a thread joined within `HANG_GUARD`, and the test asserts it ended.
- (d) A pace may be shortened, with `pace`, where the test only waits, with `until`, for what
  the pace makes happen.
- (e) Something is asserted not to have happened only after a point the test observed: a recorded
  wait, a blocked acquire, a parked sleep, a stand-in that says it was entered; never after a
  sleep or a `join(N)`.
- (f) Every bound on something that must happen in a test (`wait(N)`, `join(N)`, a socket's or a
  subprocess's timeout, `fail_after`) is `HANG_GUARD`, or `CONTAINER_HANG_GUARD` in a container
  test, or `tier_guard()` in code both tiers run; a wait for what the test cannot be told of is
  `until`'s, never a deadline loop of its own.
- (g) A stand-in meant to outlast the test sleeps beyond twice `HANG_GUARD`, or until it is
  killed.

**The helpers.** `until(condition)`, in `tests/until.py`, is a test's wait for what it cannot be
told of: it looks every `PACE`, or every `SLOW` where a look costs a process of its own, and gives
the condition's first truthy value. It takes no limit: it gives up only at the tier's guard
(`tier_guard()`, [the hang guard](#the-hang-guard)), with an `AssertionError` that names the
condition and what it last gave, so that the test fails as any other does and its teardown runs.
`until_async` is the same in a coroutine. `tests/hang_guard.py` holds the rest. `runs_out(monkeypatch,
module, name, at=0)` puts a product's deadline at 0, or at the floor `at` where the product refuses
0, for rule (a); `pace(monkeypatch, module, name, seconds)` shortens a product's pace for rule (d).
Each asserts that its value is below the product's, and is undone by the `monkeypatch` it is
given: a `monkeypatch.context()` where the test needs the product's value back within it, never a
second setattr. `handed(guard)` gives `tier_guard()` its value in a session or module fixture,
where no test's tier is known. In a child program's source a time comes from the guard too:
`{3 * HANG_GUARD:g}` for a stand-in that outlasts the test (rule (g)), and `RUNS_OUT`, the
smallest whole second every tool takes, for a `timeout` or a deadline meant to run out against a
program that never finishes (rule (a)). A child's own wait is a loop on a condition, with a pace
and no clock or count, as `while [ ! -e release ]; do sleep …; done`, which the test ends by
making the file once it has seen what it needs; `tests/test_terminals.py`'s `sleep_argv` and
`sleep_of` give the tests of the terminals a `sleep` that outlasts the test, found by a fraction
of a second of its own added to it.

**The timing guard** (`tests/test_timing_guard.py`) holds every test to these rules by a static
scan of the Python of `tests/`, `tests/container/` among it, and `devtools/` (`SCOPE`), and of
the frontend's TypeScript (below), which imports and runs nothing it scans; it reads `backend/`
and `helpers/` too, for the parameters of a timing name each function takes. It flags elapsed
real time compared, a clock read or a name derived from one in a comparison or a difference
(P1); a sleep in test code, or a `threading.Timer` or a loop's `call_later` (P2); a sleep or a
`timeout` written in a child program's source, unless it is a guard's multiple (a sleep: twice
the guard or more, rule (g)), `RUNS_OUT` or the pace of a child's wait loop, and `sleep` given
its length in an argv or a call's arguments (`["sleep", "600"]`, `["sleep", str(60)]`,
`engine.cli(…, "busybox", "sleep", "600")`), in a test or in a child's Python, or as a helper's
parameter, at each call of the helper that gives a number, unless the length is twice the guard
or more, an argv that is compared, and so runs nothing, aside (P2s); a timeout that is not
allowed, at a keyword or a parameter of a timing name, at a known position of a wait, in a
parameter's default, in a module's or a class's constant of a timing name, or in
`pytest.mark.timeout` (P3); and a product's timing constant patched to a value that is not
allowed, the time module or the guard patched, a value given back by a second setattr, a name a
setattr is given that cannot be resolved, or `runs_out` given anything but 0 or a product's
floor (P4); a module other than `tests/hang_guard.py` defining `HANG_GUARD`,
`CONTAINER_HANG_GUARD` or `tier_guard` (GUARD); and a guard of another tier (TIER): `HANG_GUARD`
in what the container tests run, `CONTAINER_HANG_GUARD` in what the host tests run, and either
in what both run, where only `tier_guard()` gives the asking test's. Which tests run a piece of
code is worked out from the import graph (`tier_reach`): every top-level name of
`tests/container/`, a test elsewhere, of the container tier where it asks for the tests' engine
through its fixtures, as the container marker is derived, and `tests/conftest.py`'s autouse
fixtures are the roots, and a name is reached through the names, imports and fixtures its
statement uses. The allowed values are the tier's guard and its multiples, also plus a number or
through `int()` (`str(int(2 * tier_guard()))`), `tier_guard()` where a test reads it, `None`,
`-1` and `0`. A timing name is a word of the name between underscores, `guard` among a
parameter's (`handed`, `started` and `usable` take only an allowed value), and a constant's
longer words also run together (`IDLETIMEOUT`, `MAXWAIT`); a string that reads as a number, as
`'0.01'`, is seconds. A name a setattr is given is resolved through a loop over literals, the
tuples such a loop unpacks, a test's literal `parametrize` and a helper's literal arguments, a
closure's too, and not where the function assigns the name again. What the scan allows where it
finds it is a call on a double (`tests/time_doubles.py`), a helper's parameter typed as one only
where every call of the helper in its module is seen to give it one, a turn of 0, a benchmark
test, and a child's wait loop. The only other exemptions are its named patterns, `NAMED`, each
with its reason and a stale check that fails once its subject holds no site: `until`'s own pace
and bound, the doubles' hang bound, the hang guard with `runs_out` and `pace`, the short bounds
the tests of these hand them, tmuxctl's give-up, at the timeout its caller passes, which a test
gives as its tier's guard, and the recorder's command-line watch, which no test reaches but a
module marked `real_claude`, run by hand, a killed run's own hang bound in
`tests/killed_runs.py`, `hold` and `shell_hold`, derived from the tier's guard, the sweep of
held folders and the pre-test hook, whose deadlines are their behaviour, the tests of
`tier_guard` and of each tier's value, which name the container tier's guard in a host test, and
the container tests' guard `mark` gives each of them; and the programs of `STAND_INS`, which run
only as another program, whose timing is theirs: the fake `claude`, and the container suites'
stand-ins for `systemctl`, `loginctl` and rcman's `podman`. Planted twins show each form
flagged, one for each way the scan detects a form (`PLANTED` and `PLANTED_TS`, each form spelled
in each language or said why not in `FORMS`), and each allowance clean, one clean twin for each
(`CLEAN` and `CLEAN_TS`); `test_no_test_waits_on_real_time` scans the real tree once, and fails
on any site, on a stale named pattern or stand-in, and on a scan that saw no site or made none
of the allowances it is known to make.

In the frontend it reads the component tests `frontend/vite.config.ts`'s `include` names,
everything under `frontend/src/test/`, and that config (`ts_scope`), parsed in one call of Vite's parser (`tests/typescript.py`). It flags
`Date.now()` or `performance.now()`, or a name given one, in a comparison, a difference or a
matcher that compares, and `shouldAdvanceTime` (P1); `waitForTimeout`, `setTimeout` with a delay,
`setInterval` and `userEvent.setup` given a `delay` but 0 or `null` (P2), a timer of no delay
being a turn; a `timeout` option or a key ending `Timeout` not given a guard of
`frontend/src/test/hangGuard.ts` or 0, `test.setTimeout` likewise, a `slow()` called on anything,
as `test.slow()`, `testInfo.slow()` and `test.info().slow()`, and `vi.waitFor` or `vi.waitUntil` without a `timeout` (P3); a name
ending `_GUARD_MS` defined elsewhere (GUARD); and a config that does not set every limit
(P3c): Vitest's `testTimeout`, `hookTimeout` and `teardownTimeout`. The frontend's tests are one
tier, so the scan has no tier rule there (TIER). `FORMS` names each form's planted twin in each
language, or why a language does not spell it.

#### What the tests no longer check

A test that raced time checked a few things only an elapsed time could tell, and these are
given up:

- whether the channel's 95th percentile is under `LATENCY_P95_SECONDS`, and how long a
  redaction takes against its size: [the benchmark](#the-benchmark) reports them, the channel's
  on the host only, and decides no run, and `make test` still ends a redaction that never ends;
- "promptly": where a test asserted that something ended within a time, its result is still
  asserted and the cause that must not act is pushed past the guard, so that a defect fails
  the test, but an ending that is slow without being a hang passes;
- the timing of `RealWatch` (`devtools/record_claude.py`), its quiet windows against the real
  `claude` and its `TIMEOUT`, after which the pane's waits give up, which only the recorder's
  command line exercises, by hand;
- how the real `claude` treats an answer typed before its question, which the `trust_too_fast`
  replay holds on the fake's side only ([fake-claude.md](fake-claude.md)).

No test drives the page in a real browser. `make screenshot` shows a page for someone to look
at, and asserts nothing. What only a browser could check is given up:

- the stand-ins the component tests put in for the browser are held to nothing real:
  `frontend/src/test/fakeDialog.ts` against the native modal dialog (its focus on opening,
  Escape and the cancel event, focus given back on closing) and
  `frontend/src/test/fakeLayout.ts` against the browser's ResizeObserver and colour scheme, and
  with them whether a phone's pointer is coarse and a mouse's fine;
- on a phone and at every width: each page walked at every width for overflow, the size of
  every control, fields that zoom in on focus, icon-only buttons with a name, the design
  system's checks, the places allowed to scroll, and every dialog and pop-up opened there (the
  sites `frontend/src/test/callSites.ts` names of pop-ups are opened by no test); mouse-sized
  controls on a desktop; hover contrast in the dark scheme; the planted twins of the walk's
  checks and of the hover check; and the phone's way from the list to a workspace and to another;
- the layout: the workspace card's scrolling at narrow widths, the list's dots and their
  alignment, the sidebar painted to the page's foot, the semibold face, guide tables that
  scroll, the skip link and the page titles, the top bar at a phone's and a desktop's width,
  the navigation and the administrators' group, dialogs as sheets or centred, the panel bar and
  the switcher's sheet, the list's heading row, the desktop's columns, the drawer's Escape,
  outside press, focus and click-through, the sheet dialog's focus trap and room for the
  keyboard, Settings and Help at both sizes, People and the audit trail as lists with More
  actions on a phone and People as a table on a desktop, Monitor as lists, the versions table's
  scrolling and the large Limits dialog;
- the pages against the real backend: the settings' verdict, a GitHub token never shown, the
  commit author's fallback, help links, administration for administrators only, the Claude Code
  versions page and marking one failed, adding and removing people with the audit trail, limits
  refused and kept, the attention banner, a stranger refused, and the favicon;
- the Terminal tab: the real shell and its size following the window, a takeover from a second
  page, and a tab left and shown again fitted to its screen;
- the workspaces in a browser against the real backend: a workspace made and ready, its device
  name in its row and its panel and its address opening it again; one made from a repository's
  address, named after it, showing its branch; New workspace as a modal dialog, focus in its
  name, Escape closing it, focus back on its button and the page behind out of reach; a refused
  address or name shown under its label and kept; a repository that is not there failing with
  git's words and deleted; a server's life: start, trust, stop, resume, stop and forget; a start
  refused while Claude is not signed in, saying so; a workspace deleted with its server, after
  asking; people never seeing each other's workspaces or reaching them by id; administrators
  seeing everyone's workspaces and acting on none of them; and a refresh that cannot reach rcman
  saying so, going once rcman answers, and reported;
- the whole stack in a browser: Caddy and oauth2-proxy in front of a real browser; a workspace
  made before Claude is connected starting no server and saying why; the fake `claude` connected
  once on the **Claude** page, and workspaces made before and after both running; staging,
  committing and pushing from the panel; a clone from GitHub, a private one once a token is
  stored; another person seeing none of it and finding nothing by its id; an address not on the
  allowed list getting nowhere; administrators seeing everyone's workspaces and acting on none
  of them; the People page adding a person and removing another with her workspaces; the audit
  trail recording what people did; the terminal through Caddy and oauth2-proxy; a forwarded port
  reached by its owner through a plain link, its WebSocket included, its way in refused when
  posted from the port's own page, and anyone else refused it; and what that left: a
  deleted person's containers and volumes gone, the pushes' author, and the token in no audit
  row or line of rcman's output;
- the rules [frontend.md](frontend.md#rules-it-keeps) lists that a browser would show are kept
  by hand.

The frontend's component tests are held to one test for each thing a person or an administrator
relies on a page for, on the path where it works ([frontend.md](frontend.md#tests)), and to the
guards [frontend.md](frontend.md#rules-it-keeps) names, each scanning check with its run on the
real tree and one planted twin: the stylesheet's colours, the terminal's colours, the danger
buttons, contrast, a field's focus, touch targets, a field's text size, the viewport's tag,
hovering, buttons and links laid out alike, every form's validation, leading button, destructive
opener and refused fields, every form and dialog in the source opened, every card's actions,
every change's letter and word, the server states' tables, the manifest's icons, every table in
the guides, and each guide held to its page; the design system's tokens, the dialog's sheet and
the manifest's fields are checked directly. The shared dialog's and `usePopup`'s own focus and
Escape are tested, and so is the skip link's focus. The failure paths kept are those that are
the behaviour: a start refused while Claude is not signed in, a refresh that cannot reach rcman
said and reported, rcman not reached and a sign-in ended, a workspace that failed saying why, a
workspace with no repository said quietly, git's refusal in its words, a commit whose push
failed, a roll-out's failed checks, a failed upgrade with the way to roll back, a terminal
refused or whose socket never opened, xterm.js that cannot be loaded, and a fault in the page
named as the page's. Given up: every other refusal, shown under a field or in a form or a
dialog, and every other failure of a request, a poll or a read; a report's fields and the
queue's cap, but for alike failures counted as one; the variants of each state of a workspace, a
server, a rebuild, a port, a stop and a start, each server action but one of each, and each rule
for offering an action, the Changes section's Commit, Push, Commit & Push and Sync without an
author or a token among them; focus, Escape, outside presses and the keyboard anywhere else, in
the drawer, the tabs, the switcher and after each action among them; what a page shows but does
not do, its layout, classes but which button leads, a button busy, and the drawer open; headers,
help lines, pills, dots and live regions; how often a page polls and an older answer dropped;
the terminal's other close reasons, its fitting, colours and strict mode; the formats but sizes
and moments; and the tests of the tests' own helpers, the fake dialog, the stylesheet's reader
and the guides' table reader among them.

The installer, the upgrade from the web interface and rcman's side of it are held to one test
for each thing an administrator relies on, on the path where it works, and to their guards
([installer.md](installer.md#tests), [backend.md](backend.md#upgrading-rcman)). Every other
input they would meet, and every refusal but a rollback's to a release older than the database,
is given up there: among them the installer's refusals of a setting, a Caddyfile it cannot edit
or Caddy refuses, a lock that is a link, held or in a folder others can write, a home or status
folder that is a link, a kernel, an OCI runtime, a built tree with a link, a download or a tmux
that is not the pinned or whole one, and a clone the upgrade's checks refuse but for one path
anyone can write; the warnings for a missing cgroup controller; the order of the installer's
steps but those of the upgrade from the web interface; two callers filling one toolchain at
once; the health check's failures; the guide's prose beyond the options and the removal section;
and, on rcman's side, malformed `systemctl show` output, a second request while one is under
way, the limit on requests, the grace for a start, the admission's races and its interlocks with
a rebuild, a Stop or Start, an image collection and a roll-out's sweep, and the report for an
administrator only. No test probes whether `git archive` omits what `.gitattributes` marks. The
stubbed installation checks only the first installation's Caddyfile and Caddy's restart. Given
up with the rest of its checks: the release's contents and owners, the accounts, rcman's
subordinate ids and linger, the drop-ins' owners, the unit's options, the images built under
their names, the Claude Code store, the saved settings' and configuration files' owners and
modes, the first administrator, `rcman-admin` run by name, the socket's folder and group, no
listener letting a local user be someone in rcman, a second run and saved settings, a dirty
clone or one that cannot fast-forward refused, self-update and rollback, and the upgrade from
the web interface turned on, off and run; `tests/test_install.py` holds what of these the
installer decides.

A workspace made from a repository's devcontainer.json, the image built for it and the builder
are held to one test for each section of the file and each step a person relies on, on the path
where it works, and to their guards, each with one planted twin: the policy table against
[the administrators' page](../docs/devcontainer.md), the workspaces' seccomp profile, the pins of
the repository's own build file, and the wrapper's lists of Podman's options against its
recorded help. The failure paths kept are those that are the rule itself: a key the policy
refuses, in the pure module and failing a workspace; the name of an image rcman built refused as
a file's image; a built image's label that asks for what a file may not; an archive that carries
a name or is also an OCI layout; a build command that fails, said in plain words with nothing
left; and a load that moves a name of rcman's own, put back and the build refused. Every other
input is given up: among them each key of the policy one by one, the reader's limits and links,
values of the wrong kind or depth, reserved variables and those that move git's configuration,
users and ids outside the range, image names, transports and registries, feature references and
options, and ports beyond their merge; every other failure in plain words, of a pull, a build, a
load or a lifecycle command; the archive's other checks; the pump's limit, cancel and timeout;
the build's limits, timeouts and cancels and the sweep of its staging folders; a cancel or a
deletion at each step and closing while a build waits; a restore's concurrency and its bound; a
musl image, an image with no git and missing instructions for Claude; and every spelling of a
published port but `podman run -p`. No test holds the recorder of
`tests/container/test_builder_in_podman.py` to the build's own steps.

The runtimes, the workspaces, their limits, the rebuild, the Terminal tab and the exec channel
are held likewise: the runtime contract on the host runtime, one argv check for each kind of
container rcman makes, and one test for each thing a workspace does, on the path where it works,
a channel busy with a request never closed as idle among them, with the guards on the wrappers'
record folder, the lock order, the images' lock in a rebuild, a slice never stopped and the
terminal's protocol, each with one planted twin. The failure paths kept are those that are the
behaviour: a rebuild's new command that fails, rolled back; a Rebuild refused while rcman is
upgraded; a container whose making fails after its run, removed with its volume kept; a limit
that could not be applied, left unstored; a memory limit below what is in use, refused, and a
cap never lowered below it at a restore; a clone git refuses, failing the workspace in plain
words; another person's workspace, and their terminal, answered as one that is not there; a
pidfile that no longer names its session's leader; and a session that ignores the hangup, killed
after its grace. Given up: every other refusal of a name, a value, a Stop, a Start, a Rebuild, a
limit or a terminal, a volume of another installation among them; every race and window, a
deletion, cancel, restart or sign-out at each step of making, rebuilding, opening a terminal or
a channel, a channel's close racing its shell's start, `close()` called while a request is in
flight, and a channel opened again during a stop; putting the old container back when the
rollback itself fails; the timeouts and give-ups of a pull, a copy, `systemctl`, rcman's close,
a person's deletion and the limits report, whose time limit is not checked; every other failure
of Podman, `systemctl` or git and its logging, a workspace whose container could not be made,
git's words in a failed clone with the token redacted, and a channel's open tried again on
conmon's error among them; a workspace's log cut to its last characters; the minimums of the
limits held to the shared rules; a person's deletion waiting on one wait with no look at the
rows; malformed limits, pidfiles, process-table entries and terminal frames; the variants of
image collection; threads of one process kept apart by the file lock; and whether the Terminal
tab's shell holds no file of rcman's.

Claude Code's versions, the fake `claude`, its recordings, the screens rcman reads and a
workspace's server are held to one test for each thing a person or an administrator relies on,
on the path where it works, to the round trip and the contract tests that hold the fake to the
real program ([fake-claude.md](fake-claude.md#how-the-fake-is-checked-against-the-real-binary)),
and to their guards, each with one planted twin: the fixtures' manifest against their files and
the recorder's sequences, the tested version against the recorded one, the kept screens against
every kind of leak and each leak mark against a sample of it, the observed screens' manifest and
their `kept_for`, the round trip's case for every sequence, every fixture and observed screen
accounted for by a state, the questions and the reconnected screens held to every screen, no
test reaching the command line's watch, and every use of Claude's path through the command
builder. The failure paths kept are those that are the behaviour: a candidate the canary's
checks fail, refused with the checks and their reports and the approved version left as it was;
a rollback to a version approved before, with neither a download nor a canary; a version marked
as failed, refused for a rollout; a download whose checksum differs, refused with nothing left;
a server that ignores Ctrl+C, ended with its process group after its grace; a screen rcman does
not know, read as unrecognised, audited, logged once without its text and named to
administrators; a wait on a pane that gives up with the screen it last showed; and the home the
run was started from, refused as a recorded `claude`'s home. Given up: every other refusal of a
route, a rule, a verb or a mark, and every other failure to get, check, store or switch to a
version, the canary's environment and a candidate that is not there among them, and the release
bundle's derivation from the fake's imports; the link's repair and adoption of an empty or
missing version; approvals append-only; the grace that keeps a version an upgrade of rcman has
just stored; every order of a rollout, a sweep, a prune, a start and a mark; a stop's prune and
a prune that fails; each check's judging of a program's streams, its timeouts, a candidate's
children, a report's limits, tail and cuts, and each kind of secret through a whole report
rather than the redaction alone, its other spellings, wrapped or written over on a terminal
among them; every terminal escape but those of the recorded logout, and the real `claude` with a
long configuration path; what the fake does that the round trip does not replay, its answers at
the Remote Control question but `y`, its spawn-mode question, a credentials file it cannot
remove or rename, and its refusals but that of an answer too early; a server's own refusals and
timeouts, a workspace that cannot be reached listed with why, a declined trust, a server that
died or was signed out, the reconnected screens through a server's verbs, its audit, the journal
and the administrators' warnings, a stop of a session of its own or of a server that ignores
SIGTERM, the workspace's folder and environment read from `/proc`, the window's pinned size and
`tmux.conf`'s global options, and a status read across a server's end; every variant of the
reconnected, answered and question screens; sanitising each placeholder alone, the recorder's
held folders, a killed version reading, the hand-over of a sign-in code, a capture that changes
while it settles, the choice of sequences and the environment of a sequence that needs the
account; tmuxctl's trimming, its rerun, a death by signal and the exit status of an unreaped
process read from `/proc`; and the journal's forgetting, its bound on the calls it remembers and
the people it keeps apart.

The routes, people, Claude sign-ins, git, the monitor, port forwarding, the audit trail and the
database are held to one test for each thing a person or an administrator relies on, on the path
where it works, to the contract tests against real git and the machine's own `/proc`, and to
their guards, each with one planted twin: every route under `/api/` but the frontend's, every
problem code with a status and a message, every route that takes parameters given a valid
request, every route answering a missing identity with a problem, every WebSocket route refusing
one before its upgrade, the administrators' routes refusing everyone else, every change without
an Origin a named navigation, every upgrade needing the public origin, no parameter's
constraints dropped, no refusal in a library's words, every field message reached, every
published pattern a helpers' rule, every changing route audited or exempt with a documented
reason, every WebSocket route recording its open and close, every action and result in the audit
guide, every audited route driven, every handler of the forwarding server audited or exempt,
every setting in the configuration guide, the settings guide and the journal's table of a page's
reports, no product code giving Podman a size of the tests, no migration controlling its own
transaction, a monitor round's cost in programs run, every verb of `repository.py` driven, the
backend and the helpers reading each rule alike, the limits' sentences holding only their
constants, and a failure reason refusing every unseen character. The failure paths kept are
those that are the behaviour: a stranger and a removed person refused at once; a change and an
upgrade from another origin refused; someone who is not an administrator refused an
administrator's route; another person's workspace answered as one that is not there, for every
git route and the administrators' workspace limits; a credentials file emptied while its account
is connected, reported as signed out unexpectedly; a push that fails after the commit of a
commit and push, reported with the commit; git's words and exit status in a refusal; a refused
change audited with its error code; a request to a forwarded port from another port's host
refused, and every way in ended when forwarding is turned off; a page's reports over their limit
refused; a path outside the frontend's build never served; an address, a slot and the removal of
a person with workspaces refused by the database, and a migration that fails part way leaving
nothing changed. Given up: every other refusal of a route, a value, a field or a person, among
them the bounds and kinds of the monitor's and forwarding's settings, each rule's values one by
one (the limits', an author's name's invisible characters, a token's shapes, Hangul fillers) and
each rule's `or_empty()` form read alike by the backend and the helpers, an address in other
letters and slot 0 refused by the database, the author's address for a sign-in in other letters,
a credential git asks for a host other than GitHub, the server's words for each field error and
each parameter's kind, a commit with no author, a branch name git refuses, a push of a detached
head, a branch with no commit or no remote, a sync that diverged or whose upstream is gone, a
push to GitHub with no token, a clone the timeout ended, and a body that is not JSON or not
UTF-8; every race and order, a sign-in, a sign-out, a start or a deletion crossing another, a
sweep that waits for a person's lock, two refreshes of the monitor at once, the forwarding
server's caps, requests at once and its kept relays, and the monitor's loops overlapping or a
round that ends late; every timeout and deadline, of a sign-in's screens and code, git's network
and clone deadlines, a disk read killed, a head or body that never ends, and a session's reaper;
every other failure, of a runtime, a disk read, an inspect, a save of a token or an author,
SQLite refusing a write, a forwarding listener that raises, a monitor round and its logging, and
a seccomp profile or instructions file missing at start; what `forwards.sweep` refuses to act
on, a sweep called alone, and a relay removing its own pidfile; the order in which the
forwarding settings call their listeners; a list of files, a commit message and a clone's token
in no argv; the monitor's readings of what is malformed, not known or another installation's; a
constraint dropped in a dependency, a partial or an embedded body, beyond the one planted twin;
audit rows that cannot be changed and a workspace name used twice; the forwarding server's
identity per request on a pooled connection, its frame policy, its half-closed requests and its
headers of an answer that sets its own; `rcman-admin`'s refusals, its runs of Podman with the
product's sizes and a deletion it leaves to the service; the listing of ports while forwarding
is off, for a workspace not ready or being rebuilt, and to an administrator listing everyone; a
workspace whose server cannot be read in everyone's listing; the service's start checking its
seccomp profile and instructions and telling the monitor of the restore; the schema of each
migration since the first one by one; the route enumeration's own checks beyond its planted
twins; audit retention run again while rcman runs, and the refill of the limit on a page's
reports; and the security headers on an answer to an error nobody expected.

The repository's guards and the tests' own tooling are held to their guards, each with its
real-tree check and one planted twin, and to one test for each thing the tooling does, on the
path where it works: the timing guard to one planted case for each way its scan detects a form
and one clean twin for each allowance; the hang guard to a hung test ended and named, in one
process and under pytest-xdist, its teardown guarded after a failed call, its children ended,
and a test that finishes left alone; `until`, the tier's guard, `runs_out` and `pace`; the time
doubles' hang bound, a notify, a set and an advance ending a wait, and their registry; a killed
run's reports, paths, `scrub` and stems; held folders refused outside `/tmp`, the next run
clearing a killed run's folders and tmux servers and leaving a live run's alone, another
program's listener passed over, and a removal following no link; the tests' engine keeping its
storage in its own folder and removed whole after a run that ends or is killed; and the
stand-ins for GitHub, github.com and `systemctl`, the collection, the snapshot, the edit-time
hook and the screenshots' world. Given up: every other spelling of a timing form, each tier's
and each named pattern's own twins; the hang guard ending a test whose stop of its processes or
whose terminal fails, a process forked past its first look and a pid given again; the doubles'
other waits, matches and expiries, `FakeTime`'s counts and its parking by a test of the seconds,
a test's own wait failing at its bound, and a double made outside a test refused; `until`'s
refusals; reports read only once complete, a planted script's report, and each unsafe path one
by one; the sweep of a socket another program listens on anywhere in a folder, a `kill-server`
that does not return, a tmux replaced on disk, a socket gone meanwhile, a folder without a lock
file, what cannot be removed, and the lock file taken last; an engine killed during a build or
left with a mount in its storage, the unmounting passes, a process it may not signal and a pid
given again; a run killed with SIGTERM; the stand-in GitHub's addresses with and without `.git`,
a wrong token, a credential stored for github.com, and a listening socket it is handed; the
snapshot from a clone or a worktree elsewhere, its folder modes, an inherited `GIT_DIR` and a
global or personal ignore file; a worktree mounted as it is in a container; the collection's
handling of options and of the environment; the docs guard's each kind of reference and each
historical phrase, the make database's comments, variables and branches, and make reading its
Makefile in a held folder; the privilege guard's each allowed form, product file and account
call, an exemption covering only its own site, and every way a root service goes beyond its
exemption; the fixed-resources guard's ports held in a module's constant, the listing of every
kind of frontend file, a file that does not parse and the frontend's packages missing; every
other way a target of `make` goes wrong, `make screenshot`'s recipe, and `make lint` on a
finding Biome reports as an info, on `index.html` and without the frontend's packages; the
edit-time hook's other pairings; the screenshots' world failing to build or to take its port;
the relay's refusals; and whether the recordings of Podman the host tests read
(`tests/fakes/podman-monitor/`, `podman-images/` and `podman-seccomp.json`) are still what real
Podman gives: they are copies of one point in time, recorded again when the product starts
failing against real Podman.

A hangup ends a terminal's shell and its jobs only where SIGHUP is at its default in them, and
an ignore survives exec: a suite started with SIGHUP ignored, as under `nohup`, would leave them
running and `end_leader` polling. So `tests/conftest.py`'s `pytest_configure` catches an ignored
SIGHUP with a handler that does nothing, which the suite's own process keeps and every program
it starts has back at the default; `tests/test_conftest.py` holds it to that.

### The container suites

The tests marked `container` run on the tests' own engine, the same way on a developer's machine
and in a workspace of rcman's: `tests/podman_service.py`, which is not a test module, runs the
user's own rootless Podman as `podman system service` on a socket of its own, with the
workspaces' shared configuration (`backend/rcman/podman/containers.conf` and `registries.conf`)
and storage of its own given by flags (rootless Podman reads the places of no other
storage.conf), in a held folder under `/tmp` short enough for the run folder Podman takes
(`RUNROOT_MAX`). Its service and its own podman calls run in that folder, never the
repository: conmon, which the service starts, writes a file named `oom` into the folder it
was started in when it sees an out-of-memory kill. The `podman_service` fixture in
`tests/conftest.py` starts one for the session; it skips where the user's rootless Podman
cannot run, unless
`RCMAN_REQUIRE_CONTAINER=1`. Removing an engine removes its containers and volumes, stops its
service and every process of the user's that names its folder (conmon among them) with every process
below them (a build's RUN among them; one whose service died before it is below nothing that
names the folder, and is not found; one the user may not signal is left and said on stderr, and
one whose start time is no longer the one read, its pid given again, is passed over), then, as root of the user's rootless namespace, which every
`podman unshare` enters and where the engine's mounts are, unmounts what is still mounted below the
folder, reading the mounts again after each pass, since mounts stack, until none is left there, a
pass unmounts nothing, or `UNMOUNT_PASSES` passes have run, and says what is left with why its last
unmount failed (a killed engine leaves its storage's mounts there, the layer of a build's working
container among them, which `podman rm --all` does not remove) and empties its storage; one a killed
run left is removed by the next run that starts one. `tests/test_podman_service.py` holds an
engine's storage in its own folder, its service and its own podman calls run there, and an
engine removed whole, with its processes and its folder, when its run ends and when the next run
finds it killed with a container running. Every module
of `tests/container/` is collected wherever the suite runs, and every test in it uses the engine,
so carries the container marker; `tests/test_conftest.py` holds every module to that, with a
twin.

What a machine needs to run them: rootless Podman for the user, with subordinate ids for every
slot the suites use (`tests/container/sizes.py`), a `newuidmap` and `newgidmap` that work for
the user, `catatonit`, the init the workspaces' shared configuration names, and `passt` with
`/dev/net/tun`, which the test of the product's network and the containers it nests need (a
workspace of rcman's has both); the images come from their registries, so the network;
and a `/tmp` that is not overlay, since the engine's storage is native overlay (`driver="overlay"`,
no mount program) in a folder under `/tmp`, and overlay over overlayfs is refused. A workspace of
rcman's has its own `/tmp`, a volume off its overlay root ([runtime.md](runtime.md#the-runtime));
the repository's `.devcontainer` has the rest of it
(`tests/test_repository_devcontainer.py` holds its pins). In such a workspace, `make check` runs
`TEST_WORKERS` workers (the `Makefile`), each starting tmux servers, git and the fake `claude`,
and `make screenshot` runs Chromium: a workspace whose limits on processes or memory are below
what those take fails them, and its limits are an administrator's
([users.md](../docs/users.md)).

The runtime on the engine is made with the sizes of `tests/container/sizes.py`, given as
`PodmanConfig`'s keyword-only fields (see [runtime.md](runtime.md#the-runtime)): a smaller range
per slot, people in the first slots and the builder after them, the engine's own network (but
for the one test of the product's network, below), and no slices, which Podman takes only with
cgroups and the systemd manager. The engine's own network is the network namespace of the user
who runs the suite, so a test workspace on a developer's machine shares that machine's network
and reaches its loopback, which a workspace of rcman's does not; nothing the suites serve listens there on a fixed port: the stand-in GitHub
(`tests/container/test_builder_in_podman.py`) listens on a port of its own on the engine's
loopback, and the workspaces, in a network of their own, reach it as `github.com:443` through
the relay (`Relay` in `tests/container/network.py`). A range of the
suites' size holds no id as high as the group `nogroup` (65534), which apt drops to: nothing in a
test workspace or build runs `apt-get` as Ubuntu ships it. `tests/container/conftest.py` holds the
shared fixtures: the tools as the installer writes them, tmux built on the engine with the
installer's own argument list from its pinned source in the toolchain (the user's cache, filled
with the installer's own download code by the `toolchain` fixture), an empty store and one
holding the fake `claude`, the default image and the builder image built on the engine from the
installer's own files, and the helpers that make a runtime, rcman's settings and the stand-ins
on the engine. The settings never keep a path of an installation's (`/var/lib/rcman`). Every
image a session's fixture builds or pulls (the default, the builder and the system image, and
the default image's base) is held there
for the session by a container that never starts (`hold` in `tests/podman_service.py`): a
service deleting a workspace collects the images nothing refers to, and a later module would
find one gone.
Every run of an image built or imported on the engine, and every build from one, says
`--pull=never`: a pull of a `localhost/` name would ask the machine's own `https://localhost`
for it, which may be another service's.

A network of its own is a holder container with no network but its own loopback
(`tests/container/network.py`): Podman refuses a container outside a pod the network of a pod's
container, and gives a container that joins another's network a hosts file of its own, so the
workspaces join `container:<holder>`, and their image, derived from the default one, has the
stand-in's certificate authority and git finding github.com on that loopback (`dockerfile` in
`tests/container/github_stand_in.py`). A server the test runs itself serves on a socket that a
helper made inside the holder's network and handed back.

The tier is a small core: one basic happy-path test of each thing only real Podman proves, and
nothing else (no variants, refusals, edge cases or timing).

- `tests/container/test_runtime_in_podman.py`: three basic cases of the runtime contract
  (`tests/runtime_contract.py`) on Podman: a workspace made, running and removed; an exec
  answering; and its files outliving a stop and start. And one workspace on the product's own
  network, `NETWORK`: it has a network namespace of its own; through `169.254.1.2` a listener of
  the test's on all the machine's addresses answers and one on its loopback refuses at once; and
  a container nested in it with `--network=pasta` runs.
- `tests/container/test_restored_in_container.py`: a workspace running again after rcman restarts.
- `tests/container/test_builder_in_podman.py`: an image built for real from a devcontainer.json
  (a Dockerfile and a local feature) and a workspace running from it.
- `tests/container/test_nest_in_podman.py`: a container running inside a workspace.
- `tests/container/test_terminal_in_podman.py`: what is typed reaching the Terminal tab's shell and
  coming back.
- `tests/container/test_forward_in_podman.py`: a forwarded port answering through the relay.
- `tests/container/test_install_in_container.py`: the first installation putting rcman in Caddy
  and restarting it.

#### The stubbed installation

`tests/container/test_install_in_container.py` runs rcman's real installer, through
`install.sh`, as root in a container of the system image (`tests/container/system/`, built on the
engine by `tests/container/system.py`), with no network: real accounts made by `useradd`, real
files, owners and modes, and `runuser`; and `systemctl`, `loginctl` and rcman's `podman` the
stand-ins of `tests/container/stand_ins.py`, mounted over the programs. They record every call,
and answer as the real programs answer what the installer asks: a unit the installer starts is
run as its `User=`, with its environment, folder and umask, so rcman and oauth2-proxy really run
and the installer's own health check is the real one; rcman's Podman reports the runtime the
installer checks, notes the images it builds, and hands the installer the tmux built on the
engine. A path unit started, as `rcman-upgrade.path` is, is only noted active and runs no
service, since nothing changes its path; `tests/test_stand_ins.py` holds that, with a folder of
planted units the stand-in reads in place of `/etc/systemd/system`, named by
`RCMAN_STAND_IN_UNITS`. A unit that is not there, such as `caddy.service`, is only noted active
when it is restarted, and one never started, such as `caddy-api.service`, is neither active nor
enabled: the installation's Caddy configuration is the package's Caddyfile, which the installer edits and
checks with the real `caddy validate`, and a restart of Caddy is a recorded call. The checkout's files are at `/src` (see the snapshot, below); `tests/container/clone.py`
makes them a git origin and a clone of the user `dev`'s, and `tests/container/installed.py` runs
the installation with the fake `claude` as the release it downloads. The frontend's and the
backend's packages come from caches filled beforehand in a container of the same image with the
network (`offline_caches`), so npm and uv run offline; the toolchain is a copy of the user's
cache, which the installer, as root there, takes its lock in. The first installation is made once; the
one test runs its check of `tests/container/install_checks.py` of the same name, as root in the
container (see [installer.md](installer.md#tests)).

#### What the container suite holds of its own

`tests/test_container_suite.py`: the engine usable; a checkout that is a git worktree reaching a
container as a snapshot that `tests/container/clone.py` commits; and a run's own containers going,
with their volumes, when it ends, and no other's (see
[What a killed run leaves](#what-a-killed-run-leaves)). What a container sees of the
checkout is a snapshot of its files, made once per pytest session by the `source` fixture in
`tests/conftest.py` with `tests/snapshot.py`, which is not a test module, and mounted read-only at
`/src`: the files git tracks and those untracked that it does not ignore, as they are on disk,
chosen by the host's git under the fixtures' environment, so that no inherited `GIT_*` variable,
global configuration or personal ignore file changes the choice. An untracked file named like a
credential, as the pre-test hook judges one (see [Pre-test hook](#pre-test-hook)), is left out. The
snapshot holds no `.git`, so the suite runs the same from a clone and from a git worktree. The
snapshot is removed when the session ends.

## What lives where

| Path | Contents |
|---|---|
| `tests/` | The test suite; `tests/test_<name>.py` tests the module `<name>.py`, wherever it is, `tests/` included, where there is one |
| `tests/fakes/` | The fake `claude`, its recorded fixtures and the screens observed on a live server — see [fake-claude.md](fake-claude.md); Podman's default seccomp profile as recorded (`podman-seccomp.json`), which `tests/test_seccomp.py` derives the workspaces' profile from; Podman's help for the subcommands `publish.sh` reads and for its global options, recorded from the default image's Podman (`podman-help/`), which `tests/test_podman_wrapper.py` holds the wrapper's lists to; and what the monitor reads of Podman, recorded from Podman on the tests' engine (`podman-monitor/`: `system df -v` with one workspace and with two, `container inspect`, `podman info` and `system df --format json`'s summary), which `tests/test_monitor.py` reads; `podman images -a --format json` of a plain build's leaf, its nameless step image and its base, recorded from Podman on the tests' engine (`podman-images/images-a.json`), which `tests/test_runtime.py` reads, these and the seccomp profile copies of one point in time, recorded again when the product starts failing against real Podman; and Ubuntu's own `/etc/caddy/Caddyfile`, the caddy package's conffile (`ubuntu-Caddyfile`), which `tests/test_install.py` edits with the installer's `edit_caddyfile` and holds to the package's md5 |
| `helpers/` | What the backend shares with the installer and the fake `claude`, the standard library only: the rules values are refused by, what Claude's screens mean, running git, Claude Code's releases and their checked download (`claude_release.py`), and the checks a Claude Code version is held to with the canary program that runs them (`claude_checks.py`, `claude_canary.py`) |
| `backend/` | The FastAPI application — see [backend.md](backend.md) |
| `frontend/` | The React application, its component tests and the screenshot capture — see [frontend.md](frontend.md) |
| `tests/container/` | The container suites, what they run in their containers, and the system image they run rcman's installer in (`tests/container/system/`) |
| `install.sh`, `installer/` | The installer — see [installer.md](installer.md) |
| `devtools/` | Development tooling, run by hand, by the edit-time hook or by the tests, which never imports the tests; each module's docstring says what it is |

## What a killed run leaves

A run that ends normally, or with Ctrl-C, removes what it made. A run killed
by a signal — SIGTERM or SIGKILL to its process group, as a harness timeout
or a closed terminal sends — runs no handler of its own, and a tmux server it
started has detached into a session of its own and keeps running. A test process the
[hang guard](#the-hang-guard) ends is the same, but for the processes below it, which the
guard kills; what escapes that is a process that detached from it, as a tmux server or an
engine's conmon does. So nothing
a test makes depends on its own run to remove it:

- What a test makes under its `tmp_path`, pytest removes in a later run.
  Fixture git's home, `FIXTURE_HOME` in `tests/git_fixtures.py`, is a folder
  that does not exist, so nothing is made for it.
- What has to be in `/tmp` is a held folder, made by `held` in
  `devtools/held_folders.py`: `tmux_sockets` there for a test's tmux
  sockets, and every other folder the tests and the recorder make in `/tmp`.
  A held folder is named with its prefix and the characters `mkdtemp` adds,
  and holds a lock file, `.lock`, locked for as long as the folder is used;
  the lock is taken under another name and then renamed, so a lock file is
  held from the moment it appears. Afterwards the folder is removed: the tmux
  servers on its sockets are stopped first, and the lock file is removed
  last, so a run killed while removing it leaves a lock file nothing holds.
- Before a run uses a held folder, it clears those of the same prefix that a
  killed run left: one whose lock nothing holds, and one without a lock file
  for longer than `UNLOCKED_FOR` in `devtools/held_folders.py`, which only a
  run killed while making or removing it leaves. The name must be the prefix
  and exactly what `mkdtemp` adds, so a sweep never takes another prefix's
  folders. It leaves a folder whose lock is held, and anything that is not a
  folder of the current user's with, if any, a lock file of theirs. It stops
  only tmux servers whose sockets are the current user's, never one a link
  points at, and among those only one a tmux server of the user's listens on.
  Which program listens it tells from the listener's credentials, through a
  connection that sends nothing: the pid and uid the kernel gives, and the
  program that pid runs, a tmux replaced on disk since it started among them;
  so it never speaks tmux to another program's socket. On a socket nothing
  listens on, or one gone meanwhile, it runs no tmux. Another program's or
  another user's listener, a killed run's engine among them, a listener it
  cannot tell, and a tmux server that does not answer its `kill-server` within
  `TMUX_GONE_WITHIN`, it leaves and says so on stderr; the socket's file goes
  with the folder. What it cannot read or remove, or what a removal refuses or fails at in a way of
  its own (the engine's removal failing, say), it leaves, says so on stderr, and goes on past, to
  the others and to the run's own folder; one another run removed first it passes over in silence. The
  screenshots' world (`devtools/world.py`) is a held folder too.
- A held folder that may be filled with anything, as the screenshots' world is, is removed
  with `remove_tree`, which also
  stops a tmux server anywhere in it, by the same rule, and makes each folder of the user's in
  it removable, following no link.
- Every removal refuses, and raises, for a path that is not absolute or holds
  `.` or `..`, for the repository, the home folder, the working folder and
  anything holding them or inside the first two, and for anything outside
  `/tmp`, or outside the root its caller names.
- An engine a killed run left is removed by the next run that starts one, with
  every container and volume in it and whatever is still mounted in its storage (see
  [The container suites](#the-container-suites)); the processes of the engine
  it kills are waited on until none is running, by the same rule as a killed
  run's (below).
- The containers the suite starts itself on the engine are labelled by
  `tests/containers.py` with the path of a folder the run holds, its holder
  (`rcman.test.holder`), and the run removes them when it ends, with their
  anonymous volumes and never a named one, so the only volumes the tests give a
  container are anonymous: one goes wherever its container goes. Nothing sweeps
  for another run's: every session has an engine of its own, and a killed run's
  engine goes whole with everything in it (above).

`tests/killed_runs.py`, which is not a test module, tests all of this as the
suite runs: a real `python -m pytest -n 2` over small test files, in a session
of its own, killed with SIGKILL; the test then checks that
the next run clears what the killed one left. A killed run is gone once no
process of its session is running, by `running` in `devtools/held_folders.py`:
a process runs while its state is not Z, or is Z with other threads still
running, which is how a process whose first thread has ended reads; such a
process still holds its open files, and with them a held folder's lock. The
test reads what the small tests made from reports written under another name
and then renamed, only once a report is complete; it takes a path from a
report only under the test's own `tmp_path`, itself a folder a removal may
take, or in `/tmp` under the test's stem, a held folder whose name and `-`
begin everything the test makes beside it, so tests that run at the same time
never clear each other's; and its `scrub` refuses anything else, as every
removal does.

## Repository guards

These hold the repository to its own rules. Each has planted twins that prove
it can fail, and a positive control that keeps an empty input from passing.

- **No test waits on real time** (`tests/test_timing_guard.py`). The timing guard of [Time in
  tests](#time-in-tests): a static scan of the tests, the Python and the frontend's
  TypeScript, for clock comparisons, sleeps, timeouts other than the tier's guard, configs that
  leave a limit unset and shortened product deadlines, whose only exemptions are its
  named patterns, each with a stale check. Planted twins prove each form is flagged and each
  allowance clean, and the scan of the real tree fails where it saw nothing.
- **References resolve** (`tests/test_docs_guard.py`). Every relative link
  and every `*.md` mention in any tracked or untracked-but-not-ignored text
  file must name a file or directory in the repository. A link resolves
  relative to the file it is written in; a mention also resolves from the
  repository root or by bare file name. Tests that plant references build
  them at run time so their own source holds none.
- **Current state only** (`tests/test_docs_guard.py`). `README.md`,
  `CLAUDE.md` and every Markdown file under `docs/` are scanned for
  historical phrasing, outside code. The phrase list in
  `devtools/docs_guard.py` catches known phrasings only; review enforces the
  rule itself. `CONTRIBUTING.md` and `project-knowledge/` are outside the scan
  because they quote those phrasings in order to forbid them.
- **Every page is in the contents list** (`tests/test_docs_guard.py`). Every
  Markdown file under `docs/` and `project-knowledge/` has a link in
  `CLAUDE.md`; a page named there without a link does not count. The pages are
  read from the file listing of the corpus, so a new page is needed in the
  table at once.
- **No privilege boundary** (`tests/test_no_privilege_boundary.py`). rcman
  has no command that raises privilege, no root helper and no switching of
  accounts in product code, and its services run as their own accounts.
  Three layers read the corpus of `devtools/docs_guard.py`, this module included,
  which builds the command from pieces so that it does not hold it itself:
  - *The command that raises privilege.* Product code — `backend/`,
    `helpers/`, the web interface's sources but not its tests, and the
    installer's templates, workspace image and builder — holds it in no form
    at all, `sudo rcman-admin` included, and has no exemption. Everywhere else
    it must be followed by a form of `ALLOWED_FORMS` or stand after a word of
    `ALLOWED_PROSE`; both are allow-lists, each entry with its reason, so a
    form nobody listed fails, and each entry fails the stale check when the
    corpus no longer uses it. The variable of the account that ran it is not
    the command.
  - *Calls into the system's accounts.* An AST layer over the Python files of
    `backend/` and `helpers/` finds reads of the user and group databases
    (`pwd.getpw*`, `grp.getgr*`), changes of the process's identity
    (`os.setuid` and its kin) and a string that is the program that makes or
    switches accounts or raises privilege (`ACCOUNT_PROGRAMS`). `installer/` is
    outside it: it is the one program that makes rcman's accounts. `EXEMPT`
    lists the right sites by file, function and text (`rcman-admin` running itself again as
    rcman is the one account switch), each with the reason;
    each covers exactly one site, and a test fails when its site is gone.
  - *The services' accounts.* Every `.service` template under
    `installer/templates/` has a `User=` naming an account that is not root,
    but those of `ROOT_SERVICES`, each named with its reason: there is one,
    `rcman-upgrade.service`, the upgrade from the web interface, off by default,
    which runs the clone's `install.sh` as root. Its reason states the risk
    accepted with it: whoever can push to `main` runs code as root, through
    `install.sh`, `installer/` and `uv sync` building the project as root, so a
    GitHub token stored in rcman that can push is a root credential, and while
    the option is on the clone owner's account is root without a password. A
    root service is held to what that reason covers: its `ExecStart=` and
    `ExecStopPost=` are exactly `ROOT_SERVICE_COMMANDS`, the wrapper under
    `/opt/rcman/bin` with `python3 -I` and its arguments, with nothing after them
    (a `;` would add a command), it has no other `Exec` key and no
    `EnvironmentFile=`, names no path under `/var/lib/rcman`, and has the
    installer fill in only `ROOT_SERVICE_PLACEHOLDERS`. A stale check fails
    when its template is gone or names an account.

  Each layer has planted twins in a scratch repository, the plant confirmed
  before the guard is shown firing, and an empty corpus, or one without a
  Python file under each root or without a service template, fails the guard.
- **The guide lists everything the installer makes** (`tests/test_install.py`).
  The "Removing rcman" section of `docs/installation.md` names every path,
  account, group, unit, command and package the installer's constants hold, as
  [installer.md](installer.md) says; a constant added to the installer must be
  in the section or, for what the installer only reads, in `NOT_MADE` with a
  reason.
- **No fixed shared paths or ports** (`tests/test_fixed_resources.py`). Tests
  run at the same time, so none may hold a fixed path under `/tmp` or a fixed
  unprivileged port. It reads the Python files in `tests/`, `tests/container/` among them
  but for the files that run only inside a container of the suites' own, whose `/tmp` and
  ports are that container's (`IN_CONTAINERS` in the test, each with its reason), every Python
  module in
  `devtools/`, which the tests and
  the recorder run, and every TypeScript and JavaScript file in `frontend/`
  that git does not ignore, of the kinds `SOURCES` in `tests/typescript.py` names — the
  component tests, their config, the screenshot capture and the rest — parsed with Vite's parser from
  the frontend's packages, so a file that does not parse, or packages that
  are missing, fail it. It catches the same forms in both languages. `FORMS`
  in the test spells each form in both, and an entry one language does not
  spell fails it; `CLEAN` does the same for what it leaves alone. The forms:
  - a path: `/tmp` or a path in it as a string, a formatted string — an
    f-string or a template literal — beginning with one, and the system's
    temporary folder, `tempfile.gettempdir()` or `tmpdir()`;
  - a fixed port, written as a number or a string of digits, held in a
    module-level name, converted or cast, in a formatted string that shows
    nothing else, or given as the fallback of a value that may be missing —
    after `or`, `??` or `||`, or as the default of a call `READS` in the
    test names, such as `os.environ.get()` — in any of these places:
    - in an address: a host and the port in a tuple, a list or an array;
    - beside an IP address or `localhost` as a call's arguments, either
      first;
    - given to a name, attribute, key, property, parameter or keyword
      argument named `port` or ending `_PORT`, in any case: assigned, as a
      parameter's default, or to a call that names it first, as `setenv`
      and `stubEnv` do. A key counts only where it is written as a string.
      A module-level name, declared or assigned at the module's top level,
      is not a site itself, but is followed to where it is used, except
      into a function with a parameter of the same name; names given values
      together, in one assignment of a tuple, are followed each as one alone;
    - as the first argument of a `listen` method;
    - after `--port`, and after `-p` or `--publish`, alone or as the host
      port of docker's mapping, in a list of arguments, written whole or
      formatted, or in a command written as one string;
    - after a host and its colon — in a URL, any host after its scheme, and
      otherwise after an IP address or `localhost` at the start of the text
      or after a character `BEFORE_HOST` in the test names — in one string,
      in a formatted string, or joined to it with `+`.

  It leaves alone a uniquely named file or folder made in `/tmp` itself:
  `/tmp` or the system's temporary folder given as the folder that a call
  making a unique name makes it in — the `dir=` of such a `tempfile` call,
  and the folder of a `join()` of a folder and a prefix given to `mkdtemp`
  or `mkdtempSync`. It leaves alone, too, a string standing alone as a
  statement, as a docstring does. It does not follow a port held in a
  function's own variable not named for one, imported or computed, or a
  path built in pieces; review covers those. In TypeScript a path given as
  a call's first argument is reported as the call, so `join()` is read with
  the rest of the path. Its planted twins write the path and the port as
  placeholders, so that the guard, reading its own module, finds neither
  there. Each exemption it lists covers one site, named by its file, the
  function or test it is in, if any, and its text, and fails when that site
  is gone.
- **The container marker is derived** (`tests/test_container_marker.py`). In
  the collected suite the tests carrying `container` are exactly those using
  one of `FIXTURES` in `tests/container_marker.py` (the tests' engine,
  `podman_service`), and no source under `tests/` places the marker by
  hand, as `mark.container`, `getattr` of it, or its name given to
  `add_marker` or `applymarker`, or asks for the fixture by name with
  `getfixturevalue`, which leaves it out of a test's fixture names; a name
  held in a variable is not followed. `tests/collected.py`, which is not a test module, collects the suite
  in a child process for it.
- **Every test runs, and container tests cannot skip** (`tests/test_make.py`).
  It runs `make test`, `make test-container` and `make check` with `uv`, `npm` and `npx`
  replaced by recorders, and reads which tests each pytest command they ran selects from
  pytest's own collection: every test is in exactly one of `make test`,
  `make test-container` and `make benchmark`. The benchmark's part of that is
  read without make, from the Makefile's rules and pyproject's `addopts`, each pytest call's
  `-m` worked out for every set of the markers a test may carry: `make benchmark` runs the
  `benchmark` tests, in one process, with `-s`, no other
  target's pytest call, nor a bare `pytest`, runs one, and no target makes `make benchmark`, as
  a prerequisite or with `$(MAKE)` in its recipe; the two other runs' split is read from
  what make ran. `make test-container` runs the `container` tests, with
  `RCMAN_REQUIRE_CONTAINER=1`; `make check` runs the host tests with
  `RCMAN_REQUIRE_REAL_CLAUDE=1` and holds no on-demand run; none of them runs a test a
  bare `pytest` leaves out for a real Claude account, as a target's own `-m` replaces the
  `-m` of pyproject's `addopts`; and every test that any target
  `.PHONY` names runs is under its tier's hang guard, as pytest-timeout itself works out
  each test's settings (see [The hang guard](#the-hang-guard)). `make lint` and
  `make format` run ruff and the frontend's script of the same name, and the
  real `make lint`, run over a frontend file planted in a scratch tree, checks it
  and fails when Biome would reformat it. Every rule Biome runs
  at info, as `biome rage` and `biome explain` report, is raised to a warning
  in `frontend/biome.jsonc`.
- **Every make target is documented** (`tests/test_docs_guard.py`). This
  file has a table row whose first cell is `make <target>` for exactly the
  targets the `Makefile` defines. The targets are read from make's own
  database (`make -pRrq`), so a comment, a variable, a target-specific
  variable, a special target such as `.PHONY`, a `define` block's body or a
  branch make does not take is not mistaken for one.
- **No tracked link** (`tests/test_export.py`). No file git tracks is a
  symbolic link (mode 120000 in `git ls-files -s`): a release is built from what
  git tracks, and the upgrade from the web interface refuses a clone with a link
  where git tracks a path. A link committed in a scratch repository is its
  planted twin, and the real index is shown to be read.
- **Deployment contents** (`tests/test_export.py`). Deployment uses
  `git archive`, so `.gitattributes` decides what is deployed. The test
  checks that every development-only file is marked `export-ignore` and that the
  files a deployment needs are not; a scratch repository whose `.gitattributes`
  no longer marks a development folder is its planted twin.

## Screenshots

`make screenshot PAGE=<path>`, with `STATE=<state>` or without, captures one page for someone to
look at, as in `make screenshot PAGE=/settings` or
`make screenshot PAGE=/ STATE=rebuild-failed`; it asserts nothing, and is neither a test nor part
of any test run, and it sets no time limit and waits on no length of time: Ctrl-C ends a run that
hangs. It builds the frontend, then `devtools/screenshots.py` starts the world's backend
(`python -m devtools.world`, see [frontend.md](frontend.md#the-screenshots)), reads the origin it
prints, and runs `frontend/screenshots/capture.ts` with Node against it, which uses Playwright
as a library to drive Chromium; then it closes the backend's standard input, and the backend
shuts down and removes its world. Its exit code is the capture's.

`PAGE` is a path the frontend routes (`src/Pages.tsx` and `src/AdminPages.tsx`, from which the
capture reads them), a `:name` segment standing for any one, as `/help/:slug` does for every
help page. Without `STATE` the page is shot as the world draws it; a state is one of the views a
page shows only when asked, such as an open dialog or pop-up, or a planted answer, each named in
the capture's table of states with the pages it is a state of. A page or state the capture does
not know, or a state the page does not have, ends it before it starts the browser, with a
non-zero exit code and the pages and states there are. A page is taken at a phone's width
(`isMobile`) and a desktop's, in the light and the dark scheme, each in a browser context of its
own, as the file names; a state only a phone has is taken at the phone's width only. The widths
are in the file. Each screenshot waits for what its view shows, then for every request the page
made to be answered and what the answers drew to be drawn, its animations ended. The world's
workspaces are plain ones: the capture plants the screenshot workspace's repository, its status,
branches and address, in what the page fetches. It writes to
`frontend/test-results/screenshots/`, ignored by git, which it empties first, a file per
screenshot named by the page, the state, the width and the scheme. The pre-test hook does not
count it as a test run.

## Edit-time hook

`.claude/settings.json` wires `devtools/on_edit.py` as a PostToolUse hook on
file edits in Claude Code sessions. It runs the edited file's paired test:

- `<dir>/<name>.py` runs `tests/test_<name>.py`;
- a test file runs itself;
- a Markdown file runs `tests/test_docs_guard.py`;
- `frontend/src/<name>.ts(x)` runs `<name>.test.ts(x)` beside it with Vitest.

Pairs are found, and tests run, in the checkout the edited file is in, which
git names from the file's own folder: the wiring starts the hook from the main
checkout, and a worktree under `.claude/worktrees/` lies inside it. A Python
test runs with that checkout's packages ahead of any the hook's Python has
installed.

A Python module outside `tests/` with no paired test is reported; a
package's `__init__.py`, and a module in `tests/` that is not a test module,
run nothing. The hook is silent when the
test passes; on failure it reports to the operator and to the agent. Its own
tests, including one that checks the wiring in `.claude/settings.json`, are in
`tests/test_on_edit.py`.

## Pre-test hook

`.claude/settings.json` wires `devtools/pre_test_commit.py` as a PreToolUse
hook on Bash commands in Claude Code sessions. Before a command that runs
tests, it commits and pushes the checkout the tests run in, as the working
rule [work is committed and pushed before a test command
runs](working-rules.md#tests-and-guards) requires:

- with changes, untracked files included, `git add -A` and a commit with the
  fixed message `WIP before tests` and a `Co-Authored-By` trailer; nothing
  of the command goes into it;
- with a branch that has no upstream, or whose upstream on origin it is ahead
  of, `git push -u origin` and the branch; with an upstream on another
  remote, `git push origin` and the branch, without `-u`, when it is ahead
  of `origin/<branch>`.

It acts only in a checkout of the project's own repository, one with the
same git common folder as `$CLAUDE_PROJECT_DIR`, so a worktree of it counts
and another repository does not. Within that, it commits nothing on `main`,
on origin's default branch or on a detached HEAD, nor during a merge,
rebase, cherry-pick or revert, and says so when there are changes. An
untracked file that is a nested repository, is larger than `MAX_FILE_BYTES`
or has a credential's name — one `CREDENTIAL_NAMES` matches, in any case,
as `named_like_a_credential` judges it — all in the module, is
left out of the commit and named to the operator and the agent; the rest is
committed and pushed. An upstream that is configured but gone, as when a
merged branch is deleted, is reported and not pushed again. It never passes
`--no-verify`: a commit the repository's own git hooks refuse is reported,
and the index is put back as it was before `git add -A`.

It never blocks the command: it makes no permission decision and exits 0 on
every path, a failure of its own included, and the wired command exits 0
without running anything when the project folder has no
`devtools/pre_test_commit.py`, as a checkout of an older branch has. Its git calls share one deadline,
`DEADLINE` in the module, and the longest it can run, `LONGEST_RUN`, is
inside the timeout the wiring gives it; `git add`,
`git commit` and `git push` stopped at the deadline are reported as such.
Git runs with no terminal prompt, and ssh, unless configured otherwise, in
batch mode. A failure is reported to the operator and to the agent; a commit
or push it made, with the untracked files the commit added, is told to the
agent.

Which commands run tests is read from the command alone, as the shell splits
it, and a command that runs none starts no process. One runs tests when it
is pytest or Vitest, without an option such as
`--version`, `--help` or `--collect-only` (`NOT_A_RUN` in the module), once
shell keywords, assignments and wrappers such as `uv run`, `npx`,
`python -m` and `timeout` are taken off; an npm script whose text does; or a
make target whose recipe or prerequisites do. The Makefile is read as text,
never run: its rules and their prerequisites, followed to the end, but not
its variables, conditionals or included files. Where the tests run follows a
`cd` at the start of the command, outside any subshell, and make's `-C`,
from the session's working directory; nothing else moves it, and a `cd` to
a command substitution, `$(…)` or backticks, leaves it in that directory. The module
docstring lists what it takes off.

What it does not see: a command inside `bash -c` or another shell's
argument, or after `xargs` or another wrapper it does not take off;
a `cd` after the start of the command, in a subshell, or a `pushd`; where a
command substitution would take it; and the
paired test the edit-time hook runs after an edit, which is not a Bash
command. Before such a run, the working rule is kept by hand.
