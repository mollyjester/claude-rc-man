# Backend

A FastAPI application in `backend/rcman/`, built by `create_app` in
`backend/rcman/main.py` and run with `uvicorn --factory rcman.main:create_app`.
The project is a Python package (`pyproject.toml`) holding `rcman` and
`helpers` — what the installer and the fake `claude` share with it, Python's
standard library only — so `uv sync` makes both importable from the
environment, and installs `rcman-admin` (see [people.md](people.md)). Its settings come
from `RCMAN_*` environment variables — see `backend/rcman/settings.py` and
[the configuration guide](../docs/configuration.md). The HTTP API reference is
the OpenAPI description it serves at `/api/openapi.json`, browsable at
`/api/openapi`.

## Layout

| Module | What it is |
|---|---|
| `main.py` | `create_app(settings, runtime)`: the app, its lifespan (migrations, the allowed list, audit retention, restoring workspaces) and its routers |
| `settings.py` | The settings model |
| `db.py` | SQLite connections and the schema, one migration; `migrate` refuses a database newer than the code |
| `identity.py` | Who is asking, and whether they may |
| `people.py`, `admin.py` | The people who may use rcman, the allowed list, and `rcman-admin` — see [people.md](people.md) |
| `workspaces.py` | Workspaces made, deleted and brought back, and their servers' verbs — see [runtime.md](runtime.md) |
| `runtime.py`, `channel.py`, `claude_server.py` | Where workspaces run, the shell commands go through, and a workspace's server — see [runtime.md](runtime.md) |
| `problems.py` | RFC 9457 problem details for every error |
| `audit.py` | The audit trail: recording, coverage, retention |
| `journal.py` | `install(log)`: sends a logger's events to standard error, one bare line each, which systemd sends to the journal |
| `unrecognised.py` | The watch on every server rcman reads, which writes output rcman does not recognise to the journal (see [Output rcman does not recognise](#output-rcman-does-not-recognise)) |
| `everyone.py` | Everyone's workspaces and servers, for administrators |
| `spa.py` | Serving the built frontend: its files, and `index.html` for every other path not under `/api`; installed after every router |
| `routes.py` | The app's routes as FastAPI resolves them after including routers, and which of them are for administrators |
| `routers/` | One module per area: `me`, `workspaces`, `servers`, `git`, `github`, `claude`, `people`, `audit_log`, `attention`, `docs`, `client_errors` |
| `routers/workspaces.py` | The person's workspaces, under `/api/workspaces`: listing them with their servers, making one, deleting one |
| `routers/servers.py` | A workspace's server, under `/api/servers`: start, resume, stop, forget, confirm and output, each by the workspace's id; and everyone's, for administrators |
| `accounts.py`, `routers/claude.py` | Each person's Claude sign-in, signing in and out of it, and its state; `routers/me.py` signs out of rcman — see [Claude accounts](#claude-accounts) |
| `routers/git.py`, `routers/github.py`, `routers/claude.py` | git, the GitHub token and Claude Code versions: not available in workspaces yet (see [Not available yet](#not-available-yet)) |
| `routers/client_errors.py` | A page's reports of failures rcman could not see, `POST /api/client-errors`, written to the journal (see [Errors from the page](#errors-from-the-page)) |
| `routers/me.py` | Each person's own settings, changed with `PATCH /api/me/settings`, which changes only the settings sent; `prefix_of` and `author_of` are how the rest of the backend reads them |

## Requests

- **Identity.** oauth2-proxy, behind Caddy's `forward_auth`, signs people in
  with Google, and Caddy passes the address on as `X-Forwarded-Email`,
  removing any the browser sent. rcman trusts the
  header because, installed, it listens only on a unix socket in a directory
  that only Caddy's group may enter — see
  [the installer](installer.md#the-socket). The address must be an active
  person's in the people table, which is read on every request, so a removal
  takes effect at once (see [people.md](people.md)). The routes that need no identity are `problems.PUBLIC_PATHS`:
  `/api/health` and the frontend's files.
- **Origin.** A `POST`, `PUT`, `PATCH` or `DELETE` must carry an `Origin`
  header equal to `RCMAN_PUBLIC_ORIGIN`.
- **Administrators.** Routes for administrators depend on `admin_user`.
  `routes.admin_only` finds them in the route table, and
  `tests/test_identity.py` drives each one as someone who is not an
  administrator, with a valid request from `CALLS` in `tests/conftest.py`.
- **Validation** happens here, in Pydantic models and query parameters; the
  frontend shows what comes back. Every rule a value is refused by is a
  `Rule` in `helpers/rules.py`, which the installer reads too, never restated,
  and a field takes it as `problems.ruled(rule, ...)`: the field publishes and
  validates the rule's anchored pattern and any length it is given, and a
  value either refuses is refused with the rule's sentence. A parameter takes it inside an `Annotated` with its
  FastAPI parameter marker, such as `Annotated[<ruled>, Path()]`: any marker
  given as the parameter's default instead leaves the type's constraints
  unchecked. Constraints given to the marker itself, such as
  `Query(ge=1)`, are kept either way. `dropped_constraints` in
  `tests/test_problems.py` walks every route's endpoint and dependencies in
  the route table and compares each parameter FastAPI resolved with the
  annotation it was declared with, read as FastAPI reads it
  (`inspect.signature`, so a class's `__init__` and a `functools.partial`
  too). Every constraint the declared type carries — annotated_types'
  lengths, bounds and patterns, and pydantic's validators, not markers or
  plain notes — must be on the resolved field, or the test fails.
- **Errors** are always problem details (`application/problem+json`) with
  `type`, `title`, `status` and `code`; validation errors add `errors`, a list
  of `field` and `message`. A field's message is never the validation
  library's: it is `FIELD_MESSAGES` in `problems.py` for the error's type,
  with any number in it taken from the error's context, so it is the field's
  own constraint. A body that is not JSON is about the field `request`; one
  that is not UTF-8 text at all is the problem `body_unreadable`. An error
  type with no message gets `field_invalid`'s, and a warning is logged on
  `rcman.problems`. `tests/test_problems.py` finds every field of every
  route in the route table, paired with the schema the API publishes, and
  holds them to this: every published pattern is a rule refused with its
  sentence — of any rule with that pattern, where two share one, as the
  prefix and a workspace's name do; every violation each field's schema implies, and every body
  that is not a JSON object, is refused at its field in a rule's sentence or
  `FIELD_MESSAGES`' text and never in the library's words; and every
  `FIELD_MESSAGES` entry is used. Every code rcman answers with is in
  `MESSAGES` and `STATUS` in `problems.py`, a workspace's and its server's
  among them, and a test keeps the two tables to each other. A failure of the
  runtime or a channel is `runtime_unavailable`, `workspace_failed` or
  `timeout` (`workspaces.answered`).

## Workspaces and servers

`create_app` takes the runtime workspaces run in: by default Podman, through
rcman's own user manager's socket (`main.podman_runtime`); the tests and
`make e2e` give it the host runtime. Everything about workspaces, their
channels and servers, and what rcman brings back when it starts, is in
[runtime.md](runtime.md).

A workspace belongs to one person, and a route reaches only the signed-in
person's: another's is `workspace_not_found`, as if it were not there. A
server's verbs need the workspace `ready`, or answer `workspace_not_ready`. The
prefix of a server's sessions is the person's, or the workspace's name while
they have set none (`prefix_of`). A start, a resume, a stop and a forget record
what should run in the workspace's row, which restoring reads. The listing
reads every workspace's server side by side, in at most `LISTING_WORKERS`
threads; a workspace whose server cannot be read is listed with why, as
`server_error`.

`everyone.py` lists every person's workspaces for administrators
(`GET /api/servers/everyone`), each server with only `SERVER_FIELDS` — never its
screen or its claude.ai address. The listing is read-only: no route lets one
person act on another's workspaces.

## Claude accounts

`accounts.py` signs each person in and out of their one Claude sign-in, the
credentials file every workspace of theirs shares (see
[runtime.md](runtime.md#a-persons-claude-sign-in)).

- **Signing in.** `POST /api/claude/login` starts a sign-in container, runs
  `claude auth login` in tmux there and answers its link; one begun before
  ends. `POST /api/claude/code` puts the code into a tmux buffer through
  `exec`'s stdin, never an argv, and pastes it; on success the
  `oauthAccount` Claude wrote is kept in the person's `claude_account`, and the
  container is removed whatever happened. It ends itself after
  `CODE_WAIT_SECONDS` should rcman not. A sign-in to another account stops
  nothing.
- **Its state.** `GET /api/claude` reads the file with `read_account`, only when
  the Claude page opens: connected while it holds `claudeAiOauth`, with the
  refresh token's expiry (`refreshTokenExpiresAt`), or none without one, against
  `login_warning_days`; and `signing_out`.
- **A server's start** (`Workspaces.start_server`) refuses `claude_not_logged_in`
  while the person has no `claude_account`, and `claude_signing_out` while a
  sign-out waits; otherwise it seeds `oauthAccount` into the workspace's
  `.claude.json`, keeping every other key, owner-only, before the server starts.
- **Signing out of Claude** (`POST /api/claude/logout`, `claude.disconnect`):
  with `keep_sessions`, the default, the person is marked signing out and
  `settle` finishes once none of their servers runs and no sign-in of theirs
  waits for its code — after a stop, a forget or a deletion, never failing
  them; every `IDLE_CHECK_SECONDS`, which catches a server that ended by
  itself and leaves a person whose lock is held for its next round; when the
  Claude page reads the state; and when rcman starts. Without it, their running
  servers are stopped first and it finishes at once; the page offers that while
  one waits, so a sign-out is never stuck. Finishing runs `claude auth logout`
  in a sign-in container, its answer only advisory, empties the file in place
  with `clear_account`, and forgets the account.
- **Order.** A start, from its account check until its server runs, and every
  sign-in and sign-out of the same person take turns under
  `Workspaces.person_lock`, taken before any workspace's lock, so no start
  slips past a sign-out and a start seeds the account a sign-in has finished.
  A code is taken only until two of `LOGIN_TIMEOUT` before the sign-in
  container ends itself.
- **Signing out of rcman** (`POST /api/me/sign-out`, `me.sign_out`) stops the
  person's running servers unless `keep_sessions`, the default, and answers
  oauth2-proxy's sign-out address, which the page goes to.

## Not available yet

Claude Code versions, git and the GitHub token are not available in workspaces
yet. Their routes — `routers/claude.py`,
`routers/git.py` and `routers/github.py` — keep the shape they will have, their
bodies validated by the rules they will be, and answer `not_available_yet`; the
changing ones are audited with that result. What a person writes for them — a
commit's message, a token, a sign-in code — is never recorded or logged.

## Attention

`routers/attention.py` (`GET /api/attention`, for administrators) reports the
servers on a screen rcman does not recognise, by person and workspace, from
`everyone.everyone`. It is built on every request and never stored, and it
carries no screen text. A server action that ends on an unrecognised screen
sets `Record.result` to `unrecognised_screen`, which the audit row records in
place of `ok`.

## Audit

A route that changes something depends on `Recorder("<action>")`, which writes
one row when the request finishes, with `ok` or the error code, and logs the
same event as a JSON line on the `rcman.audit` logger. uvicorn configures only
its own loggers, so `create_app` calls `journal.install` on each logger whose
events go to the journal — `rcman.audit`, `rcman.client`, `rcman.unrecognised`
and `rcman.workspaces` — which gives it one handler writing the bare line to
standard error; systemd sends it to the journal. `tests/test_journal.py` checks
that the app installs each of them. `tests/test_audit.py` starts the app with the unit's own
`ExecStart` and reads the line from its output. It also finds every changing
route in the route table and fails on any that neither records nor is listed
in `audit.EXEMPT` with a reason that also appears in
[the audit guide](../docs/audit.md); it then drives every audited route and
checks its row, and holds the guide's table of actions to the actions the
routes record. The results a row can have besides an error code are
`audit.RESULTS`, and a test holds the guide's results table to them. The table is append-only: a trigger in `db.py`'s migrations
aborts any `UPDATE`, so rows are only added, and deleted only by retention
(see [the audit guide](../docs/audit.md#how-long-events-are-kept)), which
`create_app`'s lifespan runs as the app starts and every
`RETENTION_INTERVAL_SECONDS` after.

## Errors from the page

`routers/client_errors.py` takes the page's reports of failures rcman could not
see (see [the frontend](frontend.md#reporting-failures)) at
`POST /api/client-errors` and writes each as one JSON line on the
`rcman.client` logger — `"event": "client_error"`, `at`, the sender's `email`,
then the report's fields — and nothing else: it keeps nothing and
is in `audit.EXEMPT`. [The journal guide](../docs/journal.md#errors-reported-by-the-page)
lists every field, and `tests/test_client_errors.py` holds its table to
`ClientErrorBody` and the fields the route adds.

Each text field is `Clipped`: text of any length is cut to its bound —
`SHORT_CHARS`, `PATH_CHARS` or `MESSAGE_CHARS` in the module — rather than
refused, since a page that is failing cannot do anything about a refusal, and
the field's published description says what it is cut to. Text holding half
of a character, a lone surrogate, is refused as every other field refuses it
(`string_unicode`). Numbers are bounded by `JS_MAX_SAFE`, the largest whole
number the page holds exactly. A field the model does not name is ignored and
not logged. A message is written by `json.dumps`, so no text in a report can
add a line or a field.

Anyone signed in can write to the journal this way, so what one request can
write is bounded: every field is bounded, the request needs a signed-in person
in the people table and the public `Origin`, like every changing request, and
each line names who sent it. There is no throttle per person; journald's own
rate limit and size limits bound the rest.

## Output rcman does not recognise

`unrecognised.py` writes one JSON line on the `rcman.unrecognised` logger —
`"event": "unrecognised"`, `at`, `email`, `code`, `verb`, `workspace`,
`lines` and `fingerprint` — when something enters a state rcman does not
recognise. The app's `Watch` is shown every server rcman reads: a listing of a
person's workspaces (`listed`), which is all of their servers and so replaces
what is remembered for them; a server an action or its output answers with
(`seen`); and a server verb that failed (`failed`) or worked (`answered`). A
server seen in state `unrecognised` that was not remembered as such is
written, as `server_unrecognised`; a verb failing with a code in
`UNRECOGNISED_OUTPUT` is written once per code, and forgotten when the same
call works. `failing` is bounded by `MAX_REMEMBERED`: past it, every
remembered call is forgotten, so a long-lived failure may be written again.
What is remembered is in the process, so the watch assumes rcman runs as one
process, and starts empty after a restart.

A line never holds the screen or anything a person sent: `lines` counts the
output and `fingerprint` is the first `FINGERPRINT_CHARS` hexadecimal
characters of the SHA-256 of its lines, each without its trailing spaces.
Nothing the watch is shown makes it raise: a fault in it is logged with its
traceback and the request goes on. [The journal
guide](../docs/journal.md#output-rcman-does-not-recognise) lists every field,
and `tests/test_unrecognised.py` holds its table to a line the watch writes.

## Tests

The app fixtures in `tests/conftest.py` build the app on temporary settings,
with alice, an administrator, bob and carol in its people, and the host
runtime (`host_runtime`) with the fake `claude`, so workspaces, their channels
and servers run for real against real tmux; `made_workspace` makes one through
the routes and waits until it is ready, and `signed_in` signs its fake
`claude` in. `tests/test_claude_server.py` drives a server's verbs on the host
runtime through a real channel; the container suite runs the same Podman
runtime against real Podman (see [tests.md](tests.md#running)).
