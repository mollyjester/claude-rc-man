# Checks with a real Claude account

Everything automated runs against the fake `claude` or, without an account,
against the real binary — see [fake-claude.md](fake-claude.md). What needs a
signed-in Claude subscription is checked by hand, with this list: before
approving a new Claude Code version, and once on a newly installed server.

Use a Claude account set aside for testing where there is one. Every server
started below creates a Remote Control session in that account's
claude.ai/code list; archive them afterwards.

## On a development machine

1. Sign the Linux account in to the test Claude account (`claude`, then
   `/login`).
2. Run `uv run pytest -m real_claude`; what it checks is in
   [fake-claude.md](fake-claude.md#how-the-fake-is-checked-against-the-real-binary).
   It creates one session.
3. When a Claude Code version is being approved, follow
   [Approving a new Claude Code version](fake-claude.md#approving-a-new-claude-code-version).

## On an installed server

With a person added for the test account (`sudo rcman-user add`):

1. Sign in to rcman with their Google address.
2. **Connect Claude account**, type the test account's address under
   **Claude account email (leave empty to use your sign-in address)**, and
   **Get sign-in link**: **Sign in to Claude** opens claude.com; after
   signing in, paste the code Claude shows under **Code from Claude** and
   **Connect**: the page shows the account and when its sign-in expires.
3. Select `projects`, make a folder with **New folder**, choose **Start**,
   answer the trust question, and wait for **Ready to accept sessions**.
4. Open **Open in claude.ai**: the session is listed in claude.ai/code under
   the session name rcman shows, and answers a message.
5. **Stop**, then **Resume**: the same session comes back in claude.ai/code.
6. **Stop** and **Forget**; archive the session in claude.ai/code.
7. On **Claude Code**, under **Admin** (administrators), the person's
   installed version is the approved one.

### Cloning from GitHub

Every automated clone comes from the tests' own GitHub; whether GitHub
itself serves rcman's clone, and a private repository with a stored token, is
checked here.

1. With no GitHub token stored, select `projects` and **Clone from GitHub**
   a public repository by the address GitHub shows for it: the clone is
   selected, carries the **git** marker, and its Changes section shows its
   branch.
2. **Clone from GitHub** a private repository set aside for testing: the
   form shows git's words for the refusal, and no folder is left in
   `projects`.
3. Create a fine-grained token for that repository only, with **Contents**
   read and write. In **Settings**, save it under **GitHub token**: the part
   says a token is stored, and its field is empty. Clone the private
   repository again: it is cloned, and its `.git/config` holds no token.
4. **Start** a server in the public clone, then **Delete folder**: the
   confirmation names the server, which is stopped and forgotten before the
   folder is deleted, and its session stays in claude.ai/code; archive it.

### Pushing to GitHub

Every automated push goes to the tests' own GitHub, which takes any
non-empty username; whether GitHub takes rcman's (`GITHUB_USERNAME` in
`helpers/rc_helper.py`) with each kind of token is checked here. Use the
private repository cloned above, in the person's `~/projects`, with the
fine-grained token stored there.

1. In **Settings**, set **Commit author name**. For Claude's commit in step
   3, set `user.name` and `user.email` in the person's git configuration
   too, since rcman's commit author applies to rcman's own commits only.
2. Select the repository, choose **New branch**, name it under **New branch
   from here** and **Create branch**; change a file, stage it, and
   **Commit & Push**: the section says where it pushed, and GitHub shows the
   branch with the commit.
3. **Start** a server in the repository and, in claude.ai, ask Claude to
   commit a change and push it: the push uses the stored token, and GitHub
   shows it.
4. Revoke the token on GitHub, change a file, and **Commit & Push**: the
   commit is made, the section shows git's words for the refused push, and
   **Settings** then says no token is stored, git having forgotten it.
5. Save a classic token with the `repo` scope: **Push** sends the commit
   step 4 left unpushed, and the section says where it pushed. Repeat
   steps 2 and 3, and step 3 of *Cloning from GitHub*, with it; then
   **Remove token**, and revoke it on GitHub.
6. Delete the test branches on GitHub; stop and forget the server; delete
   the private clone with **Delete folder**.

Write down what differed from this list. A screen rcman shows as
**Unrecognised screen** is recorded as a new fixture and handled before the
version is approved.

## Screens with no recording yet

Some screens rcman has no recording of, and so does not recognise; the full
list, and how rcman shows them, is under *Not recorded* in
[fake-claude.md](fake-claude.md). Some of them need particular conditions.
Whenever those conditions come up, record the screen: add a sequence for it to
`SEQUENCES` in `devtools/record_claude.py`, record it with
`uv run python devtools/record_claude.py --with-account --sequence <name>`,
and teach rc-helper and the fake to recognise it, tests first.

1. **Enabling Remote Control.** With a Claude account that has never used
   Remote Control, start a server: Claude asks once whether to enable it.
   Record that question, both answers, and the screen that follows each.
2. **Network loss.** With a server ready, cut the machine's network for longer
   than a server survives, then restore it. Record the screen the server ends
   on and its exit status.
3. **Expired sign-in.** With an account whose sign-in has expired — rcman
   shows the connection as expired — start a server. Record what Claude shows
   instead of the trust question or the server screen.
4. **A server in a git repository.** Step 3 of *Pushing to GitHub* starts
   one; record its screens, whose status line shows the branch.
