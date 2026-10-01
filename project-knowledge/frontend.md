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
  holds it to the states. Administration is offered only to administrators,
  and the server refuses everyone else regardless.
- **Every piece of text not from the server is in `src/messages.ts`.**
- Links between help pages — a page's file name, optionally followed by a
  `#part` — stay inside rcman; other links open in a new tab.

## Layout

| File | What it is |
|---|---|
| `src/App.tsx` | Who is signed in, the navigation, and the routes |
| `src/Dashboard.tsx`, `src/Servers.tsx` | The Servers page: folder tree, servers and their actions, refreshed every `POLL_MS` |
| `src/Claude.tsx` | The Claude account's state and the connect flow |
| `src/Settings.tsx` | The session name prefix |
| `src/People.tsx`, `src/Audit.tsx`, `src/Versions.tsx` | Administration |
| `src/Docs.tsx` | The help pages, from `/api/docs` |

## Tests

- **Component tests** (Vitest, Testing Library, jsdom): `src/*.test.ts(x)`.
  They render the whole app at a route against `src/test/fakeApi.ts`, which
  answers from a table of routes in the backend's problem-details shape and
  records every call.
- **Browser tests** (Playwright): `e2e/*.spec.ts`, against the built frontend
  served by the real backend from `tests/e2e_server.py` — a real `rc-helper`
  per person with the fake `claude`, two people with their own homes, and
  oauth2-proxy's identity header set per test. `rc-admin` there is a stand-in
  that manages the users file; the container suite tests the real one.
- **Whole-stack browser tests**: `e2e-stack/*.spec.ts` with
  `playwright.stack.config.ts`, against an installed rcman behind real Caddy
  and oauth2-proxy in a container — see [tests.md](tests.md).
