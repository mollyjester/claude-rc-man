// Validation belongs to the server: no input may carry a rule of its own.
// Each form leads with what it does: its submit button is its one primary button.
import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { m } from "./messages";
import { FIELDS } from "./Settings";
import { ADMIN_USER, NOT_CONNECTED, NOTHING_TO_ATTEND, SETTINGS, fakeApi } from "./test/fakeApi";
import { renderApp } from "./test/render";

const RULE_ATTRIBUTES = ["required", "pattern", "min", "max", "minlength", "maxlength", "step"];

function api() {
  return fakeApi({
    "GET /api/me": ADMIN_USER,
    "GET /api/claude": NOT_CONNECTED,
    "GET /api/me/settings": SETTINGS,
    "GET /api/users": { users: [{ linux: "bob", email: "bob@example.com", admin: false, account: true }] },
    "GET /api/claude/versions": { approved: "1.0.0", tested: "1.0.0", untested: false, differs: [], users: [] },
    "POST /api/claude/login": { url: "https://claude.com/x" },
    "GET /api/folders": { path: "", entries: [] },
    "GET /api/servers": { servers: [] },
    "GET /api/attention": NOTHING_TO_ATTEND,
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

// What is wrong with the buttons of each form under root: a form whose primary
// buttons are not exactly one, or whose one is not its submit button. A dialog
// asks for a choice, so it is a form too.
function hierarchyFaults(root: ParentNode): string[] {
  const formless = [...root.querySelectorAll('[role="dialog"]')]
    .filter((dialog) => !dialog.querySelector("form"))
    .map((dialog) => `${dialog.getAttribute("aria-label")}: a dialog with no form`);
  const forms = [...root.querySelectorAll("form")].flatMap((form) => {
    const primary = [...form.querySelectorAll("button.primary")];
    const name = form.textContent?.trim().slice(0, 40);
    if (primary.length !== 1) return [`${name}: ${primary.length} primary buttons`];
    if ((primary[0] as HTMLButtonElement).type !== "submit") return [`${name}: its primary button does not submit it`];
    return [];
  });
  return [...formless, ...forms];
}

test.each(PAGES)("each form on %s (%s) has one primary button, its submit button", async (_, path, open) => {
  api();
  renderApp(path);
  await open();
  await inputs();
  expect(document.querySelectorAll("form").length).toBeGreaterThan(0); // non-vacuity
  expect(hierarchyFaults(document)).toEqual([]);
});

test("the check fails on a form with no primary button, two, or one that does not submit it", () => {
  const form = (buttons: string) => {
    const root = document.createElement("div");
    root.innerHTML = `<form>${buttons}</form>`;
    return root;
  };
  expect(hierarchyFaults(form('<button type="submit" class="primary">Save</button><button type="button">Cancel</button>'))).toEqual([]);
  expect(hierarchyFaults(form('<button type="submit">Save</button>'))).toHaveLength(1);
  expect(hierarchyFaults(form('<button class="primary">Save</button><button type="button" class="primary">Cancel</button>'))).toHaveLength(1);
  expect(hierarchyFaults(form('<button type="submit">Save</button><button type="button" class="primary">Cancel</button>'))).toHaveLength(1);
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
