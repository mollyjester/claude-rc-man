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
  each field error, a raw screen or failed checks when the server passes one.
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
  sentence saying what to do.
  Administration is offered only to administrators,
  and the server refuses everyone else regardless.
- **Every piece of text not from the server is in `src/messages.ts`.**
- **Every colour is a custom property in `src/styles.css`**, with a value for
  a light scheme and one for a dark scheme, which the browser picks between by
  the system's setting; `src/styles.test.ts` holds the stylesheet to that,
  counting named colours as jsdom's colour parser reads them, and names the
  colours kept the same in both. Each pair of colours text is drawn
  in meets WCAG AA contrast in both schemes; that is checked by hand when a
  colour changes.
- Links between help pages — a page's file name, optionally followed by a
  `#part` — stay inside rcman; other links open in a new tab.

## Layout

| File | What it is |
|---|---|
| `src/App.tsx` | Who is signed in, the navigation, and the routes |
| `src/AdminPages.tsx` | `ADMIN`, the administrators' pages by name, which the links in the administrators' warning use, and `ADMIN_PAGES`, the same pages in order, which the navigation's **Admin** menu and the routes are built from |
| `src/Dashboard.tsx` | The Servers page: the folder tree and its summary beside the selected folder's panel, the selection kept in the address as `?folder=`, the servers refreshed every `POLL_MS` |
| `src/Folders.tsx` | The folder tree, fetched a folder at a time and opened to whatever is selected, each folder's status dot, and the summary of every server |
| `src/FolderPanel.tsx` | A folder's panel: its breadcrumb and its Claude server section |
| `src/Servers.tsx` | A server's states, what each allows and shows, and its card |
| `src/Claude.tsx` | The Claude account's state and the connect flow |
| `src/Settings.tsx` | The session name prefix |
| `src/People.tsx`, `src/Everyone.tsx`, `src/Audit.tsx`, `src/Versions.tsx` | Administration; `src/Everyone.tsx` shows everyone's servers with no actions |
| `src/Attention.tsx` | The administrators' warning at the top of every page, from `/api/attention`, fetched on load and on every change of page |
| `src/Docs.tsx` | The help pages, from `/api/docs` |

## Tests

- **Component tests** (Vitest, Testing Library, jsdom): `src/*.test.ts(x)`.
  They render the whole app at a route against `src/test/fakeApi.ts`, which
  answers from a table of routes in the backend's problem-details shape and
  records every call. `renderApp` in `src/test/render.tsx` also returns the
  router's current location and a `navigate`, whose `navigate(-1)` is the
  browser's back button. `src/test/setup.ts` runs the role queries once
  before each file's first test and sets how long a `findBy*` waits, both
  so that the suite passes on a loaded machine; its comments say why.
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
