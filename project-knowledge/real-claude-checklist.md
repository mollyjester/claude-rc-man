# Checks with a real Claude account

Everything automated runs against the fake `claude` or, without an account,
against the real binary — see [fake-claude.md](fake-claude.md). What needs a
signed-in Claude subscription is checked by hand, with this list: before
approving a new Claude Code version, and once on a newly installed server.

Use a Claude account set aside for testing where there is one. Every server
started below creates a Remote Control session in that account's
claude.ai/code list; archive them afterwards.

## On a development machine

1. Sign your own account on the development machine in to the test Claude
   account (`claude`, then `/login`).
2. Run `uv run pytest -m real_claude`; what it checks is in
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
5. Restart rcman (`sudo systemctl restart rcman`) and reboot the machine: the
   workspaces are ready again afterwards, and their servers running, without
   anyone doing anything.
6. **Sign out of Claude** with **Keep sessions running** ticked: both servers
   keep running and **Start** is refused; stop both, and the page shows the
   account not connected.
7. **Delete workspace**, and remove the person (`sudo rcman-admin delete`):
   `podman volume ls` as `rcman` lists nothing of theirs.

Write down what differed from this list. A screen rcman shows as
**Unrecognised screen** is recorded as a new fixture, or, where no sequence
can bring it about, kept as an
[observed screen](fake-claude.md#observed-screens), and handled before the
version is approved.

## Rolling out a version

With the real binary, in an installed rcman, as an administrator. The canary and what it
runs are in [runtime.md](runtime.md#the-canary); the fake cannot show these.

1. **The checks need no network.** Roll a real release out on the **Claude Code
   versions** page, under **Admin**, with **Check and roll out**: it passes in the
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
   store's `bin`, which follows `current`, so expect the new version while the server
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

## Screens with no recording yet

Some screens rcman has no recording of, and has not kept as observed, and so
does not recognise; the full list, and how rcman shows them, is under
*Not recorded* in
[fake-claude.md](fake-claude.md). Some of them need particular conditions.
Whenever those conditions come up, record the screen: add a sequence for it to
`SEQUENCES` in `devtools/record_claude.py`, record it with
`uv run python devtools/record_claude.py --with-account --sequence <name>`,
and teach rcman and the fake to recognise it, tests first.

1. **Enabling Remote Control.** With a Claude account that has never used
   Remote Control, start a server: Claude asks once whether to enable it.
   Record that question, both answers, and the screen that follows each.
2. **Network loss.** With a server ready, cut the machine's network for longer
   than a server survives, then restore it. Record the screen the server ends
   on and its exit status.
3. **Expired sign-in.** With an account whose sign-in has expired — rcman
   shows the connection as expired — start a server. Record what Claude shows
   instead of the trust question or the server screen.
