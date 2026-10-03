# Tests

How the test suite is organised and run. The rules the suite follows are in
[working-rules.md](working-rules.md).

## Running

| Command | What it does |
|---|---|
| `make setup` | Creates the development environment with `uv sync` |
| `make test` | Runs the whole suite with pytest |
| `make lint` | Checks formatting and lint with ruff |
| `make format` | Applies formatting and safe lint fixes |
| `make frontend-test` | Type-checks the frontend and runs its component tests |
| `make e2e` | Builds the frontend and runs the browser tests |
| `make check` | Lint, the Python tests, the frontend tests and the browser tests. It sets `RCMAN_REQUIRE_REAL_CLAUDE=1` and `RCMAN_REQUIRE_CONTAINER=1`, so tests that would skip for want of the real `claude` or of Docker fail instead |

Run pytest from the repository root; `pyproject.toml` holds its configuration.
The suite needs `tmux` and `git`. Everything git is tested against real git,
never a fake: `tests/git_fixtures.py`, which is not a test module, makes real
repositories, their origins and their branches with the system's git, under
a fixture configuration of its own, so the person's configuration rc-helper
reads is only what a test plants. GitHub is `tests/github_server.py`: the
system's `git http-backend` behind a small server, which serves anyone a
read of a public repository, and takes a push, or a read of one made
private, only with its token as the password, under any
username, as GitHub takes a personal access token. The `github` fixture in
`tests/conftest.py` runs one for the session, with `GITHUB_TOKEN`; every
rc-helper a test runs has `RC_HELPER_GITHUB` (see
[rc-helper.md](rc-helper.md#test-hooks)) pointing at it or at `UNREACHABLE`,
so that no test reaches the real GitHub; the browser tests' world runs one
of its own (see [frontend.md](frontend.md#tests)). `tests/test_container_suite.py` runs the root-only
suite in `tests/container/` inside a throwaway Ubuntu container, through
`docker` or `sudo -n docker`; it skips when neither works, unless
`RCMAN_REQUIRE_CONTAINER=1`, which `make check` sets. It runs these, each in a
fresh container: user management, with folders made and deleted and git run, with the person's own configuration, through sudo;
the GitHub token and pushing through sudo, against `https://github.com`
itself — `--add-host` makes `github.com` the container's own address, where
`tests/container/test_github_in_container.py` serves the tests' GitHub with
a certificate from a certificate authority it makes with `openssl` and adds
to the system's with `update-ca-certificates`, so that rc-helper runs with
no `RC_HELPER_GITHUB`, as in production;
a real installation (see
[installer.md](installer.md)); and the whole stack in a browser.
Run it from a full clone, not a git worktree: it mounts the checkout into the
container and installs from it with git, and a worktree's `.git` is a file
pointing outside that mount, so git fails inside the container.

The **whole stack** is `tests/container/stack.py`: rcman installed from a
clone, people added with `rcman-user` as real Linux accounts with the fake
`claude`, real oauth2-proxy whose Google endpoints point at
`tests/container/oidc_stub.py`, and real Caddy, running as its own account
with its admin API off, with its own certificate authority for `rcman.test`
and `auth.test`. Playwright on the host drives it
through the container's published port with `frontend/playwright.stack.config.ts`
and the specs in `frontend/e2e-stack/`: signing in through Caddy and
oauth2-proxy, connecting Claude, a server's lifecycle, one person unable to
reach another's servers through sudo, making and deleting a folder, which
another person cannot delete, an administrator seeing everyone's
servers without being able to act on them, an address that is not on the list, the
audit trail, and removing people with and without their home folder.

What needs a real Claude account is checked by hand:
[real-claude-checklist.md](real-claude-checklist.md). Tests marked `real_claude` are deselected by default
and run by hand with `uv run pytest -m real_claude`; what they need is in
[fake-claude.md](fake-claude.md).

## What lives where

| Path | Contents |
|---|---|
| `tests/` | The test suite; `tests/test_<name>.py` tests the module `<name>.py` |
| `tests/fakes/` | The fake `claude` and its recorded fixtures — see [fake-claude.md](fake-claude.md) |
| `helpers/` | `rc-helper` — see [rc-helper.md](rc-helper.md); `rc-admin` and `rcman-user` — see [rc-admin.md](rc-admin.md) |
| `backend/` | The FastAPI application — see [backend.md](backend.md) |
| `frontend/` | The React application, its component tests and its browser tests — see [frontend.md](frontend.md) |
| `tests/container/` | The container image and the suite that runs as root inside it |
| `devtools/` | Development tooling: the documentation guards, the edit-time hook, the tmux driver, the Claude recorder and the fake's implementation |

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
- **Deployment contents** (`tests/test_export.py`). Deployment uses
  `git archive`, so `.gitattributes` decides what is deployed. The test
  checks that every development-only file is marked `export-ignore`, that the
  files a deployment needs are not, and — in a scratch repository — that
  `git archive` really omits what the patterns mark.

## Edit-time hook

`.claude/settings.json` wires `devtools/on_edit.py` as a PostToolUse hook on
file edits in Claude Code sessions. It runs the edited file's paired test:

- `<dir>/<name>.py` runs `tests/test_<name>.py`;
- a test file runs itself;
- a Markdown file runs `tests/test_docs_guard.py`;
- `frontend/src/<name>.ts(x)` runs `<name>.test.ts(x)` beside it with Vitest.

A Python module with no paired test is reported. The hook is silent when the
test passes; on failure it reports to the operator and to the agent. Its own
tests, including one that checks the wiring in `.claude/settings.json`, are in
`tests/test_on_edit.py`.
