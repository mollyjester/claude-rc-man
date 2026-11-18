# The fake `claude`

Tests drive a fake `claude` instead of the real one, because the real one needs
a logged-in subscription account, creates real sessions, and cannot be made to
show its rarer screens on demand. Every screen the fake shows but one is test
data: a screen the real binary showed, kept as it was, or, where nothing can
bring a screen about, a screen seen on a live server; the exception is the
synthetic screen of the `unrecognised` scenario. The screens are not checked
against the real binary. What holds rcman to the real `claude` is rcman's own
checks (`helpers/claude_checks.py`), the ones every version is held to before it
is rolled out, which the contract tests run against the installed binary (see
[What holds rcman to the real binary](#what-holds-rcman-to-the-real-binary)).

## Where it is

| Path | What it is |
|---|---|
| `tests/fakes/claude` | The executable tests run; a launcher for `devtools/fake_claude.py` |
| `devtools/fake_claude.py` | The fake: a state machine that shows the fixtures and the observed screens; also the one reader of both (`load_fixtures`, `load_observed`), and `FAKE_VERSION`, the version it reports unless told another |
| `devtools/tmuxctl.py` | Runs one program in a private tmux server and reads its screen |
| `tests/claude_watches.py` | What the tests wait on the fake with: its trace and its gate |
| `tests/fakes/fixtures/` | One `<name>.txt` per screen, and `manifest.json` |
| `tests/fakes/observed/` | One `<name>.txt` per screen seen on a live server, and `manifest.json` — see [Observed screens](#observed-screens) |

## Fixtures

A fixture is a screen as the real binary showed it, with what differs from run
to run made `{placeholders}`: paths, URLs, clock times, session names, session
IDs (`cse_…`), account details (the plan and the sign-in's expiry among them)
and the version, which the fake fills in when it shows a screen (`PLACEHOLDERS`
in `devtools/fake_claude.py`). `sanitise` there makes a live screen's paths,
clock times, session IDs and version placeholders again, which is what the
observed screens hold; session IDs are found by `SESSION_ID` in
`helpers/claude_checks.py`, the pattern that blanks them in a refused upgrade's
report. A test checks that
no fixture or observed screen contains any kind of leak `LEAK_MARKS` in
`tests/test_fake_claude.py` names, each found by its marks, and that each mark
finds a planted sample of its kind.

`manifest.json` lists every fixture with its exit code (`null` when the program
was still running). `tests/test_screens.py` holds each fixture to the state
rcman reads from it.

## Observed screens

A screen that a live server has shown but that nothing can bring about on
demand is kept, where rcman has to recognise it, as seen — the whole pane,
read with `tmux capture-pane` as rcman reads it — in `tests/fakes/observed/` as
`<name>.txt`, its paths, clock times, session IDs and version made placeholders by
`sanitise`, and anything else in it that differs from run to run, or is a secret,
replaced by hand with the fixtures' placeholders. That folder's `manifest.json` lists
every screen in it, and for each records its exit code (`null` when the program
was still running), how it was seen (`seen`), and, where the
[journal](../docs/journal.md#output-rcman-does-not-recognise) line reported it,
that line (`journal`: its `code`, `verb` and `fingerprint`). Tests hold each
screen to the state rcman reads from it (`OBSERVED_STATES` in
`tests/test_screens.py`).

Every observed screen is a scenario of the fake under its own name:
`FAKE_CLAUDE_SCENARIO=<name>` makes `remote-control` show that screen and
wait. What follows Ctrl+C there was not seen, so the fake then ends as in the
`unrecognised` scenario. The exception is `remote_control_prompt`, Claude's
one-time question of an account that has never used Remote Control: it takes an
answer, as the question does, so that what rcman does with the answers can be
tested. Only the screen was seen; what the fake does after it is listed under
[Not seen](#not-seen).

## When a check fails on a new version

A new Claude Code version that rcman cannot work with fails rcman's checks when
it is rolled out, and the roll-out says which checks failed and what each ran and
got back ([Claude Code versions](../docs/claude-version.md)). Starting from that
report, change rcman's parser (`helpers/screens.py`, or the check itself) and
the fake's screen together, tests first: the fixture shows what the new version
shows, with what differs from run to run, or is a secret, replaced by hand with the
fixtures' placeholders, and the tests that hold it to the state
rcman reads fail until the parser reads it. `FAKE_VERSION` then names the
version the fake now stands for.

## What the fake does

It implements only the invocations rcman makes — `--version`, `auth status`,
`auth login` and its `--help`, `auth logout`, `remote-control` — and fails
loudly on anything else. Its configuration folder is `CLAUDE_CONFIG_DIR`, or
`~/.claude-fake` when that is unset, so it never touches a real Claude
configuration. Whether it is signed in is its credentials file there,
`.credentials.json`, as the real one's is: signed in while the file holds
`claudeAiOauth`, signed out while it is empty, `{}` or not there. Its trusted
folders and the session each folder last served are `fake-state.json` there.
Tests set a signed-in state with `write_state`.

It reproduces these behaviours of the real binary, each seen on it:

- `remote-control` refuses without a login before it asks about trust, and,
  signed in, refuses on stderr, exit status 1, while `.claude.json` holds no
  `oauthAccount`.
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
- In a git repository — the folder or one above it has a `.git` — the
  server's status line names the branch, which the fake reads from the
  repository's `HEAD`; outside one it names the folder alone.
- In a git repository, started without `--spawn`, Claude asks for a spawn
  mode after the trust question. Answered `1`, the server goes on as
  anywhere else; SIGTERM ends it, as any process is ended. With
  `--spawn same-dir`, as rcman starts every server, it is not asked.

Signing in succeeds, with its screens, for `ACCEPTED_CODE`, and fails
for any other. It writes the credentials file by rename, owner-only, and in
place, truncated first, when the rename is refused with EBUSY, as it is over a
file bound into a workspace; a link is followed, for the host runtime's links to
a person's file. It writes `oauthAccount` into `.claude.json` there, keeping
every other key. `auth logout` removes the file, as the real one is seen to do
(`tests/test_claude_contract.py`), and leaves one it cannot remove (EBUSY),
as the real one leaves a bound file, answering exit status 0 either way.

`FAKE_CLAUDE_SCENARIO=unrecognised` makes `remote-control` and `auth login`
show a synthetic screen that matches no fixture, for testing how
unrecognised output is handled; Ctrl+C then ends it with exit status 1. The
name of an observed screen makes `remote-control` show that screen (see
[Observed screens](#observed-screens), which says which of them takes an
answer). `FAKE_CLAUDE_READY_SECONDS` sets how long the connected stage lasts,
`FAKE_CLAUDE_MIN_ANSWER_SECONDS` how soon after the trust question an answer
is refused as too quick (`MIN_ANSWER_SECONDS` unset), and `FAKE_CLAUDE_VERSION`
the version it reports.

Two more let a test see what the fake has done instead of waiting for it to
have done it; with neither set, nothing changes:

- `FAKE_CLAUDE_TRACE` names a file the fake appends a JSON line to for each
  screen it has just written whole: the event `drawn`, with the screen's lines
  as `tmuxctl`'s `Pane.screen` reads a screen.
- `FAKE_CLAUDE_GATE` names a folder: a server leaves its connected stage not
  after `READY_SECONDS` but once a file named `connected` is there, which it
  removes.

`tests/claude_watches.py` is what the tests wait with over them.

`fake-exec COMMAND [ARGUMENT...]` is the one verb the real `claude` has no
counterpart of, and nothing in it was seen on the real one: it runs the command in the
environment the fake was started with but for `FAKE_CLAUDE_VERSION` (a release's
launcher exports it for the fake alone, and the real Claude's children never see
such a variable), on its own streams, and ends with the command's exit status. A workspace starts Claude through `env -i` with exactly
`runtime.child_env`, so the command sees what a command Claude runs, as git
does for it, would see.
`tests/test_fake_claude.py` holds it.

## Not seen

The fake has no screen of these, so it cannot show them, except as noted.
rcman reports a live one it does not know as `unrecognised`, and a server whose
process has ended on one as `exited` (see [runtime.md](runtime.md#server-states)):

- what follows the observed "Enable Remote Control?" question of an account
  that has never used Remote Control. In the `remote_control_prompt` scenario
  the fake guesses: `y` goes on to the connected and ready screens as after the
  trust question, `n` and anything else but `y` end it with exit status 1,
  Ctrl+C at the question ends it with exit status 1, and it takes an answer at
  once. Not seen: the screens after `y`, whether Claude exits after `n` and the
  screen and exit status then, what Ctrl+C does at the question, and whether
  Claude refuses an answer that comes too quickly, as it does the trust
  question's;
- a server exiting after an extended network outage;
- an expired login;
- logging out an account that is logged in — the fake shows the screen
  of one that is not;
- a crashed session;
- interrupting a server before its session is ready — the fake finishes
  becoming ready first;
- any `--spawn` but `same-dir`, the spawn-mode question answered otherwise
  than `1`, and whether Claude asks it again in a folder it already trusts —
  the fake refuses these as unsupported invocations;
- a server in a repository with a detached HEAD — the fake's status line
  shows the commit's hash where the branch would be;
- what follows Ctrl+C at an observed screen, and at the spawn-mode question,
  where Claude is seen to end, the screen and exit status it ends with — the
  fake ends with exit status 1, as in the `unrecognised` scenario;
- a refresh of the sign-in — the real one writes it in place over a bound
  file, truncated first, keeping keys it does not know; the fake never
  refreshes;
- the values of a sign-in — the fake's credentials and `oauthAccount` carry
  the keys a real sign-in writes (`tests/test_fake_claude.py` holds them to
  the names seen), with values of its own.

What is seen of the shared sign-in with a real account, and how to see it again,
is in [real-claude-checklist.md](real-claude-checklist.md#the-shared-sign-in).

## What holds rcman to the real binary

| Tests | Needs | Runs |
|---|---|---|
| `tests/test_claude_contract.py` | The real `claude`, no account | Always; skipped when `claude` is not installed unless `RCMAN_REQUIRE_REAL_CLAUDE=1`, which `make check` sets. The installed binary passes rcman's checks (`candidate_failures` in `helpers/claude_checks.py`), as a version rolled out must; each command's `--help` must be its own, its usage line naming the command (an unknown command prints the top-level help), and name every option rcman passes it (`options_used`, read from the backend's `claude_command` calls; `help_gaps`), and a help refused for want of a login is read as rcman reads a server's screen (`screens.classify`); an empty and a `{}` credentials file read as signed out and are left as they are; `auth logout` removes the file |
| `tests/test_claude_real_account.py` | The real `claude`, logged in | By hand: `uv run pytest -m real_claude`. Under the account of the run (the folder `CLAUDE_CONFIG_DIR` names where it is set, otherwise the default configuration): rcman's own server (`backend/rcman/claude_server.py`), in a workspace of the host runtime whose credentials file leads to the account's, comes to ready, read by `helpers/screens.py`, and stops; and each command's `--help` is checked the same way while logged in |

`tests/test_fake_claude.py` holds the fake itself: each observed screen's scenario
shows that screen, the sign-in, the logout and `fake-exec` do what they say, and no
screen it keeps gives anything away.

Every test that waits on a pane gives the pane's waits the tier's guard
(`tier_guard()`, or `HANG_GUARD` in a host test), so that a hang fails there with
tmuxctl's `TimeoutError` and the screen the pane last showed; a test meant to see a
wait give up passes `0`. The fake's own threshold, `MIN_ANSWER_SECONDS`, is held
against a clock only the test moves (`tests/test_fake_claude.py`).

## In a workspace

In the container suites, the fake is the Claude Code release the installer
downloads: `tests/claude_download.py` lays it out as a release —
`<version>/manifest.json` with its checksum, and `<version>/<platform>/claude`
— and the suite serves it and gives the installer its address. The binary is
one file, as Claude Code's is: a shell script holding the fake and what it
reads, which unpacks itself under `$TMPDIR` the first time it runs and runs it
with the image's `python3`; the default workspace image has `python3` for it. What it holds
is `PARTS` in `tests/claude_download.py`: `DATA`, the launcher it runs (`tests/fakes/claude`)
and the screens, and the fake's Python, which `python_parts` derives from the fake's
imports, followed through the repository's own modules, so a module one of them comes to
import is in the bundle at once.

A release has a kind (`KINDS` in `tests/claude_download.py`): `good`, the fake reporting the
version it is released as, which the file bakes in (the label `FAKE_CLAUDE_VERSION`, set by
`bundle(version)`); `broken`, a program that reports another version and does nothing else
Claude Code does, which fails the canary's checks; and `failing`, the fake whose manifest names
another checksum, which the download refuses. `release` writes the release and the folder's
`latest`, `candidate` a candidate for the canary, and `served` serves a folder over HTTP: the
host tests give that address to rcman (`RCMAN_CLAUDE_DOWNLOAD`), which rolls out the fake as it
does the real program; the stubbed installation serves its release to the installer from a
process of its own (`serve_releases` in `tests/container/installed.py`).

On the host, `devtools/fake_store.py` (`fake_claude`) makes a Claude Code store as the installer
leaves it, holding a version that runs the fake: `versions/<version>`, `current` and
`bin/claude`. Not a test module.

