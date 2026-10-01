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
| `routes.py` | The app's routes as FastAPI resolves them after including routers |
| `routers/` | One module per area: `me`, `folders`, `servers`, `claude`, `users`, `audit_log`, `docs` |

## Requests

- **Identity.** oauth2-proxy, behind Caddy's `forward_auth`, signs people in
  with Google and passes the address as `X-Forwarded-Email`. rcman trusts the
  header because it listens only where Caddy can reach it. The address must be
  in the users file, which is read on every request, so a removal takes effect
  at once. `/api/health` is the only route that needs no identity.
- **Origin.** A `POST`, `PUT`, `PATCH` or `DELETE` must carry an `Origin`
  header equal to `RCMAN_PUBLIC_ORIGIN`.
- **Administrators.** Routers for administrators depend on `admin_user`.
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

## Claude account and versions

`routers/claude.py` reports the signed-in user's connection — `not_connected`,
`connected`, `expiring` (within `RCMAN_LOGIN_WARNING_DAYS` of the sign-in's
expiry) or `expired` — and drives the connect flow through rc-helper's
`auth-login` and `auth-code`; the code goes to rc-helper on stdin and is
neither audited nor logged. For administrators it reports versions through
rc-admin's `claude-status`, flagging who differs from the approved version and
whether that version is the tested one, and rolls one out with
`claude-upgrade`.

## Audit

A route that changes something depends on `Recorder("<action>")`, which writes
one row when the request finishes, with `ok` or the error code, and logs the
same event as a JSON line on the `rcman.audit` logger. `tests/test_audit.py`
finds every changing route in the route table and fails on any that neither
records nor is listed in `audit.EXEMPT` with a reason that also appears in
[the audit guide](../docs/audit.md); it then drives every audited route and
checks its row. Retention runs when the app starts and once a day.

## Tests

The app fixtures in `tests/conftest.py` build the app on temporary settings.
`rc-helper` runs for real, against the fake `claude`, with a separate
temporary home and tmux socket for each mapped Linux user, so isolation
between people is tested for real. `rc-admin` needs root, so the backend's
tests replace it with a recording stand-in; the container suite tests the real
one.
