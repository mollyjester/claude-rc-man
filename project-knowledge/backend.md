# Backend

A FastAPI application in `backend/rcman/`, built by `create_app` in
`backend/rcman/main.py` and run with `uvicorn --factory rcman.main:create_app`.
The project is a Python package (`pyproject.toml`) holding `rcman` and
`helpers`, so `uv sync` makes both importable from the environment. Its settings come
from `RCMAN_*` environment variables — see `backend/rcman/settings.py` and
[the configuration guide](../docs/configuration.md). The HTTP API reference is
the OpenAPI description it serves at `/api/openapi`.

## Layout

| Module | What it is |
|---|---|
| `main.py` | `create_app(settings, helper, admin)`: the app, its lifespan (migrations, audit retention) and its routers |
| `settings.py` | The settings model |
| `db.py` | SQLite connections and the migration list |
| `identity.py` | Who is asking, and whether they may |
| `problems.py` | RFC 9457 problem details for every error |
| `audit.py` | The audit trail: recording, coverage, retention |
| `helpers_client.py` | Calling `rc-helper` and `rc-admin`, and mapping their errors to HTTP statuses |
| `everyone.py` | Everyone's servers, for administrators: one `rc-helper list` per person, side by side |
| `routes.py` | The app's routes as FastAPI resolves them after including routers, and which of them are for administrators |
| `routers/` | One module per area: `me`, `folders`, `git`, `github`, `servers`, `claude`, `users`, `audit_log`, `attention`, `docs` |
| `routers/git.py` | The git repositories in the person's home, under `/api/git`: status, branches, stage, unstage, commit, switch, a new branch, fetch and push, each one rc-helper git verb, and Commit & Push (see [Git](#git)) |
| `routers/github.py` | The person's GitHub token, under `/api/github`: whether one is stored, storing one, and removing it (see [GitHub token](#github-token)) |
| `routers/me.py` | Each person's own settings, changed with `PATCH /api/me/settings`, which changes only the settings sent; `prefix_of` and `author_of` are how the rest of the backend reads them |

## Requests

- **Identity.** oauth2-proxy, behind Caddy's `forward_auth`, signs people in
  with Google and passes the address as `X-Forwarded-Email`. rcman trusts the
  header because, installed, it listens only on a unix socket in a directory
  that only Caddy's group may enter — see
  [the installer](installer.md#the-socket). The address must be
  in the users file, which is read on every request, so a removal takes effect
  at once. `/api/health` is the only route that needs no identity.
- **Origin.** A `POST`, `PUT`, `PATCH` or `DELETE` must carry an `Origin`
  header equal to `RCMAN_PUBLIC_ORIGIN`.
- **Administrators.** Routes for administrators depend on `admin_user`.
  `routes.admin_only` finds them in the route table, and
  `tests/test_identity.py` drives each one as someone who is not an
  administrator, with a valid request from `CALLS` in `tests/conftest.py`.
- **Validation** happens here, in Pydantic models and query parameters; the
  frontend shows what comes back. Every rule the helpers also apply is a
  `Rule` (`helpers/rules.py`) taken from the helpers' modules, never restated,
  and a field takes it as `problems.ruled(rule, ...)`: the field publishes and
  validates the rule's anchored pattern and any length it is given, and a
  value either refuses is refused with the rule's sentence, which is also what
  the helper itself says. A path, query or embedded body parameter takes it as
  `Annotated[<ruled>, Path()]`, `Query()` or `Body()`: given as the parameter's
  default instead, `Path()`, `Query()` or `Body()` leaves the rule unchecked.
  `dropped_constraints` in `tests/test_problems.py` walks every route's
  endpoint and dependencies in the route table and compares each query, path,
  header, cookie and body parameter FastAPI resolved with the annotation it was
  declared with, read as FastAPI reads it (`inspect.signature`, so a class's
  `__init__` and a `functools.partial` too): a declared type that carries
  constraints whose resolved field holds none fails the test.
- **Errors** are always problem details (`application/problem+json`) with
  `type`, `title`, `status` and `code`; validation errors add `errors`, a list
  of `field` and `message`. A field's message is never the validation
  library's: it is `FIELD_MESSAGES` in `problems.py` for the error's type,
  with any number in it taken from the error's context, so it is the field's
  own constraint. A body that is not JSON is about the field `request`; one that is not
  UTF-8 text at all is the problem `body_unreadable`. An
  error type with no message gets `field_invalid`'s, and a warning is logged
  on `rcman.problems`. `tests/test_problems.py` finds every field of every
  route in the route table, paired with the schema the API publishes, and
  holds them to this: every published pattern is a helpers' rule refused with
  its sentence; every violation each field's schema implies, and every body
  that is not a JSON object, is refused at its field in a rule's sentence or
  `FIELD_MESSAGES`' text and never in the library's words; and every
  `FIELD_MESSAGES` entry is used. The backend's own codes are `MESSAGES` and
  `STATUS` in `problems.py`; errors from the helpers keep the helper's code and
  message and get their status from `HELPER_STATUS` and `ADMIN_STATUS` in
  `helpers_client.py`. A test keeps those tables complete.

## Calling the helpers

`create_app` takes the two ways of reaching the helpers as arguments. In
production they are `sudo_helper` — `sudo -n -u <linux-user> rc-helper ...` —
and `sudo_admin` — `sudo -n rc-admin ...`. Positional paths are passed after
`--`, and option values as `--option=value`, so nothing a person sends can be
read as an option.

A call that outlasts its timeout — `HELPER_TIMEOUT` and `ADMIN_TIMEOUT` in
`helpers_client.py` unless the caller passes one to `call_helper` or
`call_admin` — is the problem `helper_timeout`. Deleting a folder
(`DELETE /api/folders`) waits for its servers to stop and removes all of it,
so it has its own timeout, `FOLDER_DELETE_TIMEOUT_SECONDS` in
`helpers_client.py`. `call_helper` and `call_admin` both run through
`run_bounded`, which ends a call that times out with SIGTERM, which sudo
passes on to the helper, and SIGKILL only after `TERMINATE_GRACE_SECONDS`:
sudo cannot pass SIGKILL on, so killing it outright would leave the helper
running. The container suite shows, through real sudo, that killing sudo
leaves the helper running and that `run_bounded` ends it. A helper that
ignores SIGTERM is not waited for: once sudo is killed, `run_bounded` closes
the pipes without reading them and raises the timeout. That helper is left
behind as an orphan until it ends or `rcman.service` stops.

A git call's timeout is the helper's own deadline for it —
`rc_helper.GIT_TIMEOUT`, or `rc_helper.GIT_NETWORK_TIMEOUT` for a fetch and
for every `git-push`, `--check` too, whose deadline in the helper it is — and
`GIT_ANSWER_MARGIN_SECONDS` in `helpers_client.py` besides, so the helper's
`git_timeout` reaches the person before rcman stops waiting.

`everyone.py` lists every person's servers for administrators
(`GET /api/servers/everyone`). It runs `rc-helper list` as each person in the
users file in a thread pool of at most `EVERYONE_WORKERS`, and waits
`EVERYONE_TIMEOUT_SECONDS`, which is also each call's own timeout. A person
whose call fails is listed with the error's code and title instead of their
servers, and one still running when the wait ends with `helper_timeout`, so
one person never hides or stalls the others; a call still queued when the
wait ends is cancelled and is `helper_timeout` too. Each server is passed on
with only `SERVER_FIELDS` — never its screen or its claude.ai address. The
listing is read-only: no route lets one person act on another's servers.

## Claude account and versions

`routers/claude.py` reports the signed-in user's connection — `not_connected`,
`connected`, `expiring` (within `RCMAN_LOGIN_WARNING_DAYS` of the sign-in's
expiry) or `expired` — and drives the connect flow through rc-helper's
`auth-login` and `auth-code`; the code goes to rc-helper on stdin and is
neither audited nor logged. For administrators it reports versions through
rc-admin's `claude-status`, flagging who differs from the approved version and
whether that version is the tested one, and rolls one out with
`claude-upgrade`.

## Git

`routers/git.py` runs one rc-helper git verb per request, as the signed-in
person, on a folder in their own home. The files to stage or unstage go to
the helper on stdin as a JSON list, and a commit's message on stdin as it
is; neither is ever in an argument. The commit message is validated by
`rc_helper.COMMIT_MESSAGE` and `MAX_COMMIT_MESSAGE_LENGTH`, and is never
recorded or logged: the audit target of every git action is the folder.
`GET /api/git/status` adds `author`, from `author_of`: who commits are
recorded as, or `null` while no name is set; `POST /api/git/commit` is
then refused with `author_not_set`, without asking the helper, and
otherwise passes the name and address as `--author-name` and
`--author-email`. git's refusals, a network failure among them, come back
as `git_failed` with git's own words as `git`.

`POST /api/git/push` runs `git-push`. `POST /api/git/commit-push`, audited
once as `git.commit_push`, takes the commit's message and, in this order,
refuses with `author_not_set` without asking the helper; runs
`git-push --check`, so that a push refused before it is tried —
`detached_head`, or `github_token_not_set` — commits nothing; runs
`git-commit`, whose refusal is the request's; and runs `git-push`. It
answers 200 with the `path`, the `commit`, and `push`, `git-push`'s answer,
or `null` and `push_problem`, the push's problem details as a failed
request would have them (`problems.body`), when the commit was made and the
push failed; the audit row's result is then `committed_not_pushed`.

## GitHub token

`routers/github.py` stores, reports and removes the person's GitHub token
through rc-helper's `github-token`, `github-status` and `github-forget`
(see [rc-helper.md](rc-helper.md#github-token)). `POST /api/github/token`
validates the token by `rc_helper.GITHUB_TOKEN` and passes it to the helper
on stdin only; it is never recorded or logged, so neither `github.token` nor
`github.forget` has an audit target. `GET /api/github` answers whether a
token is stored, never the token.

## Attention

`routers/attention.py` (`GET /api/attention`, for administrators) reports what
administrators are warned about: servers on an unrecognised screen, by person
and folder, from `everyone.everyone`; and, from the same report as
`/api/claude/versions`, who differs from the approved version and whether it is
untested. The two run side by side, and when the version report fails the
unrecognised screens are still reported, with the failure as `versions_error`.
The version report has its own timeout, `ATTENTION_VERSIONS_TIMEOUT_SECONDS` in
`everyone.py`, past which it is `helper_timeout`; when the listing fails, the
request fails at once without waiting for the version report.
It is built on every request and never stored, and it carries no screen text.
The frontend asks for it when the app loads and on every change of page. A server action that ends on an unrecognised screen sets
`Record.result` to `unrecognised_screen`, which the audit row records in place
of `ok`.

## Audit

A route that changes something depends on `Recorder("<action>")`, which writes
one row when the request finishes, with `ok` or the error code, and logs the
same event as a JSON line on the `rcman.audit` logger. uvicorn configures only
its own loggers, so `create_app` calls `audit.install_logging()`, which gives
that logger one handler writing the bare line to standard error; systemd sends
it to the journal. `tests/test_audit.py` starts the app with the unit's own
`ExecStart` and reads the line from its output. It also finds every changing
route in the route table and fails on any that neither records nor is listed
in `audit.EXEMPT` with a reason that also appears in
[the audit guide](../docs/audit.md); it then drives every audited route and
checks its row. The results a row can have besides an error code are
`audit.RESULTS`, and a test holds the guide's results table to them. The table is append-only: a trigger in `db.py`'s migrations
aborts any `UPDATE`, so rows are only added, and deleted by retention, which
runs when the app starts and once a day.

## Tests

The app fixtures in `tests/conftest.py` build the app on temporary settings.
`rc-helper` runs for real, against the fake `claude`, with a separate
temporary home and tmux socket for each mapped Linux user, so isolation
between people is tested for real. `rc-admin` needs root, so the backend's
tests replace it with a recording stand-in; the container suite tests the real
one.
