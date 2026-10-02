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
| `routers/` | One module per area: `me`, `folders`, `servers`, `claude`, `users`, `audit_log`, `attention`, `docs` |

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
  frontend shows what comes back. Rules shared with the helpers — the session
  name prefix, Linux user names, email addresses, path lengths, folder depth —
  are taken from the helpers' modules, never restated.
- **Errors** are always problem details (`application/problem+json`) with
  `type`, `title`, `status` and `code`; validation errors add `errors`, a list
  of `field` and `message`. The backend's own codes are `MESSAGES` and `STATUS`
  in `problems.py`; errors from the helpers keep the helper's code and message
  and get their status from `HELPER_STATUS` and `ADMIN_STATUS` in
  `helpers_client.py`. A test keeps those tables complete.

## Calling the helpers

`create_app` takes the two ways of reaching the helpers as arguments. In
production they are `sudo_helper` — `sudo -n -u <linux-user> rc-helper ...` —
and `sudo_admin` — `sudo -n rc-admin ...`. Positional paths are passed after
`--`, and option values as `--option=value`, so nothing a person sends can be
read as an option.

A call that outlasts its timeout — `HELPER_TIMEOUT` and `ADMIN_TIMEOUT` in
`helpers_client.py` unless the caller passes one to `call_helper` or
`call_admin` — is the problem `helper_timeout`. Both run through
`run_bounded`, which ends a call that times out with SIGTERM, which sudo
passes on to the helper, and SIGKILL only after `TERMINATE_GRACE_SECONDS`:
sudo cannot pass SIGKILL on, so killing it outright would leave the helper
running. The container suite shows both through real sudo.

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

## Attention

`routers/attention.py` (`GET /api/attention`, for administrators) reports what
administrators are warned about: servers on an unrecognised screen, by person
and folder, from `everyone.everyone`; and, from the same report as
`/api/claude/versions`, who differs from the approved version and whether it is
untested. The two run side by side, and when the version report fails the
unrecognised screens are still reported, with the failure as `versions_error`.
It is built on every request and never stored, and it carries no screen text. The frontend asks for it when the app loads and on every change
of page. A server action that ends on an unrecognised screen sets
`Record.result` to `unrecognised_screen`, which the audit row records in place
of `ok`.

## Audit

A route that changes something depends on `Recorder("<action>")`, which writes
one row when the request finishes, with `ok` or the error code, and logs the
same event as a JSON line on the `rcman.audit` logger. uvicorn configures only
its own loggers, so `create_app` calls `audit.install_logging()`, which gives
that logger one handler writing the bare line to standard error; systemd sends
it to the journal. `tests/test_audit.py` starts the app with the unit's own
`ExecStart` and reads the line from its output. It also finds every changing route in the route table and fails on any that neither
records nor is listed in `audit.EXEMPT` with a reason that also appears in
[the audit guide](../docs/audit.md); it then drives every audited route and
checks its row. The table is append-only: a trigger in `db.py`'s migrations
aborts any `UPDATE`, so rows are only added, and deleted by retention, which
runs when the app starts and once a day.

## Tests

The app fixtures in `tests/conftest.py` build the app on temporary settings.
`rc-helper` runs for real, against the fake `claude`, with a separate
temporary home and tmux socket for each mapped Linux user, so isolation
between people is tested for real. `rc-admin` needs root, so the backend's
tests replace it with a recording stand-in; the container suite tests the real
one.
