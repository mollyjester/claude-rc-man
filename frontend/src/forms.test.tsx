// Validation belongs to the server: no input may carry a rule of its own.
// Each form leads with what it does: its submit button is its one primary
// button, or its one danger button where what it does cannot be undone.
import { readdirSync, readFileSync } from "node:fs";
import { resolve, sep } from "node:path";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type FormEvent, type ReactElement, useCallback, useState } from "react";
import { parseAst } from "vite";
import { vi } from "vitest";

import { type ApiError, api as client, forgetSending } from "./api";
import { Changes } from "./Changes";
import { ClaudeConnection, type Connection } from "./Claude";
import { forgetReports } from "./clientErrors";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";
import { FIELDS } from "./Settings";
import { ADMIN_USER, fakeApi, NOT_CONNECTED, NOTHING_TO_ATTEND, SETTINGS } from "./test/fakeApi";
import { renderAlone, renderApp } from "./test/render";
import { Versions } from "./Versions";

// A repository, whose Changes section has the commit and new-branch forms.
const REPOSITORY = "projects/repo";

const RULE_ATTRIBUTES = ["required", "pattern", "min", "max", "minlength", "maxlength", "step"];

function api() {
  return fakeApi({
    "GET /api/me": ADMIN_USER,
    "GET /api/claude": NOT_CONNECTED,
    "GET /api/me/settings": SETTINGS,
    "GET /api/people": {
      people: [{ id: 2, email: "bob@example.com", admin: false, state: "active", workspaces: 1 }],
    },
    "GET /api/claude/versions": {
      approved: "1.0.0",
      tested: "1.0.0",
      untested: false,
      differs: [],
      users: [],
    },
    "POST /api/claude/login": { url: "https://claude.com/x" },
    "GET /api/workspaces": {
      workspaces: [
        { id: 1, name: "alpha", state: "ready", error: null, server: null, server_error: null },
      ],
    },
    "GET /api/attention": NOTHING_TO_ATTEND,
    "GET /api/git/status": {
      path: REPOSITORY,
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
      path: REPOSITORY,
      local: { branches: [], more: 0 },
      remote: { branches: [], more: 0 },
    },
    "GET /api/github": { token: true },
  });
}

async function inputs() {
  await screen.findAllByRole("textbox");
  return [...document.querySelectorAll("input, textarea, select")];
}

// Where a page is: a path in the app, or, for a part the app does not show while
// the server refuses what it does, that part on its own. Each is held to the
// same rules, so that it is ready to be shown again.
type Where = string | (() => ReactElement);

function show(where: Where) {
  return typeof where === "string" ? renderApp(where) : renderAlone(where(), ["/settings"]);
}

const versions = () => <Versions />;
const account = () => (
  <ClaudeConnection connection={NOT_CONNECTED as Connection} onChange={() => {}} />
);
const changes = () => <Changes path={REPOSITORY} pollMs={0} />;

// Every page with a form, and what opens the forms that are not there at first.
const PAGES: [string, Where, () => Promise<void>][] = [
  ["settings", "/settings", async () => {}],
  ["people", "/admin/people", async () => {}],
  [
    "removing someone",
    "/admin/people",
    async () => {
      const bob = await screen.findByRole("row", { name: /bob/ });
      await userEvent.click(within(bob).getByRole("button", { name: m.usersRemove }));
      await screen.findByRole("dialog");
    },
  ],
  ["a new workspace", "/", async () => void (await screen.findByLabelText(m.newWorkspaceName))],
  ["Claude Code versions", versions, async () => {}],
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
  [
    "deleting a workspace",
    "/?workspace=1",
    async () => {
      const panel = await screen.findByRole("region", { name: "alpha" });
      await userEvent.click(within(panel).getByRole("button", { name: m.deleteWorkspace }));
      await screen.findByRole("dialog");
    },
  ],
];

test.each([...PAGES, ...OUTSIDE_FORMS])(
  "no input on %s carries its own validation rule",
  async (_, where, open) => {
    api();
    show(where);
    await open();
    const found = await inputs();
    expect(found.length).toBeGreaterThan(0); // non-vacuity
    for (const input of found) {
      for (const attribute of RULE_ATTRIBUTES) {
        expect(input.hasAttribute(attribute), `${input.outerHTML} has ${attribute}`).toBe(false);
      }
      if (input instanceof HTMLInputElement && input.type === "password") {
        // The one secret a page takes, the GitHub token, is typed where it is not shown.
        expect(input.closest("section")?.querySelector("h2")?.textContent).toBe(
          m.settingsGithubTitle,
        );
      } else if (input instanceof HTMLInputElement) {
        expect(["text", "checkbox", "radio"]).toContain(input.type);
      }
    }
    expect(document.querySelectorAll("form[novalidate]").length).toBe(
      document.querySelectorAll("form").length,
    );
  },
);

// What is wrong with the buttons of each form under root: a form whose leading
// buttons — primary, or danger where what it does cannot be undone — are not
// exactly one, or whose one is not its submit button. A dialog asks for a
// choice, so it is a form too.
function hierarchyFaults(root: ParentNode): string[] {
  const formless = [...root.querySelectorAll('[role="dialog"]')]
    .filter((dialog) => !dialog.querySelector("form"))
    .map((dialog) => `${dialog.getAttribute("aria-label")}: a dialog with no form`);
  const forms = [...root.querySelectorAll("form")].flatMap((form) => {
    const leading = [...form.querySelectorAll("button.primary, button.danger")];
    const name = form.textContent?.trim().slice(0, 40);
    if (leading.length !== 1) return [`${name}: ${leading.length} leading buttons`];
    if ((leading[0] as HTMLButtonElement).type !== "submit")
      return [`${name}: its leading button does not submit it`];
    return [];
  });
  return [...formless, ...forms];
}

test.each([...PAGES, ...CHOICES])(
  "each form on %s leads with one button, its submit button",
  async (page, where, open) => {
    api();
    show(where);
    await open();
    if (!CHOICES.some(([choice]) => choice === page)) await inputs();
    expect(document.querySelectorAll("form").length).toBeGreaterThan(0); // non-vacuity
    expect(hierarchyFaults(document)).toEqual([]);
  },
);

// The forms whose submit cannot be undone, which lead with danger instead.
const DESTRUCTIVE = ["removing someone", "deleting a workspace", "removing the GitHub token"];

test("every form said to be destructive is one of those checked", () => {
  const pages = [...PAGES, ...CHOICES].map(([page]) => page);
  for (const page of DESTRUCTIVE) expect(pages).toContain(page);
});

test.each([...PAGES, ...CHOICES].filter(([page]) => DESTRUCTIVE.includes(page)))(
  "the form for %s leads with danger, not primary",
  async (_, where, open) => {
    api();
    show(where);
    await open();
    const dialog = screen.getByRole("dialog");
    const submit = dialog.querySelector('button[type="submit"]');
    expect(submit).toHaveClass("danger");
    expect(dialog.querySelectorAll("button.primary")).toHaveLength(0);
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

test.each([...PAGES, ...CHOICES].filter(([page]) => !DESTRUCTIVE.includes(page)))(
  "the forms on %s lead with primary, not danger",
  async (page, where, open) => {
    api();
    show(where);
    await open();
    if (!CHOICES.some(([choice]) => choice === page)) await inputs();
    expect(document.querySelectorAll("form button.primary").length).toBeGreaterThan(0); // non-vacuity
    expect(document.querySelectorAll("form button.danger")).toHaveLength(0);
  },
);

test("the check fails on a form with no leading button, two, or one that does not submit it", () => {
  const form = (buttons: string) => {
    const root = document.createElement("div");
    root.innerHTML = `<form>${buttons}</form>`;
    return root;
  };
  expect(
    hierarchyFaults(
      form(
        '<button type="submit" class="primary">Save</button><button type="button">Cancel</button>',
      ),
    ),
  ).toEqual([]);
  expect(
    hierarchyFaults(
      form(
        '<button type="submit" class="danger">Delete</button><button type="button">Cancel</button>',
      ),
    ),
  ).toEqual([]);
  expect(hierarchyFaults(form('<button type="submit">Save</button>'))).toHaveLength(1);
  expect(
    hierarchyFaults(
      form(
        '<button class="primary">Save</button><button type="button" class="primary">Cancel</button>',
      ),
    ),
  ).toHaveLength(1);
  expect(
    hierarchyFaults(
      form(
        '<button type="submit">Save</button><button type="button" class="primary">Cancel</button>',
      ),
    ),
  ).toHaveLength(1);
  expect(
    hierarchyFaults(
      form(
        '<button type="submit" class="danger">Delete</button><button type="button" class="primary">Keep</button>',
      ),
    ),
  ).toHaveLength(1);
  expect(
    hierarchyFaults(
      form(
        '<button type="submit">Delete</button><button type="button" class="danger">Cancel</button>',
      ),
    ),
  ).toHaveLength(1);
  const dialog = document.createElement("div");
  dialog.innerHTML =
    '<div role="dialog" aria-label="Remove"><button class="primary">Remove</button></div>';
  expect(hierarchyFaults(dialog)).toEqual(["Remove: a dialog with no form"]);
  expect(hierarchyFaults(document.createElement("div"))).toEqual([]);
});

test("the settings page has an input for every field it describes, and the token's, and nothing else", async () => {
  api();
  renderApp("/settings");
  const found = await inputs();
  const github = await screen.findByRole("region", { name: m.settingsGithubTitle });
  const token = found.filter((input) => github.contains(input));
  expect(token.map((input) => (input as HTMLInputElement).type)).toEqual(["password"]);
  expect(Object.keys(FIELDS).length).toBeGreaterThan(0); // non-vacuity
  expect(
    found
      .filter((input) => !token.includes(input))
      .map((input) => (input as HTMLInputElement).name),
  ).toEqual(Object.keys(FIELDS));
  expect(document.querySelectorAll("form").length).toBeGreaterThan(1);
  expect(document.querySelectorAll("form[novalidate]").length).toBe(
    document.querySelectorAll("form").length,
  );
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
// error shown under anything but the input's label. Every request but a GET is
// refused with an error for each of the form's fields.
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
    .map((input) => ({ name: input.name, label: label(input) }));
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
    const before = new Set(screen.queryAllByText("Refused."));
    await userEvent.click(form.querySelector('button[type="submit"]')!);
    await waitFor(() =>
      expect(screen.queryAllByText("Refused.").some((shown) => !before.has(shown))).toBe(true),
    );
    for (const input of named) {
      if (!screen.queryByText(`${input.label}: planted reason for ${input.name}`)) {
        faults.push(`${input.name}: not shown as ${input.label}`);
      }
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
  const forms = [...document.querySelectorAll("form")].filter(
    (form) => fieldInputs(form).length > 0,
  );
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

// Opens every entry of PAGES and CHOICES in turn, and notes each form shown;
// once stopped, it opens no further entry, so renders nothing more.
async function observe(stopped: () => boolean): Promise<Seen[]> {
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
      } finally {
        cleanup();
        forgetReports();
        forgetSending();
      }
    }
  }
  return seen;
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

test("the source check names each form by its component and handler, and reports what it cannot name", () => {
  const planted = `
function Planted() {
  function send(event) {
    event.preventDefault();
  }
  return (
    <form
      noValidate
      onSubmit={send}
    >
      <form onSubmit={(event) => event.preventDefault()} />
    </form>
  );
}
const Arrow = () => {
  const go = () => {};
  return <form onSubmit={go} />;
};
function Bare() {
  return <form />;
}
function helper() {
  const send = () => {};
  return <form onSubmit={send} />;
}
function Wrapped() {
  const save = useCallback(() => {}, []);
  return <form onSubmit={save} />;
}
function Passed({ onSave }) {
  return <form onSubmit={onSave} />;
}
`;
  const again =
    "export function Planted() {\n  const send = function () {};\n  return <form onSubmit={send} />;\n}\n";
  expect(sourceForms([["planted.tsx", planted]])).toEqual({
    ids: new Set(["Planted.send", "Arrow.go"]),
    faults: [
      "planted.tsx:11: a form whose onSubmit is not a function's name",
      "planted.tsx:20: a form whose onSubmit is not a function's name",
      "planted.tsx:24: a form outside a component",
      "planted.tsx:28: Wrapped.save is not bound to a function, as a wrapped handler is not",
      "planted.tsx:31: Passed.onSave is not a function the component declares",
    ],
  });
  expect(sourceForms([["again.tsx", again]]).faults).toEqual([]); // positive control: alone it is fine
  expect(
    sourceForms([
      ["planted.tsx", planted],
      ["again.tsx", again],
    ]).faults,
  ).toContain("again.tsx:3: a second form known as Planted.send");
});

// How long a visit to every page may take, on a slow or busy machine.
const EVERY_PAGE_MS = 60_000;

// The tests that need the forms on screen share one visit to every page, made
// before the first of them, whichever runs. A visit that outlives its time is
// stopped and waited for, so that it renders nothing into a later test.
describe("the forms on screen", () => {
  let seen: Seen[] = [];
  let stop = false;
  let visit: Promise<unknown> = Promise.resolve();

  beforeAll(async () => {
    const visiting = observe(() => stop);
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

  test("the coverage check reports a form in the source no page opens", () => {
    const source = sourceForms(SOURCES).ids;
    const real = coverageFaults(source, seen);
    expect(source.has("Planted.send")).toBe(false); // the plant is new
    expect(coverageFaults(new Set([...source, "Planted.send"]), seen)).toEqual({
      ...real,
      missing: [...real.missing, "Planted.send"].sort(),
    });
  });

  test("the coverage check reports the form of a page left out of those visited", () => {
    const source = sourceForms(SOURCES).ids;
    // positive control: the page is visited, and shows the form
    expect(seen).toContainEqual(
      expect.objectContaining({ entry: "Claude Code versions", id: "Versions.upgrade" }),
    );
    const without = seen.filter((s) => s.entry !== "Claude Code versions");
    expect(coverageFaults(source, without).missing).toContain("Versions.upgrade");
  });

  test("the coverage check reports a form opened that the source does not have, and one with a field seen only from CHOICES", () => {
    const source = sourceForms(SOURCES).ids;
    const real = coverageFaults(source, seen);
    const planted: Seen = { entry: "planted", fromPages: false, id: "Planted.send", fields: true };
    expect(coverageFaults(source, [...seen, planted])).toEqual({
      ...real,
      unknown: [...real.unknown, "Planted.send"].sort(),
      unlabelled: [...real.unlabelled, "Planted.send"].sort(),
    });
    // positive control: the same form seen from PAGES, with its field, is labelled
    expect(
      coverageFaults(new Set([...source, "Planted.send"]), [
        ...seen,
        planted,
        { ...planted, fromPages: true },
      ]),
    ).toEqual(real);
  });
});

function PlantedForm() {
  const send = (event: FormEvent) => event.preventDefault();
  return (
    <form noValidate onSubmit={send}>
      <button type="submit">Send</button>
    </form>
  );
}

// A form whose handler is wrapped, so its function has no name of its own.
function WrappedForm() {
  const send = useCallback((event: FormEvent) => event.preventDefault(), []);
  return (
    <form noValidate onSubmit={send}>
      <button type="submit">Send</button>
    </form>
  );
}

// A form with nothing to do on submit.
function HandlerlessForm() {
  return (
    <form noValidate>
      <button type="submit">Send</button>
    </form>
  );
}

// A component made by a function, as an arrow it returns, which has no name.
function made() {
  return () => {
    const send = (event: FormEvent) => event.preventDefault();
    return (
      <form noValidate onSubmit={send}>
        <button type="submit">Send</button>
      </form>
    );
  };
}
const UnnamedForm = made();

test("a form on screen is known by the component that rendered it and its handler", () => {
  render(<PlantedForm />);
  expect(formId(document.querySelector("form")!)).toBe("PlantedForm.send");
});

test("a form on screen that cannot be known so says why", () => {
  render(<WrappedForm />);
  expect(() => formId(document.querySelector("form")!)).toThrow(
    "a form whose onSubmit has no function name, a wrapped or anonymous handler",
  );
  cleanup();
  render(<HandlerlessForm />);
  expect(() => formId(document.querySelector("form")!)).toThrow("a form with no onSubmit");
  cleanup();
  expect(UnnamedForm.name).toBe(""); // the plant took effect: the component has no name
  render(<UnnamedForm />);
  expect(() => formId(document.querySelector("form")!)).toThrow(
    "a form whose component React does not name",
  );
  cleanup();
  expect(() => formId(document.createElement("form"))).toThrow("a form React did not render");
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
    "planted: not shown as Planted label",
  ]);
});
