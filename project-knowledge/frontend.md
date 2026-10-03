# Frontend

A React application in `frontend/`, built with Vite and TypeScript into
`frontend/dist`, which the backend serves (`backend/rcman/spa.py`): its files
as they are, and `index.html` for every other path outside `/api/`. The Node.js
version is `frontend/.nvmrc`.

The `make` targets that set up, build and test it are in
[tests.md](tests.md#running). `cd frontend && npm run dev` runs a development
server that forwards `/api` to a local backend, at the address
`frontend/vite.config.ts` names.

## Rules it keeps

- **The server validates; the page shows what it said.** `src/api.ts` is the
  only way to the backend; every failure becomes an `ApiError` carrying the
  problem details as sent, built by `problemFrom`, which also turns the
  problem details an answer reports, as Commit & Push's `push_problem`,
  into one; `src/ProblemView.tsx` shows them — the title,
  each field error, and a raw screen, failed checks, the server a problem is
  about with its screen, what a delete left, or what git said and how many
  lines more, when the server passes one.
  Each input carries its field's name in its `name` attribute, and a form
  passes `ProblemView` its labels by field name, so a field error is named as
  the form names the field, and by the server's name for it where the form
  has no label; `src/forms.test.tsx` refuses every field of every form and
  checks each error is shown under its input's label.
  Forms are `noValidate` and no input carries a validation attribute;
  `src/forms.test.tsx` checks every form.
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
  says why; a push can be tried on a branch, and, when the status says it
  goes to GitHub (`push_to_github`), only while the same status says a
  token is stored (`github_token`), so a token git forgot after GitHub
  refused it withdraws it at the next refresh; otherwise the section says
  why, linking to Settings for the token; Push is offered where a push can
  be tried and where the branch pushes lacks a commit of it or is unknown
  (`push_ahead`), and Commit &
  Push where a push can be tried and Commit is offered; Switch is offered for a local branch other than the current one
  and for a remote branch no local branch tracks; Stage selected and
  Unstage selected are enabled only while something is checked. git's own refusals, which
  rcman cannot know in advance, are shown as git gave them. Clone from
  GitHub is always offered, since a public repository needs no token; a
  private one GitHub refuses without one is git's refusal, shown as such. Delete folder is offered only for a folder,
  never for the home folder, and only once the servers are listed, since its
  confirmation names those it stops and forgets. Administration is offered
  only to administrators, and the server refuses everyone else regardless.
- **Each form leads with what it does.** Its submit button is its one
  `primary` button, or its one `danger` button where what it does cannot be
  undone, such as deleting a folder; its other buttons are plain or
  `secondary`, such as Commit & Push beside Commit, and a dialog's choice is
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
| `src/App.tsx` | Who is signed in, the navigation, and the routes |
| `src/AdminPages.tsx` | `ADMIN`, the administrators' pages by name, which the links in the administrators' warning use, and `ADMIN_PAGES`, the same pages in order, which the navigation's **Admin** menu and the routes are built from |
| `src/Dashboard.tsx` | The Servers page: the folder tree and its summary beside the selected folder's panel, the selection kept in the address as `?folder=`, the servers refreshed every `POLL_MS`; after a folder is made or cloned, the folder it was made in is fetched again, and the new one selected if the folder it was made in still is, and after one is deleted the folder above it replaces it in the address |
| `src/Folders.tsx` | The folder tree, fetched a folder at a time and opened to whatever is selected — `reload` fetches a folder again after a folder is made or deleted in it, and `forget` drops a deleted folder and all below it — each folder's status dot and repository marker, and the summary of every server |
| `src/FolderPanel.tsx` | A folder's panel: its breadcrumb, its actions (**New folder**, **Clone from GitHub** and **Delete folder**, with its confirmation), its Claude server section and, for a repository, its Changes section; and the Home panel, shown while no folder is selected, with **New folder** and **Clone from GitHub** only. New folder's and Clone from GitHub's forms are one `FolderForm`: a field, a note where the form has one (Clone from GitHub's links to Settings for the token), its submit button, which says the request is running while it runs, when it and Cancel are disabled, and the server's refusal with the form's labels; while its request runs, the header's actions wait too, so no other form can take its place |
| `src/Changes.tsx` | A repository's Changes section: its branch, the staged and not staged files, with a checkbox for each file that can be selected and Select all, Commit, Push and Commit & Push, and its branches; each change's name is `m.changes`, and `src/git-guide.test.ts` holds the table of changes in `docs/git.md` to it. Only the selected folder's status is fetched, when its panel opens, every `POLL_MS` while it is open and after every action; an answer older than one asked for since is dropped. Its branches are fetched when it opens, after every action and when the current branch changes. What is checked is kept across refreshes, for files still listed. A refusal is shown beside what was tried until any later action there works. New branch is behind a secondary button, so Commit is the section's one primary |
| `src/Servers.tsx` | A server's states, what each allows and shows, and its card; `serversIn`, the servers in a folder or below it, which both a folder's dot and the delete confirmation count |
| `src/Caret.tsx` | The caret of a disclosure button, such as **Admin** and a server's **Show screen**, turned by its `aria-expanded` |
| `src/Claude.tsx` | The Claude account's state and the connect flow |
| `src/Settings.tsx` | The person's own settings, in parts that are each saved on their own; `FIELDS` is every setting it shows, and a test holds `docs/settings.md` to it. The GitHub token is a part of its own, outside `FIELDS`: a password field never filled from the server, which `/api/github` tells only whether a token is stored, and **Remove token**, which asks first |
| `src/People.tsx`, `src/Everyone.tsx`, `src/Audit.tsx`, `src/Versions.tsx` | Administration; `src/Everyone.tsx` shows everyone's servers with no actions |
| `src/Attention.tsx` | The administrators' warning at the top of every page, from `/api/attention`, fetched on load and on every change of page |
| `src/Docs.tsx` | The help pages, from `/api/docs` |

## Tests

- **Component tests** (Vitest, Testing Library, jsdom): `src/*.test.ts(x)`.
  Those that render anything render the whole app at a route against
  `src/test/fakeApi.ts`, which
  answers from a table of routes in the backend's problem-details shape and
  records every call. `renderApp` in `src/test/render.tsx` also returns the
  router's current location and a `navigate`, whose `navigate(-1)` is the
  browser's back button, and takes `pollMs`, 0 unless a test gives one. `src/test/setup.ts` runs the role queries once
  before each file's first test and sets how long a `findBy*` waits, both
  so that the suite passes on a loaded machine; its comments say why. The
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
  runs, and taking a push with `GITHUB_TOKEN` in `e2e/people.ts`. Where the
  world is, `WORLD` there too, and the token are what `playwright.config.ts`
  gives the backend, so a test can change a file there behind rcman's back,
  and enter the token. `rc-admin` there is a stand-in
  that manages the users file; the container suite tests the real one.
  The tmux sockets are in the world's `tmux/` directory; the backend stops
  their tmux servers when it shuts down — Playwright stops it with SIGTERM
  for that — and again before building the world, for a run that was killed.
- **Whole-stack browser tests**: `e2e-stack/*.spec.ts` with
  `playwright.stack.config.ts`, against an installed rcman behind real Caddy
  and oauth2-proxy in a container — see [tests.md](tests.md).
