// Validation belongs to the server: no input may carry a rule of its own.
// Each form leads with what it does: its submit button is its one primary
// button, or its one danger button where what it does cannot be undone.
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { useState } from "react";

import { ApiError, api as client } from "./api";
import { m } from "./messages";
import { ProblemView, asApiError } from "./ProblemView";
import { FIELDS } from "./Settings";
import { ADMIN_USER, NOT_CONNECTED, NOTHING_TO_ATTEND, SETTINGS, fakeApi } from "./test/fakeApi";
import { renderApp } from "./test/render";

// A repository, whose panel has the commit and new-branch forms.
const REPOSITORY = { name: "repo", path: "projects/repo", has_children: false, repository: true };

const RULE_ATTRIBUTES = ["required", "pattern", "min", "max", "minlength", "maxlength", "step"];

function api() {
  return fakeApi({
    "GET /api/me": ADMIN_USER,
    "GET /api/claude": NOT_CONNECTED,
    "GET /api/me/settings": SETTINGS,
    "GET /api/users": { users: [{ linux: "bob", email: "bob@example.com", admin: false, account: true }] },
    "GET /api/claude/versions": { approved: "1.0.0", tested: "1.0.0", untested: false, differs: [], users: [] },
    "POST /api/claude/login": { url: "https://claude.com/x" },
    "GET /api/folders": (c) => (c.query.get("path") === "projects" ? { path: "projects", entries: [REPOSITORY] } : { path: "", entries: [] }),
    "GET /api/servers": { servers: [] },
    "GET /api/attention": NOTHING_TO_ATTEND,
    "GET /api/git/status": {
      path: REPOSITORY.path,
      branch: { name: "main", commit: "0123456789", upstream: null, ahead: null, behind: null },
      staged: { files: [{ path: "a.txt", change: "added" }], more: 0 },
      not_staged: { files: [{ path: "b.txt", change: "untracked" }], more: 0 },
      author: { name: "Alice", email: "alice@example.com" },
    },
    "GET /api/git/branches": { path: REPOSITORY.path, local: { branches: [], more: 0 }, remote: { branches: [], more: 0 } },
  });
}

async function inputs() {
  await screen.findAllByRole("textbox");
  return [...document.querySelectorAll("input, textarea, select")];
}

// Every page with a form, and what opens the forms that are not there at first.
const PAGES: [string, string, () => Promise<void>][] = [
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
  ["Claude Code versions", "/admin/claude", async () => {}],
  [
    "connecting Claude, its address",
    "/",
    async () => {
      await userEvent.click(await screen.findByRole("button", { name: m.connect }));
      await screen.findByLabelText(m.connectEmail);
    },
  ],
  [
    "connecting Claude, its code",
    "/",
    async () => {
      await userEvent.click(await screen.findByRole("button", { name: m.connect }));
      await userEvent.click(screen.getByRole("button", { name: m.connectGo }));
      await screen.findByLabelText(m.connectCode);
    },
  ],
  [
    "a new folder in the home folder",
    "/",
    async () => {
      const home = await screen.findByRole("region", { name: m.home });
      await userEvent.click(within(home).getByRole("button", { name: m.newFolder }));
      await screen.findByLabelText(m.newFolderName);
    },
  ],
  [
    "a new folder in a folder",
    "/?folder=projects",
    async () => {
      const panel = await screen.findByRole("region", { name: "projects" });
      await userEvent.click(within(panel).getByRole("button", { name: m.newFolder }));
      await screen.findByLabelText(m.newFolderName);
    },
  ],
];

// The forms of a repository's Changes section, each found by its field.
PAGES.push(
  ["a commit", "/?folder=projects/repo", async () => void (await screen.findByLabelText(m.commitMessage))],
  [
    "a new branch",
    "/?folder=projects/repo",
    async () => {
      await userEvent.click(await screen.findByRole("button", { name: m.newBranch }));
      await screen.findByLabelText(m.newBranchName);
    },
  ],
);

// Pages with a form that has nothing to type in, and what opens it.
const CHOICES: [string, string, () => Promise<void>][] = [
  [
    "deleting a folder",
    "/?folder=projects",
    async () => {
      const panel = await screen.findByRole("region", { name: "projects" });
      await userEvent.click(await within(panel).findByRole("button", { name: m.deleteFolder }));
      await screen.findByRole("dialog");
    },
  ],
];

test.each(PAGES)("no input on %s (%s) carries its own validation rule", async (_, path, open) => {
  api();
  renderApp(path);
  await open();
  const found = await inputs();
  expect(found.length).toBeGreaterThan(0); // non-vacuity
  for (const input of found) {
    for (const attribute of RULE_ATTRIBUTES) {
      expect(input.hasAttribute(attribute), `${input.outerHTML} has ${attribute}`).toBe(false);
    }
    if (input instanceof HTMLInputElement) {
      expect(["text", "checkbox", "radio"]).toContain(input.type);
    }
  }
  expect(document.querySelectorAll("form[novalidate]").length).toBe(document.querySelectorAll("form").length);
});

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
    if ((leading[0] as HTMLButtonElement).type !== "submit") return [`${name}: its leading button does not submit it`];
    return [];
  });
  return [...formless, ...forms];
}

test.each([...PAGES, ...CHOICES])("each form on %s (%s) leads with one button, its submit button", async (page, path, open) => {
  api();
  renderApp(path);
  await open();
  if (!CHOICES.some(([choice]) => choice === page)) await inputs();
  expect(document.querySelectorAll("form").length).toBeGreaterThan(0); // non-vacuity
  expect(hierarchyFaults(document)).toEqual([]);
});

// The forms whose submit cannot be undone, which lead with danger instead.
const DESTRUCTIVE = ["removing someone", "deleting a folder"];

test("every form said to be destructive is one of those checked", () => {
  const pages = [...PAGES, ...CHOICES].map(([page]) => page);
  for (const page of DESTRUCTIVE) expect(pages).toContain(page);
});

test.each([...PAGES, ...CHOICES].filter(([page]) => DESTRUCTIVE.includes(page)))("the form for %s (%s) leads with danger, not primary", async (_, path, open) => {
  api();
  renderApp(path);
  await open();
  const dialog = screen.getByRole("dialog");
  const submit = dialog.querySelector('button[type="submit"]');
  expect(submit).toHaveClass("danger");
  expect(dialog.querySelectorAll("button.primary")).toHaveLength(0);
});

// What is wrong with the button that opened a destructive form: it does not
// itself do what cannot be undone, so it is a danger outline, not plain, not
// primary and not danger.
function openerFaults(opener: Element | undefined): string[] {
  if (!opener) return ["nothing opened it"];
  const name = opener.textContent?.trim();
  if (!opener.classList.contains("secondary-danger")) return [`${name}: not a danger outline`];
  if (opener.classList.contains("primary") || opener.classList.contains("danger")) return [`${name}: leads as a form would`];
  return [];
}

test.each([...PAGES, ...CHOICES].filter(([page]) => DESTRUCTIVE.includes(page)))(
  "what opens the form for %s (%s) is a danger outline",
  async (_, path, open) => {
    api();
    renderApp(path);
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
  const button = (className: string) => Object.assign(document.createElement("button"), { className, textContent: "Remove" });
  expect(openerFaults(button("secondary-danger"))).toEqual([]);
  for (const className of ["", "secondary", "primary", "danger", "secondary-danger danger"]) {
    expect(openerFaults(button(className)), className).toHaveLength(1);
  }
  expect(openerFaults(undefined)).toEqual(["nothing opened it"]);
});

test.each([...PAGES, ...CHOICES].filter(([page]) => !DESTRUCTIVE.includes(page)))("the forms on %s (%s) lead with primary, not danger", async (page, path, open) => {
  api();
  renderApp(path);
  await open();
  if (!CHOICES.some(([choice]) => choice === page)) await inputs();
  expect(document.querySelectorAll("form button.primary").length).toBeGreaterThan(0); // non-vacuity
  expect(document.querySelectorAll("form button.danger")).toHaveLength(0);
});

test("the check fails on a form with no leading button, two, or one that does not submit it", () => {
  const form = (buttons: string) => {
    const root = document.createElement("div");
    root.innerHTML = `<form>${buttons}</form>`;
    return root;
  };
  expect(hierarchyFaults(form('<button type="submit" class="primary">Save</button><button type="button">Cancel</button>'))).toEqual([]);
  expect(hierarchyFaults(form('<button type="submit" class="danger">Delete</button><button type="button">Cancel</button>'))).toEqual([]);
  expect(hierarchyFaults(form('<button type="submit">Save</button>'))).toHaveLength(1);
  expect(hierarchyFaults(form('<button class="primary">Save</button><button type="button" class="primary">Cancel</button>'))).toHaveLength(1);
  expect(hierarchyFaults(form('<button type="submit">Save</button><button type="button" class="primary">Cancel</button>'))).toHaveLength(1);
  expect(hierarchyFaults(form('<button type="submit" class="danger">Delete</button><button type="button" class="primary">Keep</button>'))).toHaveLength(1);
  expect(hierarchyFaults(form('<button type="submit">Delete</button><button type="button" class="danger">Cancel</button>'))).toHaveLength(1);
  const dialog = document.createElement("div");
  dialog.innerHTML = '<div role="dialog" aria-label="Remove"><button class="primary">Remove</button></div>';
  expect(hierarchyFaults(dialog)).toEqual(["Remove: a dialog with no form"]);
  expect(hierarchyFaults(document.createElement("div"))).toEqual([]);
});

test("the settings page has an input for every field it describes, and nothing else", async () => {
  api();
  renderApp("/settings");
  const found = await inputs();
  expect(Object.keys(FIELDS).length).toBeGreaterThan(0); // non-vacuity
  expect(found.length).toBe(Object.keys(FIELDS).length);
  expect(document.querySelectorAll("form").length).toBeGreaterThan(1);
  expect(document.querySelectorAll("form[novalidate]").length).toBe(document.querySelectorAll("form").length);
});

// The inputs of a form whose refusal names a field: each carries the field's
// name, so the server's error can be matched to it. A radio's label names one
// choice, not the field, so radios are not among them.
type Field = HTMLInputElement | HTMLTextAreaElement | HTMLSelectElement;

// Every field of a form a person fills in: inputs but radio buttons, text areas and choices.
function fieldInputs(form: HTMLFormElement): Field[] {
  return [...form.querySelectorAll<Field>("input, textarea, select")].filter((input) => input.type !== "radio");
}

// What a form's refusal shows wrongly: an input with no field name, or a field
// error shown under anything but the input's label. Every request but a GET is
// refused with an error for each of the form's fields.
async function labelFaults(form: HTMLFormElement): Promise<string[]> {
  const faults: string[] = [];
  // Read before submitting: a refusal may take the form away.
  // The label's own words, without what the field itself holds, such as a choice's options.
  const label = (input: Field) => (input.labels?.[0]?.textContent ?? "").replace(input.textContent ?? "", "").trim();
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
    const body = { type: "urn:rcman:problem:validation_failed", title: "Refused.", status: 422, code: "validation_failed", errors };
    return new Response(JSON.stringify(body), { status: 422, headers: { "content-type": "application/problem+json" } });
  });
  vi.stubGlobal("fetch", refuse);
  try {
    const before = screen.queryAllByText("Refused.").length;
    await userEvent.click(form.querySelector('button[type="submit"]')!);
    await waitFor(() => expect(screen.queryAllByText("Refused.").length).toBeGreaterThan(before));
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

test.each(PAGES)("a refused field on %s (%s) is named by its label", async (_, path, open) => {
  api();
  renderApp(path);
  await open();
  await inputs();
  const forms = [...document.querySelectorAll("form")].filter((form) => fieldInputs(form).length > 0);
  for (const form of forms) expect(await labelFaults(form)).toEqual([]);
});

test("the label check finds every form that names a field", () => {
  const checked = PAGES.map(([page]) => page);
  expect(checked).toEqual(expect.arrayContaining(["settings", "people", "Claude Code versions"]));
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
