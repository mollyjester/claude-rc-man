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
2. Run `uv run pytest -m real_claude`. It runs every recording sequence that
   needs an account — the folder-trust question, a server through
   connected, ready, stopped and resumed — and the one that sends an invalid
   sign-in code, and compares each screen with the fixtures. It creates one
   session.
3. When a Claude Code version is being approved, re-record instead:
   `uv run python devtools/record_claude.py --with-account`, then the person-
   driven sign-in sequence, `--sequence login_succeeded`, which needs someone
   to sign in and hand over the code. Review the fixture diff before
   committing it.

## On an installed server

With a person added for the test account (`sudo rcman-user add`):

1. Sign in to rcman with their Google address.
2. **Connect Claude account**: the sign-in link opens claude.com; after
   signing in, the code Claude shows connects the account, and the page shows
   the account and when its sign-in expires.
3. Start a server in a folder under `~/projects`, answer the trust question,
   and wait for **Ready**.
4. Open **Open in claude.ai**: the session is listed in claude.ai/code under
   the session name rcman shows, and answers a message.
5. **Stop**, then **Resume**: the same session comes back in claude.ai/code.
6. **Stop** and **Forget**; archive the session in claude.ai/code.
7. On **Claude Code** (administrators), the person's installed version is the
   approved one.

Write down what differed from this list. A screen rcman shows as
**Unrecognised screen** is recorded as a new fixture and handled before the
version is approved.
