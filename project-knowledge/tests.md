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
| `make check` | Lint, then tests — what CI runs on every push to `main` and every pull request |

Run pytest from the repository root; `pyproject.toml` holds its configuration.

## What lives where

| Path | Contents |
|---|---|
| `tests/` | The test suite; `tests/test_<name>.py` tests the module `<name>.py` |
| `devtools/` | Development tooling: the documentation guards and the edit-time hook |

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
- a Markdown file runs `tests/test_docs_guard.py`.

A Python module with no paired test is reported. The hook is silent when the
test passes; on failure it reports to the operator and to the agent. Its own
tests, including one that checks the wiring in `.claude/settings.json`, are in
`tests/test_on_edit.py`.
