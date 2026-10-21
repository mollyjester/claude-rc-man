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
| `main.py` | `create_app(settings, runtime, *, builder_slot)`: the app, its lifespan (migrations, the allowed list, audit retention, restoring workspaces) and its routers; with rcman's own Podman, `check_seccomp` first, which says in the journal when the workspaces' seccomp profile is missing or stale (see [runtime.md](runtime.md#containers-in-a-workspace)); and `check_instructions`, which says there when the instructions for Claude are missing from the tools (see [runtime.md](runtime.md#workspace-instructions)) |
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
| `headers.py` | The security headers every answer carries (see [Requests](#requests)) |
| `ratelimit.py`, `filelock.py` | A token bucket for each name (the limits on a page's reports and on requests for an upgrade of rcman), and a re-entrant lock that is also a lock file, shared with `rcman-admin` (see [Limits](#limits)) |
| `spa.py` | Serving the built frontend: its files, and `index.html` for every other path not under `/api`; installed after every router. `index.html` names the build's files by the hashes of their contents, which Vite puts in their names under `assets/`, so every file outside `assets/`, `index.html` among them, is served with `Cache-Control: no-cache`, which has the browser ask again before it uses the one it kept, and a page loaded after an upgrade names the new build's files; those under `assets/`, which never change, are kept for a year (`immutable`) |
| `routes.py` | The app's routes as FastAPI resolves them after including routers, and which of them are for administrators |
| `routers/` | One module per area: `me`, `workspaces`, `servers`, `git`, `github`, `claude`, `people`, `audit_log`, `attention`, `docs`, `client_errors`, `terminal`, `rcman_upgrade` |
| `routers/workspaces.py` | The person's workspaces, under `/api/workspaces`: listing them with their servers, making one, deleting one, telling whether one's devcontainer.json changed and rebuilding one (see [Workspaces and servers](#workspaces-and-servers)); the listing is an object, `workspaces`, `claude_approved` (the version a server started now would run, `null` where none is), so that the page can set each server's `claude_version` beside it, and `rcman_upgrading`, whether an upgrade of rcman is under way (see [Upgrading rcman](#upgrading-rcman)) |
| `routers/servers.py` | A workspace's server, under `/api/servers`: start, resume, stop, forget, confirm and output, each by the workspace's id; and everyone's, for administrators |
| `accounts.py`, `routers/claude.py` | Each person's Claude sign-in, signing in and out of it, and its state; `routers/me.py` signs out of rcman — see [Claude accounts](#claude-accounts) |
| `repository.py` | A workspace's git repository: the verbs of the Changes section, run in the workspace through its channel and `exec`, each wrapped in the workspace's `timeout` tool (see [git in workspaces](#git-in-workspaces)) |
| `gitaccount.py` | A person's commit author and GitHub token, kept in SQLite and written into their volume's git folder (see [git in workspaces](#git-in-workspaces)) |
| `routers/git.py`, `routers/github.py` | The git routes, by workspace id, and the person's GitHub token (see [git in workspaces](#git-in-workspaces)) |
| `claude_versions.py` | `ClaudeVersions`: the approvals, the store, rolling out a version, pruning, and the report of the versions page (see [Claude Code versions](#claude-code-versions)) |
| `routers/client_errors.py` | A page's reports of failures rcman could not see, `POST /api/client-errors`, written to the journal (see [Errors from the page](#errors-from-the-page)) |
| `terminals.py`, `routers/terminal.py` | The Terminal tab: a shell in a workspace, carried over a WebSocket (see [The terminal](#the-terminal)) |
| `rcman_upgrade.py`, `routers/rcman_upgrade.py` | Upgrading rcman from its web interface, rcman's side: the release it runs, the report, the request and its admission, and the freeze while one is under way (see [Upgrading rcman](#upgrading-rcman)) |
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
- **Origin.** A `POST`, `PUT`, `PATCH` or `DELETE`, and every upgrade to a WebSocket, must
  carry an `Origin` header equal to `RCMAN_PUBLIC_ORIGIN` (`identity.changing`): a page of any
  site may open a WebSocket, and the browser sends rcman's cookies with it.
- **Headers.** Every answer carries `headers.SECURITY_HEADERS` (`SecurityHeaders`, an ASGI
  middleware installed by `create_app`): a policy that forbids framing and nothing more, so
  that the interactive description of the API, which loads scripts of its own, works;
  `X-Frame-Options`, `X-Content-Type-Options` and `Referrer-Policy`. Neither Caddy nor
  oauth2-proxy adds them for what rcman serves. An answer that sets one keeps its own. A refused
  upgrade to a WebSocket is an answer over HTTP and carries them too.
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
  `timeout` (`workspaces.answered`). An upgrade to a WebSocket refused before it is accepted -
  for its identity, its origin or its query - is answered over HTTP with the problem, as a
  request is; a WebSocket accepted is refused with a frame of its own (see
  [The terminal](#the-terminal)).

## Workspaces and servers

`create_app` takes the runtime workspaces run in: by default Podman, through
rcman's own user manager's socket (`main.podman_runtime`); the tests and
`make e2e` give it the host runtime. `builder_slot`, the slot of builds and the canary, is the
product's unless a test engine gives its own (see [runtime.md](runtime.md#the-runtime)). Everything about workspaces, their
channels and servers, and what rcman brings back when it starts, is in
[runtime.md](runtime.md).

A workspace belongs to one person, and a route reaches only the signed-in
person's: another's is `workspace_not_found`, as if it were not there. A
server's verbs need the workspace `ready`, or answer `workspace_not_ready`, and not
having its container replaced by a rebuild, or answer `workspace_rebuilding`, which a start,
a resume, a stop, a forget and a confirm ask again under the workspace's lock. The
prefix of a server's sessions is the person's, or the workspace's name while
they have set none (`prefix_of`). A start, a resume, a stop and a forget record
what should run in the workspace's row, which restoring reads. The listing
reads every workspace's server side by side, in at most `LISTING_WORKERS`
threads; a workspace whose server cannot be read is listed with why, as
`server_error`, and one whose container a rebuild is replacing is not read and is listed with
`workspace_rebuilding` there.

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
  route makes. A failed workspace's rebuild that failed says why in its `error`.

`everyone.py` lists every person's workspaces for administrators
(`GET /api/servers/everyone`), each workspace with only its id, name, state, server,
`server_error` and `claude_version`, copied one by one from the listing, so nothing
else the listing answers reaches it, and each server with only `SERVER_FIELDS` —
never its screen or its claude.ai address. The listing is read-only: no route lets one
person act on another's workspaces.

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
values set (see [the audit guide](../docs/audit.md)). `tests/test_limits.py`,
`tests/test_workspace_limits.py` and `tests/test_people.py` hold them.

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
  running servers unless `keep_sessions`, the default, and answers
  oauth2-proxy's sign-out address, which the page goes to.

## git in workspaces

`routers/git.py` names a workspace by its id (`workspace`, a query parameter or a
body field, never a path) and runs git in it. `_run` finds the workspace among the
signed-in person's ready ones (`Workspaces.ready`: `workspace_not_found` for another's
or none, `workspace_not_ready` otherwise, `workspace_rebuilding` while a rebuild replaces
its container), and calls a verb of `repository.Repository` on
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
  workspace from the image it names or builds (see
  [runtime.md](runtime.md#a-repositorys-devcontainerjson) and
  [Building an image](runtime.md#building-an-image)). Its plan is the `environment`
  column of `workspaces` (null for a workspace of the default image), which a restore
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
  not ready is `workspace_not_ready`, and one a rebuild replaces the container of
  `workspace_rebuilding` (`Workspaces.ready`). Each is a `problem` frame, whose
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
  `workspace_rebuilding`); and, every
  `REVOCATION_SECONDS`, a look at the table finding the person no longer active (`not_allowed`)
  or the workspace no longer theirs and ready (`workspace_gone`), or being rebuilt
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
  already; `--ws` names it all the same in the unit's `ExecStart` and in the browser tests'
  backend (`frontend/playwright.config.ts`), so that which one runs is written down, and
  `tests/test_terminal.py` holds the two to each other and to `WEBSOCKETS`.
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
| `reasons` | `terminal_replaced`, `workspace_gone`, `workspace_rebuilding`, `signed_out`, `not_allowed`, `service_closing` | What a `closed` frame says |

Binary frames are what is typed and what the shell prints, as bytes, both ways.

## Claude Code versions

`claude_versions.ClaudeVersions` (`Workspaces.versions`, also `app.state.versions`) owns
the store, `claude_store`: `versions/<version>`, `current` and `bin/claude`. The
installer makes the store and rcman writes it from then on (see
[the installer](installer.md#what-workspaces-are-made-of)). Approvals are the append-only table
`claude_approvals` (triggers refuse an update and a delete): the latest is the approved
version, the previous is the latest one naming another, and a rollback is a new row. The
database is the truth and `current` follows it (`reconcile`, run when rcman starts, before
the restore); with no approval yet, `reconcile` adopts what `current` names. A workspace's
row records the version its server started with (`claude_version`).

- **Pinning.** `Workspaces.start_server` resolves the approved version (`resolve`), records
  it on the row and starts the server with a layout that runs that exact file
  (`Layout.pinned`, see [runtime.md](runtime.md)). `ClaudeVersions.lock` is held from the
  resolve to the record, and the pruning holds it too, so a version is never removed between
  the two. A stop or a forget clears the record. Restore starts servers by the same path.
  Nothing restarts a running server.
- **Rolling out.** `POST /api/claude/upgrade {version}` (administrators, audited as
  `claude.upgrade` with the version as target) is `upgrade`: synchronous, one at a time
  (`claude_upgrade_busy`), and none while an upgrade of rcman is under way (`rcman_upgrading`,
  see [Upgrading rcman](#upgrading-rcman)). The candidate is staged outside the store, in a folder of its own
  in `STAGING_FOLDER` beside the database, fetched by `helpers/claude_release.py` (streamed,
  hashed, size-capped, renamed into place only whole; the installer fetches the tested version
  with the same code) or copied from the store; run through the canary
  (`Runtime.run_canary`, see [runtime.md](runtime.md#the-canary)); and only on a pass copied into
  the store under a partial name and renamed. A version that was approved before and is still in
  the store needs neither download nor canary, since it passed then: a rollback or re-approval
  works with Podman unavailable, and a version never approved always goes through the canary.
  The commit (`_commit`) switches `current` first and records the approval only afterwards, so
  what is approved is what the link names: if the switch fails nothing is approved, and if the
  recording fails the link is put back. After a crash the start of rcman repoints the link at
  the approved version (`reconcile`; the database wins). The version field is trimmed and
  validated before anything is done. A file of no length in the store is no version (`sweep`
  removes it). A refusal or failure leaves the store and the approvals as they were:
  `claude_install_failed` (whose title says why, by
  `problems.CLAUDE_INSTALL_MESSAGES`; status 502 for the kind `network`, 409 for the others, by
  `CLAUDE_INSTALL_STATUS`), `claude_checks_failed` (the failing checks with their
  reports in the problem's extra) and `claude_canary_failed` (the canary could not run).
  The checksum comes from the release site with the program: it protects against corruption,
  not against a compromised source, and nothing checks a signature.
- **Pruning.** `prune` removes the versions that are neither approved, previous, tested
  (`helpers/claude-version`) nor the one `current` names and that no server in
  `Workspaces.pinned_servers` uses (a server that runs, cannot be read, or is being started;
  tmux is asked whatever the row's `server` flag says, since the record of a version and the
  flag are two writes, and only tmux saying there is none frees the version). `rcman-admin`
  does not prune or collect images (`Workspaces(prunes=False)`): its locks are not the
  service's, which does at the next stop, roll-out or start. A version put in the store less
  than `PRUNE_GRACE_SECONDS` ago stays: an upgrade of rcman puts its tested version there while
  the release before it runs, which does not know it. It asks the servers for their status without holding
  `lock`, then decides again under it, also keeping a version whose workspace record changed
  meanwhile; it does nothing while a roll-out runs. It runs after a roll-out, after a server
  is stopped or forgotten (`Workspaces._stopped`) and in the restore, with `sweep`,
  which removes what a roll-out cut short left: staging folders, partial files and a canary.
- **Reading.** `GET /api/claude/versions` is `report`: approved, previous and tested
  (`helpers/claude-version`) versions, the installed ones with whether each is approved or
  previous and how many servers use it, the drift (running servers on another version) and
  whether a roll-out runs (`upgrading`) or an upgrade of rcman is under way (`rcman_upgrading`).
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
  status's `started`, `finished`, `from`, `to`, `notices` and `error`, the same keys whatever
  the state. `reason` is `null`, or the problem a request would be refused with now:
  `rcman_upgrade_refused` with its `reason`, one of `problems.UPGRADE_REFUSED_MESSAGES`, whose
  words are its title. With the option off it is `disabled`, and systemd is not asked.
- **Admission.** `POST /api/rcman/upgrade` (audited as `rcman.upgrade`; each administrator
  limited by a token bucket of `REQUESTS_BURST`, refilled at `REQUESTS_PER_MINUTE`,
  `too_many_requests` with `Retry-After` past it) answers 202 with the report. `request`
  holds `Workspaces.recording_held`, the images' lock, which every workspace is recorded
  under, waiting at most `ADMISSION_WAIT` (`images` otherwise), then
  `ClaudeVersions.rollouts_held`, the roll-outs' lock, without waiting (`rolling_out`: a
  roll-out, or the `sweep` of what one left, holds it); holding both,
  it refuses while systemd cannot be read (`units_unreadable`), the path unit is not loaded and
  active (`path_inactive`), an upgrade is under way (`under_way`), a workspace is being made
  (`creating`) or one is being rebuilt (`rebuilding`, `Workspaces.any_rebuilding`; see
  [runtime.md](runtime.md#rebuilding)); otherwise it records the request and writes the request file (`write_request`:
  a write and a close, never through a link).
- **The freeze.** `Upgrades.under_way` is `Workspaces.upgrading`: a request held whose run has
  not written its status, within the grace or while systemd shows the service running, or a
  status of this boot that says `running` and that systemd does not contradict; it asks
  systemd only past the grace or for such a status, so a listing costs a file read.
  `Workspaces.create`, and so a clone, and `Workspaces.rebuild` ask it under the images' lock,
  and `ClaudeVersions.upgrade` after taking `_upgrading`, each refusing with `rcman_upgrading`;
  since the admission holds both locks from its checks to its record, a creation, a rebuild or
  a roll-out is either seen by its checks or refused. `GET /api/workspaces` and the versions
  report carry it as `rcman_upgrading`, for the pages to say why they offer none of them.
- **Tests.** `tests/test_rcman_upgrade.py` holds the report's shape and every reason, the states
  from recorded `systemctl show` output (`shown()`, which the real-machine checklist compares
  with the real thing), malformed output and status, the release read once, the admission's
  race and locks, `rcman_upgrading` from a creation, a clone and a roll-out, and an upgrade and a
  rebuild each refusing the other.

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
same event as a JSON line on the `rcman.audit` logger. A WebSocket route depends on
`Session("<action>")` instead, which writes `<action>.open` when the socket opens or is refused
once accepted, and `<action>.close` when one that opened closes; whatever the route has not
recorded by the time it ends is recorded then. uvicorn configures only
its own loggers, so `create_app` calls `journal.install` on each logger whose
events go to the journal — `rcman.audit`, `rcman.client`, `rcman.unrecognised`,
`rcman.workspaces`, `rcman.claude_versions` and `rcman.terminals` — which gives it one handler writing the bare line to
standard error; systemd sends it to the journal. `tests/test_journal.py` checks
that the app installs each of them. `tests/test_audit.py` starts the app with the unit's own
`ExecStart` and reads the line from its output. It also finds every changing
route in the route table and fails on any that neither records nor is listed
in `audit.EXEMPT` with a reason that also appears in
[the audit guide](../docs/audit.md), and on any WebSocket route (`routes.websocket_routes`)
without a `Session`; it then drives every audited route and
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
