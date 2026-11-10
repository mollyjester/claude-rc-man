// Validation belongs to the server: no input may carry a rule of its own.
// Each form leads with what it does: its submit button is its one primary
// button, or its one danger button where what it does cannot be undone.
import { readdirSync, readFileSync } from "node:fs";
import { resolve, sep } from "node:path";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type ReactElement, useState } from "react";
import { parseAst } from "vite";
import { vi } from "vitest";

import { type ApiError, api as client, forgetSending } from "./api";
import { Changes } from "./Changes";
import { ClaudeConnection, type Connection } from "./Claude";
import { forgetReports } from "./clientErrors";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";
import { callSites } from "./test/callSites";
import {
  ADMIN_USER,
  CONNECTED,
  fakeApi,
  NOT_CONNECTED,
  NOTHING_TO_ATTEND,
  SETTINGS,
  SIGNING_OUT,
} from "./test/fakeApi";
import { modalDialogs } from "./test/fakeDialog";
import { renderAlone, renderApp } from "./test/render";

const RULE_ATTRIBUTES = ["required", "pattern", "min", "max", "minlength", "maxlength", "step"];

function api() {
  return fakeApi({
    "GET /api/me": ADMIN_USER,
    "GET /api/claude": NOT_CONNECTED,
    "GET /api/me/settings": SETTINGS,
    "GET /api/people": {
      people: [
        {
          id: 2,
          email: "bob@example.com",
          admin: false,
          state: "active",
          workspaces: 1,
          limits: { memory: null, cpu: null, pids: "500" },
        },
      ],
    },
    "GET /api/people/2/limits": {
      person: { limits: { memory: null, cpu: null, pids: "500" }, usage: null },
      workspaces: [
        {
          id: 1,
          name: "alpha",
          state: "ready",
          limits: { memory: null, cpu: null, pids: null },
          usage: { memory: "1G", memory_bytes: 1073741824, tasks: 3 },
        },
      ],
    },
    "GET /api/claude/versions": {
      approved: "1.0.0",
      previous: null,
      tested: "1.0.0",
      installed: [],
      drift: [],
      failed: [],
      upgrading: false,
    },
    "GET /api/claude/latest": { latest: null },
    "GET /api/rcman/upgrade": {
      release: null,
      enabled: true,
      available: true,
      reason: null,
      state: "unknown",
      started: null,
      finished: null,
      from: null,
      to: null,
      error: null,
    },
    "POST /api/claude/login": { url: "https://claude.com/x" },
    "GET /api/workspaces": {
      claude_approved: "1.0.0",
      workspaces: [
        {
          id: 1,
          name: "alpha",
          device: "rcman-ws-1",
          state: "ready",
          error: null,
          source: null,
          source_page: null,
          log: null,
          server: null,
          server_error: null,
          claude_version: null,
          rebuild: { state: null, log: null, error: null },
          rebuildable: true,
          stoppable: true,
        },
        {
          id: 2,
          name: "beta",
          device: "rcman-ws-2",
          state: "creating",
          error: null,
          source: null,
          source_page: null,
          log: null,
          server: null,
          server_error: null,
          claude_version: null,
        },
      ],
    },
    "GET /api/workspaces/1/devcontainer": { changed: true },
    "GET /api/attention": NOTHING_TO_ATTEND,
    "GET /api/git/status": {
      branch: { name: "main", commit: "0123456789", upstream: null, ahead: null, behind: null },
      staged: { files: [{ path: "a.txt", change: "added" }], more: 0 },
      not_staged: { files: [{ path: "b.txt", change: "untracked" }], more: 0 },
      author: { name: "Alice", email: "alice@example.com" },
      merging: false,
      push_has_remote: true,
      push_to_github: false,
      github_token: null,
      push_ahead: 1,
    },
    "GET /api/git/branches": {
      local: { branches: [], more: 0 },
      remote: { branches: [], more: 0 },
    },
    "GET /api/github": { token: true },
    "GET /api/monitor": {
      scopes: { host: false, people: false, containers: false },
      usage: { collected_at: null, error: null },
      disk: { collected_at: null, error: null },
      host: null,
      people: null,
      containers: null,
    },
    "GET /api/monitor/settings": {
      settings: {
        usage_interval_seconds: 60,
        disk_interval_seconds: 3600,
        host: false,
        people: false,
        containers: false,
      },
    },
    "GET /api/forwarding/settings": {
      settings: { enabled: false, idle_minutes: 5, absolute_hours: 12 },
    },
  });
}

// Every input on screen, once the page has one of its own besides the header's:
// a form may hold only a checkbox.
async function inputs() {
  const all = () => [...document.querySelectorAll("input, textarea, select")];
  await waitFor(() => expect(all().some((input) => !input.closest("header"))).toBe(true));
  return all();
}

// Where a page is: a path in the app, or, for a part that is tried on its own, that
// part alone. Each is held to the same rules.
type Where = string | (() => ReactElement);

function show(where: Where) {
  return typeof where === "string" ? renderApp(where) : renderAlone(where(), ["/settings"]);
}

const account = () => (
  <ClaudeConnection connection={NOT_CONNECTED as Connection} onChange={() => {}} />
);
const connected = () => (
  <ClaudeConnection connection={CONNECTED as Connection} onChange={() => {}} />
);
const signingOut = () => (
  <ClaudeConnection connection={SIGNING_OUT as Connection} onChange={() => {}} />
);
// A workspace's Changes section on its own: its pop-up forms are closed by a press
// on any other form, so the others on a page would shut them before they are checked.
const changes = () => <Changes workspace={1} pollMs={0} />;

// New workspace's dialog, opened by its button beside the Workspaces page's heading.
async function openNewWorkspace() {
  await userEvent.click(await screen.findByRole("button", { name: m.newWorkspace }));
  return screen.findByLabelText(m.newWorkspaceName);
}

// The dialog a workspace's trash button opens, in the list of the Workspaces page.
async function openTrash(name: string) {
  const list = await screen.findByRole("region", { name: m.navDashboard });
  await userEvent.click(
    await within(list).findByRole("button", { name: m.deleteWorkspaceNamed(name) }),
  );
  await screen.findByRole("dialog");
}

// Every page with a form, and what opens the forms that are not there at first.
const PAGES: [string, Where, () => Promise<void>][] = [
  ["settings", "/settings", async () => {}],
  [
    "adding someone",
    "/admin/people",
    async () => {
      await userEvent.click(await screen.findByRole("button", { name: m.usersAdd }));
      await screen.findByRole("dialog", { name: m.usersAdd });
    },
  ],
  [
    "limiting someone",
    "/admin/people",
    async () => {
      const bob = await screen.findByRole("row", { name: /bob/ });
      await userEvent.click(within(bob).getByRole("button", { name: m.usersLimits }));
      await screen.findAllByLabelText(m.usersMemory); // the person's and each workspace's
    },
  ],
  ["a new workspace", "/", async () => void (await openNewWorkspace())],
  ["Claude Code versions", "/admin/claude", async () => {}],
  [
    "marking a version as failed",
    "/admin/claude",
    async () => {
      await userEvent.click(await screen.findByRole("button", { name: m.versionsMarkAnyFailed }));
      await screen.findByRole("dialog", { name: m.versionsMarkAnyFailed });
    },
  ],
  ["the service settings", "/admin/settings", async () => {}],
  [
    "signing out of rcman",
    "/settings",
    async () => {
      const banner = await screen.findByRole("banner");
      await userEvent.click(within(banner).getByRole("button", { name: m.account }));
      await within(banner).findByRole("checkbox", { name: m.keepServersOnSignOut });
    },
  ],
  ["signing out of Claude", connected, async () => {}],
  [
    "connecting Claude, its address",
    account,
    async () => {
      await userEvent.click(await screen.findByRole("button", { name: m.connect }));
      await screen.findByLabelText(m.connectEmail);
    },
  ],
  [
    "connecting Claude, its code",
    account,
    async () => {
      await userEvent.click(await screen.findByRole("button", { name: m.connect }));
      await userEvent.click(screen.getByRole("button", { name: m.connectGo }));
      await screen.findByLabelText(m.connectCode);
    },
  ],
  // The forms of a repository's Changes section, each found by its field.
  ["a commit", changes, async () => void (await screen.findByLabelText(m.commitMessage))],
  [
    "a new branch",
    changes,
    async () => {
      const plus = await screen.findByRole("button", { name: m.newBranch });
      expect(plus.textContent).toBe(""); // the + of the branch bar, an icon only
      await userEvent.click(plus);
      await screen.findByLabelText(m.newBranchName);
    },
  ],
  [
    "a commit, its menu open",
    changes,
    async () => {
      await userEvent.click(await screen.findByRole("button", { name: m.commitMenu }));
      await screen.findByRole("button", { name: m.commitPush });
    },
  ],
];

// Pages with an input outside any form, and what opens it: held only to having
// no validation rule of its own, since they add no form.
const OUTSIDE_FORMS: [string, Where, () => Promise<void>][] = [
  [
    "the branch lookup's filter",
    changes,
    async () => {
      await userEvent.click(await screen.findByRole("button", { name: m.onBranch("main") }));
      await screen.findByRole("textbox", { name: m.findBranch });
    },
  ],
];

// Pages with a form that has nothing to type in, and what opens it.
const CHOICES: [string, Where, () => Promise<void>][] = [
  [
    "removing someone",
    "/admin/people",
    async () => {
      const bob = await screen.findByRole("row", { name: /bob/ });
      await userEvent.click(within(bob).getByRole("button", { name: m.usersRemove }));
      await screen.findByRole("dialog");
    },
  ],
  [
    "finishing a sign-out at once",
    signingOut,
    async () => void (await screen.findByRole("button", { name: m.claudeSignOutNow })),
  ],
  [
    "removing the GitHub token",
    "/settings",
    async () => {
      const github = await screen.findByRole("region", { name: m.settingsGithubTitle });
      await userEvent.click(
        await within(github).findByRole("button", { name: m.githubTokenRemove }),
      );
      await screen.findByRole("dialog");
    },
  ],
  ["deleting a workspace", "/?workspace=1", () => openTrash("alpha")],
  [
    "rebuilding a workspace",
    "/?workspace=1",
    async () => {
      const card = await screen.findByRole("region", { name: m.containerTitle });
      await userEvent.click(await within(card).findByRole("button", { name: m.rebuild }));
      await screen.findByRole("dialog", { name: m.rebuildTitle("alpha") });
    },
  ],
  [
    "upgrading rcman",
    "/admin/rcman",
    async () => {
      await userEvent.click(await screen.findByRole("button", { name: m.upgradeRcman }));
      await screen.findByRole("dialog", { name: m.upgradeRcman });
    },
  ],
  // beta is being made, so its trash button says why it cannot be deleted yet
  ["a workspace that cannot be deleted", "/", () => openTrash("beta")],
];

// What is wrong with the inputs under root: a validation rule of an input's own, an input of a
// kind other than text, a checkbox or a radio but for the one secret a page takes, the GitHub
// token, typed where it is not shown, and a form that is not noValidate.
function validationFaults(root: ParentNode): string[] {
  const found: string[] = [];
  for (const input of root.querySelectorAll("input, textarea, select")) {
    for (const attribute of RULE_ATTRIBUTES)
      if (input.hasAttribute(attribute)) found.push(`${input.outerHTML} has ${attribute}`);
    if (!(input instanceof HTMLInputElement)) continue;
    const token =
      input.type === "password" &&
      input.closest("section")?.querySelector("h2")?.textContent === m.settingsGithubTitle;
    if (!token && !["text", "checkbox", "radio"].includes(input.type))
      found.push(`${input.outerHTML} is a ${input.type}`);
  }
  for (const form of root.querySelectorAll("form:not([novalidate])"))
    found.push(`${form.outerHTML.slice(0, 40)} is not noValidate`);
  return found;
}

test.each([...PAGES, ...OUTSIDE_FORMS])(
  "no input on %s carries its own validation rule",
  async (_, where, open) => {
    api();
    show(where);
    await open();
    expect((await inputs()).length).toBeGreaterThan(0); // non-vacuity
    expect(validationFaults(document)).toEqual([]);
  },
);

test("the validation check fails on a rule of an input's own, a number field, and a form left to the browser", () => {
  const root = document.createElement("div");
  root.innerHTML =
    '<form noValidate><input type="text" name="ok" /></form><form><input type="number" required /></form>';
  const faults = validationFaults(root);
  expect(faults).toHaveLength(3);
  expect(faults[0]).toMatch(/ has required$/);
  expect(faults[1]).toMatch(/ is a number$/);
  expect(faults[2]).toMatch(/ is not noValidate$/);
});

// What is wrong with the buttons of each form under root: a form whose leading
// buttons — primary, or danger where what it does cannot be undone — are not
// exactly one, or whose one is not its submit button. A dialog asks for a
// choice, so it is a form too.
//
// The header's sign-out form is the one exemption: leaving rcman is not what a
// page is for, so its button is plain, beside the box that says what happens to
// the servers. The exemption names that form and is held below to its subject.
function hierarchyFaults(root: ParentNode): string[] {
  const named = (dialog: Element) =>
    dialog.getAttribute("aria-label") ??
    document.getElementById(dialog.getAttribute("aria-labelledby") ?? "")?.textContent;
  const formless = [...root.querySelectorAll('dialog, [role="dialog"]')]
    .filter((dialog) => !dialog.querySelector("form"))
    .map((dialog) => `${named(dialog)}: a dialog with no form`);
  const forms = [...root.querySelectorAll("form")].flatMap((form) => {
    if (form.matches("header form.sign-out")) return [];
    const leading = [...form.querySelectorAll("button.primary, button.danger")];
    const name = form.textContent?.trim().slice(0, 40);
    if (leading.length !== 1) return [`${name}: ${leading.length} leading buttons`];
    if ((leading[0] as HTMLButtonElement).type !== "submit")
      return [`${name}: its leading button does not submit it`];
    return [];
  });
  return [...formless, ...forms];
}

// The forms whose submit cannot be undone, which lead with danger instead.
const DESTRUCTIVE = ["removing someone", "deleting a workspace", "removing the GitHub token"];

// Forms that lead with danger where they are, with no dialog to ask first: the choice is the
// danger button itself.
const DANGER_IN_PLACE = ["signing out of Claude", "finishing a sign-out at once"];

test.each([...PAGES, ...CHOICES])(
  "each form on %s leads with one button, its submit button, danger where it cannot be undone",
  async (page, where, open) => {
    api();
    show(where);
    await open();
    if (!CHOICES.some(([choice]) => choice === page)) await inputs();
    expect(document.querySelectorAll("form").length).toBeGreaterThan(0); // non-vacuity
    expect(hierarchyFaults(document)).toEqual([]);
    if (DESTRUCTIVE.includes(page)) {
      const dialog = screen.getByRole("dialog");
      expect(dialog.querySelector('button[type="submit"]')).toHaveClass("danger");
      expect(dialog.querySelectorAll("button.primary")).toHaveLength(0);
    } else if (DANGER_IN_PLACE.includes(page)) {
      const submits = [
        ...document.querySelectorAll<HTMLButtonElement>('form button[type="submit"]'),
      ];
      expect(submits.length).toBeGreaterThan(0);
      for (const submit of submits) expect(submit).toHaveClass("danger");
      expect(document.querySelectorAll("form button.primary")).toHaveLength(0);
    } else {
      expect(document.querySelectorAll("form button.primary").length).toBeGreaterThan(0);
      expect(document.querySelectorAll("form button.danger")).toHaveLength(0);
    }
  },
);

// What is wrong with the button that opened a destructive form: it does not
// itself do what cannot be undone, so it is a danger outline, not plain, not
// primary and not danger.
function openerFaults(opener: Element | undefined): string[] {
  if (!opener) return ["nothing opened it"];
  const name = opener.textContent?.trim();
  if (!opener.classList.contains("secondary-danger")) return [`${name}: not a danger outline`];
  if (opener.classList.contains("primary") || opener.classList.contains("danger"))
    return [`${name}: leads as a form would`];
  return [];
}

test.each([...PAGES, ...CHOICES].filter(([page]) => DESTRUCTIVE.includes(page)))(
  "what opens the form for %s is a danger outline",
  async (_, where, open) => {
    api();
    show(where);
    const clicked: Element[] = [];
    const click = userEvent.click;
    const spy = vi.spyOn(userEvent, "click").mockImplementation(async (element) => {
      clicked.push(element);
      return click(element);
    });
    try {
      await open();
    } finally {
      spy.mockRestore();
    }
    expect(screen.getByRole("dialog")).toBeInTheDocument(); // positive control: it opened
    expect(openerFaults(clicked.at(-1))).toEqual([]);
  },
);

test("the opener check fails on a plain, primary or danger button, or none", () => {
  const button = (className: string) =>
    Object.assign(document.createElement("button"), { className, textContent: "Remove" });
  expect(openerFaults(button("secondary-danger"))).toEqual([]);
  for (const className of ["", "secondary", "primary", "danger", "secondary-danger danger"]) {
    expect(openerFaults(button(className)), className).toHaveLength(1);
  }
  expect(openerFaults(undefined)).toEqual(["nothing opened it"]);
});

test("the leading check fails on a form with no leading button and a dialog with no form, and exempts only the header's sign-out", async () => {
  const root = document.createElement("div");
  root.innerHTML =
    '<form><button type="submit" class="primary">Save</button><button type="button">Cancel</button></form>';
  expect(hierarchyFaults(root)).toEqual([]); // positive control: a form that leads as it should
  root.innerHTML =
    '<form><button type="submit">Save</button></form><div role="dialog" aria-label="Remove"><button class="primary">Remove</button></div>';
  expect(hierarchyFaults(root)).toEqual([
    "Remove: a dialog with no form",
    "Save: 0 leading buttons",
  ]);
  // the exemption is held to its subject: the header's sign-out form, and that form only
  api();
  renderApp("/settings");
  const form = (await screen.findByRole("banner")).querySelector("form.sign-out")!;
  expect(form).not.toBeNull();
  expect(form.querySelectorAll("button.primary, button.danger")).toHaveLength(0);
  expect(hierarchyFaults(document)).toEqual([]);
  const copy = document.createElement("div");
  copy.append(form.cloneNode(true));
  expect(hierarchyFaults(copy)).toHaveLength(1);
  // every form named as leading with danger is one of those checked
  const pages = [...PAGES, ...CHOICES].map(([page]) => page);
  for (const page of [...DESTRUCTIVE, ...DANGER_IN_PLACE]) expect(pages).toContain(page);
});

// The inputs of a form whose refusal names a field: each carries the field's
// name, so the server's error can be matched to it. A radio's label names one
// choice, not the field, so radios are not among them.
type Field = HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;

// Every field of a form a person fills in: inputs but radio buttons, text areas and choices.
function fieldInputs(form: HTMLFormElement): Field[] {
  return [...form.querySelectorAll<Field>("input, textarea, select")].filter(
    (input) => input.type !== "radio",
  );
}

// What a form's refusal shows wrongly: an input with no field name, or a field
// error shown neither under the input, which is then marked invalid and
// described by it, nor named by the input's label in the form's list of what
// was refused. Every request but a GET is refused with an error for each of
// the form's fields.
async function labelFaults(form: HTMLFormElement): Promise<string[]> {
  const faults: string[] = [];
  // Read before submitting: a refusal may take the form away.
  // The label's own words, without what the field itself holds, such as a choice's options.
  const label = (input: Field) =>
    (input.labels?.[0]?.textContent ?? "").replace(input.textContent ?? "", "").trim();
  const named = fieldInputs(form)
    .filter((input) => {
      if (!input.name) faults.push(`${label(input)}: an input with no field name`);
      return Boolean(input.name);
    })
    .map((input) => ({ name: input.name, label: label(input), element: input }));
  const answer = globalThis.fetch;
  const refuse = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    if ((init?.method ?? "GET").toUpperCase() === "GET") return answer(input, init);
    const errors = named.map((i) => ({ field: i.name, message: `planted reason for ${i.name}` }));
    const body = {
      type: "urn:rcman:problem:validation_failed",
      title: "Refused.",
      status: 422,
      code: "validation_failed",
      errors,
    };
    return new Response(JSON.stringify(body), {
      status: 422,
      headers: { "content-type": "application/problem+json" },
    });
  });
  vi.stubGlobal("fetch", refuse);
  try {
    // A refusal not shown before: pressing this form's button may close a pop-up
    // that held another form's, so a count of them could stay the same.
    const before = new Set(screen.queryAllByRole("alert"));
    await userEvent.click(form.querySelector('button[type="submit"]')!);
    await waitFor(() =>
      expect(screen.queryAllByRole("alert").some((shown) => !before.has(shown))).toBe(true),
    );
    for (const input of named) {
      const message = `planted reason for ${input.name}`;
      const under =
        input.element.getAttribute("aria-invalid") === "true" &&
        (input.element.getAttribute("aria-describedby") ?? "")
          .split(" ")
          .some((id) => document.getElementById(id)?.textContent === message);
      const listed = screen.queryByText(`${input.label}: ${message}`) !== null;
      if (!under && !listed)
        faults.push(`${input.name}: not shown under its input or as ${input.label}`);
    }
  } finally {
    vi.stubGlobal("fetch", answer);
  }
  return faults;
}

test.each(PAGES)("a refused field on %s is named by its label", async (_, where, open) => {
  api();
  show(where);
  await open();
  await inputs();
  // A form in a pop-up that is closed, as the header's sign-out is until its menu is opened,
  // cannot be filled in; the entry that opens it checks it.
  const forms = [...document.querySelectorAll("form")].filter(
    (form) => fieldInputs(form).length > 0 && form.closest("[hidden]") === null,
  );
  expect(forms.length).toBeGreaterThan(0); // non-vacuity
  for (const form of forms) expect(await labelFaults(form)).toEqual([]);
});

// --- every form, found in the source -------------------------------------------------
// A form is known by its component and its submit handler, "Component.handler":
// in the source, the component whose render holds the <form> and the function
// its onSubmit names; on screen, the same two as React's development build
// records them on the form's element.

type AstNode = { type: string; start: number } & Record<string, unknown>;

const isNode = (value: unknown): value is AstNode =>
  typeof value === "object" &&
  value !== null &&
  typeof (value as { type?: unknown }).type === "string" &&
  typeof (value as { start?: unknown }).start === "number";

// A property of an object or a function, or undefined for anything else.
function property(value: unknown, name: string): unknown {
  if ((typeof value !== "object" || value === null) && typeof value !== "function") return;
  return (value as Record<string, unknown>)[name];
}

const capitalised = (name: unknown): name is string =>
  typeof name === "string" && /^[A-Z]/.test(name);

// The component a node is rendered by: the nearest function around it named
// with a capital, as a function declaration or a const bound to a function.
function componentOf(ancestors: AstNode[]): { name: string; node: AstNode } | null {
  for (let i = ancestors.length - 1; i >= 0; i--) {
    const node = ancestors[i];
    const own = property(property(node, "id"), "name");
    if (node.type === "FunctionDeclaration" || node.type === "FunctionExpression") {
      if (capitalised(own)) return { name: own, node };
    }
    if (node.type === "ArrowFunctionExpression" || node.type === "FunctionExpression") {
      const parent = ancestors[i - 1];
      const bound = property(property(parent, "id"), "name");
      if (parent?.type === "VariableDeclarator" && capitalised(bound)) return { name: bound, node };
    }
  }
  return null;
}

// Every node under a node, the node first.
function* nodesIn(node: AstNode): Generator<AstNode> {
  yield node;
  for (const value of Object.values(node)) {
    for (const child of Array.isArray(value) ? value : [value]) {
      if (isNode(child)) yield* nodesIn(child);
    }
  }
}

const FUNCTIONS = ["ArrowFunctionExpression", "FunctionExpression"];

// How a name is declared in a component: as a function, which carries the name
// React shows; as a const bound to anything else, such as a handler wrapped in
// useCallback, whose function has no name of its own; or not at all.
function declared(component: AstNode, name: string): "function" | "other" | "none" {
  for (const node of nodesIn(component)) {
    if (property(property(node, "id"), "name") !== name) continue;
    if (node.type === "FunctionDeclaration") return "function";
    if (node.type === "VariableDeclarator")
      return FUNCTIONS.includes(String(property(property(node, "init"), "type")))
        ? "function"
        : "other";
  }
  return "none";
}

// The forms in the source files, each as "Component.handler", and what keeps a
// form from being known so: an onSubmit that is missing or not the bare name of
// a function its component declares, a form outside a component, or two forms
// known by the same name.
function sourceForms(files: [string, string][]): { ids: Set<string>; faults: string[] } {
  const ids = new Set<string>();
  const faults: string[] = [];
  for (const [file, source] of files) {
    const at = (node: AstNode) => `${file}:${source.slice(0, node.start).split("\n").length}`;
    const visit = (node: AstNode, ancestors: AstNode[]) => {
      const opening = property(node, "openingElement");
      if (node.type === "JSXElement" && property(property(opening, "name"), "name") === "form") {
        const attributes = property(opening, "attributes");
        const onSubmit = (Array.isArray(attributes) ? attributes : []).find(
          (a) => property(property(a, "name"), "name") === "onSubmit",
        );
        const expression = property(property(onSubmit, "value"), "expression");
        const handler = property(expression, "name");
        const component = componentOf(ancestors);
        if (property(expression, "type") !== "Identifier" || typeof handler !== "string")
          faults.push(`${at(node)}: a form whose onSubmit is not a function's name`);
        else if (component === null) faults.push(`${at(node)}: a form outside a component`);
        else {
          const how = declared(component.node, handler);
          const id = `${component.name}.${handler}`;
          if (how === "other")
            faults.push(
              `${at(node)}: ${id} is not bound to a function, as a wrapped handler is not`,
            );
          else if (how === "none")
            faults.push(`${at(node)}: ${id} is not a function the component declares`);
          else if (ids.has(id)) faults.push(`${at(node)}: a second form known as ${id}`);
          else ids.add(id);
        }
      }
      for (const value of Object.values(node)) {
        for (const child of Array.isArray(value) ? value : [value]) {
          if (isNode(child)) visit(child, [...ancestors, node]);
        }
      }
    };
    visit(parseAst(source, { lang: "tsx" }) as unknown as AstNode, []);
  }
  return { ids, faults };
}

// The page's own source files: every .tsx under src but the tests and their tools.
const SOURCES: [string, string][] = readdirSync(__dirname, { recursive: true, encoding: "utf-8" })
  .filter((f) => f.endsWith(".tsx") && !f.endsWith(".test.tsx") && !f.startsWith(`test${sep}`))
  .map((f) => [f, readFileSync(resolve(__dirname, f), "utf-8")]);

// The form on screen as "Component.handler": the handler's name from the
// element's current props, and the component that rendered it from its fiber's
// owner. The fiber's own props are not read: React reuses a form's element for
// the next form rendered in its place, and they may be the earlier form's.
function formId(form: HTMLFormElement): string {
  const react = (prefix: string) => {
    const key = Object.keys(form).find((k) => k.startsWith(prefix));
    return key === undefined ? undefined : property(form, key);
  };
  const props = react("__reactProps$");
  const fiber = react("__reactFiber$");
  const where = form.outerHTML.slice(0, 80);
  if (props === undefined || fiber === undefined)
    throw new Error(`a form React did not render: ${where}`);
  const onSubmit = property(props, "onSubmit");
  if (typeof onSubmit !== "function") throw new Error(`a form with no onSubmit: ${where}`);
  if (!onSubmit.name)
    throw new Error(
      `a form whose onSubmit has no function name, a wrapped or anonymous handler: ${where}`,
    );
  const owner = property(property(property(fiber, "_debugOwner"), "type"), "name");
  if (typeof owner !== "string" || !owner)
    throw new Error(`a form whose component React does not name: ${where}`);
  return `${owner}.${onSubmit.name}`;
}

// A form seen on screen: the entry that opened it, whether that entry is one of
// PAGES, which the label check visits, and whether the form has a field.
type Seen = { entry: string; fromPages: boolean; id: string; fields: boolean };
// A dialog seen on screen: the entry that opened it, the component that drew it with the
// shared Dialog (src/Dialog.tsx), and whether it was the native element, opened as a modal.
type SeenDialog = { entry: string; owner: string; modal: boolean };

// The component that drew a dialog: the owner of the Dialog that rendered the element.
function dialogOwner(dialog: Element): string {
  const key = Object.keys(dialog).find((k) => k.startsWith("__reactFiber$"));
  const shared = property(key === undefined ? undefined : property(dialog, key), "_debugOwner");
  if (property(property(shared, "type"), "name") !== "Dialog") return "not the shared Dialog";
  return String(property(property(property(shared, "_debugOwner"), "type"), "name") ?? "");
}

// Opens every entry of PAGES and CHOICES in turn, and notes each form shown;
// once stopped, it opens no further entry, so renders nothing more.
async function observe(stopped: () => boolean, dialogs: SeenDialog[]): Promise<Seen[]> {
  const seen: Seen[] = [];
  for (const [entries, fromPages] of [
    [PAGES, true],
    [CHOICES, false],
  ] as const) {
    for (const [entry, where, open] of entries) {
      if (stopped()) throw new Error("stopped before every page was visited");
      api();
      show(where);
      try {
        await open();
        if (fromPages) await inputs();
        for (const form of document.querySelectorAll("form"))
          seen.push({ entry, fromPages, id: formId(form), fields: fieldInputs(form).length > 0 });
        for (const dialog of document.querySelectorAll('dialog, [role="dialog"]'))
          dialogs.push({
            entry,
            owner: dialogOwner(dialog),
            modal:
              dialog.tagName === "DIALOG" && modalDialogs().includes(dialog as HTMLDialogElement),
          });
      } finally {
        cleanup();
        forgetReports();
        forgetSending();
      }
    }
  }
  return seen;
}

// What the tests' pages miss of the dialogs in the source: components that draw one that no
// entry opens, dialogs opened that no such component drew, and any not opened as a modal.
function dialogFaults(source: string[], seen: SeenDialog[]) {
  const owners = new Set(seen.map((d) => d.owner));
  return {
    missing: source.filter((name) => !owners.has(name)).sort(),
    unknown: [...owners].filter((name) => !source.includes(name)).sort(),
    notModal: seen.filter((d) => !d.modal).map((d) => `${d.entry}: ${d.owner}`),
  };
}

// What the tests' pages miss of the forms in the source: forms none of them
// opens, forms opened that the source does not have, and forms with a field
// that no entry of PAGES shows with it, which the label check would not reach.
function coverageFaults(source: Set<string>, seen: Seen[]) {
  const ids = new Set(seen.map((s) => s.id));
  const labelled = new Set(seen.filter((s) => s.fromPages && s.fields).map((s) => s.id));
  return {
    missing: [...source].filter((id) => !ids.has(id)).sort(),
    unknown: [...ids].filter((id) => !source.has(id)).sort(),
    unlabelled: [...new Set(seen.filter((s) => s.fields).map((s) => s.id))]
      .filter((id) => !labelled.has(id))
      .sort(),
  };
}

// How long a visit to every page may take, on a slow or busy machine.
const EVERY_PAGE_MS = 60_000;

// The tests that need the forms on screen share one visit to every page, made
// before the first of them, whichever runs. A visit that outlives its time is
// stopped and waited for, so that it renders nothing into a later test.
describe("the forms on screen", () => {
  let seen: Seen[] = [];
  const dialogs: SeenDialog[] = [];
  let stop = false;
  let visit: Promise<unknown> = Promise.resolve();

  beforeAll(async () => {
    const visiting = observe(() => stop, dialogs);
    visit = visiting.catch(() => {});
    seen = await visiting;
  }, EVERY_PAGE_MS);

  afterAll(async () => {
    stop = true;
    await visit;
  }, EVERY_PAGE_MS);

  test("every form in the source is opened by a page these tests visit, each with a field by one the label check visits", () => {
    const source = sourceForms(SOURCES);
    expect(source.faults).toEqual([]);
    expect(source.ids).toContain("Versions.upgrade"); // positive control: the source was read
    expect(coverageFaults(source.ids, seen)).toEqual({
      missing: [],
      unknown: [],
      unlabelled: [],
    });
  });

  test("every component that draws a dialog is opened by a page these tests visit, each the native modal, and no other file draws one", () => {
    const source = callSites(SOURCES).dialogs;
    expect(source).toContain("LimitsDialog"); // positive control: the source was read
    expect(dialogFaults(source, dialogs)).toEqual({ missing: [], unknown: [], notModal: [] });
    // and no other file draws a dialog of its own: the shared one is among the files read
    expect(SOURCES.some(([file, text]) => file === "Dialog.tsx" && text.includes("<dialog"))).toBe(
      true,
    );
    const own = SOURCES.filter(
      ([file, text]) =>
        file !== "Dialog.tsx" && (/role=["{]+dialog/.test(text) || text.includes("<dialog")),
    ).map(([file]) => file);
    expect(own).toEqual([]);
  });

  test("the dialog check reports a dialog in the source no page opens, and one not opened as a modal", () => {
    const source = callSites(SOURCES).dialogs;
    expect(dialogFaults([...source, "Planted"], dialogs).missing).toEqual(["Planted"]);
    const plain = { entry: "planted", owner: "Planted", modal: false };
    expect(dialogFaults(source, [...dialogs, plain])).toEqual({
      missing: [],
      unknown: ["Planted"],
      notModal: ["planted: Planted"],
    });
  });

  test("the coverage check reports a form in the source no page opens", () => {
    const source = sourceForms(SOURCES).ids;
    const real = coverageFaults(source, seen);
    expect(source.has("Planted.send")).toBe(false); // the plant is new
    expect(coverageFaults(new Set([...source, "Planted.send"]), seen)).toEqual({
      ...real,
      missing: [...real.missing, "Planted.send"].sort(),
    });
  });
});

// A form whose ProblemView is given no labels.
function Unlabelled() {
  const [error, setError] = useState<ApiError | null>(null);
  return (
    <form
      noValidate
      onSubmit={async (event) => {
        event.preventDefault();
        await client.post("/planted", {}).catch((e) => setError(asApiError(e)));
      }}
    >
      <label>
        Planted label
        <input type="text" name="planted" />
      </label>
      <label>
        Nameless
        <input type="text" />
      </label>
      <label>
        Nameless box
        <textarea />
      </label>
      <label>
        Nameless choice
        <select>
          <option>one</option>
        </select>
      </label>
      <button type="submit">Send</button>
      <ProblemView error={error} />
    </form>
  );
}

test("the label check fails on a form that shows a field by its name, or has an input with none", async () => {
  fakeApi({});
  render(<Unlabelled />);
  expect(await labelFaults(document.querySelector("form")!)).toEqual([
    "Nameless: an input with no field name",
    "Nameless box: an input with no field name",
    "Nameless choice: an input with no field name",
    "planted: not shown under its input or as Planted label",
  ]);
});
