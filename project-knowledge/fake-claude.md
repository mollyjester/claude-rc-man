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
placeholder set is `PLACEHOLDERS` in `devtools/record_claude.py`; email
addresses and session IDs are found by `EMAIL_ADDRESS` and `SESSION_ID` in
`helpers/claude_checks.py`, the same patterns that blank them in a refused
upgrade's report. A test checks that no fixture or observed screen contains
any kind of leak `LEAK_MARKS` in `tests/test_record_claude.py` names, each
found by its marks, and that each mark finds a planted sample of its kind.

A sequence marked `without_oauth_account` runs with the account's credentials
file linked into a configuration folder of its own that has no `.claude.json`,
as a workspace's is before rcman seeds `oauthAccount`: Remote Control refuses
there (`rc_no_oauth_account`), on stderr.

Every `claude` the recorder and the tests run has a home of its own, a temporary folder
(`claude_env` in `devtools/record_claude.py`, which refuses the home the run was started
from), so Claude writes nothing into a developer's own home, a workspace's among them. A
sequence that needs the account and is given no configuration folder runs with no
`CLAUDE_CONFIG_DIR`, in a home whose `.claude` and `.claude.json` lead to the account's own in
the home the run was started from (`account_home`): Claude keeps the account's `.claude.json`
beside the folder, not in it, so naming the folder would find no account.
`tests/test_record_claude.py` holds the environment of each kind of sequence, and the home the
run was started from untouched otherwise.

A sequence marked `git_repository` runs in a throwaway repository the
recorder makes in its own scratch folder, on `RECORD_BRANCH`, and the branch
in a server's status line becomes `{branch}`.

`manifest.json` records the Claude Code version the fixtures were recorded
from, and for each capture its sequence, its exit code (`null` when the
program was still running) and whether it needed an account.

## Observed screens

A screen that a live server has shown but that no sequence can bring about
cannot be recorded. Where rcman has to recognise one, the screen as seen —
the whole pane, read with `tmux capture-pane` as rcman reads it — is kept
in `tests/fakes/observed/` as `<name>.txt`, sanitised as the fixtures are.
That folder's `manifest.json` lists every screen in it, and for each records
its exit code (`null` when the program was still running), the Claude Code
version it was seen on (`seen_on`), the versions it is kept for (`kept_for`,
starting with `seen_on`), how it was seen (`seen`), and, where the
[journal](../docs/journal.md#output-rcman-does-not-recognise) line reported
it, that line (`journal`: its `code`, `verb` and `fingerprint`).
`load_observed` in `devtools/record_claude.py` is the one reader.

Tests hold the manifest to exactly the files beside it
(`tests/test_record_claude.py`), and each screen to the state rcman reads
from it (`OBSERVED_STATES` in `tests/test_screens.py`). An observed screen
cannot be re-recorded when a new Claude Code version is approved, so
`test_every_observed_screen_is_kept_for_the_approved_version` fails while a
screen's `kept_for` does not name the version in the fixtures' manifest; see
[Approving a new Claude Code version](#approving-a-new-claude-code-version).

Every observed screen is a scenario of the fake under its own name:
`FAKE_CLAUDE_SCENARIO=<name>` makes `remote-control` show that screen and
wait. What follows Ctrl+C there was not seen, so the fake then ends as in the
`unrecognised` scenario. The exception is `remote_control_prompt`, Claude's
one-time question of an account that has never used Remote Control: it takes an
answer, as the question does, so that what rcman does with the answers can be
tested. Only the screen was seen; what the fake does after it is listed under
[Not recorded](#not-recorded).

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

It reproduces these behaviours of the real binary, each recorded:

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
  repository's `HEAD`.
- In a git repository, started without `--spawn`, Claude asks for a spawn
  mode after the trust question. Answered `1`, the server goes on as
  anywhere else. Ctrl+C there changes nothing, and no answer is taken after
  it; SIGTERM ends it, as any process is ended. With
  `--spawn same-dir` it is not asked.

Signing in succeeds, with the recorded screens, for `ACCEPTED_CODE`, and fails
for any other. It writes the credentials file by rename, owner-only, and in
place, truncated first, when the rename is refused with EBUSY, as it is over a
file bound into a workspace; a link is followed, for the host runtime's links to
a person's file. It writes `oauthAccount` into `.claude.json` there, keeping
every other key. `auth logout` removes the file, as the real one is seen to do
(`tests/test_claude_contract.py`), and leaves one it cannot remove (EBUSY),
as the real one leaves a bound file, answering exit status 0 either way.

`FAKE_CLAUDE_SCENARIO=unrecognised` makes `remote-control` and `auth login`
show a synthetic screen that matches no recording, for testing how
unrecognised output is handled; Ctrl+C then ends it with exit status 1. The
name of an observed screen makes `remote-control` show that screen (see
[Observed screens](#observed-screens), which says which of them takes an
answer). `FAKE_CLAUDE_READY_SECONDS` sets how long the connected stage lasts,
and `FAKE_CLAUDE_VERSION` the version it reports.

`fake-exec COMMAND [ARGUMENT...]` is the one verb the real `claude` has no
counterpart of, and nothing in it is recorded: it runs the command in the
environment the fake was started with but for `FAKE_CLAUDE_VERSION` (a release's
launcher exports it for the fake alone, and the real Claude's children never see
such a variable), on its own streams, and ends with the command's exit status. A workspace starts Claude through `env -i` with exactly
`runtime.child_env`, so the command sees what a command Claude runs, as git
does for it, would see; the container test of git uses it to show Claude's own git
and gh using the person's token (`tests/container/test_github_in_podman.py`).
`tests/test_fake_claude.py` holds it.

## Not recorded

The fake has no recording of these, so it cannot show them, except as noted.
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
  status 1, as in the `unrecognised` scenario;
- a refresh of the sign-in — the real one writes it in place over a bound
  file, truncated first, keeping keys it does not know; the fake never
  refreshes;
- the values of a sign-in — the fake's credentials and `oauthAccount` carry
  the keys a real sign-in writes (`tests/test_fake_claude.py` holds them to
  the names seen), with values of its own.

What is seen of the shared sign-in with a real account, and how to see it again,
is in [real-claude-checklist.md](real-claude-checklist.md#the-shared-sign-in).

## How the fake is checked against the real binary

| Tests | Needs | Runs |
|---|---|---|
| `tests/test_fake_claude.py` | Nothing | Always. The recorder drives the fake exactly as it drives the real binary, each sequence in a test case of its own with an account of its own, and must reproduce every fixture but those of the sequences that need a person, whose screens the fake's sign-in tests check; each observed screen's scenario must show that screen |
| `tests/test_claude_contract.py` | The real `claude`, no account | Always; skipped when `claude` is not installed unless `RCMAN_REQUIRE_REAL_CLAUDE=1`, which `make check` sets. The installed version must be the manifest's; the no-account screens must match the fixtures; every option rcman passes must be in `--help`, which is printed without a login; an empty and a `{}` credentials file read as signed out and are left as they are; `auth logout` removes the file |
| `tests/test_claude_real_account.py` | The real `claude`, logged in | By hand: `uv run pytest -m real_claude`. Runs the account sequences and the one that submits an invalid sign-in code, and checks every option rcman passes against `--help` while logged in, under the account of the run: the folder `CLAUDE_CONFIG_DIR` names where it is set, otherwise the default configuration |

The contract tests compare the installed `claude` with `manifest.json`. A
machine whose `claude` updates itself fails them as soon as the installed
version moves past the fixtures; that failure is the signal to approve the new
version.

`test_fake_reproduces_the_recorded_screens_of[trust_too_fast]` replays the
recorder's own `trust_too_fast` sequence, which waits for `[y/N]` and only then
types its answer. On a heavily loaded machine the answer can reach the fake
later than `MIN_ANSWER_SECONDS` after the question, so the fake accepts it and
the test fails with `'too quickly' did not appear` (seen in 4 of 16 runs at a
load average of about 36). It is kept as it is because the recorder drives the
real `claude` the same way, and how the real one treats an answer typed before
its question has not been recorded. When it goes red it is fixed then, not
re-run: either the recorder types the answer before the question, confirmed
by a real-account recording, or the replay alone does so.
`test_an_instant_trust_answer_is_refused_and_asked_again` has no such race:
it starts the fake only once the answer is waiting on the terminal.

## Approving a new Claude Code version

1. Install the candidate version.
2. Re-record every sequence: `uv run python devtools/record_claude.py --with-account`,
   then each sequence that needs a person, by name (see
   [Sequences that need a person](#sequences-that-need-a-person)). This needs
   a logged-in account, the one the run was started with: the folder
   `CLAUDE_CONFIG_DIR` names where it is set, otherwise the `.claude` and
   `.claude.json` of the home of the run. It creates a Remote Control session in its
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
6. In a workspace of the default image on an installed rcman running the candidate, check
   what Claude is told there, as the step on rcman's instructions in
   [On an installed server](real-claude-checklist.md#on-an-installed-server) says: the managed
   instructions still load into a session, and the text Remote Control appends to its system
   prompt still has the heading the correction in `backend/rcman/instructions/common.md` names
   (see [runtime.md](runtime.md#workspace-instructions)). The canary checks neither.
7. Commit the fixtures and both manifests together, with `helpers/claude-version`
   naming the version: it is the version the installer installs and the one the
   Versions page calls the tested one (see [Claude Code versions](../docs/claude-version.md)).
   Approving it for workspaces is then an administrator's roll-out, not part of this list.

Recording also leaves the scratch folders it trusted in that account's Claude
configuration.

## Sequences that need a person

A sequence marked `needs_human` runs only when named with `--sequence`, never
in an automated run. `login_succeeded` records a real sign-in: it makes a
`handoff-*` folder in the recording's working folder and names it on stderr,
writes the sign-in URL to `url` there, waits for a person to sign in and put
Claude's code in `code` there, as one line ended with a newline, which is read
only once it is ended, captures the success screens, and logs out,
which revokes that sign-in. It uses a
throwaway configuration, so the account's other sign-ins are untouched.

## In a workspace

In the container suites, the fake is the Claude Code release the installer
downloads: `tests/claude_download.py` lays it out as a release —
`<version>/manifest.json` with its checksum, and `<version>/<platform>/claude`
— and the suite serves it and gives the installer its address. The binary is
one file, as Claude Code's is: a shell script holding the fake and what it
reads, which unpacks itself under `$TMPDIR` the first time it runs and runs it
with the image's `python3`. The default workspace image has `python3` for it.

A release has a kind (`KINDS` in `tests/claude_download.py`): `good`, the fake reporting the
version it is released as, which the file bakes in (the label `FAKE_CLAUDE_VERSION`, set by
`bundle(version)`); `broken`, a program that reports another version and does nothing else
Claude Code does, which fails the canary's checks; and `failing`, the fake whose manifest names
another checksum, which the download refuses. `release` writes the release and the folder's
`latest`, `candidate` a candidate for the canary, and `served` serves a folder over HTTP: the
container suite gives that address to the installer and to rcman (`RCMAN_CLAUDE_DOWNLOAD`),
which roll out the fake as they do the real program.

On the host, `tests/fake_store.py` (`fake_claude`) makes a Claude Code store as the installer
leaves it, holding a version that runs the fake: `versions/<version>`, `current` and
`bin/claude`. Not a test module.

