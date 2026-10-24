# Frontend

A React application in `frontend/`, built with Vite and TypeScript into
`frontend/dist`, which the backend serves (`backend/rcman/spa.py`): its files
as they are, and `index.html` for every other path outside `/api/`. The Node.js
version is `frontend/.nvmrc`.

The `make` targets that set up, build and test it are in
[tests.md](tests.md#running). `cd frontend && npm run dev` runs a development
server that forwards `/api` to a local backend, the Terminal tab's WebSocket
included, at the address `frontend/vite.config.ts` names.

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
  only way to the backend, the Terminal tab's WebSocket included
  (`api.openTerminal`); every failure the server describes becomes an
  `ApiError` carrying the problem details as sent, built by `problemFrom`, which also turns the
  problem details an answer reports, as Commit & Push's `push_problem`,
  into one; `src/ProblemView.tsx` shows them — the title,
  each field error, and a raw screen, failed checks with what each ran and
  got back under its name and how many lines were not shown, the server a problem is
  about with its screen, what a delete left, or what git said and how many
  lines more, when the server passes one.
  Each input carries its field's name in its `name` attribute. A form shows
  each refused field's message right under its input (`useFieldErrors` in
  `src/ProblemView.tsx`: the input is `aria-invalid` and described by the
  message, which keeps any help it already had, and focus moves to the first
  refused field), and passes `ProblemView` its labels by field name and
  `inline`, so that the alert is the short summary
  (`m.problemCheckFields`) and lists only what no input of the form shows,
  named as the form names the field, or by the server's name for it where the
  form has no label. A form that does not show them under its inputs, the
  top bar's sign-out, lists them all. `src/forms.test.tsx` refuses every field of
  every form and checks each error is shown under its input or named by its
  label. A refusal that names a field only in its extras, as the limit below
  use does (`limit`), is shown under that field too.
  Forms are `noValidate` and no input carries a validation attribute;
  `src/forms.test.tsx` checks every form. It finds every form in the source:
  each `<form>` in the page's own `.tsx` files under `src/`, known by the
  component that renders it and the function its `onSubmit` names, which a
  form must have, declared in that component as a function of its own, not
  wrapped, so that it keeps its name. A page the test visits must open each
  of them, and each that has a field must be opened by a page the label
  check visits; on screen the test knows a form by the same two, as React's
  development build records them on the form's element.
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
  maps every server state to the actions it allows; its states are kept to
  the table in [runtime.md](runtime.md#server-states) by hand, and
  `src/Dashboard.test.tsx` checks that each card offers exactly its state's
  actions. Beside it, `KIND` gives each state its badge colour, `PRIMARY` the
  one action it leads with, and `m.states` and `m.nextStep` its name and what
  to do; `src/Servers.test.ts` holds each of them to the states in `ACTIONS`,
  and `src/workspaces-guide.test.ts` holds the table in `docs/workspaces.md`
  to all of them, and checks that every control its *The Workspaces page*
  section names in bold is text from `src/messages.ts`. A workspace's panel
  offers **Start** only while the workspace is ready, has no server, and its
  server could be read. Every row of the list has a trash button, which acts on
  that row's workspace whatever is selected; it asks to delete a workspace only
  while it is ready or failed, or being made from a clone (`source` set), where
  it cancels the clone (`whyNotDeletable` in `src/Dashboard.tsx`), and for any
  other state, an empty workspace being made or one being deleted among them,
  says why it cannot, and sends nothing. Whether a server may start
  — the Claude account connected and not signing out — the server decides, so
  **Start** is offered regardless and its refusal shown. **Sign out of Claude**
  is offered only while an account is connected and not already signing out;
  while a sign-out waits, **Stop my servers and sign out now** is offered
  instead, so it is never stuck. Administration is offered only to administrators,
  and the server refuses everyone else regardless. What the server refuses
  everyone, it is not offered at all. The Claude Code versions page shows a
  roll-out as working while its request lasts, and offers no second one
  meanwhile, nor while the report says another runs; it offers neither **Use**
  nor **Roll out** for a version marked as failed, nor **Mark as failed** for
  the approved one, and offers marking and **Remove mark** while a roll-out
  runs, which the server allows; a person's workspace
  panel shows the version its server runs, and a quiet note while that is not
  the approved version (`claude_approved` in the Workspaces
  answer). **Rebuild** is offered where the listing's entry says a rebuild may be asked
  for now (`rebuildable`, which the server works out as its route does; the page derives
  nothing of it) and rcman is not being upgraded (`rcman_upgrading`), and shown waiting,
  **Rebuilding…** and disabled, while the entry's `rebuild.state` says one is under way
  (`RebuildAction` in `src/Dashboard.tsx`); it is primary only where the server says the
  workspace's devcontainer.json changed (`changed` true), in the Container card, and in a
  failed workspace's panel, where it is the way back. The Changes section is in the panel of
  every workspace that is ready, and in none other, and so are the panel's tabs,
  **Terminal** with them. The Settings page's GitHub
  token part asks whether a token is stored, and offers its field and Remove only
  once the server answers; when the server refuses the question, the part shows
  the refusal and no field.
- **The Changes section's rules**, which hold wherever it is shown: it names its
  workspace by id and holds no path; a workspace with no repository (the server
  answers `not_a_repository`) is said quietly (`m.notARepository`), with no
  error and no branch bar, and its branches are fetched once a repository has
  come to be. Commit is
  offered only with
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
  Push, where a push can be tried and Commit is offered; Sync takes Refresh
  branches' place once the status is known, where a push can be tried, the
  branch has a commit, no merge is under way (`merging`, which a
  fast-forward refuses), the upstream, where there is one, is not gone
  (`ahead` and `behind` not null), and the branch and its upstream have not
  diverged (both `ahead` and `behind` above 0), which the section otherwise
  says (`m.diverged`); what a sync did is said (`m.synced`), and a push that
  failed after a pull is shown as the sync's refusal beside it; Switch is
  offered for a local branch other than the current one
  and for a remote branch no local branch tracks and whose name no local
  branch takes (`taken_by`), which it says otherwise; Stage selected and
  Unstage selected are shown only while their list has files, and enabled
  only while something is checked. The branch bar shows how far the branch
  is from its upstream only where it has one, and says where that upstream
  is gone (`ahead` and `behind` null); with no upstream it shows nothing,
  since the status says only whether one is set, not whether the branch is
  on the remote. git's own refusals, which
  rcman cannot know in advance, are shown as git gave them.
- **Each form leads with what it does.** Its submit button is its one
  `primary` button, or its one `danger` button where what it does cannot be
  undone, such as deleting a workspace; its other buttons are plain or
  `secondary`, such as Commit & Push in Commit's menu, and a dialog's choice is
  a form too, the one that says why a workspace cannot be deleted included, whose
  button is `primary`. `src/forms.test.tsx` checks every form, and
  that only the forms it lists as destructive, or as leading with `danger`
  in place, use `danger`: signing out of Claude and finishing a sign-out at
  once have no dialog, the button is the choice. The top bar's sign-out form, in the account menu, is
  the one exemption, named in that test and held to its subject: its button is
  plain, in one group with the box that says what happens to the servers. A button outside
  a form that opens such a form, such as a workspace's trash button in the list or
  the People page's **Remove**, is a
  danger outline, `secondary-danger` (the list's rows draw their trash button quiet
  until it is pointed at or has focus, and with a mouse show it only on the row
  pointed at, the selected row and the row focus is in); `src/forms.test.tsx` checks the button
  that opens each form it lists as destructive. `src/styles.test.ts` checks
  that both look unlike any other button in both schemes.
- **Every piece of text not from the server is in `src/messages.ts`.** The
  words for each action `docs/audit.md` lists, and for each result, are there
  by code, and `src/audit-guide.test.ts` holds the two together; a code with
  no words is shown as it is.
- **A page can be skipped to and named.** The first thing Tab reaches is the
  **Skip to content** link, which moves focus to `main`; each page sets the
  browser tab's title with `useTitle` (`src/useTitle.ts`) to its name, then
  rcman's; the Workspaces page has its own `h1`.
- **No page is wider than the window.** Every table is inside `TableScroll`
  (`src/TableScroll.tsx`), a named box that scrolls sideways and takes focus, as
  WCAG 1.4.10 allows a table to;
  help pages' pipe tables, which the Markdown renderer has no syntax for, are
  split out of the Markdown by `src/markdownTables.ts` and drawn by
  `src/Docs.tsx`, and `src/markdownTables.test.ts` finds every table in
  `docs/`. On a phone (the `max-width` query in `src/styles.css`) the top bar
  is one row, the sidebar a drawer, a help page one column, a dialog a sheet
  from the screen's foot, the tables of People, the Audit trail and Monitor lists of rows
  (`table.stacked`), and the
  Workspaces page shows the list or the selected workspace's panel, not both
  (the `showing` class of `.dashboard`), the panel under one sticky bar of the
  way back and the switcher, which opens the workspaces as a sheet from the
  screen's foot, and the page's heading row out of sight while it is shown; the
  top bar names the page, so the heading row holds how many workspaces there
  are (`m.workspaceCount`) and **New workspace** in its short words
  (`m.newWorkspaceShort`), its `h1` and the button's whole name, its
  `aria-label`, kept for assistive technology; what is scrolled to stops below the bar over a panel
  too (`--panel-bar`). `e2e/touch.spec.ts` holds every page of the route
  table to it at two phones' widths.
- **Big enough for a finger, and not zoomed in on.** On a touch screen
  (`pointer: coarse`), at any width, everything pressed is at least as high as
  WCAG 2.2's 2.5.5 asks, and a button or a navigation link as wide, as the touch
  screen's rules in `src/styles.css` set it; a rule that
  sizes a target of its own is named again in the touch screen's rules, which
  `src/styles.test.ts` checks against every rule that sets a `min-height`,
  with what is exempt named in `NOT_PRESSED`. A field's text is never smaller
  than a phone zooms in on (the field rules in `src/styles.css`), and the field rules set the font's
  family, never the `font` shorthand, which would reset its size; the
  viewport's meta tag in `index.html` never stops the page being zoomed, and asks for the on-screen keyboard to resize the page (`interactive-widget=resizes-content`), which `src/styles.test.ts` checks.
  With a mouse (`pointer: fine`) a control is a mouse's size, the `--control`
  token, which is at least WCAG 2.2's 2.5.8 minimum.
  `e2e/touch.spec.ts` measures every control on every page, with each dialog
  and pop-up open and a workspace on each of its tabs, on touch screens and
  with a mouse, and fails on one too small for the pointer (a text link is
  measured on a touch screen only, as 2.5.8 lets one be smaller where nothing
  else is near it) but for what `SMALL_ALLOWED` describes, a link inside a sentence (in a
  table's cell, one with three words or more besides it), on a box that scrolls
  sideways but for what `SCROLL_ALLOWED` describes, and on an icon-only button
  with no name; its views name the dialog or pop-up each opens, which is held to
  the `data-component` read off what opened, and are held to the components
  that draw a `<Dialog>` or call `usePopup()` in the sources (`callSites` in
  `src/test/callSites.ts`, which reads every `.ts` and `.tsx` file of the page,
  knows the two by their import under any name, and fails on a site outside a
  component, in a component used more than once, or not marked with its
  component's name; it counts only the direct uses of the component that holds
  a site, so a component used once inside a wrapper that is itself used more
  than once is not caught). On a
  phone the top bar is one row: the menu button that opens the sidebar as a
  drawer, where you are, and the avatar that opens the **Account** menu.
- **Drawn by the design system.** `e2e/touch.spec.ts`'s walk also fails, at
  every width it walks, on a button's label off its middle, the controls of an
  action row (`.actions`) of differing heights, the fields of a form drawn
  unlike each other, a box (a block bordered on every side, not a control)
  inside another outside a dialog or pop-up, but for what `NESTED_ALLOWED`
  describes, each of which a walk must meet, and an underlined link in the top
  bar or the sidebar. With a mouse, in the light scheme and again in the dark,
  it also hovers each look of button once (its element, classes, whether it is
  the current one, and its colours at rest) and fails on its text under WCAG
  AA's contrast, or its icon under 3:1, against what is behind it while
  hovered, on a kind in `HOVERED_KINDS` it never hovered, and on a look it met
  only with something lying over it. `src/styles.test.ts` holds the stylesheet to the
  [design system](#design-system)'s tokens, its buttons laid out alike for
  `<button>` and `a.button`, a field's visible focus, and the dialog's sheet.
- **Every dialog is the shared one.** `src/Dialog.tsx` is the browser's own modal
  `<dialog>`; `src/forms.test.tsx` opens every page and dialog it knows, and
  checks that every component that draws a `<Dialog>` (`callSites` in
  `src/test/callSites.ts`) is opened by one of them, each as a modal, and that no
  other file draws a dialog of its own. `e2e/layout.spec.ts` opens each of
  them, held to the same list, and checks that it sits in the middle of a
  desktop's window and is a sheet from the foot of a phone's, and that a sheet
  keeps focus in it, closes on Escape and keeps its footer (a row of its
  buttons marked `footer`) above the on-screen keyboard.
- **Every colour is a custom property in `src/styles.css`**, with a value for
  a light scheme and one for a dark scheme, which the browser picks between by
  the system's setting; `src/styles.test.ts` holds the stylesheet to that and
  names the colours kept the same in both. It counts a word as a colour where
  jsdom's colour parser reads it as one, and only in custom properties and in
  properties that take a colour, since the parser also reads words such as
  `background` as system colours. Each pair of colours text is drawn in meets
  WCAG AA contrast in both schemes, which `src/styles.test.ts` works out: the
  pairs are read from the stylesheet, a rule's text colour on its own
  background, or, where it has none, on each background text may be drawn on
  (`CANVASES`), and a field's border and focus are held to 3:1 against the
  field. The Terminal tab's colours are the `--terminal-*` properties, which
  `src/terminalTheme.ts` reads into xterm.js (`THEME` names each, and
  `src/styles.test.ts` holds every one it names to both schemes); xterm.js's own
  `xterm.css`, whose colours are written out, is not imported: what xterm.js needs
  of a stylesheet is in `src/styles.css`, with its colours taken from those
  properties; since those rules take the outline off xterm.js's own element, the
  screen shows where focus is (`.terminal-screen:focus-within`).
- **Each kind of status dot has its own shape.** `src/workspaces-guide.test.ts`
  reads each `.dot-<kind>` rule in `src/styles.css`, requires the shapes to
  differ, and holds the shape table in `docs/workspaces.md` to them.
- Links between help pages — a page's file name, optionally followed by a
  `#part` — stay inside rcman; other links open in a new tab.

## Design system

`src/styles.css` is the design system, in the order its opening comment
gives: the tokens, then the base elements, the shell, the shared components,
each page's own parts, and last the phone's and the touch screen's rules. The
tokens are custom properties of `:root` — the type, the spacing on a 4px grid,
the corners of a control, a card and a pill, a control's size with a mouse
and on a touch screen, the shell's sizes, the elevations, and the colours,
which the dark scheme gives values of their own; their values are there, not
here. The rules it keeps:

- Plain CSS, no component library, no font and no icon package: the system's
  own face, and the icons of `src/Icons.tsx`.
- One accent, a saturated blue with white on it, for the primary action, the
  page where you are, the selected row and focus in a field; a state's colour
  only for that state, as text, a dot, a pill's tint or a message bar's.
- Buttons are laid out alike for `<button>` and `a.button`: a row, their label
  in the middle, a control's height. The kinds are plain, `primary` (a form's
  one leading button), `subtle` (icon-only buttons and quiet actions),
  `danger`, `secondary-danger` and `secondary`; [Rules it keeps](#rules-it-keeps)
  says which a form leads with.
- One look for every field, its border showing where focus is.
- One bordered level: a card (`.card`) holds no box of its own; what is inside
  is divided by strokes. A shadow is only on what floats over the page: a
  pop-up, a dialog, the phone's drawer.
- Hovering is drawn only under `(hover: hover)`, where a pointer can hover; the
  plain button's hover is in `:where()`, so that it weighs nothing against a
  kind drawn by a class of its own, and a kind whose hover differs says so in
  a rule of its own; with
  forced colours what a background marks, as the page where you are, is
  marked by an outline too, and a field's focus keeps a transparent outline
  for forced colours to draw.
- Pivot tabs underlined in the accent; states as tinted pills (`.badge-*`);
  problems, warnings, what to know and what was done as message bars
  (`src/MessageBar.tsx`, `.message-*`), each with its icon, which are never
  boxes; list rows (`.list-row`), divided by strokes, with the accent's bar on
  the selected one's quiet tint; empty states (`.empty-state`): an icon, a
  title, one line and the action.
- Every page's title in `.page-head`, its leading action at the other end; a
  page of cards one under another (`.cards`); a card's own head and foot
  (`.card-head`, `.card-foot`) and a card whose rows run edge to edge
  (`.card.flush`), as a table's do, what was last done in it said under its
  head (`.card-status`, a live region that takes no room while it says
  nothing): a small quiet heading row, rows divided by
  strokes, and on a phone a cell on one line, so that a wide table scrolls
  sideways inside its card. A table marked `stacked` is instead, on a phone, a
  list of rows drawn from the same table, as a mobile list view has them: the
  headings kept for assistive technology only, the row's lead in bold on its
  first line with what is done or what came of it at the right, the rest muted
  under it, a dot between each value, and a value that alone does not say what
  it is shown after its column's name (`data-label`, `.named`), which is not
  read, since the column's heading names the cell; since
  a table drawn other than as a table loses its roles, the markup says them
  outright (`ROLE` in `src/TableScroll.tsx`). A row's several actions are then
  one subtle **More actions** button whose menu holds them; facts as rows of a definition list (`dl.facts`); what cannot be undone
  set apart at a card's foot under a stroke (`.danger-zone`).
- Anthropic's design guidance, vendored in
  [.claude/skills/frontend-design/SKILL.md](../.claude/skills/frontend-design/SKILL.md),
  is applied within these rules.

The tests that hold the stylesheet to them are under
[Rules it keeps](#rules-it-keeps).

## Layout

| File | What it is |
|---|---|
| `src/App.tsx` | Who is signed in, the routes, from `PAGES` and `ADMIN_PAGES`, the skip link and `main`'s focus target, and the chrome (`Navigation`): the top bar, with rcman's mark leading home, the name of the page where you are, an icon link to Help and the person's avatar, the first letter of their address, which opens the **Account** menu, a `usePopup` holding the address they are signed in as (`m.signedInAs`) and **Sign out of rcman**, a plain button in one group with **Keep servers running after sign-out**, which leaves through `away` for oauth2-proxy's sign-out; and one `<nav>`, the sidebar of the pages that have an icon in `PAGES`, then, for administrators only, the **Administration** group built from `ADMIN_PAGES`, the page where you are marked. On a phone the stylesheet makes the same `<nav>` a drawer, which the top bar's menu button opens as a `usePopup` that also closes when a page is chosen and when the page changes; opened, it puts focus on the page where you are, a page chosen in it puts focus on `main`, and the page dimmed beside it is a scrim of its own, inside the pop-up, which takes a press and closes the drawer, so that the press reaches nothing under it |
| `src/Pages.tsx` | `PAGES`, the pages everyone signed in is offered, with each one's name, its title and, for those the sidebar offers, its icon; the routes, the sidebar and `e2e/touch.spec.ts`'s walk over every page are built from it |
| `src/Dialog.tsx` | Every dialog: the native `<dialog>`, opened with `showModal()` as it is drawn and closed as it goes, so that focus moves in and stays, the page behind cannot be reached, and focus returns to what had it; named by its heading, in its head, what it holds in its body; Escape asks its parent to close it unless `closable` is false, as while the Limits dialog saves, and so does the cross after its body, **Close dialog**, which comes last so that focus starts on what the dialog asks and is disabled while it cannot be closed; focus on its first control, or on itself (`focusDialog`); `large` for one that holds several forms; marked with the name of the component that draws it (`owner`, as `data-component`), for `e2e/touch.spec.ts` to read off what opened. The stylesheet centres it, and on a phone makes it a sheet from the screen's foot; a row of its buttons marked `footer` keeps to its foot while the rest scrolls |
| `src/AdminPages.tsx` | `ADMIN`, the administrators' pages by name, each with its icon, which the links in the administrators' warning use, and `ADMIN_PAGES`, the same pages in order, which the sidebar's **Administration** group and the routes are built from |
| `src/Dashboard.tsx` | The Workspaces page, under its own `h1`, beside which **New workspace**, a primary button with its icon, opens its dialog (a name, and an optional GitHub address, each with one line of help, the whole story in `docs/workspaces.md`: with an address it posts to `/api/git/clone`, naming the workspace only when a name is typed, and otherwise to `/api/workspaces`; the server judges both and its refusal of either is shown under the field's label, the dialog staying open with what was typed; once made, the dialog closes and the new workspace is selected); the person's workspaces, a list in a card named by that heading, each a row (`.list-row`, the selected one marked) of its name's button, which holds its dot (`dotFor` in `src/Servers.tsx`) before the name, the dot's room kept where it has none, its state as a pill while it is not ready (`WorkspaceBadge`), and its trash button, named after it, which opens the question for that workspace whatever is selected, or says why it cannot be deleted now; with no workspaces, the list is an empty state that offers **New workspace** in the heading's place, and there is no panel; beside the list the selected workspace's panel, or a placeholder saying to select one, and over the panel, shown only on a phone (`PanelBar`), a bar of an icon link back to the list (`m.backToWorkspaces`) and the switcher, a button showing the workspace's name, which its name holds (`m.switchWorkspaceFrom`), that opens a `usePopup` of the workspaces, drawn as a sheet from the screen's foot over the page dimmed by a scrim of its own, focus on the workspace shown; the selection kept in the address as `?workspace=`; the workspaces refreshed every `POLL_MS`, a refresh's failure shown until a later refresh works, and an answer older than one asked for since dropped. The panel's head holds its name, its state as a pill, and, where `source_page` is not null, the repository it names, owner first, after its icon, a link to the page in a new tab named by the whole address (`source_page`, which the server derives; the page derives nothing from `source`), in every state; the panel says in a message bar what a workspace being made, deleted or failed is, with the steps (`log`) in a figure while it is not ready, and for a ready one has two ARIA tabs (`TABS`), **Overview**, chosen first, and **Terminal**, moved between with the arrow keys, Home and End, each panel taking focus (`tabIndex` 0) as the WAI-ARIA tab pattern has it, the one chosen kept in the address as `?tab=` (none for Overview), replacing the entry, so that Back leaves the workspace rather than the tab; Overview holds three sibling cards, **Claude server**, which says the server is not read while a rebuild replaces the container (`server_error` `workspace_rebuilding`, which is no problem and gives the list's row no dot), **Container** (`ContainerCard`: what the container is made from, the server's `GET /api/workspaces/{id}/devcontainer` asked as the card opens and every `pollMs`, a failed or older answer taken as not known, an info message bar where it answers `changed` true, Rebuild, the rebuild's steps (`rebuild.log`) in a figure that announces new steps politely while one runs, and why the last one did not finish, `rebuild.error`, as a problem message bar, its first line in the open and the rest behind **Details**) and **Changes** (`src/Changes.tsx`, polled at the page's own `pollMs`), and Terminal `src/Terminal.tsx`, mounted when the tab is first shown, so that visiting a workspace takes over no terminal; each tab's panel stays mounted, hidden, while the other is shown, so that the terminal stays open; a workspace that failed says why in a sentence (`src/workspaceError.ts`) with what the server said behind **Details**, and Rebuild where it may be rebuilt; while it is rebuilt, the panel says so in its place, and its steps are the last rebuild's (`rebuild.log`) wherever it had one. Rebuild asks first (`RebuildWorkspaceDialog`, whose button is primary: a rebuild keeps the workspace's files), saying what ends, what is kept and that the repository's commands run again, and for a failed workspace that there is nothing earlier to go back to; it posts `{"confirm": true}` to `/api/workspaces/{id}/rebuild`, puts the entry the answer gives in the list's place and refreshes it, and moves focus to the card's heading, or the panel's for a failed workspace, since the button waits from then on; a refusal is shown in the dialog, and the list refreshed. The question before a delete asks `/api/git/status` itself for a ready workspace, names the files not committed and the commits not pushed (`unsavedIn` in `src/Changes.tsx`), says so where it could not ask (a workspace with no repository has nothing to lose), and offers **Delete** only once the check has answered; after a delete the page says what was deleted, focus goes to the list's heading, since the trash button went with its row, and no workspace is selected where the deleted one was |
| `src/Changes.tsx` | The Changes card of a ready workspace's panel, for the repository of the workspace it is given by id: its heading with the branch bar (`src/BranchBar.tsx`) beside it as its toolbar, what keeps a push or a commit from being offered as message bars, the staged and not staged files, each list a flat group folding under a heading that counts its files and more, with its Stage selected or Unstage selected, a subtle button, beside it, its files' rows divided by strokes, a checkbox for each file that can be selected and Select all, and each file's change as its letter (`m.changeLetters`) in a colour of its own, with its word (`m.changes`) as its accessible text and title; and at the card's foot, under a stroke, Commit, a split button whose menu holds Commit & Push, or why it is not offered, and the status line, "Checked …" (`m.checkedAgo`) worked out again every `CHECKED_TICK_MS` while the section is shown, and Refresh changes. `src/Changes.test.tsx` holds every change to a letter and a word, and the letters to differing. A list is open at first while it has files, opens when files come to it, and otherwise stays as the person left it. The status is fetched when the section opens, every `POLL_MS` while it is open and after every action; an answer older than one asked for since is dropped. Its branches are fetched when it opens, after every action and when the current branch changes. What is checked is kept across refreshes, for files still listed. A refusal is shown beside what was tried until any later action there works |
| `src/Terminal.tsx` | The Terminal tab, a card of a slim bar, which says how the terminal stands beside a status dot of its kind's shape and holds Reconnect, over the screen, edge to edge: a shell in the workspace over the WebSocket `api.openTerminal` opens (`ws:` or `wss:` as the page is, `binaryType` `arraybuffer`), opened as the tab is mounted and closed as it is unmounted — by another workspace selected, another page or the panel going. It loads `src/xtermView.ts` with a dynamic `import()`, so xterm.js is a chunk of its own that the page fetches only when a Terminal tab is first shown. A load that fails is noted as a `page_error` and said in words of its own (`m.terminalNotLoaded`), with a button that reloads the page (`browser.reload` in `src/browser.ts`, which the tests replace) in place of Reconnect: it fails for good where the page was loaded before an upgrade and names a chunk the new build does not have, and the reload gets the new `index.html`, which the backend serves for the browser to check again each time (see [backend.md](backend.md)). The screen is made only once the tab is shown with xterm.js loaded, so that it is never made, nor its size sent, in a hidden panel that cannot be measured; the socket waits for the screen. It sends the size first, as soon as the socket opens, and again on every fit that changes it, then what is typed as binary frames (`TextEncoder`), and draws what rcman sends; it says whether it is connecting, connected or closed, and, once closed, what closed it, in the words of `m.terminalClosed` for a `closed` frame's reason (a reason with none shown as it is), `m.terminalExited` for an `exit` frame, a `problem` frame's problem shown by `ProblemView`, rcman stopping for a close of `close_closing` with no frame, and otherwise the connection lost; **Reconnect** opens a new socket on the same screen, cleared, and puts focus in the screen, since the button goes as it is pressed; nothing reconnects by itself, so a terminal taken over elsewhere (`terminal_replaced`) stays closed. Reconnect is not offered where rcman would refuse another terminal: after a `problem` frame, and after a `closed` frame whose reason is in `REFUSED_AGAIN` (the workspace gone, its person signed out or no longer allowed in); `onFailure` is then called, so that the page refreshes what it shows of the workspace. Where that refresh still lists the workspace as ready, as it can just before a deletion or a sign-out reaches the list, the tab stays without Reconnect until another workspace is selected and then this one again, or the page is reloaded, either of which mounts a new Terminal tab. A socket that closes before it opened, as a refused upgrade does (the page is told no more), is noted as `unreachable` for `GET /api/terminal` (see [Reporting failures](#reporting-failures)) and calls `onFailure`, which refreshes the workspaces once, so that the page says whether rcman is not reached or the sign-in has ended. The screen's height is a share of the window's in `dvh`, which follows the browser's own bars as they come and go, with a floor; `interactive-widget=resizes-content` in `index.html`'s viewport tag has browsers that honour it, Chrome on Android, shrink it for the on-screen keyboard too, so the screen fits above the keyboard, while iOS Safari ignores the key and keeps its own behaviour. The screen is fitted when the tab is shown again and whenever its box changes size (`ResizeObserver`), and its colours read again when the system's colour scheme changes |
| `src/terminalSocket.ts` | The terminal's protocol: `PROTOCOL`, which `tests/test_terminal.py` holds to the backend's (and so to [backend.md](backend.md#the-terminals-protocol)'s table) by loading the module with Node, for which it imports nothing; the resize frame and the reading of rcman's text frames |
| `src/xtermView.ts`, `src/terminalTheme.ts` | xterm.js and its fit add-on behind the few calls the Terminal tab makes, which the component tests replace (jsdom has neither a canvas nor a layout): a `minimumContrastRatio` of WCAG AA's, so that colours a program asks for are readable too, and a cursor that blinks only where the system does not ask for reduced motion; the theme read from the `--terminal-*` properties. Both packages are pinned exactly in `frontend/package.json`, as Biome is: the stylesheet copies xterm.js's own rules, which name its classes, and the fit add-on reads xterm.js's internals, so a new release of either is taken only on purpose |
| `src/BranchBar.tsx` | The branch bar: the branch lookup, a subtle button naming where the repository is, its branch's name in the monospace face, whose pop-up has a filter, which asks rcman nothing, over the local and remote branches listed, each a switch where one is offered; New branch, a subtle icon button of a plus, as Refresh is, whose pop-up holds its form; Refresh branches, which posts `/api/git/fetch`, or, where the section says a sync can run (`syncable`), Sync in its place, which posts `/api/git/sync`, each label calling only its own route, its icon turning while either runs; ahead and behind as muted arrows and counts, with `m.tracking` as their accessible text; and Push, a plain button. It is shown once either the status or the branches are known, so a status that cannot be had leaves the branches to switch to; the arrows and Push wait for the status |
| `src/usePopup.ts` | A pop-up opened by a button, as a disclosure: it closes on Escape, putting focus back on the button, on a press outside it and when focus moves outside it. Every pop-up a button opens uses it, the phone's drawer of the sidebar, the **Account** menu, the Workspaces page's switcher and People's **More actions** among them. It is called with the name of the component that calls it, which its panel carries as `data-component`, for `e2e/touch.spec.ts` to read off what opened. The stylesheet places the branch bar's pop-ups against the Changes heading's row, so they stay inside the card at any width |
| `public/favicon.svg` | The tab's icon, `*rc` in white on a black circle, which `index.html` links; Vite copies `public/` into the build as it is |
| `src/TableScroll.tsx`, `src/markdownTables.ts`, `src/useTitle.ts`, `src/workspaceError.ts` | The box a table scrolls in, and `ROLE`, a table's roles said outright for one a phone draws as a list; the pipe tables of a help page; the browser tab's title; a failed workspace's sentence |
| `src/Icons.tsx` | rcman's own icons, inline SVG strokes on one grid drawn in the text's colour and hidden from assistive technology; the button or text they are in carries the name, from `src/messages.ts`. A new icon is drawn here in the same way; the page takes no icon package |
| `src/Servers.tsx` | A server's states, what each allows and shows, and its card: its state's pill and version, what to do next, a question it asks as a message bar, and its actions in one row; a workspace as the page lists it, `dotFor`, the dot it carries, and `WORKSPACE_KIND`, the kind of pill its own state is drawn as |
| `src/Caret.tsx` | The caret of a disclosure button, wherever one has a caret, turned by its `aria-expanded` |
| `src/Claude.tsx` | The **Claude** page (`ClaudePage`), the account's state read once as the page opens, as a form sheet in a card: the account's address as its title, with its state as a pill (`m.claudeStates`), or that none is connected; what to know of that state as a message bar; the account's facts as rows of a definition list; **Connect Claude account** or **Connect again** where it is offered, the page's one primary button, which opens the connect flow in its place; and at the sheet's foot, under a stroke, a section of its own, **Sign out**: **Sign out of Claude** with **Keep servers running until the last one stops**, in the danger style, as is **Stop my servers and sign out now** while a sign-out waits, even where the sign-in has meanwhile gone |
| `src/Settings.tsx` | The person's own settings, in parts that are each saved on their own, each a card with its title and one line on what it is for beside its form, over it on a phone, and one line of help under each field, the whole story being in `docs/settings.md`; `FIELDS` is every setting it shows, and a test holds `docs/settings.md` to it; the GitHub token part, which stores or removes the token through `/api/github` and never shows it |
| `src/People.tsx`, `src/Everyone.tsx`, `src/Audit.tsx` | Administration. The people: beside the page's heading **Add person**, a primary button opening a dialog that adds a person by address, as a user or an administrator, and stays open with what was typed when the server refuses it; a table in a card of each person, their role, how many workspaces they have and their stored memory, CPU and process limits ("No limit" where there is none), with **Limits**, a subtle button, and **Remove**, a danger outline, which removes them after a confirmation; on a phone a list (`table.stacked`), each row the address in bold over the role, the workspace count and the limits that are set, its actions behind **More actions** (`PersonMenu`), a subtle icon button named with the person's address that opens a `usePopup` menu of **Limits** and **Remove**, Remove in danger's text, and that a choice closes, focus back on it; what was done said in a message bar, in a status region there before it; **Limits** opens a large dialog (a sheet on a phone) that reads `GET /api/people/{id}/limits` and holds a form for the person's limits and one for each workspace's, divided by strokes, each saved on its own with `PUT` (all three fields sent as typed, null for an empty one), with what each uses beside its title, a line of help under each field (`m.limitsHints`) and the long explanation folded under **How limits work**, one refusal shown at a time beside the scope it concerns and under the field it is about, focus moved into the dialog and back to what opened it, the row's **Limits** or its **More actions**, and the words about limits in `src/messages.ts` (`src/users-guide.test.ts` holds `docs/users.md` to them). Everyone's workspaces: a card for each person, their address and how many workspaces they have at its head, and a row for each workspace, its name, its state as a pill, its session's name and its server's Claude Code version, with no actions. The audit trail: loading until its first page is had, then a table in a card, on a phone a list (`table.stacked`) of the action and its result over who and the target, then the time, each action in plain words, dates in UTC, a failed result marked by a danger pill beside its code, a settings target read as a list, and **Older** at the card's foot |
| `src/Versions.tsx` | The Claude Code versions page, among the administrators' pages: the approved, previous and tested versions as facts in a card of its own heading; beside them a card of the newest release, which the server suggests and nothing installs by itself, with **Roll out**, or, where the newest release is marked as failed, a line that says so and no **Roll out**, and the roll-out form; the installed versions, a table in a card, each with how many running servers use it, a **Failed** pill on one marked as failed, and on each that is neither approved nor marked **Use** and **Mark as failed**; the **Failed versions**, a table in a card of each mark, the newest first, with its reason, by whom and when, and **Remove mark**, which asks nothing first, and at the card's head **Mark a version as failed**, for any version, installed or not; and the servers running another version than the approved one, a table in a card, with the **Failed** pill beside a version marked as failed. **Mark as failed** and **Mark a version as failed** open `MarkFailedDialog`, the version (filled in from a row, or typed) and an optional reason, both judged by the server, a refusal shown in the dialog with what was typed kept; a mark made or removed is said in the card's own live region, and focus moves to the card's heading, since the button that had it may go with its row. A refusal that says the report shown is out of date (a roll-out under way, a version marked or its mark removed by another administrator, `STALE`) reads the report again. A roll-out is one request that lasts as long as the checks do; a refusal, the failed checks with what each ran and got back among them, is shown by `src/ProblemView.tsx` |
| `src/Monitor.tsx` | The administrators' page **Monitor**: what the host, each person and each workspace container use now, as `GET /api/monitor` says rcman last collected it (see [backend.md](backend.md)), read as the page opens with `api.get`, whose failure is reported to rcman as any page's first read's is, and again `MONITOR_POLL_MS` after each answer with `api.poll`, whose failure is shown and not reported; a read waits for one still out, a save's too, so reads never overlap; a failed read is shown as the page's problem over the figures read before, until a read works. Under the page's line, the live region of the collection says when each round, usage and disk, last collected and how often it runs, its words worked out again only when a collection time or a saved interval changes, so a read that finds the same times leaves it alone; a round's `error` is a problem message bar, while its time stays that of the last round that worked. **Host**, a card of facts (`dl.facts`): CPU as a percent of one core beside the cores, memory used of the total, processes, each figure large in tabular numerals (`.figure`), and under **Filesystems** each filesystem by its path, with used, size and free, and the folders of rcman's on it in words (`m.monitorRoles`); **People** and **Containers**, tables in cards, on a phone lists of rows (`table.stacked`), a container's state as its pill at the row's right. A scope that is off (null) is an empty state whose link moves focus to the settings card (`SETTINGS_ANCHOR`), as the skip link does, without a fragment in the address, and a figure that is null reads **Not known**. **Monitoring settings**, a card as a Settings page part is (`.setting`), read once with `GET /api/monitor/settings` and never overwritten by a read of the figures: a form of the two intervals, text fields in seconds that ask for a number pad, and the three scopes as checkboxes in a group (`fieldset.choices`), all five sent by `PUT`, a whole number as a number and anything else as typed, for the server to judge; its refusals shown under their fields, and that it is saving and then saved in a status line of its own, after which the figures are read again at once. `src/monitor-guide.test.ts` holds `docs/monitor.md` to the page's words |
| `src/format.ts` | Figures as the page shows them, with `Intl.NumberFormat` in the catalogue's language and a non-breaking space before a unit: sizes in decimal SI units, as Podman's are, rounded further than Podman rounds them (`SIZE`); a percent, with no space before the sign; a count; an interval in the largest unit that holds it whole; and a moment as a date and a time in UTC, which the audit trail, the rcman release page and Monitor show alike |
| `src/Upgrade.tsx` | The administrators' page **rcman release** (`m.navRcman`, a noun as its neighbours are), headed **Upgrade rcman** (see [backend.md](backend.md#upgrading-rcman)): beside the heading **Upgrade rcman**, its one primary button, offered only while the report says one may be asked for, which opens `UpgradeDialog`, a question that says what an upgrade cuts off, whose button keeps its words while the request is out; once one is admitted, focus moves to the card's heading, since the button goes. In the card, the release rcman runs and the last upgrade as facts, each commit whole in a `code` that is not translated; under a stroke, with the option off, a message bar that says so and links the installation guide, and where none may be asked for, the server's reason. While the report says one is requested or running it is read again every `RCMAN_POLL_MS` with `api.poll`, a quiet read that notes no failure, so the restart it makes is not reported to rcman; how it goes, and how it ended, is said in the page's one live region, and once a followed upgrade succeeded **Reload page** (`browser.reload`) follows it. A failure shows its error line and says to roll back from a shell, linking the guide (`GUIDE_ROLLBACK`); `src/upgrade-guide.test.ts` holds `docs/installation.md` to the page's names and links. **New workspace** and **Rebuild** on the Workspaces page and every roll-out on the versions page are not offered while the server says an upgrade is under way (`rcman_upgrading`), and each page says why |
| `src/MessageBar.tsx` | A message bar: a problem, a warning, something to know or something done, with its icon, the element and its role given by what shows it |
| `src/Attention.tsx` | The administrators' warning at the top of every page, a warning's message bar, from `/api/attention`, naming each server on an unrecognised screen by its person and workspace, with a link to everyone's workspaces; fetched on load and on every change of page |
| `src/Docs.tsx` | The help pages, from `/api/docs`: their contents, a column beside the article that stays in view, the page you are on marked, and the article in a card set for reading; on a phone the contents fold under **Help pages**, a disclosure button over the article, open on the index and folded on a page and whenever a page is chosen; a page chosen in them puts focus on `main`, as the sidebar's does |
| `src/clientErrors.ts` | The failures noted for rcman, kept until they are sent (see [Reporting failures](#reporting-failures)) |

## Reporting failures

A failure rcman could not see is reported to it, to be written to the journal
(see [the backend](backend.md#errors-from-the-page) and
[the journal guide](../docs/journal.md#errors-reported-by-the-page)). Its kinds
are `UNREACHABLE`, `SIGNED_OUT` and `UNEXPECTED_RESPONSE` from `src/api.ts`,
and `PAGE_ERROR` from `src/clientErrors.ts`; a problem the server sent is never
reported. A Terminal tab's socket that closed before it opened is an
`UNREACHABLE` for `GET /api/terminal`, with the close code in its message: the
browser says nothing more of a refused upgrade, so the page cannot tell rcman not
reached from a refusal. `noteFailure` in `src/clientErrors.ts` writes the original error to
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
  Those that render anything render the whole app at a route, or, for a part
  that is on no page, that part on its own (`renderAlone` in
  `src/test/render.tsx`, with a page at each other path a test names to
  follow a link to, or to leave it for), against
  `src/test/fakeApi.ts`, which
  answers from a table of routes in the backend's problem-details shape and
  records every call; a route answering `UNREACHABLE_REPLY` makes `fetch`
  reject, as a browser's does, and one answering `SIGNED_OUT_REPLY` answers an
  opaque redirect, as oauth2-proxy's redirect is seen. `renderApp` and `renderAlone` also return the
  router's current location and a `navigate`, whose `navigate(-1)` is the
  browser's back button, and takes `pollMs`, 0 unless a test gives one, and
  `strict`, to render in React's strict mode, as `src/main.tsx` does.
  `src/test/setup.ts` runs the role queries once
  before each file's first test and sets how long a `findBy*` waits, both
  so that the suite passes on a loaded machine; its comments say why. After
  each test it empties the queue of reports (`forgetReports`) and forgets the
  sender (`forgetSending`), so that neither sends during the next test. It puts
  `src/test/fakeXterm.ts` in the place of `src/xtermView.ts` for every test,
  and installs before each `src/test/fakeSocket.ts`, a WebSocket that records
  each socket opened and what the page sent on it, closes as a browser's does,
  its close event a moment after `close()`, and through which a test plays
  rcman's end, and `src/test/fakeLayout.ts`'s ResizeObserver and `matchMedia`,
  which jsdom lacks, and whose layout change and colour scheme a test brings
  about. jsdom has the `<dialog>` element but not its behaviour, so
  `src/test/fakeDialog.ts` stands in for `showModal()`, `close()` and Escape,
  as `e2e/touch.spec.ts` proves the browser's own dialog behaves:
  focus moves into it, Escape cancels it and closes it, and focus returns; what
  it cannot do, keep the page behind out of reach, is proved in the browser. The tests that hold a guide in `docs/` to the page read its tables with
  `guideTable` in `src/test/guideTable.ts`, and the tests that read
  `src/styles.css` take its comments out with `uncommented` in
  `src/test/css.ts`.
- **Browser tests** (Playwright): `e2e/*.spec.ts`, against the built frontend
  served by the real backend from `tests/e2e_server.py` — workspaces on the
  host runtime with the fake `claude`, the people in `e2e/people.ts`, an
  administrator whose workspaces' Claude the world signs in as each is made
  and another person whose it does not, and oauth2-proxy's identity header
  set per test. The parts that are on no page are not in the browser tests.
  No fake `claude` there shows a screen rcman does not recognise, so the
  test of the administrators' warning plants `/api/attention`'s answer.
  `e2e/touch.spec.ts` first proves what the stylesheet and the dialogs build on:
  that Playwright's `isMobile` and `hasTouch` make the pointer coarse, and a
  mouse does not, that a desktop's controls with a mouse are a mouse's size,
  under a finger's, and that the native modal dialog takes focus, keeps it,
  closes on Escape and gives it back. It then walks every page of the route
  table at two phones' widths and a tablet's, on a touch screen, and a
  desktop's with a mouse, with each
  dialog and pop-up open and a workspace on each of its tabs, and fails on a
  page wider than the window, a box that scrolls sideways, inside a dialog or a
  pop-up too, but for those `SCROLL_ALLOWED` names (a data table's, a help
  page's command), a control too small for the pointer, a field's text
  small enough for a phone to zoom in on, an icon-only button with no name,
  and what the design system's checks find (see
  [Rules it keeps](#rules-it-keeps)); each exemption must be met; a planted
  twin of each shows the walk sees it. It also goes from the list to a
  workspace, to another and back on a phone. `e2e/layout.spec.ts` holds the
  shell at two phones' widths and a desktop's: the top bar one row, the one
  navigation as a sidebar or a drawer, with the administrators' group for
  administrators only, the drawer closing on Escape, a page chosen and a press
  outside it, every dialog a sheet or in the middle, and the Workspaces page:
  the list a column of its own beside the panel on a desktop, its selected row
  marked by the accent's bar on a quiet tint, and on a phone the panel under one
  sticky bar of the way back and the switcher, whose sheet rises from the
  screen's foot; the Settings page's parts, each title beside its form on a
  desktop and over it on a phone; the help pages' contents, a column that stays
  in view on a desktop and a disclosure over the article on a phone; a table
  that scrolls sideways inside its card on a phone, the Versions page's, a cell
  planted wider than the window showing the measurement sees it; People and the
  Audit trail as lists at two phones' widths, with a long person and a failed
  event planted, the box not scrolling, each row's shown cells inside it and
  apart, its lead not squeezed (with a planted twin), People's More actions in view and a finger's size, and a planted twin,
  the table not reflowed with its actions kept sticky at its right edge, which
  the check catches; the menu of More actions inside the window and over the
  rows, closed by Escape back to its button; People on a desktop a table with
  its own buttons; Monitor's two tables as lists at the same widths, its answer planted
  with a long address and name and figures not known, each row's shown cells inside it and
  apart; and the Limits dialog, larger
  than the others on a desktop.

  Each run has a world and a port of its own, which `e2e/world.ts` names:
  the world a folder under `/tmp`, with a name no other run has, the prefix
  and a random UUID, whose exact shape the backend's `WORLDS` in
  `tests/e2e_server.py` matches, and the port one the
  system has free. Playwright loads the config again in each worker, so
  `e2e/world.ts` keeps both in the environment, `RCMAN_E2E_DIR` and
  `RCMAN_E2E_PORT`, which the workers and the backend inherit, and keeps
  any already given there; `playwright.config.ts` gives the backend the
  world as `WORLD` from there.

  The world holds one public repository, `https://github.com/octo/widget`
  (`SEEDED_ADDRESS` in `tests/e2e_server.py`), which the specs that make a
  workspace from an address clone; `keep_off_github` gives every git command
  the host runtime runs that may reach a remote, as `tests/git_fixtures.py`
  judges one, the world's folder in place of github.com and refuses one not
  told so, so that nothing typed into the page reaches the real GitHub (see
  [tests.md](tests.md)).

  The backend makes the world, and refuses a folder that already exists.
  It holds the world as a held folder is held, and clears the worlds beside
  it that a killed run left, those whose names `WORLDS` matches, and
  nothing else that begins the same way, as a held folder's prefix is cleared: the
  rules are in [tests.md](tests.md#what-a-killed-run-leaves). When it shuts
  down — Playwright stops it with SIGTERM for that — or fails to start, it
  stops the tmux servers on the sockets in the world's `tmux/` directory
  and removes the world; it stops only those, in the world itself, when it
  clears a world too.

  So runs at the same time, from different checkouts or worktrees, or two
  in one, do not share a world, a port or a tmux server. Two runs in one
  checkout share `dist/`, which `make e2e` builds before each, and the
  config's output folder, whose files one run's Playwright removes while
  the other's still writes there; runs at the same time are each in a
  checkout or worktree of their own.
- **Whole-stack browser tests**: `e2e-stack/*.spec.ts` with
  `playwright.stack.config.ts`, against the whole stack, rcman behind real Caddy and
  oauth2-proxy in a container of the tests' engine — see [tests.md](tests.md#the-whole-stack).
  They run in Playwright's own image, in a container that joins the stack's network. They are
  run on demand by `make check-ui` (not by `make e2e`, `make check` or `make test-container`),
  which needs `make frontend-setup` once, like `make e2e`: the browser's container uses the
  frontend's packages. The config takes the port Caddy listens on in the stack's network from
  `RCMAN_STACK_PORT`, which `tests/container/test_stack_in_a_browser.py` sets, and refuses to load
  without it. A spec that needs something done inside the stack, such as a repository in a
  workspace (`stack.py repository`), runs the command in `RCMAN_STACK_EXEC`, a JSON argv
  prefix that hands it to the suite (`tests/container/stack_exec.mjs`), which runs it in the
  stack's container. The specs that make a workspace from a GitHub address clone from the
  stack's stand-in GitHub.

Every config gives Chromium `--disable-dev-shm-usage`: `/dev/shm` is small in a container, a
workspace's and the browser's container of the whole stack among them;
`tests/test_e2e_server.py` holds every config to it, `playwright.screenshots.config.ts`,
which `make screenshots` runs and no test does ([tests.md](tests.md#screenshots)), among them.

Each Playwright config writes to an output folder of its own under
`test-results/`, its `outputDir`, because Playwright empties a run's output
folder as the run starts.
