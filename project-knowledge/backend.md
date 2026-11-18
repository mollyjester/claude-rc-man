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
| `main.py` | `create_app(settings, runtime, *, builder_slot)`: the app, its lifespan (migrations, the allowed list, audit retention, restoring workspaces, the monitor's loops, the forwarding server, started where `RCMAN_FORWARD_SOCKET` is set and stopped, its WebSockets closed and the workspaces it used swept, before the workspaces' service closes) and its routers; with rcman's own Podman, `check_seccomp` first, which says in the journal when the workspaces' seccomp profile is missing or stale (see [runtime.md](runtime.md#containers-in-a-workspace)); and `check_instructions`, which says there when the instructions for Claude are missing from the tools (see [runtime.md](runtime.md#workspace-instructions)) |
| `settings.py` | The settings model |
| `db.py` | SQLite connections and the schema, as migrations; `migrate` refuses a database newer than the code |
| `identity.py` | Who is asking, and whether they may |
| `people.py`, `admin.py` | The people who may use rcman, the allowed list, and `rcman-admin` — see [people.md](people.md) |
| `limits.py` | The limits on what a person's workspaces and each workspace may use: reading and showing values, the slices' names, the properties set, and usage — see [Limits](#limits) |
| `workspaces.py` | Workspaces made, deleted and brought back, and their servers' verbs — see [runtime.md](runtime.md) |
| `runtime.py`, `channel.py`, `claude_server.py` | Where workspaces run, the shell commands go through, and a workspace's server — see [runtime.md](runtime.md) |
| `problems.py` | RFC 9457 problem details for every error |
| `audit.py` | The audit trail: recording, coverage, retention |
| `journal.py` | `install(log)`: sends a logger's events to standard error, one bare line each, which systemd sends to the journal |
| `unrecognised.py` | The watch on every server rcman reads, which writes output rcman does not recognise to the journal (see [Output rcman does not recognise](#output-rcman-does-not-recognise)) |
| `everyone.py` | Everyone's workspaces and servers, for administrators |
| `monitor.py`, `routers/monitor.py` | What the host, rcman's own storage, each person and each workspace container use now, collected in two rounds, on their intervals or at once on a refresh, and the monitor's settings, for administrators (see [Monitor](#monitor)) |
| `forwarding_settings.py`, `routers/forwarding_settings.py` | The settings of port forwarding, for administrators, and the hook that hears of forwarding being turned off (see [Port forwarding's settings](#port-forwardings-settings)) |
| `sealing.py` | Sealing with `RCMAN_SECRET_KEY`: AES-256-GCM under a key HKDF derives for each use, the stored client secret's and the cookies', and payloads that name their `kind` and expiry (see [Microsoft sign-in](#microsoft-sign-in)) |
| `microsoft_settings.py`, `routers/microsoft_settings.py` | The settings of Microsoft sign-in, for administrators: the store, the generation, the sealed client secret, and `GET` and `PUT /api/microsoft/settings` (see [Microsoft sign-in](#microsoft-sign-in)) |
| `microsoft.py`, `routers/auth.py` | Microsoft sign-in through MSAL, its refusals, its two cookies and the session; and the public routes of signing in, `GET /oauth2/start` (the sign-in page), `GET /api/auth/microsoft/login` and `GET /api/auth/microsoft/callback` (see [Microsoft sign-in](#microsoft-sign-in)) |
| `headers.py` | The security headers every answer carries (see [Requests](#requests)) |
| `ratelimit.py`, `filelock.py` | A token bucket for each name, refilled by the clock it is given (the limits on a page's reports, see [Errors from the page](#errors-from-the-page), on requests for an upgrade of rcman, see [Upgrading rcman](#upgrading-rcman), on Microsoft sign-in's starts and callbacks, see [Microsoft sign-in](#microsoft-sign-in), and on the forwarding server's new hosts and ways in, see [Port forwarding](#port-forwarding)), and a re-entrant lock that is also a lock file, shared with `rcman-admin` (see [Limits](#limits)) |
| `spa.py` | Serving the built frontend: its files, and `index.html` for every other path not under `/api`; installed after every router. `index.html` names the build's files by the hashes of their contents, which Vite puts in their names under `assets/`, so every file outside `assets/`, `index.html` among them, is served with `Cache-Control: no-cache`, which has the browser ask again before it uses the one it kept, and a page loaded after an upgrade names the new build's files; those under `assets/`, which never change, are kept for a year (`immutable`) |
| `routes.py` | The app's routes as FastAPI resolves them after including routers, and which of them are for administrators |
| `routers/` | One module per area: `me`, `workspaces`, `servers`, `git`, `github`, `claude`, `people`, `audit_log`, `attention`, `monitor`, `forwarding_settings`, `files_settings`, `microsoft_settings`, `auth`, `forward`, `docs`, `client_errors`, `terminal`, `files`, `rcman_upgrade` |
| `routers/workspaces.py` | The person's workspaces, under `/api/workspaces`: listing them with their servers, making one, deleting one, telling whether one's devcontainer.json changed, rebuilding, stopping and starting one (see [Workspaces and servers](#workspaces-and-servers)); the listing is an object, `workspaces`, `claude_approved` (the version a server started now would run, `null` where none is), so that the page can set each server's `claude_version` beside it, `rcman_upgrading`, whether an upgrade of rcman is under way (see [Upgrading rcman](#upgrading-rcman)), and `forwarding`, whether ports are forwarded now; every entry of the person's own, the listing's and those its other routes answer, has its forwarded ports (`ports`, see [Port forwarding](#port-forwarding)) |
| `routers/servers.py` | A workspace's server, under `/api/servers`: start, resume, stop, forget, confirm and output, each by the workspace's id; and everyone's, for administrators |
| `accounts.py`, `routers/claude.py` | Each person's Claude sign-in, signing in and out of it, and its state; `routers/me.py` signs out of rcman — see [Claude accounts](#claude-accounts) |
| `repository.py` | A workspace's git repository: the verbs of the Changes section, run in the workspace through its channel and `exec`, each wrapped in the workspace's `timeout` tool (see [git in workspaces](#git-in-workspaces)) |
| `gitaccount.py` | A person's commit author and GitHub token, kept in SQLite and written into their volume's git folder (see [git in workspaces](#git-in-workspaces)) |
| `routers/git.py`, `routers/github.py` | The git routes, by workspace id, and the person's GitHub token (see [git in workspaces](#git-in-workspaces)) |
| `claude_versions.py` | `ClaudeVersions`: the approvals, the versions marked as failed, the store, rolling out a version, deleting one by hand, pruning, and the report of the versions page (see [Claude Code versions](#claude-code-versions)) |
| `routers/client_errors.py` | A page's reports of failures rcman could not see, `POST /api/client-errors`, written to the journal (see [Errors from the page](#errors-from-the-page)) |
| `forwards.py` | What relays to a workspace's forwarded ports leave behind, ended by the sweep (see [runtime.md](runtime.md#forwarded-ports)) |
| `forward.py`, `routers/forward.py` | The forwarding server on `RCMAN_FORWARD_SOCKET`, its sessions, the ways in the hand-off hands out, the hosts marked as wanted, Caddy's ask and the caps on relays; and the hand-off, `GET /api/forward/hand-off` (see [Port forwarding](#port-forwarding)) |
| `files.py`, `transfers.py`, `routers/files.py` | The Files tab: a workspace's files listed, a download checked, a file or a folder (as a zip) downloaded and a file uploaded, each path resolved in the workspace first; the transfers under way, their admission and their end (see [A workspace's files](#a-workspaces-files)) |
| `files_settings.py`, `routers/files_settings.py` | The settings of a workspace's files, for administrators: the folder download limit (see [The files settings](#the-files-settings)) |
| `terminals.py`, `routers/terminal.py` | The Terminal tab: a shell in a workspace, carried over a WebSocket (see [The terminal](#the-terminal)) |
| `rcman_upgrade.py`, `routers/rcman_upgrade.py` | Upgrading rcman from its web interface, rcman's side: the release it runs, the report, the request and its admission, and the freeze while one is under way (see [Upgrading rcman](#upgrading-rcman)) |
| `routers/me.py` | Each person's own settings, changed with `PATCH /api/me/settings`, which changes only the settings sent; `prefix_of` and `author_of` are how the rest of the backend reads them |

## Requests

- **Identity** (`identity.current_user`). oauth2-proxy, behind Caddy's `forward_auth`, signs
  people in with Google, and Caddy passes the address on as `X-Forwarded-Email`,
  removing any the browser sent. rcman trusts the
  header because, installed, it listens only on a unix socket in a directory
  that only Caddy's group may enter — see
  [the installer](installer.md#the-socket). The header is read first. Without it, the
  Microsoft session cookie, `__Host-rcman-session`, which Caddy passes to rcman without asking
  oauth2-proxy, is opened, and its generation must be the settings' now, with Microsoft sign-in
  on and the secret key there (see [Microsoft sign-in](#microsoft-sign-in)). Either way the
  address must be an active person's in the people table, which is read on every request, so a
  removal takes effect at once (see [people.md](people.md)). Where the address came from the
  cookie, `not_authenticated` (a cookie that is no valid session) and `not_allowed` also delete
  the cookie, so a stale Microsoft session never keeps anyone from signing in with Google; the
  page reads `not_authenticated` as an ended sign-in ([frontend.md](frontend.md#reporting-failures)).
  The routes that need no identity are `problems.PUBLIC_PATHS`: `/api/health`, the frontend's
  files, and the routes of signing in.
- **Origin.** A `POST`, `PUT`, `PATCH` or `DELETE`, and every upgrade to a WebSocket, must carry
  an `Origin` header equal to `RCMAN_PUBLIC_ORIGIN` (`identity.changing`): a page of any site
  may open a WebSocket, and the browser sends rcman's cookies with it. A route that changes
  something although it is a `GET`, which carries no Origin, is named in `identity.NAVIGATIONS`
  with what stands for the Origin there; `tests/test_identity.py` finds every route that records
  an audit row with a method that is not one of `CHANGING` in the route table, and fails on one
  not named, and on a name of no such route. Each one's reason the test finds here verbatim, on
  one line. The hand-off to a forwarded port:
  a navigation from a forwarded port's host, which sends no Origin: refused unless the browser says it loads a document, and it hands out a way in only to the person's own port.
  A workspace's download (`routers/files.py`):
  a navigation that saves a workspace's file or folder, which sends no Origin: refused unless the browser says it comes from rcman's own pages (Sec-Fetch-Site: same-origin), and only for the person's own workspace.
- **Forward hosts.** Requests to a forwarded port's host never reach the app's routes: Caddy
  passes them to the forwarding server (see [Port forwarding](#port-forwarding)), which trusts no
  identity header, judges each request by its own session cookie, and holds every request that
  is not a top-level navigation to having come from the host's own pages.
- **Headers.** Every answer carries `headers.SECURITY_HEADERS` (`SecurityHeaders`, an ASGI
  middleware installed by `create_app`): a policy that forbids framing and nothing more, so
  that the interactive description of the API, which loads scripts of its own, works;
  `X-Frame-Options`, `X-Content-Type-Options` and `Referrer-Policy`. Neither Caddy nor
  oauth2-proxy adds them for what rcman serves. An answer that sets one keeps its own. A refused
  upgrade to a WebSocket is an answer over HTTP and carries them too.
- **Administrators.** Routes for administrators depend on `admin_user`.
  `routes.admin_only` finds them in the route table, and
  `tests/test_identity.py` drives each one as someone who is not an
  administrator, with a valid request from `CALLS` in `tests/conftest.py`; a planted route
  whose `admin_user` lets anyone in is its twin.
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
  `rcman.problems`. `tests/test_problems.py` finds every field of every route
  in the route table, paired with the schema the API publishes, and holds them
  to this: every published pattern is a rule refused with its sentence — of
  any rule with that pattern, where two share one, as the prefix and a
  workspace's name do; every violation each field's schema implies, and every
  body that is not a JSON object, is refused at its field in a rule's sentence
  or `FIELD_MESSAGES`' text and never in the library's words; and every
  `FIELD_MESSAGES` entry is used. Every code rcman answers with is in
  `MESSAGES` and `STATUS` in `problems.py`, a workspace's and its server's
  among them. A failure of the runtime or a channel is `runtime_unavailable`,
  `workspace_failed` or `timeout` (`workspaces.answered`). An upgrade to a
  WebSocket refused before it is accepted - for its identity, its origin or
  its query - is answered over HTTP with the problem, as a request is; a
  WebSocket accepted is refused with a frame of its own (see [The
  terminal](#the-terminal)).

## Workspaces and servers

`create_app` takes the runtime workspaces run in: by default Podman, through
rcman's own user manager's socket (`main.podman_runtime`); the tests and
`make screenshot` give it the host runtime. `builder_slot`, the slot of builds and the canary, is the
product's unless a test engine gives its own (see [runtime.md](runtime.md#the-runtime)). Everything about workspaces, their
channels and servers, and what rcman brings back when it starts, is in
[runtime.md](runtime.md).

A workspace belongs to one person, and a route reaches only the signed-in
person's: another's is `workspace_not_found`, as if it were not there. The exceptions are an
administrator's: the limits the People page sets on anyone's workspace (see [Limits](#limits)),
and a Stop of anyone's workspace (below), which the Everyone page offers. A
server's verbs need the workspace `ready` (`Workspaces.ready`), or answer `workspace_not_ready`,
`workspace_stopped` for one that is stopped, `workspace_stopping` or `workspace_starting` while a
Stop or a Start of it is under way, and `workspace_rebuilding` while a rebuild replaces its
container; a start, a resume, a stop, a forget and a confirm ask again under the workspace's lock
(`Workspaces._still_ready`), so that one let in before a Stop of the workspace finds it stopped
and changes nothing, `wanted` among it. The
prefix of a server's sessions is the person's, or the workspace's name while
they have set none (`prefix_of`). A start, a resume, a stop and a forget record
what should run in the workspace's row, which restoring reads. The listing
reads every workspace's server side by side, in at most `LISTING_WORKERS`
threads; a workspace whose server cannot be read is listed with why, as
`server_error`, and one whose container a rebuild is replacing, one stopped and one being
stopped or started are not read and are listed with `workspace_rebuilding`, `workspace_stopped`,
`workspace_stopping` or `workspace_starting` there. Each entry has `device`, the workspace's container name
(`Workspace.container`, `rcman-ws-<id>`), in every state: under Podman it is also the
container's hostname, which Claude Code names its server after, so it is the device name
claude.ai and the Claude app show for the workspace's server
([runtime.md](runtime.md#the-runtime)); the host runtime sets no hostname, so there it is only
the name.

Rebuilding (`routers/workspaces.py`; how it works is [runtime.md](runtime.md#rebuilding)):

- `GET /api/workspaces/{workspace}/devcontainer` answers `changed`: whether the
  devcontainer.json in the workspace, or its Dockerfile, differs from the digest recorded when
  it was made or last rebuilt; null where that is not known (`Workspaces.devcontainer_changed`),
  which reads the file through the workspace's channel only when it is ready, not being
  rebuilt, has its container and a digest.
- `POST /api/workspaces/{workspace}/rebuild`, with `{"confirm": true}` (else
  `rebuild_confirm_required`, 422), answers 202 and the workspace's entry, and rebuilds it in the
  background; audited as `workspace.rebuild`, the target the workspace's name. Its refusals are
  those of `Workspaces.rebuild_refusal`: `rcman_upgrading`, `rcman_starting`,
  `workspace_rebuilding`, `workspace_not_ready`, `rebuild_empty` and `rebuild_no_container`
  (409), and `workspace_not_found` for another's.
- Each entry of the listing has `rebuild` - its `state` (`preparing`, `switching`,
  `promoting` or null), its `log` and its `error`, which keeps why the last one did not finish
  (`problems.REBUILD_MESSAGES`, or a refusal of `DEVCONTAINER_MESSAGES` in a rebuild's words,
  `problems.rebuild_advice`) - and `rebuildable`, `rebuild_refusal` being none, the test the
  route makes. A failed workspace's rebuild that failed says why in its `error`. A stopped
  workspace cloned from a repository whose volume is there may be rebuilt: it is started first,
  without its server.

Stopping and starting (`routers/workspaces.py`; how they work is
[runtime.md](runtime.md#stopping-and-starting-a-workspace)):

- `POST /api/workspaces/{workspace}/stop` answers 200 and the workspace's entry, stopped
  (`halted`); audited as `workspace.stop`, the target the workspace's name, or for another's,
  `<email>/<name>`. Its owner may stop it, and so may an administrator, anyone's, recorded as
  themselves, and answered the workspace as the
  Everyone page shows it (`everyone.shown`, below), nothing more. A container that is still running, or whose state cannot be told, afterwards is
  `workspace_stop_failed` (502), in the catalogue's words and never Podman's, and the workspace
  stays ready.
- `POST /api/workspaces/{workspace}/start` answers 202 and the workspace's entry, and starts it in
  the background; audited as `workspace.start`, the target the workspace's name. Only its owner
  may start it.
- The refusals of both are those of `Workspaces._power_refusal`, in its order: `rcman_upgrading`,
  `rcman_starting`; for a Start, the instructions for Claude missing from the tools
  (`workspace_failed`, 502, with the installer's words as its title); `workspace_not_ready` for
  one being made, deleted or failed; `workspace_stopping` and `workspace_starting` while a Stop
  or a Start of it is under way; `workspace_rebuilding` while it is rebuilt or worked on;
  `workspace_stopped` for a Stop of a stopped one, `workspace_not_stopped` for a Start of a ready
  one (409); and `workspace_not_found` for another's.
- Each entry of the listing has `stoppable` and `startable`, `_power_refusal` being none for each
  verb, and `stopping` and `starting`, whether a Stop or a Start of it is under way. A stopped
  workspace whose Start could not bring its container up is stopped still, and says why in its
  `error` (`problems.POWER_MESSAGES` and the cause).

`everyone.py` lists every person's workspaces for administrators
(`GET /api/servers/everyone`), each workspace with only its id, name, `device`, state, server,
`server_error`, `claude_version`, `stoppable` and `stopping`, copied one by one from the listing
(`everyone.shown`, which the administrator's Stop answers with too), so nothing else the listing
answers reaches it, and each server with only `SERVER_FIELDS` — never its screen or its
claude.ai address. The one thing an administrator may do to another person's workspace from it is
stop it (`POST /api/workspaces/{workspace}/stop`); no other route lets one person act on
another's workspaces.

## Limits

The administrators' account is [Limits](../docs/users.md#limits); how they are applied, the
slices and the restore are [runtime.md](runtime.md#limits). The routes are the People page's, for
administrators (`routers/people.py`), and `rcman-admin limits` does the same from a shell (see
[people.md](people.md#rcman-admin)):

- `GET /api/people` lists each person with their stored `limits`;
- `GET /api/people/{person}/limits` is the report (`Workspaces.limits_report`): the person's
  limits and what their workspaces use together, and each workspace's own; a usage that could not
  be read is null, not zero (the report has a total deadline, after which readings are null);
- `PUT /api/people/{person}/limits` and `PUT /api/people/{person}/workspaces/{workspace}/limits`
  set the whole set, `LimitsBody`: a field sent null is taken away. Each field is checked by the
  rule of `helpers/rules.py` and the minimum of `limits.py`, and refused in the rule's words.
  The lock `_limits_lock` is also a lock file beside the database (`filelock.FileLock`,
  `workspaces.LOCK_FILE`), so `rcman-admin limits`, a process of its own, and the service
  take turns.

The problem codes are in `problems.py`: `memory_invalid`, `cpu_invalid` and `pids_invalid`
(each the sentence of its rule), `limit_below_use`, which names the limit and what is in use (409; the rule is
[runtime.md](runtime.md#limits)'s), `usage_unknown` (409) when a tightening is asked and what is in
use cannot be told, and `limits_not_applied` when the runtime could not set them; with `person_not_found`,
`workspace_not_found` and `runtime_unavailable`. A limit that could not be applied is not
recorded. Both `PUT`s are audited as `person.limits` and `workspace.limits`, the target holding the
values set (see [the audit guide](../docs/audit.md)). `tests/test_limits.py` holds how the
values are read and shown, `tests/test_workspace_limits.py` the limits set and stored,
`limit_below_use` and `limits_not_applied`, and `tests/test_people.py` the routes' limits set,
stored and reported; no test holds the other codes.

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
  `login_warning_days`; `signing_out`; and `signed_out_unexpectedly`, true when SQLite has
  the account connected, no sign-out waits and the file holds no sign-in
  (`runtime.credentials_state`: empty or unreadable), as two workspaces' Claude writing it at
  once can leave it (an accepted risk, see [runtime.md](runtime.md#a-persons-claude-sign-in)).
  The state is given even when finishing a sign-out that waits keeps failing: that is logged. It
  and `begin`, `finish` refuse a person being deleted or gone (`not_allowed`), the read under
  the person's lock so that it cannot make the volume again after a deletion, as
  `GitAccount` does, since reading or starting a sign-in makes their volume.
- **A server's start** (`Workspaces.start_server`) refuses a workspace that is no longer
  ready once it holds the workspace's lock, as one deleted meanwhile (`workspace_not_ready`),
  `claude_not_logged_in`
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
- **Signing out of rcman** (`POST /api/me/sign-out`, `me.sign_out`) ends the
  person's terminals (see [The terminal](#the-terminal)), stops the person's
  running servers unless `keep_sessions`, the default, deletes a Microsoft session's cookie in
  its answer, and answers oauth2-proxy's sign-out address, which the page goes to: oauth2-proxy
  clears its own cookie and sends the browser to `/`, whose `forward_auth` leads to
  `/oauth2/start`. So it signs out of both.

## git in workspaces

`routers/git.py` names a workspace by its id (`workspace`, a query parameter or a
body field, never a path) and runs git in it. `_run` finds the workspace among the
signed-in person's ready ones (`Workspaces.ready`: `workspace_not_found` for another's
or none, `workspace_stopped`, `workspace_stopping` and `workspace_starting` for one stopped or
being stopped or started, `workspace_not_ready` otherwise, `workspace_rebuilding` while a rebuild
replaces its container), and calls a verb of `repository.Repository` on
`Workspaces.repository(workspace)`: nothing is started and no server or workspace lock
is taken. A change's audit target is the workspace's name; a commit's message and the
files to stage reach git on stdin and are never recorded or logged.

- **Where git runs.** In the workspace, at the root of its work folder, as the
  workspace's user, through the workspace's channel for reads and through
  `Runtime.exec` for what carries stdin or reaches a remote, which may take minutes
  and would hold the channel meanwhile. Every call is wrapped in the layout's
  `timeout` tool (`GIT_TIMEOUT`, `GIT_NETWORK_TIMEOUT` for fetch, push and sync, shared
  by the calls of one verb), and `exec` itself has a `timeout` of its own that ends a
  call whose tool did not. `helpers/git_run.py` keeps the parsers and the rules for
  git's output and refusals; `repository.py` is the process half.
- **What git is run with.** An environment of its own (`Repository.env`): the variables the
  workspace's plan sets but for every `GIT_` one and every name that moves git's
  configuration (`runtime.moves_git_config`), and the few of git's that rcman sets. The same
  names are refused in a devcontainer.json (see [runtime.md](runtime.md#a-repositorys-devcontainerjson)).
  Whether a token is stored is asked of the workspace's shell by exit status
  (`HAS_PASSWORD`), so no byte of the credential file passes through the channel.
- **A repository is the workspace's own `.git`.** A workspace with none, as a new
  one has, answers `not_a_repository` (409). `git init` is not rcman's: Claude runs
  it.
- **Cloning makes a workspace.** `POST /api/git/clone` (`url`, optional `name`;
  202 with the workspace, audit `git.clone`, target the address, which
  `GITHUB_URL` makes safe to record) is the one git route that names no
  workspace. It calls `Workspaces.create(person, name, source)`; the clone is a
  phase of `creating`, in the workspace's own thread, and the rest is in
  [runtime.md](runtime.md#what-rcman-keeps-and-brings-back). `Repository.clone`
  is a verb like fetch, with `CLONE_TIMEOUT`, in its own process so it holds no
  channel; it raises `CloneFailed` with the exit code and all git said. A name
  left out is `workspaces.name_from(url)` (the repository's, made a valid
  name, `DEFAULT_NAME` when nothing is left), numbered `-2`, `-3` when the
  person has it already; a name given that is taken is `workspace_name_taken`.
- **Why a clone failed.** `CLONE_CAUSES` in `workspaces.py` recognises a cause
  from git's words; `problems.CLONE_MESSAGES` is what the workspace's `error`
  says of it in plain words, ahead of the exit code and the tail of git's words,
  all cut to `MAX_ERROR_CHARS`. The person's token is replaced by `REDACTED` in
  the error and the log.
- **A workspace's `source` and `log`.** Migration columns of `workspaces`:
  `source` is the address as given (null for an empty one), `log` the steps
  rcman wrote while making it, cut to the last `LOG_MAX_CHARS`, from a line's
  start, and cleared once the workspace is ready. The listing answers both
  (`log` never for a ready one), and the poll the page already makes shows them.
  It also answers `source_page`, the repository's page on GitHub, which
  `source_page()` in `workspaces.py` derives from a `source` that
  `rules.GITHUB_URL` takes whole, and null otherwise.
- **The image check.** After `runtime.create`, `Workspaces._check_git` runs
  `git --version`: no git, or one that does not answer as rcman needs, or one older
  than `git_run.GIT_MIN_VERSION`, fails the workspace with the reason. The same check
  holds the image a repository's devcontainer.json names.
- **A repository's devcontainer.json.** After the clone, `Workspaces._configure` reads
  the file with `devcontainer.read`, holds it to `devcontainer.POLICY` and makes the
  workspace from the image it names or builds, or from the default image where it names
  none and builds none (see
  [runtime.md](runtime.md#a-repositorys-devcontainerjson) and
  [Building an image](runtime.md#building-an-image)). Its plan is the `environment`
  column of `workspaces` (null for a workspace with no file), which a restore
  reads. A build copies its image through a staging folder in `scratch_dir`
  (see [Building an image](runtime.md#building-an-image)).
  `problems.DEVCONTAINER_MESSAGES` is what a refusal or a failed command says in
  plain words, and holds no number; the workspace's `error` carries it and the steps in
  `log` carry the rest. Nothing here adds a route: the file is read inside the clone's
  own thread, so the audit and the permission rules of `git.clone` cover it.
- **Author and token.** `gitaccount.GitAccount` keeps the commit author
  (`user_settings`) and the token (`github_tokens`, removed with the person's row)
  and writes them into the git folder of the person's volume with
  `Runtime.put_account` (see [runtime.md](runtime.md#the-git-folder)): git's `config` and
  `credential`, and gh's `config.yml` and `hosts.yml`, the token in that one as one
  single-quoted YAML string, a quote doubled, with no `user` key, since rcman does not know
  the account's login. The volume first, SQLite second, under the person's lock. A save
  writes `config` and `config.yml` as they are, then the token files (`token_files`:
  `hosts.yml`, then `credential`); one whose write fails puts back the token files it had
  begun to write, the one whose write failed among them, since it may have landed before the
  failure was said. A forget empties the token files and puts nothing back when a write
  fails, so the token is in fewer places, never more, and its row stays for Remove to be
  tried again. A failed SQLite write, after either, puts the volume's token files back.
  `PATCH /api/me/settings` goes through it as well. At every restore,
  `write_gh_for_everyone`, a restore hook (see
  [runtime.md](runtime.md#what-rcman-keeps-and-brings-back)), writes `config.yml` for every
  person let in and `hosts.yml` for each with a token stored, read under their lock; one no
  longer let in is skipped, and one whose write fails is logged without stopping the others. A write
  that races a person's deletion removes what it wrote and answers `not_allowed`.
  `GET /api/github` answers whether a token is stored from SQLite alone, never from
  the runtime. A commit is refused with `author_not_set` until a name is set; a
  push or a sync to GitHub with `github_token_not_set` while no token is stored.
- **Problem codes** are `problems.py`'s: `git_not_installed`, `git_failed` (what git
  said is in the problem's extra), `git_timeout`, `git_unrecognised_output`,
  `not_a_repository`, `file_invalid`, `file_outside_repository`, `nothing_staged`,
  `branch_invalid`, `branch_not_found`, `branch_name_taken`, `detached_head`,
  `no_push_remote`, `no_commits_yet`, `github_token_not_set`, `diverged` and
  `upstream_gone`.
- **`commit-push`** commits and pushes; when the commit is made and the push fails,
  the answer carries the push's problem and the audit row's result is
  `committed_not_pushed`.
- **`sync`** (`POST /api/git/sync`, audit `git.sync`) is the branch bar's Sync, beside
  `fetch` (`git.fetch`), which is a `git fetch --prune` alone. `Repository.sync`
  refuses what a push refuses (`_push_refusal`, shared with `push`) before anything is
  fetched; then fetches with `--prune`, publishes a branch with no upstream as `push`
  does, answers `upstream_gone` where the upstream is not there once fetched and
  `diverged` where the branch and its upstream both have commits the other lacks,
  fast-forwards with `git merge --ff-only`, which the person's `pull.*`, `merge.ff`
  and `branch.<b>.mergeOptions` cannot turn into a merge or a rebase, and pushes where
  the branch's push destination (`@{push}`, else its upstream) lacks a commit of it.
  With a push remote of its own under `push.default=simple`, git names no `@{push}`,
  so what is unpushed is counted against the upstream, as `status`'s `push_ahead` and
  the page's Push count it: after a pull nothing is pushed there, and a commit the
  upstream lacks is pushed where `git push` sends it. The
  answer says what it did (`pulled`, `pushed`, `upstream_set`). When something was
  pulled and the push fails, the answer carries the push's problem (`push_problem`, as
  `commit-push` does, through the same `_push_problem`) and the audit row's result is
  `pulled_not_pushed`; a push that fails with nothing pulled is the sync's own
  refusal.

## The terminal

The **Terminal** tab is a shell in one of the signed-in person's ready workspaces, carried by
the WebSocket route of `routers/terminal.py`, `/api/terminal?workspace=<id>`, which the
OpenAPI description does not list; its protocol is the table below. `terminals.py` runs the
shells, one per workspace (`Terminals`, `app.state.terminals`).

- **Who.** The upgrade is refused before it is accepted, over HTTP with the problem and the
  security headers, for want of an identity (`not_authenticated`, `not_allowed`) or the public
  origin (`origin_refused`), and for a query naming no workspace (`validation_failed`). Once
  accepted, the workspace is found among the person's (`Workspaces.owned`): another's, or none,
  is `workspace_not_found`, an administrator's look at another's included; one of theirs that is
  not ready is `workspace_not_ready`, one stopped or being stopped or started
  `workspace_stopped`, `workspace_stopping` or `workspace_starting`, and one a rebuild replaces
  the container of `workspace_rebuilding` (`Workspaces.ready`). Each is a `problem` frame, whose
  `problem` holds the problem details, and a close of `close_problem` plus the problem's status.
  The page then has `FIRST_RESIZE_SECONDS` to send its first `resize`, which it sends as the
  socket opens; a page that does not is closed with `close_policy`. The page cannot read a
  refused upgrade; what it then does is [frontend.md](frontend.md)'s.
- **The shell.** `Runtime.terminal_argv` starts it on a pty of rcman's own, as the container's
  own user, with no restriction added or removed (see
  [runtime.md](runtime.md#the-terminals-shell)). What is typed is written to the pty as it
  comes, and what the shell prints is sent as it is read; the reader stops watching the pty while
  a send is awaited, so nothing is queued. Nothing typed or shown is recorded or logged.
- **Opening.** A terminal is open once its shell has written its pidfile (within
  `PIDFILE_SECONDS`): on Podman, ending the client does not end its exec, so a terminal ended
  before its shell named itself would leave the shell running. One whose shell does not name
  itself is ended at once, and the page is told `workspace_failed`; one whose opening is
  cancelled, its page gone, is ended at once. Each opening first ends what earlier terminals of
  the workspace left (`terminals.sweep`, below).
- **One per workspace.** A terminal opened on a workspace that has one replaces it: the first
  is told `terminal_replaced` and ended, its session included, before the second's shell starts.
  Opening, ending and sweeping a workspace's terminals take turns under its lock, so a deletion
  that comes while one opens ends it once it is open; a sign-out is counted per person, the
  count taken as the socket is accepted, and a terminal a sign-out came before since is ended
  as it opens, with `signed_out`.
- **What ends it.** Its shell ending (`exit`, with the shell's exit code; the row is read again,
  so a shell that ended because its workspace went is `workspace_gone`); its page closing the
  socket or vanishing, which uvicorn's keepalive ping finds (`terminal_lost`); its person
  signing out of rcman (`me.sign_out`: `signed_out`); its workspace being deleted
  (`Workspaces.on_removing`, before the container goes: `workspace_gone`); a rebuild replacing
  its container (`Workspaces.on_rebuilding`, before the old one is discarded:
  `workspace_rebuilding`); a Stop of its workspace (`Workspaces.on_stopping`, before the
  container is stopped: `workspace_stopped`); and, every
  `REVOCATION_SECONDS`, a look at the table finding the person no longer active (`not_allowed`)
  or the workspace no longer theirs (`workspace_gone`), stopped or being stopped
  (`workspace_stopped`), not ready (`workspace_gone`), or being rebuilt
  (`workspace_rebuilding`), which catches what rcman did
  not do itself, as `rcman-admin`'s deletions. That is revocation, not a time limit: there is
  none, nor an idle timeout. A look that fails, as on a database that is busy, is logged and
  tried again at the next. Of what ends a terminal at the same moment, its own end says most,
  then a revocation, then the page (`PRIORITY`). When rcman stops, uvicorn closes every socket
  (`close_closing`), and the app's shutdown closes what is left (`service_closing`); both are
  best-effort - a terminal that fails to close is logged, and keeps neither the others nor the
  rest of the shutdown from closing - and the restore's sweep is what makes sure (see
  [runtime.md](runtime.md#the-terminals-shell)).
- **Ending.** `Terminal.cleanup` closes the pty, ends the program on it (SIGTERM, then SIGKILL,
  each after `END_GRACE_SECONDS`), then, through the workspace's channel, ends the shell's
  session (`end_leader`): SIGHUP to every process in it, then SIGKILL, by the leader its
  pidfile named when it opened, which closing the pty may have ended already (see
  [runtime.md](runtime.md#the-terminals-shell)). A process that left the session, as `setsid`
  or a daemon does, is not the terminal's.
- **uvicorn.** uvicorn speaks WebSocket only through a package it does not depend on itself;
  rcman depends on `websockets`. uvicorn's `auto` picks `WEBSOCKETS`, the implementation over it,
  already; `--ws` names it all the same in the unit's `ExecStart`, so that which one runs is
  written down, and `tests/test_terminal.py` holds it to `WEBSOCKETS`, which the screenshots'
  backend (`devtools/world.py`) runs with too.
- **Audit.** The route depends on `audit.Session("terminal")`, which records `terminal.open`
  (`ok`, or the problem it was refused with) and, after an open that worked, `terminal.close`
  with how it ended (see [Audit](#audit)).

### The terminal's protocol

`PROTOCOL` in `routers/terminal.py`; `tests/test_terminal.py` holds this table to it.

| Name | Value | Meaning |
|---|---|---|
| `path` | `/api/terminal` | The route |
| `query` | `workspace` | The query parameter naming the workspace by its id |
| `resize` | `resize` | The page's one kind of text frame, `{"type", "cols", "rows"}`: first, before anything is typed, and on every fit |
| `exit` | `exit` | The shell ended: `{"type", "code"}`, its exit code |
| `closed` | `closed` | Something else ended the terminal: `{"type", "reason"}` |
| `problem` | `problem` | The workspace was refused: `{"type", "problem"}`, the problem details |
| `size_min` | `1` | The fewest columns or rows a resize may give |
| `size_max` | `65535` | The most |
| `close_normal` | `1000` | The close after `exit` or `closed` |
| `close_unsupported` | `1003` | A text frame that is not a JSON object of type `resize`, first or later |
| `close_policy` | `1008` | A resize whose size is not a whole number in range; a binary frame before the first resize; no first resize within `FIRST_RESIZE_SECONDS` |
| `close_closing` | `1012` | rcman is stopping |
| `close_problem` | `4000` | Plus the problem's status, after a `problem` frame |
| `reasons` | `terminal_replaced`, `workspace_gone`, `workspace_rebuilding`, `workspace_stopped`, `signed_out`, `not_allowed`, `service_closing` | What a `closed` frame says |

Binary frames are what is typed and what the shell prints, as bytes, both ways.

## A workspace's files

The routes of `routers/files.py`, under `/api/workspaces/{workspace}/files`, each for the
workspace's owner only and while it is ready (`Workspaces.ready`, whose refusals say why it is
not):

- `GET ?path=` lists a folder: `{path, entries, more, now}`, and at the top `reserved` too, the
  names `Layout.hidden` gives, which are never listed and which an upload there is refused. Each
  entry is `{name, kind, size, changed, target}`: `kind` is `folder`, `file`, `link` or `other`;
  `size` is a file's only; `changed` and `now` are seconds since the epoch by the workspace's
  clock; `target` is a link's, `folder`, `file` or `other`, and null where it cannot be reached
  or is not there. Folders, and links to folders, come first, then the rest, each by name; at
  most `files.LISTING_CAP`, folders first, and `more` says how many were left out.
- `GET /download/check?path=` answers whether a download may go now, before the page starts
  one: `{kind, name, size}`, the name it is saved as (a folder's `<name>.zip`, the top's
  `rcman.zip`) and its size, a folder's only where there is a limit; or its refusal, among them
  `transfers_busy` where no transfer would be admitted now. It is not audited.
- `GET /download?path=` streams a file as it is, or a folder as a zip, audited as
  `workspace.files.download`. It is a navigation (see [Requests](#requests)), so it is refused
  unless `Sec-Fetch-Site: same-origin`.
- `PUT ?path=&name=&overwrite=` streams the request's body into the file `name` of the folder,
  audited as `workspace.files.upload`, and answers `{name}`.

**Paths.** `path` is relative to the volume, empty for the top, held to `rules.FILES_PATH`, and
`name` to `rules.FILE_NAME`, as fields. Every script `files.py` holds begins with the one
resolver, `files.RESOLVE`, run by the workspace's shell in the same process as what reads or
writes: `realpath` resolves the path, and the script refuses it (`files.REFUSED`,
`file_path_refused`) unless it stays in the volume and out of the hidden folders at its top, and
tests itself that it is there (`files.NOT_FOUND`, `file_not_found`), since `realpath` passes a
missing last part. The scripts' other refusals are `files.REFUSALS`' codes in
`routers/files.py`. Each script is one line (`files.one_line`), as the channel carries only
that; a listing, a path's kind (`files.KIND`) and an upload's early check run through the
channel, which is why a path holds no line break.

**Downloads.** A file is a stream of `files.CAT`; a folder of `files.ZIP`, the workspace's own
bsdtar (see [runtime.md](runtime.md#streams)). A folder is looked through first, in the check and
again in the download, by `files.SCAN`, an exec of its own that holds a transfer's place and a
thread of `transfers.TRANSFER_THREADS` while it runs, and whose `find` and `du` are each killed
in the workspace at `SCAN_SECONDS` (`folder_scan_timeout`): stats only, the first path the
workspace cannot read (`folder_unreadable`, with that `path`), and with a limit set, its size by
`du` (`folder_too_big`, with `size` and `limit_mb`); a `du` that did not measure every entry is
`files_unreachable`, never a size of 0. The answer is a `TransferResponse` whose body takes the
transfer's place and starts its stream only as it starts, so an answer dropped before its body
(a browser gone during the scan, a row that could not be written) holds nothing; its reads run on
`transfers.TRANSFER_THREADS`, and at the end of the program's output its exit code is waited for.
One that is not 0, a stream ended by a Stop, a rebuild or a deletion, and a read that waited too
long each cut the answer (`TransferCut`): it is left unfinished, which the browser takes for a
failed download, so a zip with something missing never ends as whole. The body is always
closed, which ends the stream in the workspace, also when the browser goes away.

**Uploads.** The early check, `files.UPLOAD` with nothing written, gives the refusals before the
body is read: `file_exists` without `overwrite`, `file_name_taken_by_folder` for a folder, a link
or anything but a file of that name, and `file_path_refused` for a hidden name at the top. The
stream then checks again, writes its input to a temporary file beside the target
(`files.TEMPORARY_PREFIX`), and links it to its name, which fails where the name is taken, or with
`overwrite` renames it over the file; a link of that name is never followed (`ln -n`, `mv -T`).
A refusal as the stream checks again answers its own code, and an upload ended by a Stop, a
rebuild or a deletion that workspace's (`Transfers.ended_code`). A body that moves nothing for
`transfers.TRANSFER_IDLE_SECONDS`, either way, is `transfer_idle`.

**Transfers.** `transfers.Transfers` (`app.state.transfers`) admits at most
`TRANSFERS_PER_PERSON` of a person's and `TRANSFERS_IN_ALL` in all (`transfers_busy`), starts each
stream under the workspace's lock once it is still ready, and lets the lock go once the stream's
pidfile is written. A Stop, a rebuild and a deletion end every transfer of the workspace first
(`end_all`, chained to `on_stopping`, `on_rebuilding` and `on_removing`), with the temporary file
of an upload, which then answers that workspace's code. Each row is written before the first
byte: a download's when the endpoint returns (`Depends(Recorder(...), scope="function")`), an
upload's once its stream has started (`Record.let_through`). It says the transfer was let
through, not that it completed.

`tests/test_files.py` holds the routes on the host runtime; the resolver and the zip are contract
cases (`tests/runtime_contract.py`), run on Podman among the basic ones.

## Claude Code versions

`claude_versions.ClaudeVersions` (`Workspaces.versions`, also `app.state.versions`) owns
the store, `claude_store`: `versions/<version>`, `current` and `bin/claude`. The
installer makes the store and rcman writes it from then on (see
[the installer](installer.md#what-workspaces-are-made-of)). Approvals are the append-only table
`claude_approvals` (triggers refuse an update and a delete): the latest is the approved
version, the previous is the latest one naming another that is not marked failed, and a
rollback is a new row. The database is the truth and `current` follows it (`reconcile`, run
when rcman starts, before the restore); with no approval yet, `reconcile` adopts what
`current` names. A workspace's row records the version its server started with
(`claude_version`).

- **Pinning.** `Workspaces.start_server` resolves the approved version (`resolve`), records
  it on the row and starts the server with a layout that runs that exact file
  (`Layout.pinned`, see [runtime.md](runtime.md)). `ClaudeVersions.lock` is held from the
  resolve to the record, and the pruning holds it too, so a version is never removed between
  the two. A stop or a forget clears the record. Restore starts servers by the same path.
  Nothing restarts a running server.
- **Rolling out.** `POST /api/claude/upgrade {version}` (administrators, audited as
  `claude.upgrade` with the version as target) is `upgrade`: synchronous, one at a time
  (`claude_upgrade_busy`, also while a version is being deleted), and none while an upgrade of rcman is under way (`rcman_upgrading`,
  see [Upgrading rcman](#upgrading-rcman)). The candidate is staged outside the store, in a folder of its own
  in `STAGING_FOLDER` beside the database, fetched by `helpers/claude_release.py` (streamed,
  hashed, size-capped, renamed into place only whole; the installer fetches with the same
  code) or copied from the store; run through the canary
  (`Runtime.run_canary`, see [runtime.md](runtime.md#the-canary)); and only on a pass copied into
  the store under a partial name and renamed. A version that was approved before and is still in
  the store needs neither download nor canary, since it passed then: a rollback or re-approval
  works with Podman unavailable, unless the version is marked as failed (below); a version never
  approved, and one approved before that left the store, always goes through the canary.
  The commit (`_commit`) switches `current` first and records the approval only afterwards, so
  what is approved is what the link names: if the switch fails nothing is approved, and if the
  recording fails the link is put back. After a crash the start of rcman repoints the link at
  the approved version (`reconcile`; the database wins). The version field is validated
  before anything is done. A file of no length in the store is no version (`sweep`
  removes it). A refusal or failure leaves the store and the approvals as they were:
  `claude_version_failed` (the version is marked as failed, below), `claude_install_failed`
  (whose title says why, by `problems.CLAUDE_INSTALL_MESSAGES`; status 502 for the kind
  `network`, 409 for the others, by `CLAUDE_INSTALL_STATUS`), `claude_checks_failed` (the
  failing checks with their reports in the problem's extra, and `version`, the one that failed,
  and `approved`, the one the workspaces stay on, there and in its title, by
  `problems.CHECKS_FAILED_MESSAGES`; `approved` is null, with a wording of its own, where
  nothing is approved, and nothing is adopted for it) and `claude_canary_failed` (the
  canary could not run).
  The checksum comes from the release site with the program: it protects against corruption,
  not against a compromised source, and nothing checks a signature.
- **Versions marked as failed.** `PUT /api/claude/failures` (body `{version, reason}`: the
  version held to `rules.VERSION` as a field, so an empty one is refused under it; the reason
  optional, trimmed and then held to `rules.FAILURE_REASON` or empty, blank being none) is
  `mark_failed`, and `DELETE /api/claude/failures/{version}` is `clear_failed`; both are for
  administrators, answer the report, and are audited as `claude.mark_failed` (target: the
  version and the reason as JSON, not escaped to ASCII, the reason left out when there is
  none) and `claude.clear_failed` (target: the version). A mark is a row of
  `claude_failures`, one per version, deleted when the mark is removed, so the audit trail is
  its history; it outlives the version's file. Both run under `lock`. Any version may be
  marked but the approved one (`claude_version_approved`; with nothing approved yet,
  `reconcile` adopts first); one marked already is `claude_version_failed`, and removing a
  mark that is not there `claude_version_not_failed`. Marking is allowed while a roll-out or
  an upgrade of rcman runs, and is followed by `prune_quietly`, which does nothing while a
  roll-out or a deletion runs. A roll-out of a marked version is `claude_version_failed` before anything
  else, the shortcut for a version approved before among it, and `_commit` asks again under
  `lock`, so a mark made while the canary runs stops the approval and the store and the
  approvals stay as they were. A marked version is never the previous one: the previous is
  the last good one, which pruning keeps while it is in the store; one that left the store
  before is downloaded and run through the canary again when an administrator goes back to
  it. Nothing marks a version by itself: a canary that fails marks nothing.
- **Deleting a version.** `DELETE /api/claude/versions/{version}` (administrators, audited as
  `claude.delete_version` with the version as target) is `delete`, which answers the report. It
  holds `_upgrading`, the roll-outs' lock, for its whole run, taken without waiting, so no
  roll-out and no admission of an upgrade of rcman comes between its checks and the removal;
  with the lock taken elsewhere it is `claude_version_kept` with the reason `rolling_out`, and
  while an upgrade of rcman is under way `rcman_upgrading` (asked outside `lock`, as it may ask
  systemd). Then, under `lock`, which `Workspaces._pin` records a server's version under, a
  version not in the store is `claude_version_not_installed`, and `claude_version_kept` is
  given for the approved version (`approved`; with nothing approved yet `reconcile` adopts
  first), the one `current` names (`current`) and one any workspace's record names, read again
  by `recorded_versions` (`in_use`): a record outlives a server that ended on its own until a
  stop clears it. Every problem's title is its reason's words, by
  `problems.VERSION_KEPT_MESSAGES`. The previous version may be deleted; rolling it out again
  then downloads it and runs the canary. `claude_failures` is not touched: a mark outlives the
  file.
- **Pruning.** `prune` removes the versions that are neither approved, previous nor the one
  `current` names and that no server in
  `Workspaces.pinned_servers` uses (a server that runs, cannot be read, or is being started;
  tmux is asked whatever the row's `server` flag says, since the record of a version and the
  flag are two writes, and only tmux saying there is none frees the version). `rcman-admin`
  does not prune or collect images (`Workspaces(prunes=False)`): its locks are not the
  service's, which does at the next stop, roll-out or start. It asks the servers for their status without holding
  `lock`, then decides again under it, also keeping a version whose workspace record changed
  meanwhile; it does nothing while a roll-out or a deletion runs. It runs after a roll-out, after a version
  is marked as failed, after a server is stopped or forgotten (`Workspaces._stopped`) and in
  the restore, with `sweep`,
  which removes what a roll-out cut short left: staging folders, partial files and a canary.
- **Reading.** `GET /api/claude/versions` is `report`: the approved and previous versions,
  the installed ones with whether each is approved or
  previous, whether it is marked failed (`failed`) and how many servers use it, the drift
  (running servers on another version, each with whether its version is marked failed), the
  marks (`failed`: version, at, by and reason, the newest first) and whether a roll-out or a
  deletion runs (`upgrading`) or an upgrade of rcman is under way (`rcman_upgrading`). Each
  installed version's `delete_refused` is null, or the `claude_version_kept` problem a deletion
  would be refused with for a reason of the version's own (`approved`, then `current`, then
  `in_use`); the reasons of the moment, a roll-out or an upgrade of rcman, are the report's
  `upgrading` and `rcman_upgrading`.
  `GET /api/claude/latest` is the release site's newest version, only
  a suggestion (`null` where the site does not say). Both are for administrators.
  `GET /api/workspaces` and `everyone` carry each workspace's `claude_version`.
- **No self-update.** `DISABLE_AUTOUPDATER=1` is in the server's environment (`child_env`), in
  the lifecycle commands' and in the canary's; `tests/test_claude_server.py` holds every use of
  the path of Claude to the command builder or the pinned layout.
- **The setting** `claude_download` (`RCMAN_CLAUDE_DOWNLOAD`, validated by
  `helpers/claude_release.DOWNLOAD_ADDRESS`) is where releases are downloaded from.
- **The journal.** `ClaudeVersions` logs to `rcman.claude_versions`, which `create_app`
  installs like the others (below).

## Upgrading rcman

An administrator upgrades rcman from the **rcman release** page when the installer turned it
on (`web_upgrade`, `RCMAN_WEB_UPGRADE`); what carries it out, as root, and the files it reads
and writes are the installer's, in
[Upgrading from the web interface](installer.md#upgrading-from-the-web-interface).
`rcman_upgrade.py` mirrors their paths (`STATUS`, `REQUEST`) and the status's fields rather than
import the installer; `rcman_upgrade.Upgrades` is `app.state.upgrade`.

- **The release.** `running_release`, called once as `create_app` builds the app and kept as
  `Upgrades.release`, is the commit in the `.complete` mark of the release whose virtualenv
  rcman runs in (`PREFIX`, `sys.prefix`, resolved, and its folder), or `null` where there is
  none or it names no commit, as in a development run. It is never read again: an upgrade
  switches `current` while the release before still runs.
- **Running.** Whether an upgrade runs comes from systemd, read-only: `show_units` runs
  `SYSTEMCTL` with `SHOW_ARGS` (`show` of the service and the path unit, for `PROPERTIES`) and
  `SHOW_ENVIRONMENT` alone, within `SHOW_TIMEOUT`, and `read_units` takes exactly a block of
  those properties for each of the two units, an `ActiveState` among `ACTIVE_STATES`; anything
  else is an error, never idle. The service in one of `RUNNING` is a run under way, and the
  status's fields are given only where the status names the same invocation. Otherwise the
  status (`read_status`: exactly its fields, of their types, the error within `ERROR_LENGTH`)
  says how the last one ended; none, or one it does not take, is `unknown`, never an error. A
  status that says `running` is `failed` where it is of another boot (`BOOT_ID`) or systemd
  shows the service not running, and `running` only where systemd cannot be read.
- **Requested.** An admitted request is held in memory with the invocations the service and
  the status named then. It is `requested`, and `not_started` once `UPGRADE_START_GRACE` has
  passed, until the service runs, and `running` while it does; it is let go only when the
  status names another invocation, which its run wrote, or systemd shows another invocation
  of the service that no longer runs, a run that ended before it wrote anything. So the
  moment between the service's start and the wrapper's first write freezes as the run does. It is not kept across a
  restart, which only an upgrade that started makes.
- **The report.** `GET /api/rcman/upgrade` is `release`, `enabled`, `available`, `reason`,
  `state` (`requested`, `running`, `succeeded`, `failed`, `not_started` or `unknown`) and the
  status's `started`, `finished`, `from`, `to` and `error`, the same keys whatever
  the state. `reason` is `null`, or the problem a request would be refused with now:
  `rcman_upgrade_refused` with its `reason`, one of `problems.UPGRADE_REFUSED_MESSAGES`, whose
  words are its title. With the option off it is `disabled`, and systemd is not asked.
- **Admission.** `POST /api/rcman/upgrade` (audited as `rcman.upgrade`; each administrator
  limited by a token bucket of `REQUESTS_BURST`, refilled at `REQUESTS_PER_MINUTE`,
  `too_many_requests` with `Retry-After` past it) answers 202 with the report. `request`
  holds `Workspaces.recording_held`, the images' lock, which every workspace is recorded
  under, waiting at most `ADMISSION_WAIT` (`images` otherwise), then
  `ClaudeVersions.rollouts_held`, the roll-outs' lock, without waiting (`rolling_out`: a
  roll-out, a deletion of a version, or the `sweep` of what a roll-out left, holds it); holding
  both,
  it refuses while systemd cannot be read (`units_unreadable`), the path unit is not loaded and
  active (`path_inactive`), an upgrade is under way (`under_way`), a workspace is being made
  (`creating`), one is being rebuilt (`rebuilding`, `Workspaces.any_rebuilding`; see
  [runtime.md](runtime.md#rebuilding)) or one is being stopped or started
  (`stopping_or_starting`, `Workspaces.any_powering`, which a Stop's and a Start's admission
  make true under the images' lock; see
  [runtime.md](runtime.md#stopping-and-starting-a-workspace)); otherwise it records the request and writes the request file (`write_request`:
  a write and a close, never through a link).
- **The freeze.** `Upgrades.under_way` is `Workspaces.upgrading`: a request held whose run has
  not written its status, within the grace or while systemd shows the service running, or a
  status of this boot that says `running` and that systemd does not contradict; it asks
  systemd only past the grace or for such a status, so a listing costs a file read.
  `Workspaces.create`, and so a clone, `Workspaces.rebuild`, `Workspaces.stop` and
  `Workspaces.start` ask it under the images' lock, and `ClaudeVersions.upgrade` after taking
  `_upgrading`, each refusing with `rcman_upgrading`; since the admission holds both locks from
  its checks to its record, a creation, a rebuild, a Stop, a Start or a roll-out is either seen by
  its checks or refused. `GET /api/workspaces` and the versions
  report carry it as `rcman_upgrading`, for the pages to say why they offer none of them.
- **Tests.** `tests/test_rcman_upgrade.py` holds the report with the option off and from a
  finished run's status, a run's state from recorded `systemctl show` output (`shown()`, which
  the real-machine checklist compares with the real thing), `systemctl show` run with its
  fixed argv and environment, the release read once, a request admitted and followed to its
  run's end, and `rcman_upgrading` from a creation, a clone and a roll-out.

## Monitor

The administrators' account is [the Monitor guide](../docs/monitor.md); what the runtime
runs for it is [runtime.md](runtime.md#what-the-monitor-reads). `monitor.py` holds the readers,
the settings and the collector, `Monitor(runtime, database, *, clock, wait, now)`, whose seams
are the monotonic clock, the wait between rounds and the wall time a round is stamped with.

- **The rounds.** `usage_round` reads `/proc/stat` (the change since the round before, as a
  percent of one core), `/proc/meminfo` (`MemTotal` less `MemAvailable`), the fourth field of
  `/proc/loadavg` (tasks, threads counting, which `hidepid` does not hide) and `statvfs` of the
  database's folder, `/` and Podman's storage (`filesystems`, one entry per device), and asks
  the runtime once for the slices of the people and the workspaces that are on
  (`show_slices`), matched to their blocks by `Id` (`read_units`): a block that cannot be read
  makes only its unit unknown. A slice's CPU (`unit_cpu`) is unknown on its first reading, when
  `CPUUsageNSec` dropped, when the slice was not active in the round before, and when it has
  stopped since; switching a scope off drops the readings its CPU is counted from. `disk_round`
  asks the runtime for `system df -v` (`read_disk`: the text tables, cut at the offsets of each
  header's columns, sizes decimal SI) and for `container inspect` of every workspace's container
  (`read_inspected`), and sums a workspace's volume, its `/tmp` volume and its container's layer
  where the container's labels name the workspace, its person and this database's install
  (`workspace_disk`). With the scope `storage` on, it also asks for `system df --format json`'s
  summary (`disk_summary`, read for the images' `RawSize` by `read_images_bytes`, which counts a
  layer two images share once and a layer that belongs to no image too) and for what the Claude
  Code store holds (`claude_versions_bytes`), and splits rcman's own use (`storage_figures`, kept
  under the key `storage`): the images; the workspace volumes, `rcman-ws-<id>` of the
  workspaces' rows and the `/tmp` volume inspect names for each of their containers; every other
  volume; the Claude Code versions; and their total. With `storage` off the summary and the walk
  do not run. The disk round runs when `people`, `containers` or `storage` is on
  (`MonitorSettings.disk_on`). Each disk read that fails is tried once more (`DISK_ATTEMPTS`),
  one that worked not again; a round that fails, in any part, keeps all the figures of the last
  one that did not, and its `collected_at`. Each round
  catches `Exception`, keeps its own `error`, and logs on `rcman.monitor` only when its failure
  starts, changes or ends. An unknown figure is `None`, never 0.
- **The settings.** `MonitorSettings`: the two intervals, with their defaults and bounds
  (`Field(ge, le)` on a `StrictInt`), and the scopes `SCOPES` (`StrictBool`); every one is sent
  on a save. They are kept in the table `monitor_settings`, one row (`id = 1`) the first save
  makes, a column for each field, which `read_settings` and `write_settings` take from the
  model's fields; no row is `DEFAULTS`. `Monitor.changed` takes up a save: a new generation, the figures
  of a scope now off dropped, and both loops woken with `loop.call_soon_threadsafe`. A round
  takes the settings and the generation as it starts, and what it read of a scope that is off
  by its end is dropped (`_keep`).
- **The loops.** `Monitor.start()`, from the lifespan, makes one task per round on the
  service's event loop. Each runs its round in a thread (`_in_thread`) and awaits it, so it
  never runs two at once; with the host on, the usage loop asks the runtime where Podman's
  storage is (`prepare`, one `podman info`) before its round until it knows, which is outside
  the rounds and so outside the cost guard below. A failure of a loop outside its round is
  logged when it starts or changes. The first usage round runs once the workspaces
  are restored, which the restore tells from its thread: the lifespan sets
  `Workspaces.on_restored` to `Monitor.restored_now`, which sets the loops' event through
  `loop.call_soon_threadsafe`, or, before `start`, has `start` make it set; the first disk round
  runs one disk interval after that. A loop's next round is due
  its interval after its last one ended, which a save does not move, and a loop with nothing on
  waits for a save. `Monitor.stop`, which the lifespan awaits before anything closes, sets
  `halted` (which kills a disk read and starts no round), cancels both tasks and awaits a round
  under way.
- **Refresh.** `Monitor.refresh()`, from any thread, marks each round that is on as asked for
  unless it is running (`_asked`, `_running`, under `_lock`), and wakes both loops with
  `loop.call_soon_threadsafe`; it does nothing once halted. A loop takes an asked round as due
  now, and clears asked and sets running in one `_lock` section before `_in_thread`, so a press
  while a round runs or is asked for joins it and no second disk round starts. Asked and running
  are cleared when the round ends, on the loop's branch for a round that is off, on its
  exception path and when the loop ends, so `snapshot()["refreshing"]`, true while either is set
  for either round, cannot stay true. A failure of the loop itself, outside its round (such as
  the settings not read), is kept as the round's `error` as well as logged. The next timed round
  counts from the refreshed one's end. A press during a timed round joins it with the settings
  that round took as it started, so a scope switched on after it began, Storage say, stays
  unknown until the next round.
- **The routes**, for administrators (`routers/monitor.py`): `GET /api/monitor` is
  `Monitor.snapshot`, built from the database's people and workspaces as they are now and the
  figures kept; `GET /api/monitor/settings` answers `{"settings": ...}`; `PUT
  /api/monitor/settings` saves the whole set (`Monitor.save`) and answers the same, audited as
  `monitor.settings` with the settings as JSON. A value outside its bounds, or of the wrong
  type, is refused at its field. `POST /api/monitor/refresh` calls `Monitor.refresh` and answers
  202 `{"refreshing": true}`, audited as `monitor.refresh`; what the rounds read, or why they
  failed, comes with the snapshot.

`tests/test_monitor.py` holds the readers to the recordings, the rounds on the host runtime, the
loops on a clock and a wait of its own, and what a round costs: with `subprocess` stubbed on a
`PodmanRuntime`, a usage round runs `systemctl` once and Podman never, a disk round one `system
df --verbose`, one `container inspect` and, with `storage` on, one `system df --format json` and
no `systemctl`; a planted runtime that asks `systemctl` once per slice fails it.
`tests/test_monitor_routes.py` holds the routes: a save of the settings, the snapshot's shape,
and a press of Refresh that runs the rounds at once. The page that changes the settings is
**Service settings** ([the guide](../docs/service-settings.md#monitoring)).

## Port forwarding's settings

The administrators' account is [Port forwarding](../docs/service-settings.md#port-forwarding).
`forwarding_settings.py` holds them; the forwarding server (see [Port forwarding](#port-forwarding))
reads them on every request, and hears through the store's listener of forwarding being turned
off.

- **The settings.** `ForwardingSettings`: `enabled` (`StrictBool`), `idle_minutes` and
  `absolute_hours` (`Field(ge, le)` on a `StrictInt`), with their defaults (`DEFAULTS`) and
  bounds in the module; every one is sent on a save. An idle timeout as long as the lifetime or
  longer is refused at `idle_minutes` with `IDLE_NOT_SHORTER` (a field validator, so the
  lifetime is declared first and the check is skipped where the lifetime was refused itself).
  The model speaks in the units the page shows, so a value refused is refused with a bound in
  its own field's unit; `idle_seconds` and `absolute_seconds` are the same in seconds, which is
  what the table `forwarding_settings` keeps, one row (`id = 1`) the first save makes, each a
  whole number of its unit (a `check`); no row is `DEFAULTS`.
- **The store.** `Store` (`app.state.forwarding_settings`) reads them once and then answers from
  memory; `save` writes them and takes them up under a lock, so two saves take effect in the order
  they were stored. `on_off(listener)` registers a listener that every save leaving forwarding off
  calls, in the request's thread, once the settings are stored and taken up, in the order they were
  registered: the hook through which whatever forwards ends its sessions in the process. A listener
  that raises keeps none of the others from being called; the first exception is raised after them,
  so the save answers `internal_error` and is audited so, with the settings stored.
- **The routes**, for administrators (`routers/forwarding_settings.py`): `GET
  /api/forwarding/settings` answers `{"settings": ...}`; `PUT /api/forwarding/settings` saves the
  whole set (`Store.save`) and answers the same, audited as `forwarding.settings` with the settings
  as JSON. A value outside its bounds, or of the wrong type, is refused at its field.

`tests/test_forwarding_settings.py` holds a save and its audit; `tests/test_forward.py` what a
save that leaves forwarding off ends.

## The files settings

The administrators' account is [Files](../docs/service-settings.md#files). `files_settings.py`
holds them: `FilesSettings.folder_download_limit_mb`, a `StrictInt` within `Field(ge, le)`, in
decimal megabytes (`MEGABYTE`), 0 for no limit, with `DEFAULTS`; the table `files_settings` keeps
one row (`id = 1`) the first save makes, and no row is `DEFAULTS`. `Store`
(`app.state.files_settings`) reads them once and then answers from memory; a save takes them up
at once, for the next download's check. The routes, for administrators
(`routers/files_settings.py`): `GET /api/files/settings` answers `{"settings": ...}`; `PUT
/api/files/settings` saves the whole set and answers the same, audited as `files.settings` with
the settings as JSON. `tests/test_files.py` holds a save, its audit, and the limit's refusal.

## Port forwarding

A workspace's forwarded ports (`Workspaces.ports_of`, its plan's `ports`) are reached by their
owner's browser at `https://<port>-<workspace id>.ws.<DOMAIN>/`, DOMAIN the host of
`RCMAN_PUBLIC_ORIGIN`; the transport and its relays are in
[runtime.md](runtime.md#forwarded-ports), Caddy's two sites in
[installer.md](installer.md#forwarded-ports).

- **The server.** `forward.Forwarding` (`app.state.forward`), asyncio and h11 on the unix socket
  `RCMAN_FORWARD_SOCKET` (mode `0666`, in the folder only Caddy's group may enter), started by the
  lifespan once the restore is under way and stopped before the workspaces' service closes. It
  answers `HEALTH_PATH` on any Host; until `Workspaces.restored` is set it forwards nothing.
  `route` gives each request its handler (`HANDLERS`): the health, the ask, the way in, a
  refusal, or the proxy. Every request is judged on its own, never by its connection, which Caddy
  pools across hosts and people.
- **Hosts.** `Hosts` holds the canonical form: lowercase, decimal, no leading zero, a port up to
  65535; any other spelling is no port's, so no port has two certificates.
- **The ask.** A request whose Host is the ask site's address (`ASK_HOST`) for `ASK_PATH` with
  one `domain` is answered 200 only while forwarding is on and the restore is done, for a
  canonical host of a ready workspace's port, marked as wanted in the last `WANTED_SECONDS`
  (`mark_wanted`, which the listing and the hand-off call), and, for a host never approved
  before, while a token of `new_hosts` (a `ratelimit.TokenBuckets` of `NEW_HOSTS_BURST` and
  `NEW_HOSTS_PER_WEEK`) is left; 404 otherwise. Paths under `RESERVED` on a forward host are
  refused, but the way in.
- **The listing.** Each entry of a person's own workspaces (`owned` in `routers/workspaces.py`,
  for the listing and the entries making, cloning and rebuilding answer) has `ports`
  (`Forwarding.shown`): each port of its plan (`Workspaces.forwarded_ports`), its `label` (null
  where it has none) and its `url`, `https://<host>/` with the public origin's port where that is
  not the scheme's own; none while forwarding is off (`Forwarding.on`: the server serving and the
  setting on), for a workspace that is not ready, or for another person's. Each host listed is
  marked as wanted, its owner being shown its address, but while a rebuild replaces the
  container, when it cannot be reached. Nothing that lists another person's workspaces, as
  `everyone.py` does for administrators, gives or marks their ports. `tests/test_forward.py`
  holds the owner's listing.
- **The hand-off.** A navigation (`GET` or `HEAD`) with no session is answered 303 to
  `HAND_OFF_PATH` on rcman's origin, its path and query kept in `RETURN_COOKIE`. The hand-off
  (`routers/forward.py`), through oauth2-proxy, is refused unless `Sec-Fetch-Dest: document`,
  serves only a canonical host of the person's own ready workspace's port, an administrator held
  to their own ports alike, so that no one reaches another's port, marks it as wanted and mints
  a way in: a value of its own whose hash alone is kept, bound to person, workspace and port,
  spent once within `TOKEN_SECONDS`, with at most `MOST_OUTSTANDING_TOKENS` unspent and `mints`
  per person (429 beyond either). It is audited as `forward.hand_off`. Its page has its own
  `Referrer-Policy: strict-origin`, `Cache-Control: no-store` and a policy with `frame-ancestors
  'none'`, and a script carrying a nonce posts the way in to `ENTER_PATH` on the port's host,
  with a button where scripts do not run.
- **The way in.** `POST ENTER_PATH` must carry rcman's own Origin; the way in is spent, whatever
  comes of it, and the session (`SESSION_COOKIE`, `Secure; HttpOnly; Path=/; SameSite=Lax`) is
  made with the setting read again and nothing awaited before, so a switch-off meanwhile is not
  missed; a person holding `MOST_SESSIONS_PER_PERSON` already has one ended first: their oldest
  for the same host, or else their oldest of all. The
  way in waits `BODY_SECONDS` at most for its body, and answers 408 after. It is audited as
  `forward.session`. The answer clears the return cookie and sends the
  browser to its path, `/` for one that does not begin with `/`, or begins with `//` or `/\`.
- **Every request.** `fetch_refusal`: `Sec-Fetch-Site` `same-origin` or `none` passes,
  `same-site` and `cross-site` only for a top-level navigation `GET` or `HEAD`; without it, a
  request that is not `GET` or `HEAD` needs the host's own Origin; an upgrade or such a request
  with an Origin not the host's own is refused. Then the session for the host, and `refusal`: the
  person active, owning the workspace, ready with the port in its plan.
- **To the app and back.** `upstream_headers` drops the connection's own headers, the sign-in's
  identity headers and rcman's and oauth2-proxy's cookies, and makes the Host, and an Origin of
  the host's own, `localhost:<port>`; `downstream_headers` drops a `Set-Cookie` of rcman's names
  and adds `Content-Security-Policy: frame-ancestors 'self'`. A 101 is spliced raw
  (`_splice`), audited as `websocket.open` and `websocket.close`; plain proxied HTTP is a named
  exemption in `HANDLERS`.
- **Connections.** The server takes heads up to `MOST_HEAD` and answers a longer one 431, closes
  a connection with no request on it for `IDLE_CONNECTION_SECONDS` (longer than Caddy keeps an
  idle pooled one, so Caddy ends it first) or whose head takes longer than `HEAD_SECONDS` from
  its first byte. An answer of its own waits for the rest of a request's body only where it is
  short (`MOST_DRAINED`) and only for `DRAIN_SECONDS`; where the body has not all come by then,
  or is longer, the answer closes the connection (`Exchange.answer`).
- **Relays.** One per exchange, kept idle for the next request to its port
  (`MOST_IDLE_PER_PORT`, `POOL_IDLE_SECONDS`); a workspace's and a person's are capped
  (`MOST_RELAYS_PER_WORKSPACE`, `MOST_RELAYS_PER_PERSON`), idle ones ended first to make room,
  the room taken (`Slot`) with nothing awaited after the last count and given back if the relay
  does not start, so requests at once are held to the caps too; a request beyond is answered 503
  with `Retry-After`. A relay kept idle that turns out to have ended before it said anything is
  tried again once on a new one, where none of the request's body was spent on it and the
  request is a `REPLAYABLE` one, or its head was never handed to the relay, its writing begun:
  the app cannot have acted on it. A relay's input is closed only once the app's answer is
  passed back whole or the app has closed; a WebSocket whose browser has gone waits
  `SPLICE_DRAIN_SECONDS` for the app first.
- **Sessions** end after the idle timeout with nothing under way (an exchange or an open
  WebSocket counts, an idle connection does not), at their lifetime (`reap`, every
  `REAP_SECONDS`), when forwarding is turned off (`off`, the store's listener, which only passes
  the work to the server's loop, where the idle relays are ended too), when their workspace is
  deleted, rebuilt or stopped or their person removed (after the workspaces' `on_removing`,
  `on_rebuilding`, `on_stopping` and `on_person_deleting`; a request the forwarding server
  refuses ends its session as `workspace_stopped` for a workspace stopped or being stopped, and
  as `workspace_gone` otherwise), and for a newer one of their person's. A session's
  end (`_end_session`) closes its WebSockets and cancels its exchanges under way, which closes
  their browsers' connections, an answer still streaming among them; an exchange reads the
  session's end, and the setting, again after anything it awaits (`_gone`), so an upgrade the
  app switches once the session has ended is closed unswitched and unrecorded. Everything is in
  memory and lost when rcman restarts.

`tests/test_forward.py` holds the ask, the hand-off, the check of a request from another port's
host, what passes to the app and back, a WebSocket spliced through, a session ended when idle
and at its lifetime, the end of every way in when forwarding is turned off and no way in in any
address, log or audit row, on the host runtime.

## Microsoft sign-in

Microsoft (Entra ID) sign-in is an optional addition to Google's, which is always on through
oauth2-proxy. Administrators set it up and turn it on and off on the Service settings page
([docs/service-settings.md](../docs/service-settings.md#microsoft-sign-in)); the installer asks
nothing about it.

- **The settings** (`microsoft_settings.py`, `routers/microsoft_settings.py`): one row of
  `microsoft_sign_in`, read once into a `Store` and taken up at once by a save, as port
  forwarding's are. The IDs are held to `helpers.rules.GUID`, either empty, and kept in
  lowercase; the client secret, without the spaces around it, is sealed with the secret key
  (`sealing.STORED_SECRET`) and never given back: `GET /api/microsoft/settings` says
  `secret_set`, the `redirect_uri` (`RCMAN_PUBLIC_ORIGIN` and `microsoft.CALLBACK_PATH`) and
  whether the key is there (`available`). `PUT` sends `enabled`, both IDs and, to replace the
  stored one, `client_secret`. Turning sign-in on without both IDs and a secret, stored or sent,
  is refused at `enabled` (`problems.MICROSOFT_INCOMPLETE`). Without the key, a save that turns
  sign-in on or sends a secret is refused with `microsoft_unavailable` (409), since nothing can
  be sealed; one that leaves it off and sends no secret is taken. A save is audited as
  `microsoft.settings`, its target the IDs, `enabled` and `secret_changed`, never the secret.
- **The generation.** Every save that changes `enabled` or either ID increases `generation`. A
  session carries the generation it was issued under, and one that differs is refused. So a
  session is revoked only by removing the person or by such a save; a copied cookie lasts until
  it expires, as oauth2-proxy's does.
- **The secret key** (`RCMAN_SECRET_KEY`, in the form `helpers.rules.SECRET_KEY` holds it to,
  which `settings.py` checks without repeating a refused value in its error), which the
  installer generates and writes in `secret.env`. `sealing.py` derives a key for
  each use with HKDF-SHA256 from its bytes, under the use's label (`sealing.STORED_SECRET` for
  the stored client secret, `sealing.COOKIES` for both cookies), and seals with AES-256-GCM, a
  fresh nonce each time and the label as associated data. A key that is missing, or that cannot open the
  stored secret (replaced, or a database restored elsewhere), refuses every sign-in with
  `secret_unreadable` until the secret is entered again; with no key, the sign-in page offers
  Google alone.
- **The flow** (`microsoft.py`, `routers/auth.py`). MSAL's `ConfidentialClientApplication`, a
  fresh one for each request from the settings as they are then, every one sharing
  `HTTP_CACHE`, so the tenant's discovery is fetched once and MSAL's token cache keeps no one's
  tokens past the request; `MSAL_LOCK` is held while one is made and used, since MSAL locks
  that cache per application only. Each of MSAL's requests to Microsoft waits at most
  `MSAL_TIMEOUT_SECONDS`, and a sign-in waits for the lock at most `LOCK_SECONDS`, no longer,
  past which it is refused `idp_unreachable`: an outage at Microsoft cannot hold the server's
  shared threads. `GET /api/auth/microsoft/login?rd=` asks for `User.Read`
  with `prompt=select_account`, keeps MSAL's flow and the page to return to sealed in
  `__Host-rcman-ms-flow` (`kind` `flow`, `FLOW_SECONDS`, SameSite=Lax, which holds only because
  Microsoft comes back by a top-level `GET` with the code in the query), and answers 303 to
  Microsoft. The callback has MSAL check the state and the ID token's audience and nonce and
  redeem the code, then checks the claims; it issues `__Host-rcman-session` (`kind` `session`,
  the address and the generation, `SESSION_SECONDS`, Secure, HttpOnly, Path=/, SameSite=Lax) and
  answers 303 to the page, held to `forward.safe_return` again. Both routes answer
  `not_found` while sign-in is off. They share one `ratelimit.TokenBuckets` for everyone
  (`auth.new_limit`); past it, they answer 303 to the sign-in page with `too_many_requests` and
  a `Retry-After`. The sign-in page itself is not limited: it asks Microsoft nothing, and a
  limit there would keep people from Google's sign-in. A sign-in is not audited, as a Google
  one is not.
- **The refusals.** Each is a code the sign-in page words (`problems.SIGN_IN_REFUSALS`, and
  `SIGN_IN_UNFINISHED` for any other), sent back to `/oauth2/start?error=<code>` with the flow
  cookie deleted. In order, and with no person looked up before the tenant is checked:
  `wrong_tenant` (`tid` is not the configured tenant, compared in lowercase), `acct_missing`
  (no `acct` claim, which the app registration must send), `guest_account` (`acct` is not
  exactly the integer 0), `no_address` (`preferred_username`, lowercased, is not an address by
  `helpers.rules.EMAIL`; the `email` claim, an attribute any tenant's administrator may set,
  is never read), and `not_allowed` (no active person has the address). The other
  failures are fixed codes too: a flow cookie missing, forged or expired is `bad_state` or
  `expired_state` (`read_state_cookie`); an exception of MSAL's while redeeming is
  `bad_state`, `nonce_mismatch`, `token_invalid` or `idp_unreachable` (`_failure_code`,
  `token_invalid` for one it does not know); the token endpoint's own error is
  `exchange_failed`; a wait for `MSAL_LOCK` that runs out is `idp_unreachable`; Microsoft's own
  `error` is `access_denied` or `idp_error` (`callback_error_code`); a secret that cannot be
  opened is `secret_unreadable`; and any other failure is `sso_failed`. What is logged, and
  what is not, is in [docs/journal.md](../docs/journal.md): a code with an exception's class,
  the token endpoint's error code, or `reason=busy`, never MSAL's text, since MSAL writes the ID
  token's claims into its exceptions and its loggers, which are set above `CRITICAL`. The callback's code and state
  are in Caddy's access log and uvicorn's lines, as Google's callback's are.
- **The sign-in page**, `GET /oauth2/start?rd=`, public and outside the published API. Caddy
  sends a browser without a sign-in there, with the identity header taken off. While sign-in is
  off, or the key is missing, it answers 303 to oauth2-proxy's `/oauth2/sign_in?rd=`, whose
  `skip_provider_button` starts Google's sign-in at once. Otherwise it is a page without a
  script: rcman's top bar, its mark and the page's name, with no menu button, so on a phone it
  starts at the page's gutter, an `h1`, and a card with the refusal
  named by `?error=` (looked up, never echoed) as a `role="alert"` message bar, with the app's
  problem icon drawn inline (`auth.PROBLEM_ICON`, the frontend's `ProblemIcon`), and
  **Sign in with Google** and **Sign in with Microsoft** as two plain links. `rd` passes
  `forward.safe_return` first, and each address is built as
  `html.escape(base + "?rd=" + urllib.parse.quote(rd, safe="/"), quote=True)`, the 303's
  `Location` encoded the same way. Its own `Content-Security-Policy` (`auth.POLICY`: nothing
  but inline styles and images of its own, no form, no base, no framing) and
  `Cache-Control: no-store`; `headers.SecurityHeaders` keeps a header an answer sets itself, so
  the policy repeats `frame-ancestors 'none'`. Its words are the backend's (`SIGN_IN_PAGE`). It
  inlines the built stylesheet `index.html` names and copies its `theme-color` metas, read
  again only when `index.html` changes; without a build, it is unstyled.
- **Trust.** An administrator who points rcman at a tenant they control, with a verified domain,
  can sign in as anyone rcman lists at that domain's addresses; the change is audited, and
  administrators can already add and remove people. A reused UPN inherits its address's place.

## Attention

`routers/attention.py` (`GET /api/attention`, for administrators) reports the
servers on a screen rcman does not recognise, by person and workspace, from
`everyone.everyone`. It is built on every request and never stored, and it
carries no screen text. A server action that ends on an unrecognised screen
sets `Record.result` to `unrecognised_screen`, which the audit row records in
place of `ok`.

## Audit

A route that changes something depends on `Recorder("<action>")`, which writes one row when the
request finishes, with `ok` or the error code, and logs the same event as a JSON line on the
`rcman.audit` logger. A WebSocket route depends on `Session("<action>")` instead, which writes
`<action>.open` when the socket opens or is refused once accepted, and `<action>.close` when one
that opened closes; whatever the route has not recorded by the time it ends is recorded then.
uvicorn configures only its own loggers, so `create_app` calls `journal.install` on each logger
whose events go to the journal — `rcman.audit`, `rcman.client`, `rcman.unrecognised`,
`rcman.workspaces`, `rcman.claude_versions`, `rcman.terminals`, `rcman.monitor`,
`rcman.forward`, `rcman.forwards`, `rcman.microsoft` and `rcman.transfers` — which gives it one handler writing the bare line to
standard error; systemd sends it to the journal. `tests/test_journal.py` checks that the app installs each of them.
`tests/test_audit.py` starts the app with the unit's own `ExecStart` and reads the line from its
output. It also finds every changing route in the route table and fails on any that neither
records nor is listed in `audit.EXEMPT` with a reason that also appears in [the audit
guide](../docs/audit.md), and on any WebSocket route (`routes.websocket_routes`) without a
`Session`; it then drives every audited route and checks its row, and holds the guide's table of
actions to the actions the routes and the forwarding server's handlers record
(`forward.HANDLERS`, held by `tests/test_forward.py` to the handlers the server serves, each
recording or exempt with a reason the guide gives). The results a row can have besides an error
code are `audit.RESULTS`, and a test holds the guide's results table to them. The table is
append-only: a trigger in `db.py`'s migrations aborts any `UPDATE`, so rows are only added, and
deleted only by retention (see [the audit guide](../docs/audit.md#how-long-events-are-kept)),
which `create_app`'s lifespan runs as the app starts and every `RETENTION_INTERVAL_SECONDS`
after.

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
each line names who sent it. A person's reports are limited by a token bucket (`REPORTS_BURST`, refilled at
`REPORTS_PER_MINUTE`, `ratelimit.TokenBuckets` on `app.state.client_error_limit`): the next is
refused with `too_many_reports` (429, `Retry-After`) and writes nothing, and the page drops what
rcman refuses. journald's own rate limit and size limits bound the rest.

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
