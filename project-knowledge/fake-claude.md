# The fake `claude`

Tests drive a fake `claude` instead of the real one, because the real one needs
a logged-in subscription account, creates real sessions, and cannot be made to
show its rarer screens on demand. The fake is a liability — it can drift from
the real binary — so it is built to be checked against it: every screen it
shows is a recording of the real binary, and contract tests compare the two.

## Where it is

| Path | What it is |
|---|---|
| `tests/fakes/claude` | The executable tests run; a launcher for `devtools/fake_claude.py` |
| `devtools/fake_claude.py` | The fake: a state machine that replays recorded screens |
| `devtools/record_claude.py` | The recorder: drives the real `claude`, sanitises and writes fixtures |
| `devtools/tmuxctl.py` | Runs one program in a private tmux server and reads its screen |
| `tests/fakes/fixtures/` | One `<capture>.txt` per recorded screen, and `manifest.json` |

## Fixtures

The recorder's `SEQUENCES` define every recording: the arguments `claude` is
run with, how it is driven, and the named screens captured along the way. A
fixture exists for exactly the captures the sequences define, and tests hold
the two to each other in both directions.

Captured text is sanitised before it is written: paths, URLs, clock times,
session names, account details and the version become `{placeholders}`, which
the fake fills in when it replays a screen. The placeholder set is
`PLACEHOLDERS` in `devtools/record_claude.py`. A test checks that no fixture
contains an email address, a home or temporary path, a sign-in URL or an
environment ID.

`manifest.json` records the Claude Code version the fixtures were recorded
from, and for each capture its sequence, its exit code (`null` when the
program was still running) and whether it needed an account.

## What the fake does

It implements only the invocations rcman makes — `--version`, `auth status`,
`auth login`, `remote-control` — and fails loudly on anything else. Its state
(logged in or not, trusted folders, the session each folder last served) is
`fake-state.json` under `CLAUDE_CONFIG_DIR`, or under `~/.claude-fake` when
that is unset, so it never touches a real Claude configuration. Tests set a
logged-in state with `write_state`.

It reproduces these behaviours of the real binary, each recorded:

- `remote-control` refuses without a login before it asks about trust.
- The folder-trust prompt refuses an answer that arrives too quickly after the
  question and asks again. An instant answer is refused; one a fraction of a
  second later is accepted. Anything that answers this prompt has to wait.
- A server first shows a *connected* screen with no session attached, then a
  *ready* screen once a session has attached and is named. How long the
  connected stage lasts varies.
- A new session's name is the prefix followed by generated words; the fake
  generates its own words.
- Ctrl+C stops a server, exit status zero, with the environment preserved;
  running `remote-control` again in the same folder resumes the same session
  without asking about trust.
- `remote-control --help` is refused without a login.

`FAKE_CLAUDE_SCENARIO=unrecognised` makes `remote-control` show a synthetic
screen that matches no recording, for testing how unrecognised output is
handled. `FAKE_CLAUDE_READY_SECONDS` sets how long the connected stage lasts.

## Not recorded

The fake has no recording of these, so it cannot show them, and rcman treats
them as unrecognised screens until they are recorded:

- a successful sign-in — every code the fake receives is answered with the
  recorded failure;
- the one-time "Enable Remote Control?" confirmation of an account that has
  never used Remote Control;
- a server exiting after an extended network outage;
- an expired login;
- a crashed session;
- interrupting a server before its session is ready — the fake finishes
  becoming ready first;
- a folder inside a git repository, where the status line shows a branch.

## How the fake is checked against the real binary

| Tests | Needs | Runs |
|---|---|---|
| `tests/test_fake_claude.py` | Nothing | Always. The recorder drives the fake exactly as it drives the real binary and must reproduce every fixture |
| `tests/test_claude_contract.py` | The real `claude`, no account | Always; skipped when `claude` is not installed unless `RCMAN_REQUIRE_REAL_CLAUDE=1`, which CI sets. The installed version must be the manifest's; the no-account screens must match the fixtures; every option rcman passes must be in `--help`, except where help is refused without a login |
| `tests/test_claude_real_account.py` | The real `claude`, logged in | By hand: `uv run pytest -m real_claude`. Runs the account sequences and the one that submits an invalid sign-in code, and checks the options whose help needs a login |

CI installs exactly the version in `manifest.json`. A machine whose `claude`
updates itself fails the contract tests as soon as the installed version moves
past the fixtures; that failure is the signal to approve the new version.

## Approving a new Claude Code version

1. Install the candidate version.
2. Re-record everything: `uv run python devtools/record_claude.py --with-account`.
   This needs a logged-in account and creates one Remote Control session in
   its claude.ai/code list (`--sequence <name>` re-records one sequence; a
   version change requires recording them all).
3. Review the fixture diff. Every change is a change in the real binary that
   rcman's screen reading may have to follow.
4. Run the whole suite, and `uv run pytest -m real_claude`.
5. Commit the fixtures and the manifest together.

Recording also leaves the scratch folders it trusted in that account's Claude
configuration.
