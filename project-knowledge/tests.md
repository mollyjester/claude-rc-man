# Tests

How the test suite is organised and run. The rules the suite follows are in
[working-rules.md](working-rules.md).

## Running

| Command | What it does |
|---|---|
| `make setup` | Creates the development environment: `make frontend-setup`, then `uv sync` |
| `make test` | Runs the whole suite with pytest |
| `make lint` | Checks formatting and lint with ruff |
| `make format` | Applies formatting and safe lint fixes |
| `make frontend-setup` | Installs the frontend's packages and Playwright's browser |
| `make frontend-test` | Type-checks the frontend and runs its component tests |
| `make frontend-build` | Builds the frontend into `frontend/dist` |
| `make e2e` | Builds the frontend and runs the browser tests |
| `make check` | Lint, the Python tests, the frontend tests and the browser tests. It sets `RCMAN_REQUIRE_REAL_CLAUDE=1` and `RCMAN_REQUIRE_CONTAINER=1`, so tests that would skip for want of the real `claude` or of Docker fail instead |

Run pytest from the repository root; `pyproject.toml` holds its configuration.
The suite needs `tmux`, `git` and sudo's `visudo`. Each test's tmux sockets
are in a folder of its own, made under `/tmp` by `tmux_sockets` in
`tests/conftest.py`, because a socket's path has to stay short, and removed,
with every tmux server in it, when the test ends. Everything git is tested against real git,
never a fake: `tests/git_fixtures.py`, which is not a test module, makes real
repositories, their origins and their branches with the system's git, under
a fixture configuration of its own, in a home removed when the tests end, so the person's configuration rc-helper
reads is only what a test plants; fixture git refuses every transport but a
local repository's, so an http, https, ssh, git or scp-like address a test
gives it is refused before anything is connected to. GitHub is `tests/github_server.py`: the
system's `git http-backend` behind a small server, which serves anyone a
read of a public repository, and takes a push, or a read of one made
private, only with its token as the password, under any
non-empty username, as GitHub takes a personal access token. The `github` fixture in
`tests/conftest.py` runs one for the session, with `GITHUB_TOKEN`. Every
rc-helper a test runs gets its environment from `helper_env` in
`tests/helper_env.py`, which always sets `RC_HELPER_GITHUB` (see
[rc-helper.md](rc-helper.md#test-hooks)) to it or to `UNREACHABLE`, so that
no test reaches the real GitHub; a guard holds the tests to that (see
[Repository guards](#repository-guards)). Before the first test, `github_hook_works`
in `tests/conftest.py` checks, without reaching anything, that git as
rc-helper runs it would send GitHub to the hook's address, and only then
that the tests' GitHub sees the request. The browser tests' world runs a
GitHub of its own (see [frontend.md](frontend.md#tests)). `tests/test_container_suite.py` runs the root-only
suite in `tests/container/` inside a throwaway Ubuntu container, through
`docker` or `sudo -n docker`; it skips when neither works, unless
`RCMAN_REQUIRE_CONTAINER=1`, which `make check` sets. Its tests take the
`docker` fixture, and a test that uses it, directly or through another
fixture, carries the `container` marker, which `tests/container_marker.py`
adds as tests are collected. It runs these, each in a
fresh container: user management and Claude Code versions with `rc-admin`
and `rcman-user`, and everything rc-helper does as a person through sudo
under the sudoers rules — folders made and deleted, git run with the
person's own configuration;
the GitHub token, pushing and cloning through sudo, against `https://github.com`
itself, in a container with no network — `--add-host` makes `github.com` the
container's own address, which the suite checks before running git, where
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
| `tests/` | The test suite; `tests/test_<name>.py` tests the module `<name>.py`, wherever it is, `tests/` included, where there is one |
| `tests/fakes/` | The fake `claude` and its recorded fixtures — see [fake-claude.md](fake-claude.md) |
| `helpers/` | `rc-helper` — see [rc-helper.md](rc-helper.md); `rc-admin` and `rcman-user` — see [rc-admin.md](rc-admin.md) |
| `backend/` | The FastAPI application — see [backend.md](backend.md) |
| `frontend/` | The React application, its component tests and its browser tests — see [frontend.md](frontend.md) |
| `tests/container/` | The container image and the suite that runs as root inside it |
| `install.sh`, `installer/` | The installer — see [installer.md](installer.md) |
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
- **No test reaches the real GitHub** (`tests/test_helper_env.py`). It finds
  any place in `tests/` that builds an rc-helper environment other than
  `helper_env`, any rc-helper `Config` made in a test without saying where
  GitHub is, and any git run in a test with GitHub's address written into
  its arguments outside the modules that keep it local and the exemptions it
  lists.
- **No fixed shared paths or ports** (`tests/test_fixed_resources.py`). Tests
  run at the same time, so none may hold a fixed path under `/tmp` or a fixed
  unprivileged port: it finds, outside `tests/container/`, any such path other
  than a `tempfile` folder given a unique name there, and any fixed port given
  to a call, as `port=`, or after `--port`, outside the exemptions it lists.
- **The container marker is derived** (`tests/test_container_marker.py`). In
  the collected suite the tests carrying `container` are exactly those using
  the `docker` fixture, and no source under `tests/` places the marker by
  hand. `tests/collected.py`, which is not a test module, collects the suite
  in a child process for it.
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

Pairs are found, and tests run, in the checkout the edited file is in, which
git names from the file's own folder: the wiring starts the hook from the main
checkout, and a worktree under `.claude/worktrees/` lies inside it. A Python
test runs with that checkout's packages ahead of any the hook's Python has
installed.

A Python module outside `tests/` with no paired test is reported; a
package's `__init__.py`, and a module in `tests/` that is not a test module,
run nothing. The hook is silent when the
test passes; on failure it reports to the operator and to the agent. Its own
tests, including one that checks the wiring in `.claude/settings.json`, are in
`tests/test_on_edit.py`.
