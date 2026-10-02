# Frontend

A React application in `frontend/`, built with Vite and TypeScript into
`frontend/dist`, which the backend serves (`backend/rcman/spa.py`): its files
as they are, and `index.html` for every other path outside `/api/`. The Node.js
version is `frontend/.nvmrc`.

| Command | What it does |
|---|---|
| `make frontend-setup` | Installs the packages and Playwright's browser |
| `make frontend-test` | Type-checks and runs the component tests |
| `make frontend-build` | Builds `frontend/dist` |
| `make e2e` | Builds, then runs the browser tests |
| `cd frontend && npm run dev` | A development server that forwards `/api` to a backend on port 8000 |

## Rules it keeps

- **The server validates; the page shows what it said.** `src/api.ts` is the
  only way to the backend; every failure becomes an `ApiError` carrying the
  problem details as sent, and `src/ProblemView.tsx` shows them — the title,
  each field error, and a raw screen, failed checks, the server a problem is
  about with its screen, what a delete left, or what git said and how many
  lines more, when the server passes one.
  A form passes `ProblemView` its labels by field name, so a field error is
  named as the form names the field, and by the server's name for it where
  the form has no label.
  Forms are `noValidate` and no input carries a validation attribute;
  `src/forms.test.tsx` checks every form.
- **Only what the server allows is offered.** `ACTIONS` in `src/Servers.tsx`
  maps every server state rc-helper reports to the actions it allows; a test
  holds it to the states. Beside it, `KIND` gives each state its badge colour,
  `PRIMARY` the one action it leads with, and `m.states` and `m.nextStep` its
  name and what to do; `src/Servers.test.ts` holds each of them to the states
  in `ACTIONS`, and `src/servers-guide.test.ts` holds the table in
  `docs/servers.md` to all of them. `START_REASON` in `src/Claude.tsx` says,
  for each state of the Claude account in `CONNECTION_STATES`, why no server
  can start, or null where one can; Start, and the actions that start Claude
  again, are offered only where it is null. Otherwise it says why in place of
  each: in the panel instead of Start, and on a server's card instead of the
  sentence saying what to do. The Changes section is shown only for a
  folder the tree lists as a repository; Commit is offered only with
  something staged and a commit author name set, and otherwise the section
  says why; Switch is offered for a local branch other than the current one
  and for a remote branch no local branch tracks; Stage selected and
  Unstage selected only while something is checked. git's own refusals, which
  rcman cannot know in advance, are shown as git gave them. Delete folder is offered only for a folder,
  never for the home folder, and only once the servers are listed, since its
  confirmation names those it stops and forgets. Administration is offered
  only to administrators, and the server refuses everyone else regardless.
- **Each form leads with what it does.** Its submit button is its one
  `primary` button, or its one `danger` button where what it does cannot be
  undone, such as deleting a folder; its other buttons are plain, and a
  dialog's choice is a form too. `src/forms.test.tsx` checks every form, and
  that only the forms it lists as destructive use `danger`. A button outside
  a form that opens such a form, such as a panel's **Delete folder**, is a
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
| `src/Dashboard.tsx` | The Servers page: the folder tree and its summary beside the selected folder's panel, the selection kept in the address as `?folder=`, the servers refreshed every `POLL_MS`; after a folder is made it is selected, and after one is deleted the folder above it replaces it in the address |
| `src/Folders.tsx` | The folder tree, fetched a folder at a time and opened to whatever is selected — `reload` fetches a folder again after a folder is made or deleted in it, and `forget` drops a deleted folder and all below it — each folder's status dot and repository marker, and the summary of every server |
| `src/FolderPanel.tsx` | A folder's panel: its breadcrumb, its actions (**New folder** and **Delete folder**, with its confirmation), its Claude server section and, for a repository, its Changes section; and the Home panel, shown while no folder is selected, with **New folder** only |
| `src/Changes.tsx` | A repository's Changes section: its branch, the staged and not staged files with a checkbox each, Commit, and its branches. Only the selected folder's status is fetched, when its panel opens, every `POLL_MS` while it is open and after every action; an answer older than one asked for since is dropped. Its branches are fetched when it opens, after every action and when the current branch changes. What is checked is kept across refreshes, for files still listed |
| `src/Servers.tsx` | A server's states, what each allows and shows, and its card; `serversIn`, the servers in a folder or below it, which both a folder's dot and the delete confirmation count |
| `src/Caret.tsx` | The caret of a disclosure button, such as **Admin** and a server's **Show screen**, turned by its `aria-expanded` |
| `src/Claude.tsx` | The Claude account's state and the connect flow |
| `src/Settings.tsx` | The person's own settings, in parts that are each saved on their own; `FIELDS` is every field it shows, and a test holds `docs/settings.md` to it |
| `src/People.tsx`, `src/Everyone.tsx`, `src/Audit.tsx`, `src/Versions.tsx` | Administration; `src/Everyone.tsx` shows everyone's servers with no actions |
| `src/Attention.tsx` | The administrators' warning at the top of every page, from `/api/attention`, fetched on load and on every change of page |
| `src/Docs.tsx` | The help pages, from `/api/docs` |

## Tests

- **Component tests** (Vitest, Testing Library, jsdom): `src/*.test.ts(x)`.
  They render the whole app at a route against `src/test/fakeApi.ts`, which
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
  per person with the fake `claude`, two people with their own homes, and
  oauth2-proxy's identity header set per test. `rc-admin` there is a stand-in
  that manages the users file; the container suite tests the real one.
  The tmux sockets are in the world's `tmux/` directory; the backend stops
  their tmux servers when it shuts down — Playwright stops it with SIGTERM
  for that — and again before building the world, for a run that was killed.
- **Whole-stack browser tests**: `e2e-stack/*.spec.ts` with
  `playwright.stack.config.ts`, against an installed rcman behind real Caddy
  and oauth2-proxy in a container — see [tests.md](tests.md).
