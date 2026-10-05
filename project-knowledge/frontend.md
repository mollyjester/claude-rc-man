# Frontend

A React application in `frontend/`, built with Vite and TypeScript into
`frontend/dist`, which the backend serves (`backend/rcman/spa.py`): its files
as they are, and `index.html` for every other path outside `/api/`. The Node.js
version is `frontend/.nvmrc`.

The `make` targets that set up, build and test it are in
[tests.md](tests.md#running). `cd frontend && npm run dev` runs a development
server that forwards `/api` to a local backend, at the address
`frontend/vite.config.ts` names.

## Formatting and lint

[Biome](https://biomejs.dev) formats and lints the frontend: its TypeScript,
its stylesheet and its JSON files; it lints `index.html` too, whose
formatting, as HTML's, Biome leaves alone by default. `frontend/biome.jsonc` names the files it
covers and holds every setting that differs from Biome's defaults, each with
its reason. Its version is pinned exactly in `frontend/package.json`, so a new
release of Biome cannot change what passes.

In `frontend/`, `npm run lint` checks without changing anything: formatting,
the lint rules and the order of imports. Any finding fails it: an error or a
warning does, and the recommended rules Biome reports as infos, which would
not, are raised to warnings by name in `frontend/biome.jsonc`; a test in
`tests/test_make.py` asks Biome which rules it runs at info and fails on one
left out. `npm run format` applies the
formatting, the order of imports and Biome's safe fixes. `make lint` and
`make format` run them after ruff (see [tests.md](tests.md#running)).

A finding is answered first by fixing the code. Where a rule is wrong for
one place, it is suppressed there, with the reason after the colon:
`// biome-ignore lint/<group>/<rule>: <reason>` — in JSX
`{/* biome-ignore lint/<group>/<rule>: <reason> */}`, and in CSS a
`/* … */` comment of the same form. Biome refuses a suppression without a
reason, and reports one that no longer suppresses anything, so a suppression
fails the check once its subject is gone. Where a rule is wrong for whole
files, it is switched off or scoped in `frontend/biome.jsonc`, with a comment
giving the reason; Biome does not report a setting there that no longer
matters, so a suppression at the site is preferred.

## Rules it keeps

- **The server validates; the page shows what it said.** `src/api.ts` is the
  only way to the backend; every failure the server describes becomes an
  `ApiError` carrying the problem details as sent, built by `problemFrom`, which also turns the
  problem details an answer reports, as Commit & Push's `push_problem`,
  into one; `src/ProblemView.tsx` shows them — the title,
  each field error, and a raw screen, failed checks with what each ran and
  got back under its name and how many lines were not shown, the server a problem is
  about with its screen, what a delete left, or what git said and how many
  lines more, when the server passes one.
  Each input carries its field's name in its `name` attribute, and a form
  passes `ProblemView` its labels by field name, so a field error is named as
  the form names the field, and by the server's name for it where the form
  has no label; `src/forms.test.tsx` refuses every field of every form and
  checks each error is shown under its input's label.
  Forms are `noValidate` and no input carries a validation attribute;
  `src/forms.test.tsx` checks every form.
- **Every failure is an `ApiError`, and the page names its own.** `request()`
  in `src/api.ts` gives a failure that is not the server's problem details one
  of the page's own codes, each with its message in `src/messages.ts`:
  `UNREACHABLE` when `fetch` rejects, or when the answer is a 502, 503 or 504
  that is not JSON — the proxy in front of rcman saying it could not reach it,
  since rcman's own answers are JSON; `SIGNED_OUT` when the answer is a
  redirect, which oauth2-proxy sends once the sign-in has ended and rcman's own
  routes never do, seen as an `opaqueredirect` because every request is sent
  with `redirect: "manual"`; and `UNEXPECTED_RESPONSE` for an answer that is
  not JSON, or an error without the server's `code`. A request's body is
  serialised before the request is sent and outside the `try` around it, so a
  body that cannot be serialised is a fault in the page, not `UNREACHABLE`.
  `asApiError` in `src/ProblemView.tsx` turns anything that is not an
  `ApiError` into `UNEXPECTED_RESPONSE`, and notes it as a `page_error`.
- **Only what the server allows is offered.** `ACTIONS` in `src/Servers.tsx`
  maps every server state rc-helper reports to the actions it allows; its
  states are kept to the table in [rc-helper.md](rc-helper.md#server-states)
  by hand, and `src/Dashboard.test.tsx` checks that each card offers exactly
  its state's actions. Beside it, `KIND` gives each state its badge colour,
  `PRIMARY` the one action it leads with, and `m.states` and `m.nextStep` its
  name and what to do; `src/Servers.test.ts` holds each of them to the states
  in `ACTIONS`, and `src/servers-guide.test.ts` holds the table in
  `docs/servers.md` to all of them, and checks that every control its
  *The Servers page* section names in bold is text from `src/messages.ts`. `START_REASON` in `src/Claude.tsx` says,
  for each state of the Claude account in `CONNECTION_STATES`, why no server
  can start, or null where one can; Start, and the actions that start Claude
  again, are offered only where it is null. Otherwise it says why in place of
  each: in the panel instead of Start, and on a server's card instead of the
  sentence saying what to do. The Changes section is shown only for a
  folder the tree lists as a repository; Commit is offered only with
  something staged and a commit author name set, and otherwise the section
  says why; a push can be tried on a branch that, as the status says, has
  a remote to push to (`push_has_remote`), and, when the status says it
  goes to GitHub (`push_to_github`), only while the same status says a
  token is stored (`github_token`), so a token git forgot after GitHub
  refused it withdraws it at the next refresh; otherwise the section says
  why, linking to Settings for the token; Push is offered where a push can
  be tried, the branch has a commit (`branch.commit`), which the section
  otherwise says it lacks, and where the branch pushes lacks a commit of
  it or is unknown (`push_ahead`), and Commit's menu, which holds Commit &
  Push, where a push can be tried and Commit is offered; Switch is offered for a local branch other than the current one
  and for a remote branch no local branch tracks and whose name no local
  branch takes (`taken_by`), which it says otherwise; Stage selected and
  Unstage selected are shown only while their list has files, and enabled
  only while something is checked. The branch bar shows how far the branch
  is from its upstream only where it has one, and says where that upstream
  is gone (`ahead` and `behind` null); with no upstream it shows nothing,
  since the status says only whether one is set, not whether the branch is
  on the remote. git's own refusals, which
  rcman cannot know in advance, are shown as git gave them. Clone from
  GitHub is always offered, since a public repository needs no token; a
  private one GitHub refuses without one is git's refusal, shown as such. Delete folder is offered only for a folder,
  never for the home folder, and only once the servers are listed, since its
  confirmation names those it stops and forgets. Administration is offered
  only to administrators, and the server refuses everyone else regardless.
- **Each form leads with what it does.** Its submit button is its one
  `primary` button, or its one `danger` button where what it does cannot be
  undone, such as deleting a folder; its other buttons are plain or
  `secondary`, such as Commit & Push in Commit's menu, and a dialog's choice is
  a form too. `src/forms.test.tsx` checks every form, and
  that only the forms it lists as destructive use `danger`. A button outside
  a form that opens such a form, such as a panel's **Delete folder** or
  Settings' **Remove token**, is a
  danger outline, `secondary-danger`; `src/forms.test.tsx` checks the button
  that opens each form it lists as destructive. `src/styles.test.ts` checks
  that both look unlike any other button in both schemes.
- **Every piece of text not from the server is in `src/messages.ts`.**
- **Every colour is a custom property in `src/styles.css`**, with a value for
  a light scheme and one for a dark scheme, which the browser picks between by
  the system's setting; `src/styles.test.ts` holds the stylesheet to that and
  names the colours kept the same in both. It counts a word as a colour where
  jsdom's colour parser reads it as one, and only in custom properties and in
  properties that take a colour, since the parser also reads words such as
  `background` as system colours. Each pair of colours text is drawn in meets
  WCAG AA contrast in both schemes; that is checked by hand when a colour
  changes.
- **Each kind of status dot has its own shape.** `src/servers-guide.test.ts`
  reads each `.dot-<kind>` rule in `src/styles.css`, requires the shapes to
  differ, and holds the shape table in `docs/servers.md` to them.
- Links between help pages — a page's file name, optionally followed by a
  `#part` — stay inside rcman; other links open in a new tab.

## Layout

| File | What it is |
|---|---|
| `src/App.tsx` | Who is signed in, the navigation, whose **Admin** menu is a `usePopup` that also closes when a page is chosen and when the page changes, and the routes |
| `src/AdminPages.tsx` | `ADMIN`, the administrators' pages by name, which the links in the administrators' warning use, and `ADMIN_PAGES`, the same pages in order, which the navigation's **Admin** menu and the routes are built from |
| `src/Dashboard.tsx` | The Servers page: the folder tree and its summary beside the selected folder's panel, the selection kept in the address as `?folder=`, the servers refreshed every `POLL_MS`; a refresh's failure is shown until a later refresh works, and an answer older than one asked for since is dropped; the Claude account is loaded once, and again after a refresh works when the page did not reach rcman for it (`UNREACHABLE` or `SIGNED_OUT`); its failure and the refresh's are shown once when they are alike, by code and title; after a folder is made or cloned, the folder it was made in is fetched again, and the new one selected if the folder it was made in still is, and after one is deleted the folder above it replaces it in the address |
| `src/Folders.tsx` | The folder tree, fetched a folder at a time and opened to whatever is selected — `reload` fetches a folder again after a folder is made or deleted in it, and `forget` drops a deleted folder and all below it — each folder's status dot and repository marker, and the summary of every server |
| `src/FolderPanel.tsx` | A folder's panel: its breadcrumb, its actions (**New folder**, **Clone from GitHub** and **Delete folder**, with its confirmation), its Claude server section and, for a repository, its Changes section; and the Home panel, shown while no folder is selected, with **New folder** and **Clone from GitHub** only. New folder's and Clone from GitHub's forms are one `FolderForm`: a field, a note where the form has one (Clone from GitHub's links to Settings for the token), its submit button, which says the request is running while it runs, when it and Cancel are disabled, and the server's refusal with the form's labels; while its request runs, the header's actions wait too, so no other form can take its place |
| `src/Changes.tsx` | A repository's Changes section: its heading with the branch bar (`src/BranchBar.tsx`) beside it, the staged and not staged files, each list folding under a heading that counts its files and more, with its Stage selected or Unstage selected beside it, a checkbox for each file that can be selected and Select all, and each file's change as its letter (`m.changeLetters`) in a colour of its own, with its word (`m.changes`) as its accessible text and title; Commit, a split button whose menu holds Commit & Push; and the status line, "Checked …" (`m.checkedAgo`) worked out again every `CHECKED_TICK_MS` while the section is shown, and Refresh changes. `src/git-guide.test.ts` holds the table of changes in `docs/git.md` to the letters and words. A list is open at first while it has files, opens when files come to it, and otherwise stays as the person left it. Only the selected folder's status is fetched, when its panel opens, every `POLL_MS` while it is open and after every action; an answer older than one asked for since is dropped. Its branches are fetched when it opens, after every action and when the current branch changes. What is checked is kept across refreshes, for files still listed. A refusal is shown beside what was tried until any later action there works |
| `src/BranchBar.tsx` | The branch bar: the branch lookup, a button naming where the repository is, whose pop-up has a filter, which asks rcman nothing, over the local and remote branches listed, each a switch where one is offered; **+**, New branch's form in a pop-up; Refresh branches, its icon turning while the fetch runs; ahead and behind as arrows, with `m.tracking` as their accessible text; and Push. It is shown once either the status or the branches are known, so a status that cannot be had leaves the branches to switch to; the arrows and Push wait for the status |
| `src/usePopup.ts` | A pop-up opened by a button, as a disclosure: it closes on Escape, putting focus back on the button, on a press outside it and when focus moves outside it. Every pop-up a button opens uses it, the **Admin** menu among them. The stylesheet places the branch bar's pop-ups against the Changes heading's row, so they stay inside the section at any width |
| `public/favicon.svg` | The tab's icon, `*rc` in white on a black circle, which `index.html` links; Vite copies `public/` into the build as it is |
| `src/Icons.tsx` | rcman's own icons, inline SVG drawn in the text's colour and hidden from assistive technology; the button or text they are in carries the name, from `src/messages.ts` |
| `src/Servers.tsx` | A server's states, what each allows and shows, and its card; `serversIn`, the servers in a folder or below it, which both a folder's dot and the delete confirmation count |
| `src/Caret.tsx` | The caret of a disclosure button, wherever one has a caret, turned by its `aria-expanded` |
| `src/Claude.tsx` | The Claude account's state and the connect flow |
| `src/Settings.tsx` | The person's own settings, in parts that are each saved on their own; `FIELDS` is every setting it shows, and a test holds `docs/settings.md` to it. The GitHub token is a part of its own, outside `FIELDS`: a password field never filled from the server, which `/api/github` tells only whether a token is stored, and **Remove token**, which asks first |
| `src/People.tsx`, `src/Everyone.tsx`, `src/Audit.tsx`, `src/Versions.tsx` | Administration; `src/Everyone.tsx` shows everyone's servers with no actions |
| `src/Attention.tsx` | The administrators' warning at the top of every page, from `/api/attention`, fetched on load and on every change of page |
| `src/Docs.tsx` | The help pages, from `/api/docs` |
| `src/clientErrors.ts` | The failures noted for rcman, kept until they are sent (see [Reporting failures](#reporting-failures)) |

## Reporting failures

A failure rcman could not see is reported to it, to be written to the journal
(see [the backend](backend.md#errors-from-the-page) and
[the journal guide](../docs/journal.md#errors-reported-by-the-page)). Its kinds
are `UNREACHABLE`, `SIGNED_OUT` and `UNEXPECTED_RESPONSE` from `src/api.ts`,
and `PAGE_ERROR` from `src/clientErrors.ts`; a problem the server sent is never
reported. `noteFailure` in `src/clientErrors.ts` writes the original error to
the browser's console and adds a report to the queue, without the query of the
request or of the page. Alike failures — the same kind, method, path, status
and error name — are one report, counted, with the first and the last time;
the queue holds at most `MAX_QUEUED` reports, leaves out any more, and is in
memory only, so a reload loses it.

After any request that works, `request()` starts `sendReports`, which sends
the queue to `POST /api/client-errors` one report at a time; one sender runs at
a time, and it takes the queue again until it is empty. Its requests are quiet:
they note no failure of their own and start no sender, so the page never
reports its reports. A report rcman refuses is dropped; once rcman is not
reached, or the sign-in has ended, the report and those after it go back in
the queue for the next request that works. Each is given up after
`REPORT_TIMEOUT_MS` in `src/api.ts`, so one never answered does not hold up
the rest. Nothing of this is shown to the person.

## Tests

- **Component tests** (Vitest, Testing Library, jsdom): `src/*.test.ts(x)`.
  Those that render anything render the whole app at a route against
  `src/test/fakeApi.ts`, which
  answers from a table of routes in the backend's problem-details shape and
  records every call; a route answering `UNREACHABLE_REPLY` makes `fetch`
  reject, as a browser's does, and one answering `SIGNED_OUT_REPLY` answers an
  opaque redirect, as oauth2-proxy's redirect is seen. `renderApp` in `src/test/render.tsx` also returns the
  router's current location and a `navigate`, whose `navigate(-1)` is the
  browser's back button, and takes `pollMs`, 0 unless a test gives one. `src/test/setup.ts` runs the role queries once
  before each file's first test and sets how long a `findBy*` waits, both
  so that the suite passes on a loaded machine; its comments say why. After
  each test it empties the queue of reports (`forgetReports`) and forgets the
  sender (`forgetSending`), so that neither sends during the next test. The
  tests that hold a guide in `docs/` to the page read its tables with
  `guideTable` in `src/test/guideTable.ts`, and the tests that read
  `src/styles.css` take its comments out with `uncommented` in
  `src/test/css.ts`.
- **Browser tests** (Playwright): `e2e/*.spec.ts`, against the built frontend
  served by the real backend from `tests/e2e_server.py` — a real `rc-helper`
  per person with the fake `claude`, two people with their own homes, one of
  them with a git repository whose origin is on GitHub and another there to
  clone, and oauth2-proxy's
  identity header set per test. GitHub is the world's own
  `tests/github_server.py`, serving the world's `origins/` while the backend
  runs, and taking a push with `GITHUB_TOKEN` in `e2e/people.ts`, which
  `playwright.config.ts` gives the backend, so a test can enter the token.
  `rc-admin` there is a stand-in
  that manages the users file; the container suite tests the real one.

  Each run has a world and a port of its own, which `e2e/world.ts` names:
  the world a folder under `/tmp`, with a name no other run has, which the
  backend's `WORLDS` in `tests/e2e_server.py` matches, and the port one the
  system has free. Playwright loads the config again in each worker, so
  `e2e/world.ts` keeps both in the environment, `RCMAN_E2E_DIR` and
  `RCMAN_E2E_PORT`, which the workers and the backend inherit, and keeps
  any already given there. A spec finds the world as `WORLD` from there,
  so a test can change a file in it behind rcman's back.

  The backend makes the world, and refuses a folder that already exists.
  While it runs it holds a lock on the world's lock file, which appears
  only once the lock is held. When it shuts down — Playwright stops it with
  SIGTERM for that — or fails to start, it stops the tmux servers on the
  sockets in the world's `tmux/` directory and removes the world, the lock
  file last. Before making its own, it clears the worlds beside it that a
  killed run left: it stops and removes one whose lock nothing holds, and
  one without a lock file for longer than `UNLOCKED_FOR` in
  `tests/e2e_server.py`, which only a run killed while making or removing
  its world leaves. It leaves a world whose lock is held, and anything that
  is not a folder of the current user's with, if any, a lock file of its
  own; it stops only tmux servers whose sockets are the current user's and
  in the world itself, never one a link points at; and what it cannot
  read or remove it leaves, and goes on.

  So runs at the same time, from different checkouts or worktrees, or two
  in one, do not share a world, a port or a tmux server. Two runs in one
  checkout share `dist/`, which `make e2e` builds before each, and the
  config's output folder, whose files one run's Playwright removes while
  the other's still writes there; runs at the same time are each in a
  checkout or worktree of their own.
- **Whole-stack browser tests**: `e2e-stack/*.spec.ts` with
  `playwright.stack.config.ts`, against an installed rcman behind real Caddy
  and oauth2-proxy in a container — see [tests.md](tests.md). The config
  takes the container's published port from `RCMAN_STACK_PORT`, which
  `tests/test_container_suite.py` sets, and refuses to load without it.

Each Playwright config writes to an output folder of its own under
`test-results/`, its `outputDir`, because Playwright empties a run's output
folder as the run starts.
