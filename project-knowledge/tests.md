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
| `make check` | Lint, the Python tests, the frontend tests and the browser tests — what CI runs on every push to `main` and every pull request |

Run pytest from the repository root; `pyproject.toml` holds its configuration.
The suite needs `tmux`. `tests/test_container_suite.py` runs the root-only
suite in `tests/container/` inside a throwaway Ubuntu container, through
`docker` or `sudo -n docker`; it skips when neither works, unless
`RCMAN_REQUIRE_CONTAINER=1`, which CI sets. Tests marked `real_claude` are deselected by default
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
