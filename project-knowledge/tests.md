# Tests

How the test suite is organised and run. The rules the suite follows are in
[working-rules.md](working-rules.md).

## Running

| Command | What it does |
|---|---|
| `make setup` | Creates the development environment: `make frontend-setup`, then `uv sync` |
| `make test` | Runs every Python test that runs on the host, in parallel with pytest-xdist: all but those marked `container`, and those marked `real_claude`, which no target runs |
| `make test-container` | Runs the Python tests marked `container`, which need a container, in one process; the `Makefile` says why. It sets `RCMAN_REQUIRE_CONTAINER=1`, so they fail instead of skipping where Docker is not usable |
| `make lint` | Checks formatting and lint: ruff for the Python code, Biome for the frontend, with the frontend's packages `make frontend-setup` installs |
| `make format` | Applies formatting and safe lint fixes: ruff for the Python code, Biome for the frontend |
| `make frontend-setup` | Installs the frontend's packages and Playwright's browser |
| `make frontend-test` | Type-checks the frontend and runs its component tests |
| `make frontend-build` | Builds the frontend into `frontend/dist` |
| `make e2e` | Builds the frontend and runs the browser tests |
| `make check` | Lint, `make test`, `make test-container`, the frontend tests and the browser tests. It sets `RCMAN_REQUIRE_REAL_CLAUDE=1`, so tests that would skip for want of the real `claude` fail instead |

When the full suite runs, and how a run is read, is a working rule:
[the full check runs at the edges of a change](working-rules.md#tests-and-guards).

Run pytest from the repository root; `pyproject.toml` holds its configuration.
The Python tests are split by where they run, not by how long they take:
`make test` runs everything that runs on the host, slow tests included, and a
slow host test is made cheaper or split into cases that workers share rather
than moved out; `make test-container` runs what needs a container, the tests
marked `container` (see below). `tests/test_make.py` holds the split: every
test is in exactly one of the two, and `make check` runs both. To work on
what the container suite covers, run it directly, as in
`uv run pytest tests/test_container_suite.py -k github`.
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
`RCMAN_REQUIRE_CONTAINER=1`, which `make test-container` sets. Its tests take the
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
| `tests/fakes/` | The fake `claude`, its recorded fixtures and the screens observed on a live server — see [fake-claude.md](fake-claude.md) |
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
  unprivileged port. It reads the Python files in `tests/` but not those in
  `tests/container/`, and every TypeScript and JavaScript file in `frontend/`
  that git does not ignore, of the kinds `SOURCES` in the test names — the
  browser tests, their configs and the rest — parsed with Vite's parser from
  the frontend's packages, so a file that does not parse, or packages that
  are missing, fail it. It catches the same forms in both languages. `FORMS`
  in the test spells each form in both, and an entry one language does not
  spell fails it; `CLEAN` does the same for what it leaves alone. The forms:
  - a path: `/tmp` or a path in it as a string, a formatted string — an
    f-string or a template literal — beginning with one, and the system's
    temporary folder, `tempfile.gettempdir()` or `tmpdir()`;
  - a fixed port, written as a number or a string of digits, held in a
    module-level name, converted or cast, in a formatted string that shows
    nothing else, or given as the fallback of a value that may be missing —
    after `or`, `??` or `||`, or as the default of `get()` or `getenv()` —
    wherever it appears:
    - in an address: a host and the port in a tuple, a list or an array;
    - beside an IP address or `localhost` as a call's arguments, either
      first;
    - given to a name, attribute, key, property, parameter or keyword
      argument named `port` or ending `_PORT`: assigned, as a parameter's
      default, or to a call that names it first, as `setenv` and `stubEnv`
      do. A key counts only where it is written as a string. A module-level
      name is not a site itself, but is followed to where it is used,
      except into a function with a parameter of the same name;
    - as the first argument of a `listen` method;
    - after `--port`, and after `-p` or `--publish`, alone or as the host
      port of docker's mapping, in a list of arguments, written whole or
      formatted, or in a command written as one string;
    - after a host and its colon — in a URL, after its scheme, and
      otherwise after an IP address or `localhost` at the start of the text
      or after a character `BEFORE_HOST` in the test names — in one string,
      in a formatted string, or joined to it with `+`.

  It leaves alone a uniquely named file or folder made in `/tmp` itself:
  `/tmp` or the system's temporary folder given as the folder that a call
  making a unique name makes it in — the `dir=` of such a `tempfile` call,
  and the folder of a `join()` of a folder and a prefix given to `mkdtemp`
  or `mkdtempSync`. It leaves alone, too, a string standing alone as a
  statement, as a docstring does. It does not follow a port held in a
  function's own variable not named for one, imported or computed, or a
  path built in pieces; review covers those. In TypeScript a path given to
  a call is reported as the call, so `join()` is read with the rest of the
  path. Its planted twins write the path and the port as placeholders, so
  that the guard, reading its own module, finds neither there. Each
  exemption it lists covers one site, named by its file, the function or
  test it is in and its text, and fails when that site is gone.
- **The container marker is derived** (`tests/test_container_marker.py`). In
  the collected suite the tests carrying `container` are exactly those using
  the `docker` fixture, and no source under `tests/` places the marker by
  hand, as `mark.container`, `getattr` of it, or its name given to
  `add_marker` or `applymarker`, or asks for the fixture by name with
  `getfixturevalue`, which leaves it out of a test's fixture names; a name
  held in a variable is not followed. `tests/collected.py`, which is not a test module, collects the suite
  in a child process for it.
- **Every test runs, and container tests cannot skip** (`tests/test_make.py`).
  It runs `make test`, `make test-container` and `make check` with `uv`,
  `npm` and `npx` replaced by recorders, and reads which tests each pytest
  command they ran selects from pytest's own collection: every test is in
  exactly one of `make test` and `make test-container`, `make test-container`
  runs exactly the `container` tests, and `make check` runs every test, its
  container tests with `RCMAN_REQUIRE_CONTAINER=1` and all of them with
  `RCMAN_REQUIRE_REAL_CLAUDE=1`; none of them runs a test a
  bare `pytest` leaves out, as a target's own `-m` replaces the
  `-m 'not real_claude'` of pyproject's `addopts`. `make lint` and
  `make format` run ruff and the frontend's script of the same name, and the
  real `make lint`, run over a frontend file planted in a scratch tree, fails
  when Biome would reformat it, when Biome finds a lint finding it reports as
  an info, and when the frontend's packages are missing; it checks the
  frontend's `index.html`, and fails on a lint finding there. Every rule Biome runs
  at info, as `biome rage` and `biome explain` report, is raised to a warning
  in `frontend/biome.jsonc`.
- **Every make target is documented** (`tests/test_docs_guard.py`). This
  file has a table row whose first cell is `make <target>` for exactly the
  targets the `Makefile` defines. The targets are read from make's own
  database (`make -pRrq`), so a comment, a variable, a target-specific
  variable, a special target such as `.PHONY`, a `define` block's body or a
  branch make does not take is not mistaken for one.
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
