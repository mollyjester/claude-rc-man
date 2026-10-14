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

Signing in to Claude in a workspace is not there yet, so what an installed
server is checked for by hand needs no Claude account. With a person added for
the test (`sudo rcman-admin add`):

1. Sign in to rcman with their Google address.
2. **New workspace**: it is listed as being made, then ready. The first of a
   person's takes longest, while Podman copies the image for their range.
3. **Start**: rcman says Claude is not signed in in this workspace.
4. Restart rcman (`sudo systemctl restart rcman`) and reboot the machine: the
   workspace is ready again afterwards, without anyone doing anything.
5. **Delete workspace**, and remove the person (`sudo rcman-admin delete`).

Write down what differed from this list. A screen rcman shows as
**Unrecognised screen** is recorded as a new fixture, or, where no sequence
can bring it about, kept as an
[observed screen](fake-claude.md#observed-screens), and handled before the
version is approved.

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
