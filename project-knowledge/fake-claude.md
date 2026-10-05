# The fake `claude`

Tests drive a fake `claude` instead of the real one, because the real one needs
a logged-in subscription account, creates real sessions, and cannot be made to
show its rarer screens on demand. The fake is a liability — it can drift from
the real binary — so it is built to be checked against it. Every screen it
shows but one is a recording of the real binary, or, where no recording can
bring a screen about, a screen seen on it; the exception is the synthetic
screen of the `unrecognised` scenario. Contract tests compare the recordings
with the binary.

## Where it is

| Path | What it is |
|---|---|
| `tests/fakes/claude` | The executable tests run; a launcher for `devtools/fake_claude.py` |
| `devtools/fake_claude.py` | The fake: a state machine that replays recorded and observed screens |
| `devtools/record_claude.py` | The recorder: drives the real `claude`, sanitises and writes fixtures; also the one reader of the observed screens (`load_observed`) |
| `devtools/tmuxctl.py` | Runs one program in a private tmux server and reads its screen |
| `tests/fakes/fixtures/` | One `<capture>.txt` per recorded screen, and `manifest.json` |
| `tests/fakes/observed/` | One `<name>.txt` per screen seen on a live server that no recording can bring about, and `manifest.json` — see [Observed screens](#observed-screens) |

## Fixtures

The recorder's `SEQUENCES` define every recording: the arguments `claude` is
run with, how it is driven, and the named screens captured along the way. A
fixture exists for exactly the captures the sequences define, and tests hold
the two to each other in both directions.

Captured text is sanitised before it is written: paths, URLs, clock times,
session names, session IDs (`cse_…`), account details and the version become
`{placeholders}`, which the fake fills in when it replays a screen. The
placeholder set is `PLACEHOLDERS` in `devtools/record_claude.py`. A test
checks that no fixture or observed screen contains an email address, a home
or temporary path, a sign-in URL, an environment ID or a session ID.

A sequence marked `git_repository` runs in a throwaway repository the
recorder makes in its own scratch folder, on `RECORD_BRANCH`, and the branch
in a server's status line becomes `{branch}`.

`manifest.json` records the Claude Code version the fixtures were recorded
from, and for each capture its sequence, its exit code (`null` when the
program was still running) and whether it needed an account.

## Observed screens

A screen that a live server has shown but that no sequence can bring about
cannot be recorded. Where rcman has to recognise one, the screen as seen —
the whole pane, read with `tmux capture-pane` as rc-helper reads it — is kept
in `tests/fakes/observed/` as `<name>.txt`, sanitised as the fixtures are.
That folder's `manifest.json` lists every screen in it, and for each records
its exit code (`null` when the program was still running), the Claude Code
version it was seen on (`seen_on`), the versions it is kept for (`kept_for`,
starting with `seen_on`), how it was seen (`seen`), and the
[journal](../docs/journal.md#output-rcman-does-not-recognise) line that
reported it (`journal`: its `code`, `verb` and `fingerprint`).
`load_observed` in `devtools/record_claude.py` is the one reader.

Tests hold the manifest to exactly the files beside it
(`tests/test_record_claude.py`), and each screen to the state rc-helper reads
from it (`OBSERVED_STATES` in `tests/test_rc_helper.py`). An observed screen
cannot be re-recorded when a new Claude Code version is approved, so
`test_every_observed_screen_is_kept_for_the_approved_version` fails while a
screen's `kept_for` does not name the version in the fixtures' manifest; see
[Approving a new Claude Code version](#approving-a-new-claude-code-version).

Every observed screen is a scenario of the fake under its own name:
`FAKE_CLAUDE_SCENARIO=<name>` makes `remote-control` show that screen and
wait. What follows Ctrl+C there was not seen, so the fake then ends as in the
`unrecognised` scenario.

## What the fake does

It implements only the invocations rcman makes — `--version`, `auth status`,
`auth login` and its `--help`, `auth logout`, `remote-control` — and fails
loudly on anything else. Its state
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
- A new session's name is the prefix followed by generated words. The fake
  names it `--name` when given, and otherwise the prefix followed by
  `fake-session`.
- Ctrl+C stops a server, exit status zero, with the environment preserved;
  running `remote-control` again in the same folder resumes the same session
  without asking about trust.
- `remote-control --help` is refused without a login.
- In a git repository — the folder or one above it has a `.git` — the
  server's status line names the branch, which the fake reads from the
  repository's `HEAD`.
- In a git repository, started without `--spawn`, Claude asks for a spawn
  mode after the trust question. Answered `1`, the server goes on as
  anywhere else. Ctrl+C there changes nothing, and no answer is taken after
  it; SIGTERM changes nothing either; SIGKILL ends it. With
  `--spawn same-dir` it is not asked.

Signing in succeeds, with the recorded screens, for a code listed in the
state's `accept_codes`, and fails for any other.

`FAKE_CLAUDE_SCENARIO=unrecognised` makes `remote-control` and `auth login`
show a synthetic screen that matches no recording, for testing how
unrecognised output is handled; Ctrl+C then ends it with exit status 1. The
name of an observed screen makes `remote-control` show that screen (see
[Observed screens](#observed-screens)). `FAKE_CLAUDE_READY_SECONDS` sets how long the
connected stage lasts, and `FAKE_CLAUDE_VERSION` the version it reports.

## Not recorded

The fake has no recording of these, so it cannot show them. rc-helper
reports a live one as `unrecognised`, and a server whose
process has ended on one as `exited` (see [rc-helper.md](rc-helper.md)):

- the one-time "Enable Remote Control?" confirmation of an account that has
  never used Remote Control;
- a server exiting after an extended network outage;
- an expired login;
- logging out an account that is logged in — the fake shows the screen
  recorded for one that is not;
- a crashed session;
- interrupting a server before its session is ready — the fake finishes
  becoming ready first;
- any `--spawn` but `same-dir`, the spawn-mode question answered otherwise
  than `1`, and whether Claude asks it again in a folder it already trusts —
  the fake refuses these as unsupported invocations;
- a server in a repository with a detached HEAD — the fake's status line
  shows the commit's hash where the branch would be;
- what follows Ctrl+C at an observed screen — the fake ends with exit
  status 1, as in the `unrecognised` scenario.

## How the fake is checked against the real binary

| Tests | Needs | Runs |
|---|---|---|
| `tests/test_fake_claude.py` | Nothing | Always. The recorder drives the fake exactly as it drives the real binary, each sequence in a test case of its own with an account of its own, and must reproduce every fixture but those of the sequences that need a person, whose screens the fake's sign-in tests check; each observed screen's scenario must show that screen |
| `tests/test_claude_contract.py` | The real `claude`, no account | Always; skipped when `claude` is not installed unless `RCMAN_REQUIRE_REAL_CLAUDE=1`, which `make check` sets. The installed version must be the manifest's; the no-account screens must match the fixtures; every option rcman passes must be in `--help`, except where help is refused without a login |
| `tests/test_claude_real_account.py` | The real `claude`, logged in | By hand: `uv run pytest -m real_claude`. Runs the account sequences and the one that submits an invalid sign-in code, and checks every option rcman passes against `--help` while logged in |

The contract tests compare the installed `claude` with `manifest.json`. A
machine whose `claude` updates itself fails them as soon as the installed
version moves past the fixtures; that failure is the signal to approve the new
version.

## Approving a new Claude Code version

1. Install the candidate version.
2. Re-record every sequence: `uv run python devtools/record_claude.py --with-account`,
   then each sequence that needs a person, by name (see
   [Sequences that need a person](#sequences-that-need-a-person)). This needs
   a logged-in account and creates a Remote Control session in its
   claude.ai/code list for each sequence that brings a server to ready, as
   the recorder's docstring names them (`--sequence <name>` re-records one
   sequence; a version change requires recording them all).
3. Review the fixture diff. Every change is a change in the real binary that
   rcman's screen reading may have to follow.
4. For each [observed screen](#observed-screens), which cannot be
   re-recorded, decide whether the new version may still show it: if so, add
   the version to its `kept_for` in `tests/fakes/observed/manifest.json`;
   if not, remove the screen and what recognises it, tests first.
5. Run the whole suite, and `uv run pytest -m real_claude`.
6. Commit the fixtures and both manifests together.

Recording also leaves the scratch folders it trusted in that account's Claude
configuration.

## Sequences that need a person

A sequence marked `needs_human` runs only when named with `--sequence`, never
in an automated run. `login_succeeded` records a real sign-in: it writes the
sign-in URL to `HANDOFF/url` (`HANDOFF` in `devtools/record_claude.py`), waits
for a person to sign in and put Claude's code in `HANDOFF/code`, captures the
success screens, and logs out, which revokes that sign-in. It uses a
throwaway configuration, so the account's other sign-ins are untouched.
