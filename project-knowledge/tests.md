# Tests

How the test suite is organised and run. The rules the suite follows are in
[working-rules.md](working-rules.md).

## Running

| Command | What it does |
|---|---|
| `make setup` | Creates the development environment: `make frontend-setup`, then `uv sync` |
| `make test` | Runs every Python test that runs on the host, in parallel with pytest-xdist: all but those marked `container`, and those marked `real_claude`, which no target runs. `TEST_WORKERS` (in the `Makefile`, which says why) is the number of workers; `--dist worksteal` gives an idle worker the queue of a busy one |
| `make test-container` | On demand: runs the Python tests marked `container`, except the whole-stack one, in one process; the `Makefile` says why. It sets `RCMAN_REQUIRE_CONTAINER=1`, so they fail instead of skipping where the user's rootless Podman cannot run |
| `make check-ui` | On demand: runs the whole-stack test, the one marked `stack`, in one process; it needs `make frontend-setup` once, like `make e2e`. It sets `RCMAN_REQUIRE_CONTAINER=1` |
| `make lint` | Checks formatting and lint: ruff for the Python code, Biome for the frontend, with the frontend's packages `make frontend-setup` installs |
| `make format` | Applies formatting and safe lint fixes: ruff for the Python code, Biome for the frontend |
| `make frontend-setup` | Installs the frontend's packages and Playwright's browser |
| `make frontend-test` | Type-checks the frontend and runs its component tests |
| `make frontend-build` | Builds the frontend into `frontend/dist` |
| `make e2e` | Builds the frontend and runs the browser tests |
| `make screenshots` | Not a test: builds the frontend and captures every page, at a phone's and a desktop's width, light and dark, into `frontend/test-results/screenshots/`, for a look at the design; see [Screenshots](#screenshots) |
| `make check` | Lint, `make test`, the frontend tests and the browser tests; no on-demand run. It sets `RCMAN_REQUIRE_REAL_CLAUDE=1`, so tests that would skip for want of the real `claude` fail instead |
| `make check-all` | `make check`, `make test-container` and `make check-ui`, one after the other: for the end of a change and before the live reinstall |

`make check` is the gating check; `make test-container` and `make check-ui` are
the on-demand runs. When each runs, and how a run is read, are working rules:
[the full check runs at the edges of a change](working-rules.md#tests-and-guards),
and the on-demand runs happen once, at the end, when required. The pre-test
hook counts every one of these targets as a test run (see
[Pre-test hook](#pre-test-hook)).

Run pytest from the repository root; `pyproject.toml` holds its configuration.
The Python tests are split by where they run, not by how long they take:
`make test` runs everything that runs on the host, slow tests included, and a
slow host test is made cheaper or split into cases that workers share rather
than moved out; the tests that need a container, marked `container` (see
below), run on demand: `make test-container` runs them but the whole-stack
test, which carries the `stack` marker as well and which `make check-ui` runs.
`tests/test_make.py` holds the split: every test is in exactly one of
`make test`, `make test-container` and `make check-ui`, `make check` holds no
on-demand run, and `make check-all` runs every test. To work on
what a container suite covers, run its module directly, as in
`RCMAN_REQUIRE_CONTAINER=1 uv run pytest tests/container/test_github_in_podman.py -k gh`.

What keeps `make test` fast, none of it losing an assertion: it runs
`TEST_WORKERS` workers with `--dist worksteal`; an autouse fixture in
`tests/conftest.py` opens every database the backend opens in the test process
with SQLite's `synchronous = off` (a backend started as a subprocess keeps the
default), since no test is about surviving a crash and production
keeps the default; the collections `tests/test_make.py` reads are made once per
run and shared by all the workers through pytest's basetemp
(`tests/collected.py`); and `tests/test_e2e_server.py` calls the Playwright
binary directly, without `npx`. The channel's latency test (`tests/test_channel.py`)
takes up to a few batches of requests and fails only when every batch is over
the bound, as a busy host can push one batch over it.
The suite needs `tmux`, `git` and procps' `kill` on the host. Each test's tmux
sockets are in a folder of its own, made under `/tmp` by `tmux_sockets` in
`devtools/held_folders.py`, because a socket's path has to stay short, and removed,
with every tmux server in it, when the test ends; one a killed run left is
removed by a later run (see [What a killed run leaves](#what-a-killed-run-leaves)).

Workspaces in the backend's tests run on the host runtime (see
[runtime.md](runtime.md#the-runtime)): a folder and a tmux socket per workspace,
the fake `claude`, real tmux, and a real channel through the host's `sh`.
`tests/runtime_contract.py`, which is not a test module, holds the cases every
runtime keeps, which `tests/test_runtime.py` runs against the host runtime and
`tests/container/test_runtime_in_podman.py` against real Podman on the tests' engine, and
its `PODMAN_CASES`, which hold what only the Podman runtime does, such as the instructions for
Claude bound read-only at Claude Code's managed path, and which only
`tests/container/test_runtime_in_podman.py` runs.

The tools' `podman` and `docker` wrapper's reading of a published port,
`backend/rcman/podman/publish.sh`, is tested on the host by `tests/test_podman_wrapper.py`,
sourced as the wrapper sources it, by the static busybox's shell; its lists of Podman's options
are held there to the help recorded in `tests/fakes/podman-help/`, and the recording to the
default image's Podman by `tests/container/test_nest_in_podman.py`.

Limits (see [runtime.md](runtime.md#limits)) are tested on the host runtime and a stand-in for
`systemctl`, which records what it is asked: `tests/test_limits.py` the pure module `limits.py`
(reading, showing, the slices' names, the properties, usage), `tests/test_runtime_limits.py`
`PodmanRuntime`'s calls, `tests/test_workspace_limits.py` setting, refusing below use, reporting,
restoring and dropping in `Workspaces`, `tests/test_people.py` and `tests/test_admin.py` the
routes and `rcman-admin limits`, and `tests/test_rules.py` the rules of the three values. On the
tests' engine the limits reach the stand-in for `systemctl` of `tests/container/stand_ins.py`, which
keeps a slice's properties as the user manager does until a restart, and answers the monitor's
one show of many slices with a block for each, nothing in use but CPU time that grows with the
clock, so that wherever the stand-in runs, the stack's **Monitor** page among them, every person
and workspace reads as one whole core busy; `tests/test_stand_ins.py` holds it to what rcman's
own `limits.py`, monitor and runtime read of it; what the kernel does with a slice is checked by
hand ([real-claude-checklist.md](real-claude-checklist.md)).

Everything git is tested against real git,
never a fake: `tests/test_git_run.py` holds the parsers of `helpers/git_run.py`,
`tests/test_repository.py` every verb of `repository.py` in host-runtime workspaces
through a real channel and `exec`, `tests/test_devcontainer.py` the pure module (`devcontainer.py`) and the guard that holds the administrators' devcontainer page equal to its policy, `tests/test_devcontainer_flow.py` a workspace made from a repository's devcontainer.json on a host runtime with planted images, builds on a host runtime that records what it is asked (the places for builds and the waiting line, a cancel, the failures in plain words, the restore removing orphans and failing a workspace whose built image is gone), `tests/test_rebuild.py` rebuilding a workspace on its volume on the same host runtime (telling that its devcontainer.json changed, refusals that leave everything as it was, the switch and putting the old container back, a restart of rcman in each phase, deletions in each phase from the service and from `rcman-admin`, the interlock with an upgrade, the images' two names, and the rebuild of a failed workspace that kept its volume), `tests/test_builder.py` the pure module `builder.py` (its argument lists, the reading of the builder's words, the pump, the copy of a repository with the host's own tar), `tests/test_runtime_build.py` `PodmanRuntime.build_image` against a stand-in for podman (the archive that is loaded, rcman's own names held across the load, the staging folders, a rebuild's next name and its promotion to the built one), `tests/test_headers.py` the security headers of every answer and the limit on a page's reports of failures, `tests/test_filelock.py` the lock file the service shares with `rcman-admin`, `tests/test_clone.py` a workspace made by cloning
(a phase of `creating`: its log, failures and their causes, token redaction, names,
the repository's page, the route, cancel by deletion, restart recovery)
on a host runtime that runs real git
and can answer a clone with what git would say or hold it as a running git, `tests/test_git_account.py` `gitaccount.py`
against a host runtime that records what it is asked (git's and gh's files, the failures of
a save and a forget, and the restore's writes of gh's files), and `tests/test_git.py` the
git routes. `tests/github_tokens.py`, which is not a test module, holds tokens of the shapes
the token rule lets in that YAML or a shell would read as something else. `tests/git_fixtures.py`, which is not a test module, makes real
repositories, their origins and their branches with the system's git, under
a fixture configuration of its own, with a home, `FIXTURE_HOME`, that does
not exist, so the configuration git reads is only what a test plants;
fixture git refuses every transport but a local repository's, so an http,
https, ssh, git or scp-like address a test gives it is refused before anything
is connected to. GitHub is `tests/github_server.py`: the
system's `git http-backend` behind a small server, which serves anyone a
read of a public repository, and takes a push, or a read of one made
private, only with its token as the password, under any
non-empty username, as GitHub takes a personal access token. Its `gate` event, when a
test clears it, makes every request wait, so that a clone is under way and stays so.
Nothing a host test drives reaches the real github.com: the autouse
`github_stand_in` fixture in `tests/conftest.py` gives every git command the host
runtime runs whose subcommand may reach a remote (`NETWORK_SUBCOMMANDS` in
`tests/git_fixtures.py`) a folder in place of `https://github.com/` and forbids the
ssh and git transports (`through_stand_in`), and such a command not given it is an
`AssertionError` (`assert_through_stand_in`). Any other git command is left as it is:
`remote get-url --push` rewritten would name the folder, and a push to GitHub would no
longer be known as one. The git of an argv is the first token named `git` that is no
`NAME=value`, so a variable rcman passes before it is not taken for it.
`tests/test_stand_in.py` holds the rewrite of each subcommand, and plants one it missed
to see the guard fire. The fixture sees only `HostRuntime.exec`, not a channel's `run`:
it covers rcman's git that reaches a remote because `repository.py` runs every such
call with `network=True`, through `exec`. The `github` fixture in
`tests/conftest.py` runs one for the session, with `GITHUB_TOKEN`. Git in a workspace of
real Podman, against the same stand-in served as `https://github.com`, is the container suites'
(see [The container suites](#the-container-suites)).

The Terminal tab is tested on the host runtime, whose shells are the host's own processes, found
in the host's `/proc` by a `sleep` of a length no other test uses: `tests/test_terminals.py`
drives `terminals.py` without a WebSocket (what is typed and shown, a resize, ^C, what an end
leaves and what it does not, open files, a replacement, an open only once the shell named itself,
a deletion or sign-out that crosses an opening, the pidfile's checks, a program named with a
newline, a process whose first thread has ended, kept in the process table and ended with its
session, and the sweeps, the opening's and the restore's among them), and `tests/test_terminal.py`
the route through the test client's WebSocket (who may open what, the frames, what closes a
terminal, the audit, nothing typed recorded or logged, the protocol held to backend.md's table and
to the page's, `frontend/src/terminalSocket.ts`, loaded with Node) and, for a refused upgrade, a
peer that vanished and a service that stops, through real uvicorn running `tests/e2e_server.py`
with the options of the unit's `ExecStart`. `tests/container/test_terminal_in_podman.py` runs on
the tests' engine, with the default workspace image built from the installer's build file and
workspaces made through the app: the shell through `podman --remote exec` on a tty of its own, a
resize reaching it (seen by the SIGWINCH a process in the container gets, as the podman client
passes a resize apart from what is typed, which can arrive first), ^P^Q passing through, a terminal closed as it opens leaving nothing, the
shell as root of the person's range and neither root nor the user on the host, `apt-get` in the
default image, the podman client's memory, and no process and no running exec of the terminal's
left in the container once it is closed. A remote exec's record stays after it ends until
Podman's own cleanup removes it, once the exit delay conmon is given has passed, so whether an
exec runs is read from Podman's API. The runtime contract's terminal case (`tests/runtime_contract.py`) runs on both
runtimes. In a browser, `frontend/e2e/terminal.spec.ts` types into the tab on the host runtime and
reads what the shell works out, narrows the window and finds the size `stty size` gives follow it,
opens the terminal in a second page, which takes it over, and finds a tab left at once and shown
again fitted to its screen; the last test of `frontend/e2e-stack/stack.spec.ts` opens it through
Caddy and oauth2-proxy in a workspace of real Podman, as the container's root, reloads it and ends
its shell. The page's side of the protocol, its states and its fakes are in
[frontend.md](frontend.md).

What needs a real Claude account, and what needs systemd, the kernel's limits or a real
machine, is checked by hand: [real-claude-checklist.md](real-claude-checklist.md). Tests marked `real_claude` are deselected by default
and run by hand with `uv run pytest -m real_claude`; what they need is in
[fake-claude.md](fake-claude.md).

### Known flaky test

`tests/test_claude_server.py::test_a_server_in_a_git_repository_starts_stops_and_resumes`
can fail in a parallel `make test`, at its last assertion: the ready read after
the resume has `session_name` `None`. It does not fail in repeated runs on an
idle machine. The cause is suspected, not proven: `wait_state` returns on the
first capture whose state is `ready` without looking at `session_name`, and
`_session_name` in `helpers/screens.py` gives `None` when the Capacity line is
the last line or the line after it is blank, so a capture between the pane
receiving the Capacity line and receiving the name line reads as ready with no
name. What would settle it is a probe of capture timing on an idle machine,
reading the pane repeatedly while the fake draws its ready screen. The test,
`wait_state` and `_session_name` are left as they are.

### The container suites

The tests marked `container` run on the tests' own engine, the same way on a developer's machine
and in a workspace of rcman's: `tests/podman_service.py`, which is not a test module, runs the
user's own rootless Podman as `podman system service` on a socket of its own, with the
workspaces' shared configuration (`backend/rcman/podman/containers.conf` and `registries.conf`)
and storage of its own given by flags (rootless Podman reads the places of no other
storage.conf), in a held folder under `/tmp` short enough for the run folder Podman takes
(`RUNROOT_MAX`). The `podman_service` fixture in `tests/conftest.py` starts one for the
session; it skips where the user's rootless Podman cannot run, unless
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
run left is removed by the next run that starts one. `tests/test_podman_service.py` holds this, the killed run among it. Every module
of `tests/container/` is collected wherever the suite runs, and every test in it uses the engine,
so carries the container marker; `tests/test_conftest.py` holds every module to that, with a
twin.

What a machine needs to run them: rootless Podman for the user, with subordinate ids for every
slot the suites use (`tests/container/sizes.py`; `test_the_sizes_fit` below holds the engine's map
to it), a `newuidmap` and `newgidmap` that work for the user, and `catatonit`, the init the
workspaces' shared configuration names; the images come from their registries, so the network;
and a `/tmp` that is not overlay, since the engine's storage is native overlay (`driver="overlay"`,
no mount program) in a folder under `/tmp`, and overlay over overlayfs is refused. A workspace of
rcman's has its own `/tmp`, a volume off its overlay root ([runtime.md](runtime.md#the-runtime));
the repository's `.devcontainer` has the rest of it
(`tests/test_repository_devcontainer.py` holds its pins). In such a workspace, `make check` runs
`TEST_WORKERS` workers (the `Makefile`), each starting tmux servers, git and the fake `claude`,
and the browser tests run Chromium: a workspace whose limits on processes or memory are below
what those take fails them, and its limits are an administrator's
([users.md](../docs/users.md)).

The runtime on the engine is made with the sizes of `tests/container/sizes.py`, given as
`PodmanConfig`'s keyword-only fields (see [runtime.md](runtime.md#the-runtime)): a smaller range
per slot, people in the first slots and the builder after them, the engine's own network, and no
slices, which Podman takes only with cgroups and the systemd manager. The engine's own network is
the network namespace of the user who runs the suite, so a test workspace on a developer's
machine shares that machine's network and reaches its loopback, which a workspace of rcman's does
not; nothing the suites serve listens there on a fixed port, and what has to be on a fixed one
(the stand-in GitHub on 443, Caddy, oauth2-proxy) is in a network of its own. A range of the
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
find one gone; `test_the_sessions_images_survive_a_collection_and_an_image_nothing_holds_does_not`
in `tests/container/test_engine_in_podman.py` holds this, with an image not held as its twin.
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

- `tests/container/test_runtime_in_podman.py`: the runtime contract, and what only Podman has —
  a workspace's uid range and its volume's owner, its init, no port published, the read-only
  mounts and the tmpfs, a person's credentials file their range's root's alone, a rename over it
  refused busy and a write in place shared, a sign-in container that removes itself, an
  unreachable socket told from a missing workspace, an exec running the image's own program
  and answering without its mark, the channel's latency and its podman client's memory; the
  `PODMAN_CASES` of the runtime contract; the instructions for Claude as a single file bound
  read-only where the image has no `/etc/claude-code`, readable with swapped ids, in place of an
  image's own `CLAUDE.md` with its other files there left visible, and a file renamed over on the
  host reaching the workspace at its next start and not before; and the image probe's reading
  of `/etc`, `/etc/claude-code` and the managed `CLAUDE.md` in real images, a link followed
  along the path and at its end; every command the default image's instructions name there
  in the default image; and what the monitor reads of Podman: the disk read naming a
  workspace's container and both its volumes, the recordings of `tests/fakes/podman-monitor/`
  held to this Podman's tables and inspect, an inspect naming a container that is gone
  answering those that are there, and the storage folder `info` names.
- `tests/container/test_github_in_podman.py`: git in a workspace of the Podman runtime against
  `tests/github_server.py` served as `https://github.com` in a network of its own: the workspace
  in that network's namespace and not this process's, rcman's push (`Repository`), Claude's git
  (the fake `claude`'s `fake-exec`) and the credential helper using the person's token, the token
  written by `GitAccount` through `put_account`, no push without a token, the environment of
  Claude's children holding no token, the image check, and forgetting the token making the very
  next push fail for both. It also makes workspaces by cloning through `Workspaces`: a public
  repository cloned into a workspace that then works, a private one failing without a token and
  cloning with one (which is kept out of the repository), a refused token, a repository that is
  not there and a GitHub that cannot be reached, each said in plain words, and the deletion of a
  workspace being cloned stopping git and leaving nothing. Its tests of `gh` run the default
  image, with Ubuntu's `gh` as one of Claude's children, in the module's network, which has no
  way out (one of them, with a token and no `config.yml`, has gh ask GitHub whose the token is):
  no token before a save
  and the token after one, none from the very next command after a forget, every shape of token
  in `tests/github_tokens.py` read back as it was saved, and gh writing nothing into the folder
  as rcman writes it, against a control in which a command that writes gh's configuration does,
  and one without `config.yml` in which a stored token makes every command fail.
- `tests/container/test_devcontainer_in_podman.py`, sharing that module's stand-in, image,
  network and person: how an image is read without being run, pulled and, when only the engine
  has it, used without a registry; the uid swap map with `--user` and `--group-add`; files an
  ordinary-map root made being the user's, and a user's rename in them; the tmpfs being the
  user's; the shared credentials file and git folder written in place from a root workspace and
  the user's of one person, two refreshing at once, with its owner and mode unchanged as the
  engine sees them; and, through `Workspaces` and a clone from the stand-in, Claude and the
  commands running as `remoteUser` with the file's environment and the image's, git with the
  person's author and token, the create commands running once and `postStartCommand` at every
  start across a restart of rcman, a refused key or an Alpine image leaving nothing behind, a
  failing command keeping the workspace with its token redacted, and a deletion killing a
  running command; and rebuilds: a new image applied with the work folder kept, a change of user
  (root and uid 1000, both ways) leaving the user's files writable, a failed rebuild leaving the
  old container running from the old image, a restart of rcman while the container was replaced
  coming back on the old image, and a workspace whose `postCreateCommand` failed rebuilt and
  ready.
- `tests/container/test_builder_in_podman.py`: the devcontainer builder (`builder.py`,
  `build_image` in `runtime.py`) against real Podman and the real Dev Containers CLI, with the
  builder in the suites' builder slot. The builder pulls the images a build starts from, so the
  module's workspaces and builders are on the engine's own network, and the stand-in GitHub is
  reached through git's `http.proxy`, a relay on the loopback that takes only `CONNECT
  github.com:443` (`Relay` in `tests/container/network.py`), so nothing listens on the machine's
  port 443. A *stub builder*, the workspace image with a stand-in for the CLI that records its
  arguments, its environment, the build-only configuration it was given and a listing of the
  repository, and waits to be told to fail, shows the builder running in the builder's uid range
  with no privileges, one capability more than a workspace and no device more, and no mount but
  its own volume; the repository arriving as a tar archive without its top-level git folder, its
  files the builder root's, its links and modes kept; the CLI run with the argument list and the
  environment `builder.py` gives it and a configuration of only the build keys; the words of a
  failing CLI told in plain words; the size limit refusing before the CLI runs; a deletion
  removing the builder and its volume; and the formats Podman prints that `builders`,
  `built_images` and `remove_build` read, with the restore removing orphans. The *real builder*,
  built from `installer/builder`, builds a Dockerfile and a local feature into an image in rcman's
  storage, the workspace made from it with the feature's tool on Claude's PATH, the label holding
  the feature's entry only so that each lifecycle command runs once, the builder, its volume and
  the image in it gone, the progress in the creation log, the copy logged, two builds of one
  person at once, the deletion of one removing its image and the copies Podman made of it in the
  person's range and leaving the other's, the policy's refusal of a feature's request before the
  image is copied out and of a musl base after it, each leaving nothing, and a capability a build
  gives `newuidmap` writing another user's map in the person's range, against a copy without it;
  and a built image rebuilt while its container runs, taking the built name only once the
  rebuild is done, and a deletion by `rcman-admin` while a rebuild builds leaving nothing of it.
- `tests/container/test_claude_versions_in_podman.py`: `ClaudeVersions` against real Podman, with
  the default image and a store of the module's own, and the fake `claude` as good, broken and
  checksum-failing Claude Code versions (`tests/claude_download.py`), served from a folder: a
  server keeping the version it started with through an approval, the drift report and a restart
  of rcman, and a stop and start taking the approved version by its exact file; a version leaving
  the store once the last server that used it has stopped, the approved and previous ones
  staying; a broken or checksum-failing candidate leaving the store, the approvals, the
  containers and the staging folder as they were; the canary container as `run_canary` designs
  it, looked at from outside and inside while it runs (the builder's uid range, no network, its
  `/proc/net/dev` naming only `lo` against one on the engine's network that names more, a
  read-only root, no capabilities, only the candidate and rcman's helpers mounted read-only and
  readable by that range, a stale one removed first, none left after); `--timeout` ending a
  container through `podman --remote run`; and the canary cases of `tests/runtime_contract.py`, in
  the suites' builder slot.
- `tests/container/test_terminal_in_podman.py`: the Terminal tab's shell, in workspaces of the
  default image made through the app (see the Terminal tab, below).
- `tests/container/test_restored_in_container.py`: rcman's app on the engine, with `systemctl`
  the stand-in, makes a workspace with a running server and limits on the person and the
  workspace; rcman stops, every container on the engine stops and the stand-in forgets what a
  restart forgets; rcman starts again, and the workspace runs again, its server is ready again,
  and the limits are set again, from rcman's table, before the container starts; and a rebuild
  that the restart cut short comes back on the old image.
- `tests/container/test_install_in_container.py` and `tests/container/test_stack_in_podman.py`:
  below.
- `tests/container/test_nest_in_podman.py`: the tools' `podman` and `docker` in a workspace of
  the default image, as root in the nest (an imported busybox run in a pid namespace with a fresh
  `/proc`, a build from a subfolder whose whiteout holds, `--init`, root's storage native overlay
  in the volume with its mounts in the nest and not in the workspace's own mount namespace, the
  record of the nest replaced when stale or unreadable, a lock whose owner is gone taken over,
  `compose` refused wherever it is the subcommand and not as another's argument; a Podman of a
  program's own, its storage in a folder mktemp makes under the workspace's `/tmp` with a
  storage.conf pinning native overlay and no mount program, as `tests/podman_service.py`'s
  `started` makes the engine, running an imported busybox as root through the tools' `podman`
  and as a user who is not root, with its twin under `/var/tmp`, on the workspace's overlay root,
  refused as overlay over overlayfs; one nest from
  calls made at once, in rounds, with a lock whose holder is gone; a nest slow to make its user
  namespace still made, against a copy of the wrapper without the wait for it; a lock whose
  folder is gone taken over and let go, against a copy that does not make the folder and is
  still trying when stopped; letting go keeping a folder whose claimer runs; after a stop and a
  start, the record's tmpfs empty and a record naming a process with the workspace's own
  namespaces refused, against a copy without those checks; the record's folder root's alone in a
  workspace whose user is not root; the record's folder refused when it is another's or others
  may write in it, and a workspace made without its tmpfs refused as one made before) and as a
  user who
  is not root (a map over every id of the workspace but root's, a run and a build rootless), with
  its twin, a setuid `newuidmap` that cannot write the map; the image's `newuidmap` and
  `newgidmap` with their capability and no setuid, every package it asks for installed, the
  tools a repository's setup needs, and no `/etc/claude-code` of its own; a published port
  explained by `podman` and `docker`, as root and as a user who is not root, with the container
  still run, the recorded helps held to the image's own Podman's; and
  in an image without Podman, the image's own `docker` after the wrappers run, and none told so,
  a port it publishes not explained,
  the wrapper never taking a link to itself for it; and `podman` and `docker` found by name as the wrappers, and
  `git` as the image's, in the environment of Claude's programs and of the lifecycle commands.
- `tests/container/test_engine_in_podman.py`: `create`'s own argv on the engine (the hostname,
  the range, the network), rcman's exec, channel and the Terminal tab's shell; the workspace's
  `/tmp`, an anonymous volume whose type in `/proc/self/mountinfo` is not overlay, against `/`
  and `/var/tmp`, which are, mounted `nosuid` and `nodev`; `1777` and `0:0` under the ordinary
  map and a swapped one, in images of busybox with no `/tmp`, with a `/tmp` of a user's that only
  they may enter, and with that declared `VOLUME /tmp` (one volume there), each against what
  Podman alone makes of it, which is not; the twins, an image whose `/tmp` is a link to `var/tmp`
  and one whose `/tmp` is a link to the workspace's volume, refused, with nothing made `1777`
  through the link where the container runs, and the container then removed and its volume kept; a file in it kept across a stop and a start, the volume kept
  by `_clean_volumes` while its container is stopped and gone once it is removed; a UTS namespace
  the workspace makes taking a hostname and its own not, and a pid namespace it makes mounting a
  fresh `/proc`, each against a sign-in container, which has neither the profile nor the
  unmasked `/proc`; no process of a workspace left once it is stopped or removed, against the
  one seen while it runs; a workspace made without `UNMASK_PROC`, and one with Podman's own
  seccomp profile, each running no nested container; the kernel's own files of `/proc` kept from
  the workspace's root, against a setting of its own IPC namespace it may write; and
  `test_the_sizes_fit`: the slots within the engine's map, every
  `*_ID` integer of the modules of `tests/container/` and the owner of every file in every image
  on the engine within one slot's range, with its twins, a smaller map and an image with a file
  owned beyond it.
- `tests/container/test_builder_on_engine.py`: the builder image in the builder's slot, its
  Podman on native overlay with no FUSE device, building a Dockerfile with a whiteout and a local
  feature into an image a workspace then runs from; and its twin, a builder without
  `SYS_ADMIN`, which runs nothing.

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
`RCMAN_STAND_IN_UNITS`. The checkout's files are at `/src` (see the snapshot, below); `tests/container/clone.py`
makes them a git origin and a clone of the user `dev`'s, and `tests/container/installed.py` runs
the installation with the fake `claude` as the release it downloads. The frontend's and the
backend's packages come from caches filled beforehand in a container of the same image with the
network (`offline_caches`), so npm and uv run offline; the toolchain is a copy of the user's
cache, which the installer, as root there, takes its lock in. The first installation is made once; each
test runs its check of `tests/container/install_checks.py` of the same name, as root in the
container, in order (see [installer.md](installer.md#tests)).

#### The whole stack

`tests/container/stack.py` is the whole of rcman in one container of the system image with no
network but its own loopback, which the stack's workspaces and the browser's container join:
rcman's app made as `tests/e2e_server.py` makes one but with the Podman runtime at the suites'
sizes on the engine, whose socket it is given, alice its first administrator, bob and carol let
in; oauth2-proxy from the installer's toolchain with the installer's own configuration, its
Google endpoints pointed at `tests/container/oidc_stub.py`; Caddy with the installer's own site
block for `rcman.test`, its own certificate authority and its admin API off, in front of
`rcman.test`, `auth.test` and `github.com`; the stand-in GitHub, `tests/github_server.py` through
`github_stand_in.py serve`, with a public and a private repository, under a certificate of an
authority of its own; the fake `claude`'s releases, served; and `systemctl` the stand-in. The
tools, the store and the stack's own state are at the same paths in the container as on the
machine, as the engine resolves what the stack's rcman asks it to mount. The backend's packages
and the built frontend are made once per run from the checkout's files in a container with the
network (`stack_prepared`). `stack.py` says what each of its verbs does.

`tests/container/test_stack_in_podman.py` runs, against a stack of its own, what an installation
does once people and workspaces are there (see [installer.md](installer.md#tests)), through
rcman's socket as Caddy passes requests on, and through Caddy and oauth2-proxy; and the stack's
network: a workspace in the stack's network namespace and not this process's, with a hostname and
a hosts file of its own, against the stack's, and git there finding the stand-in GitHub through
the image. `tests/container/test_stack_in_a_browser.py` holds the one test `make check-ui`
runs, which starts a stack of its own (the two stacks would number their workspaces alike, so they
are never up at once) and runs the specs of `frontend/e2e-stack/` with `frontend/playwright.stack.config.ts` in
Playwright's own image (`PLAYWRIGHT_IMAGE` in `stack.py`, by digest, at the version
`frontend/package-lock.json` pins, which `tests/test_stack.py` holds it to) in a container that joins the
stack's network, with the frontend's packages from `make frontend-setup`. The specs connect the
fake `claude` on the **Claude** page with the code it accepts, which the suite passes them in
`RCMAN_FAKE_CLAUDE_CODE`, and the stand-in GitHub's token and addresses in `RCMAN_STACK_GITHUB`.
`stack.py repository <workspace id>` is the verb a spec runs, through `RCMAN_STACK_EXEC`: a relay
(`tests/container/stack_exec.mjs`) hands it to the suite, which runs it in the stack's container
and nothing else, answering 2 to anything else, on a socket in a folder that are the user's
alone (`command_relay` in `stack.py`; `tests/test_stack.py` holds it, with the node client, and
`tests/test_network.py` the relay git reaches the builder module's stand-in through, each
against what it refuses); it makes a repository in that workspace with a bare origin of its own
and changes to stage. `stack.py check` then finds carol, whom the specs deleted, with nothing
labelled hers left, the changes the specs staged, committed and pushed in the origin, as the
author the spec set, with an audit row for each step, each workspace made by cloning ready with
its repository's commit, and the token the specs stored in no audit row, workspace row or line of
rcman's output.

A person's first workspace in the stack is slow, because rootless Podman keeps one copy of the
default image per range of ids and copies it into the person's range, so the specs wait for it
as long as `FIRST_WORKSPACE_WITHIN` in `frontend/e2e-stack/timing.ts` says, and the test making
it gets that much more time.

#### What the container suite holds of its own

`tests/test_container_suite.py`: the engine usable; a checkout that is a git worktree reaching a
container as a snapshot that `tests/container/clone.py` commits, against the worktree mounted as
it is, whose `.git` names a folder the container does not have; and a run's own containers going,
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
| `tests/fakes/` | The fake `claude`, its recorded fixtures and the screens observed on a live server — see [fake-claude.md](fake-claude.md); Podman's default seccomp profile as recorded (`podman-seccomp.json`), which `tests/test_seccomp.py` holds to the machine's; Podman's help for the subcommands `publish.sh` reads and for its global options, recorded from the default image's Podman (`podman-help/`), which `tests/test_podman_wrapper.py` holds the wrapper's lists to and `tests/container/test_nest_in_podman.py` holds to that Podman's own help; and what the monitor reads of Podman, recorded from Podman on the tests' engine (`podman-monitor/`: `system df -v` with one workspace and with two, and with one holding many files, what it says when a volume cannot be read, `container inspect` and `info`), which `tests/test_monitor.py` reads and `tests/container/test_runtime_in_podman.py` holds to that Podman |
| `helpers/` | What the backend shares with the installer and the fake `claude`, the standard library only: the rules values are refused by, what Claude's screens mean, running git, Claude Code's releases and their checked download (`claude_release.py`), and the checks a Claude Code version is held to with the canary program that runs them (`claude_checks.py`, `claude_canary.py`) |
| `backend/` | The FastAPI application — see [backend.md](backend.md) |
| `frontend/` | The React application, its component tests and its browser tests — see [frontend.md](frontend.md) |
| `tests/container/` | The container suites, what they run in their containers, and the system image they run rcman's installer and the whole stack in (`tests/container/system/`) |
| `install.sh`, `installer/` | The installer — see [installer.md](installer.md) |
| `devtools/` | Development tooling, run by hand, by the edit-time hook or by the tests, which never imports the tests; each module's docstring says what it is |

## What a killed run leaves

A run that ends normally, or with Ctrl-C, removes what it made. A run killed
by a signal — SIGTERM or SIGKILL to its process group, as a harness timeout
or a closed terminal sends — runs no handler of its own, and a tmux server it
started has detached into a session of its own and keeps running. So nothing
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
  folder of the current user's with, if any, a lock file of theirs; it stops
  only tmux servers whose sockets are the current user's, never one a link
  points at; what it cannot read or remove, or what a removal refuses or fails at in a way of
  its own (the engine's removal failing, say), it leaves, says so on stderr, and goes on past, to
  the others and to the run's own folder; one another run removed first it passes over in silence. The
  browser tests' worlds are held and cleared the same way, chosen by the
  exact shape of their name (see [frontend.md](frontend.md#tests)).
- A held folder a test may fill with anything, as the `world` fixture in
  `tests/test_e2e_server.py` does, is removed with `remove_tree`, which also
  stops a tmux server anywhere in it and makes each folder of the user's in
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
of its own, killed with SIGTERM and with SIGKILL; the test then checks that
the next run clears what the killed one left. A killed run is gone once no
process of its session is running, by `running` in `devtools/held_folders.py`:
a process runs while its state is not Z, or is Z with other threads still
running, which is how a process whose first thread has ended reads; such a
process still holds its open files, and with them a held folder's lock.
`FirstThreadEnded` in `tests/killed_runs.py` makes such a process for the
tests of `held_folders`, `killed_runs`, `podman_service` and `terminals`. The
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
  ports are that container's (`IN_CONTAINERS` in the test, each with its reason; the whole
  stack's fixed ports, on its own network, are named exemptions), every Python module in
  `devtools/`, which the tests and
  the recorder run, and every TypeScript and JavaScript file in `frontend/`
  that git does not ignore, of the kinds `SOURCES` in the test names — the
  browser tests, their configs and the rest — parsed with Vite's parser from
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
  It runs `make test`, `make test-container`, `make check-ui`, `make check`
  and `make check-all` with `uv`, `npm` and `npx` replaced by recorders, and
  reads which tests each pytest command they ran selects from pytest's own
  collection: every test is in exactly one of `make test`,
  `make test-container` and `make check-ui`; `make test-container` runs the
  `container` tests but the `stack` one, which `make check-ui` runs;
  `make check` runs the host tests and holds no on-demand run;
  `make check-all` runs every test, its container tests with
  `RCMAN_REQUIRE_CONTAINER=1`, and `make check` with
  `RCMAN_REQUIRE_REAL_CLAUDE=1`; none of them runs a test a
  bare `pytest` leaves out, as a target's own `-m` replaces the
  `-m 'not real_claude'` of pyproject's `addopts`. `make lint` and
  `make format` run ruff and the frontend's script of the same name, and the
  real `make lint`, run over a frontend file planted in a scratch tree, fails
  when Biome would reformat it, when Biome finds a lint finding it reports as
  an info, and when the frontend's packages are missing; it checks the
  frontend's `index.html`, and fails on a lint finding there. Every rule Biome runs
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
  checks that every development-only file is marked `export-ignore`, that the
  files a deployment needs are not, and — in a scratch repository — that
  `git archive` really omits what the patterns mark.

## Screenshots

`make screenshots` captures the pages for someone to look at; it asserts nothing, and is
neither a test nor part of any test run. `frontend/e2e/screenshots.capture.ts` visits every
page of the route table (`PAGES` and `ADMIN_PAGES`, a help page standing for every help page),
and the views a page shows only when asked, such as an open dialog or pop-up, which the file
lists. It takes each at a phone's width (`isMobile`) and a desktop's, in the light and the
dark scheme, as the file names; the widths are in the file.

It runs with `frontend/playwright.screenshots.config.ts`, which takes the browser tests' config,
their backend and world (see [frontend.md](frontend.md#tests)) and so a world and a port of its
own, and writes to an output folder of its own, which Playwright empties as a run starts:
`frontend/test-results/screenshots/`, ignored by git. The browser tests' config does not match
the file, and no test target runs its config; `tests/test_e2e_server.py` and
`tests/test_make.py` hold both. The pre-test hook counts it as a test run, since it runs
`playwright test`.

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
inside the timeout the wiring gives it, which a test checks; `git add`,
`git commit` and `git push` stopped at the deadline are reported as such.
Git runs with no terminal prompt, and ssh, unless configured otherwise, in
batch mode. A failure is reported to the operator and to the agent; a commit
or push it made, with the untracked files the commit added, is told to the
agent.

Which commands run tests is read from the command alone, as the shell splits
it, and a command that runs none starts no process. One runs tests when it
is pytest, Vitest or `playwright test`, without an option such as
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

Its own tests are in `tests/test_pre_test_commit.py`. They act only in
scratch repositories under `tmp_path`, each with a local bare repository as
its origin, with git under the fixtures' configuration, which refuses every
transport but a local repository's; an autouse fixture checks that the
repository the tests run from has the same HEAD, branch, upstream and index
after each test, and a planted twin shows the same check sees a commit and a
push. One test runs the command the wiring gives, as a shell runs it.
