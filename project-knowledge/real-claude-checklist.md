# Checks with a real Claude account

Everything automated runs against the fake `claude` or, without an account,
against the real binary — see [fake-claude.md](fake-claude.md). What needs a
signed-in Claude subscription is checked by hand, with this list: before
approving a new Claude Code version, once on a newly installed server, and again
after a live reinstall.

Use a Claude account set aside for testing where there is one. Every server
started below creates a Remote Control session in that account's
claude.ai/code list; archive them afterwards.

## On a development machine

1. Sign your own account on the development machine in to the test Claude
   account (`claude`, then `/login`).
2. Run `uv run pytest -m real_claude`; what it checks, and the account it uses, are in
   [fake-claude.md](fake-claude.md#how-the-fake-is-checked-against-the-real-binary).
   It creates a session for each sequence that brings a server to ready
   (see the docstring of `devtools/record_claude.py`).
3. When a Claude Code version is being approved, follow
   [Approving a new Claude Code version](fake-claude.md#approving-a-new-claude-code-version).

## On an installed server

With a person added for the test (`sudo rcman-admin add`):

1. Sign in to rcman with their Google address.
2. **New workspace**: it is listed as being made, then ready. The first of a
   person's takes longest, while Podman copies the image for their range.
3. **Start**: rcman says to connect the Claude account on the Claude page.
4. Connect the test Claude account on the **Claude** page; start the
   workspace's server, then make a second workspace and start its server: both
   reach ready.
5. In a new Remote Control session of a workspace of the default image, ask Claude
   what it was told about this workspace: it answers from rcman's instructions
   ([runtime.md](runtime.md#workspace-instructions)), chooses `gh` for GitHub work
   over GitHub MCP tools, and, asked "will your work here be lost if you don't
   push?" and "is `df` accurate here?", answers no and yes. The session's appended
   system prompt still has the section headed "Your current remote execution
   environment" that the instructions correct; if that heading changed, the
   correction in `backend/rcman/instructions/common.md` is changed with it.
6. Restart rcman (`sudo systemctl restart rcman`): the workspaces stay ready,
   and their servers keep running, without anyone doing anything.
7. Reboot the machine, as [After the host restarts](#after-the-host-restarts)
   says: the workspaces are ready again afterwards, and their servers running,
   without anyone doing anything.
8. **Sign out of Claude** with **Keep servers running until the last one stops** ticked: both servers
   keep running and **Start** is refused; stop both, and the page shows the
   account not connected.
9. Delete the workspace with its trash button, **Delete workspace <name>** in the list, and remove the person (`sudo rcman-admin delete`):
   `podman volume ls` as `rcman` lists nothing of theirs.
10. **Run `sudo ./install.sh` again** from the dedicated clone while a workspace's
    server is running: it changes nothing and ends nothing, and the server is still
    running afterwards, and `podman info` as `rcman` reports `crun` as its OCI
    runtime.

Write down what differed from this list. A screen rcman shows as
**Unrecognised screen** is recorded as a new fixture, or, where no sequence
can bring it about, kept as an
[observed screen](fake-claude.md#observed-screens), and handled before the
version is approved.

## After a fresh install or a reinstall

On a real machine, as root, after `./install.sh` has ended and its notices about Caddy
have been done (Caddy restarted). Write down, before the install, `/etc/subuid`,
`/etc/subgid`, `getent group` and what the machine's other services answer. Podman is
run as `rcman` the way the installer runs it (see
[Podman's volumes are rcman's](../docs/installation.md#podmans-volumes-are-rcmans)):

```
runuser -u rcman -- env -i PATH=/usr/bin:/bin HOME=/var/lib/rcman \
  XDG_RUNTIME_DIR=/run/user/$(id -u rcman) podman <arguments>
```

1. **The services.** `systemctl --failed` lists nothing, and `rcman`,
   `rcman-oauth2-proxy` and Caddy are active.
2. **The runtime.** `podman info --format '{{.Host.OCIRuntime.Name}}'` run as `rcman` prints
   `crun`.
3. **A first workspace.** The first administrator signs in with the address given to the
   installer and makes a workspace, which reaches ready.
4. **An address that is not listed.** Sign in to Google with an account that is not on the
   **People** page: oauth2-proxy refuses it and no page of rcman opens. Add a person for the
   test, sign in as them, then remove them (`sudo rcman-admin delete`) while their page is
   open: their next request is refused at once, and they cannot sign in again.
5. **No way to the machine's loopback.** From a shell in a workspace, a connection to
   oauth2-proxy's port (`RCMAN_INSTALL_PROXY_PORT` in `/etc/rcman/install.env`) on
   `127.0.0.1`, and on the gateway address the workspace's network shows, fails. rcman has
   no port to try, only its socket, which no workspace can reach.
6. **Nothing of the operator's disturbed.** Compared with what was written down: the
   machine's other sites answer through Caddy, Docker (where it is there) lists the same
   containers, `/etc/subuid` and `/etc/subgid` have gained rcman's lines and no other
   changed, and the groups are the same but for `rcman`, `rcman-proxy` and `rcman-web`
   (with Caddy's account in the last).
7. **Run the installer again** (step 10 of
   [On an installed server](#on-an-installed-server)): it changes nothing, and a workspace's
   container with limits on its slices keeps its process, its start time and its limits in the
   kernel's files (step 10), and rcman's user manager its process.
8. **rcman's own rootless Podman, under systemd.** `/var/lib/systemd/linger/rcman` is there;
   `systemctl --user --machine rcman@ is-active podman.socket` says `active`;
   `podman info --format '{{.Host.Security.Rootless}}'` as `rcman` prints `true`; and `rcman`'s
   lines in `/etc/subuid` and `/etc/subgid` hold `SUBORDINATE_COUNT` ids
   (`installer/install.py`).
9. **The service and its images.** `systemctl show rcman.service -p NoNewPrivileges,Environment`
   shows `NoNewPrivileges=yes` and `XDG_RUNTIME_DIR` naming rcman's runtime folder. The default
   image and the builder image (`RCMAN_WORKSPACE_IMAGE` and `RCMAN_BUILDER_IMAGE` in
   `/etc/rcman/rcman.env`) are in rcman's Podman, and `devcontainer --version` runs in the
   builder image (`podman run --rm --pull=never <builder image> devcontainer --version`). A
   running server's container and its conmon are in cgroups under `user@<rcman's uid>.service`,
   not under `rcman.service` (`/proc/<pid>/cgroup` of each, the pids from
   `podman container inspect`).
10. **Limits in the kernel.** The drop-ins `ensure_limits` writes are there with the installer's
    text, `pids.max` of `user-<rcman's uid>.slice` under `/sys/fs/cgroup/user.slice` says
    `max`, `systemctl --user --machine rcman@ show -p DefaultTasksMax` says `infinity`, and
    `cgroup.controllers` of `user@<rcman's uid>.service` lists `cpu`, `memory` and `pids`. Before
    any limit is set, nothing in the chain from rcman's slices down to a workspace container's
    `libpod-` scope and the cgroup inside it has a limit on memory, CPU or processes. Set a
    person's limits on the **People** page and a workspace's with `rcman-admin limits`:
    `memory.max`, `memory.swap.max`, `cpu.max` and `pids.max` of the person's and the
    workspace's slices, under `user@<rcman's uid>.service` in `/sys/fs/cgroup`, hold them, and
    the workspace's container is in a `libpod-` scope beneath the workspace's slice. Take them
    off: the files say `max` again, and the server ran throughout.
11. **The audit reaches the journal.** Change a setting on the **Settings** page:
    `journalctl -u rcman -o cat` has a JSON line for it (`settings.update`, `ok`).
12. **A short image name in a workspace.** In a workspace's **Terminal** tab,
    `podman pull alpine` pulls `docker.io/library/alpine`, and `podman run --rm alpine true`
    runs it.
13. **Limits as installed.** `sudo rcman-admin limits <address> --workspace <name> --memory
    <size> --cpu <cpus> --pids <count>` says the limits it set; `sudo rcman-admin limits <address>
    --workspace <name>` shows them with what is in use; the **People** page's report of the
    person shows tasks in use above none; and the workspace's container is in a `libpod-` scope
    beneath the workspace's slice. Delete that workspace: its slice's files under
    `/sys/fs/cgroup` say `max` again, so a later workspace given the same id inherits nothing.
14. **A limit below what is in use.** In a workspace's **Terminal** tab, fill memory and keep it
    (for example `head -c <size> /dev/zero | tail` in the background), and read its slice's
    `memory.current` under `/sys/fs/cgroup`. Set a memory limit below that, on the **People**
    page and with `rcman-admin limits`: both are refused as below what is in use
    (`limit_below_use`), naming the use, and the slice's files and the **People** page's report
    are as they were. A limit above the use is then accepted.
15. **A build's builder in the person's slice.** While a repository's devcontainer.json with a
    Dockerfile is being built, the builder's container (`rcman-build-<id>`) is in a `libpod-`
    scope beneath the slice of the person who asked for it, beside their workspaces' slices and
    not in one of them.
16. **No way to the machine's loopback from a workspace of a repository's own image.** As
    step 5, from a workspace made from a devcontainer.json naming an image with a `remoteUser`:
    the same connections fail.
17. **Rollback.** `sudo ./install.sh --rollback` switches `/opt/rcman/current` to the previous
    release and rcman answers; running `sudo ./install.sh` again brings the newest back.
18. **A workspace's own `/tmp`.** After the reinstall, make a new workspace from rcman's own
    repository (one made before keeps the container it was made with, `/tmp` and all, until it is
    rebuilt). In its **Terminal** tab, `stat -f -c %T /tmp` does not print `overlayfs` and `stat -f -c %T /var/tmp`
    does; `stat -c '%a %u %g' /tmp` prints `1777 0 0`. Run `RCMAN_REQUIRE_CONTAINER=1 make
    test-container` there: it collects the container suites and runs them, and the totals it
    prints show them passed, none skipped.
19. **The repository's own checks in a workspace.** In that workspace run `make check-all`:
    every part passes, as on a development machine.
20. **Removal, on a spare machine.** Follow
   [Removing rcman](../docs/installation.md#removing-rcman): the commands that check that
   nothing is left print nothing, Caddy restarts and still serves its other sites, and
   the packages and the operator's accounts are as they were.

## After the host restarts

On a real machine, not in a container: the order of boot, lingering and the user
manager are what is checked, and a container cannot show them. With a person
added for the test, two workspaces of theirs with their servers running, one
more workspace stopped on purpose, and a limit set on one workspace (see
[Limits](../docs/users.md#limits)):

1. **Before.** Write down what rcman's Podman lists (`podman ps` as `rcman`) and
   the sessions the account shows in claude.ai/code.
2. **Restart the host** (`sudo reboot`) and touch nothing.
3. **The services are up.** `rcman`, `rcman-oauth2-proxy` and Caddy are running,
   and `systemctl --failed` lists nothing.
4. **The workspaces came back.** On the dashboard the workspaces are ready, the
   servers that were running are running again, and the one stopped on purpose is
   still stopped. `podman ps` as `rcman` lists the same containers as before.
5. **The limit holds.** The workspace's limit is shown, and the unit manager has
   it again (see [Limits](runtime.md#limits)): the slice's files under `/sys/fs/cgroup` hold
   it, and they were written before the container started (their times, against the
   container's start in `podman container inspect`).
6. **The sessions reconnect.** The sessions of the servers that were running
   show again in claude.ai/code, and answer.
7. **The journal is clean.** `sudo journalctl -u rcman -b` shows no error.

## Servers surviving an rcman upgrade

Needs a second release to install. With a server in the middle of a conversation,
and a workspace's **Terminal** tab open with `sleep` running in its shell:

1. Write down the container ids and start times of the workspaces, and the server
   pids, and which Claude Code version is approved.
2. Run `sudo ./install.sh`.
3. The container ids, the start times and the server pids are the same as
   before, the session stays attached in claude.ai/code and answers the next
   message, and the approved version is the one written down.
4. The Terminal tab says rcman is restarting and offers **Reconnect**, and neither the
   shell nor its `sleep` is left in the workspace (`podman top` as `rcman`).

## Upgrading from the web interface

On a throwaway Ubuntu 24.04 machine, before a change to the upgrade from the web interface is
merged, with it turned on (`sudo ./install.sh --web-upgrade yes`) from a clone of a private
repository whose owner has credentials that ask nothing, and a second release on `main` to
upgrade to. What the tests stand in for here is checked against the real thing:

1. **What systemd shows rcman.** As root, run
   `runuser -u rcman -- systemctl show --no-pager --property=Id,LoadState,ActiveState,InvocationID -- rcman-upgrade.service rcman-upgrade.path`
   with the service inactive, while it runs, and with the units removed
   (`sudo ./install.sh --web-upgrade no`, then again `yes`), and also from inside
   `rcman.service` (the **rcman release** page reads it: the report answers, and its reason is
   not `units_unreadable`). Write the three outputs down and compare them with `shown()` in
   `tests/test_rcman_upgrade.py`, which `rcman_upgrade.read_units` is tested against: each
   property, its value and the blocks' order. A difference is fixed in the test's recording and,
   where needed, in `read_units`.
2. **The request starts the run.** With no `/var/lib/rcman/upgrade.request`, **Upgrade rcman**
   writes it and `rcman-upgrade.service` starts (`sudo journalctl -u rcman-upgrade -f`); after
   that run, a second **Upgrade rcman**, the file now there, starts it again. Both are
   `PathModified=` firing on rcman's own write.
3. **A press during a run is taken by it.** While a run goes on, a request written by hand as
   rcman starts no second run, and the page offers no **Upgrade rcman** meanwhile.
4. **The run outlives rcman's restart.** The installer restarts `rcman.service` part way; the
   service's run goes on to its end, and `ExecStopPost=` writes the end: stop a run with
   `sudo systemctl stop rcman-upgrade.service` once, and `/opt/rcman/upgrade/status.json` says
   `failed` with the result systemd gave.
5. **The release is the one that runs.** Before and after an upgrade, the page's **Running
   release** is the commit in `/opt/rcman/current/.complete` of the release `rcman.service`
   started from (the virtualenv `sys.prefix` names, resolved): after the switch, and before
   rcman restarts, it still names the release before.
6. **The pull works with nobody there.** The upgrade pulls the private repository under
   `env -i` with no terminal, and `npm ci` and `uv` build the release in the unit's environment.
7. **The installer's lock is kept across its re-exec.** While an upgrade runs, a
   `sudo ./install.sh` from a shell is refused, saying another run holds the lock.
8. **The page follows the restart.** In a browser, through Caddy and oauth2-proxy, the page says
   the upgrade runs, says rcman is restarting while it is not reached, and offers
   **Reload page** at the end; the browser's console shows the failed reads, and
   `sudo journalctl -u rcman --grep client_error` shows none from them.

## A repository with devcontainer.json and a feature

What [devcontainer.json support](../docs/devcontainer.md) says, with the real
builder and the real Claude binary. Use a small repository of your own with a
devcontainer.json that names an image, a feature, `remoteUser`, a lifecycle
command that leaves a marker file, and `customizations` and `forwardPorts`:

1. **The steps.** Clone it as a new workspace: it passes through the building
   states to ready.
2. **The feature.** The tool the feature installs exists in the workspace.
3. **The user.** `whoami` in a session shows `remoteUser`, and Claude's server
   runs as that user.
4. **The lifecycle command.** Its marker file exists.
5. **Ignored keys.** `customizations` and `forwardPorts` do not block the
   workspace, and the steps name them as skipped.
6. **No builder is left.** `podman ps -a` as `rcman` lists no builder
   container after the build.
7. **The same repository again.** A second workspace from it starts.
8. **Refused keys.** A repository that asks for `mounts` or for `privileged` is
   refused, and the reason names the key.
9. **A Dockerfile.** The same checks with a repository whose devcontainer.json
   builds a Dockerfile instead of naming an image.
10. **Claude under a non-root user.** With the real binary, a server started in
    such a workspace reaches ready and answers a message; Claude does not run as
    root.

## A rebuild

What [Rebuilding a workspace](../docs/devcontainer.md#rebuilding-a-workspace) says, with the real
Claude binary. With the repository above cloned as a workspace whose server is running, in the
middle of a conversation, and a file of your own in its work folder:

1. **The change is seen.** Change its devcontainer.json in the workspace (another lifecycle
   command, say): the workspace says the file changed.
2. **The rebuild.** Ask for a Rebuild and confirm: the server stops, the Terminal tab says the
   workspace is being rebuilt, and the workspace comes back ready, its server running again,
   `podman ps` as `rcman` showing a new container of the same name.
3. **The work stays.** Your file is in the work folder, and the new command ran.
4. **The sessions.** Claude's server in the new container takes up its Remote Control
   sessions again: the session in claude.ai/code shows again and answers the next message.
   Until this is checked, rcman's page says only that the server was started again after a
   Rebuild, and promises nothing of its sessions.
5. **A user changed.** Rebuild with `remoteUser` changed (to root and back): the work folder,
   Claude's sign-in and git's configuration stay writable, and the server starts.

## Rolling out a version

With the real binary, in an installed rcman, as an administrator. The canary and what it
runs are in [runtime.md](runtime.md#the-canary); the fake cannot show these.

1. **The checks need no network.** Roll a real release out on the **Claude Code
   versions** page, under **Administration**, with **Check and roll out**: it passes in the
   canary, which has no network. Where it fails only for want of the network, the
   real binary reaches for it during the checks: the canary has to use the
   workspace network instead (`run_canary` in `backend/rcman/runtime.py`), and the
   guide says so.
2. **The canary needs only the image.** The checks run with the default workspace image's
   `python3` and nothing else mounted but the candidate and rcman's helpers; a failing
   check's report on the page shows what it ran and got back, with no account details.
3. **No self-update.** After a server has run for some minutes, its `HOME` holds no
   `.local/share/claude`: `DISABLE_AUTOUPDATER` keeps Claude from installing a version of
   its own.
4. **A command typed in a session.** After a roll-out, in the shell of an already
   running session, type `claude --version`. The shell's `PATH` starts with the
   store's `bin`, which follows `current` (then the tools' `podman` and `docker`), so expect the new version while the server
   keeps the old one. Then find which program the server's own children run:
   `/proc/<pid>/exe` of a process the server spawned for a session. Write down what
   the real product does; `docs/claude-version.md` claims only that the server
   process and the sessions it spawns keep their version, with the typed command as
   the exception, and it is to be softened further if this shows otherwise.
5. **Servers keep their version.** With a server running, approve another version: the
   server keeps running the old one, its session stays attached, and stopping and
   starting it runs the new one.

## The shared sign-in

What Claude Code does with one credentials file bound into several workspaces,
each with its own `CLAUDE_CONFIG_DIR`, as seen with a real account on the version
in `tests/fakes/fixtures/manifest.json`. rcman and the fake follow it; check it
again when a version is approved, in an installed rcman with the test account
connected on the **Claude** page.

1. **What a sign-in writes.** The credentials file holds `claudeAiOauth`, whose
   keys, and those of `oauthAccount` in `.claude.json`, are the ones
   `tests/test_fake_claude.py` names. Through the bind the file stays the same
   file, owner-only.
2. **No `oauthAccount`.** `claude remote-control` in a configuration whose
   `.claude.json` holds none refuses on stderr with exit status 1: the fixture
   `rc_no_oauth_account`, which `uv run pytest -m real_claude` records again.
   rcman seeds `oauthAccount` before every start.
3. **A session's commands.** The sessions Remote Control spawns stay in the
   pane's process group; the commands a session runs are in a session of their
   own (setsid). The stop collects every process the server started, by parent,
   before it signals anything, and kills what is left (see
   [runtime.md](runtime.md#a-workspaces-server)). Check that a stop with a
   command running leaves no process in the workspace.
4. **A refresh.** A refresh from one workspace writes the file in place,
   truncated first and keeping keys it does not know; a server in another
   workspace stays connected.
5. **Another account under a running server.** A server keeps working when
   another account is connected under it; it takes the new account's details
   at its next start.
6. **`auth logout` over the bound file.** It answers exit status 0 and leaves
   the file as it is, and `auth status` still says signed in, though the
   sign-in is revoked. rcman therefore empties the file in place after it, and
   reads whether someone is signed in from the file and its own record, never
   from `auth status` alone.
7. **Connected once.** Connect the account on the **Claude** page once. A
   workspace made afterwards reaches ready and starts with no sign-in prompt.
8. **One account.** The sessions of both servers appear under the one account in
   claude.ai/code.
9. **Deleting one.** Deleting a workspace leaves the other signed in and its
   server connected.
10. **Another person.** A second person's workspace uses their own account:
    their sessions appear under theirs, and nothing of the first person's.

## Screens with no recording yet

Some screens rcman has no recording of, and has not kept as observed, and so
does not recognise, and some it has seen but not what follows them; the full
list, and how rcman shows them, is under *Not recorded* in
[fake-claude.md](fake-claude.md). Some of them need particular conditions.
Whenever those conditions come up, record the screen: add a sequence for it to
`SEQUENCES` in `devtools/record_claude.py`, record it with
`uv run python devtools/record_claude.py --with-account --sequence <name>`,
and teach rcman and the fake to recognise it, tests first.

1. **Enabling Remote Control.** The question itself is kept as an observed
   screen. With a Claude account that has not enabled Remote Control, start a
   server: Claude asks whether to enable it. Record what the observed screen
   leaves out: the screens that follow `y`; whether Claude exits after `n` and
   asks again next time, as its documentation says, and the screen it leaves
   and the exit status then; what Ctrl+C does at the question; and whether an
   answer that arrives straight after the question is refused, as the trust
   question's is.
2. **Network loss.** With a server ready, cut the machine's network for longer
   than a server survives, then restore it. Record the screen the server ends
   on and its exit status.
3. **Expired sign-in.** With an account whose sign-in has expired — rcman
   shows the connection as expired — start a server. Record what Claude shows
   instead of the trust question or the server screen.
