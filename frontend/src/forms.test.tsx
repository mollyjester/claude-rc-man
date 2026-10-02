// Validation belongs to the server: no input may carry a rule of its own.
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { m } from "./messages";
import { FIELDS } from "./Settings";
import { ADMIN_USER, NOTHING_TO_ATTEND, SETTINGS, fakeApi } from "./test/fakeApi";
import { renderApp } from "./test/render";

const RULE_ATTRIBUTES = ["required", "pattern", "min", "max", "minlength", "maxlength", "step"];

function api() {
  return fakeApi({
    "GET /api/me": ADMIN_USER,
    "GET /api/claude": { connection: "not_connected", account: null, login_expires_at: null },
    "GET /api/me/settings": SETTINGS,
    "GET /api/users": { users: [] },
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

test.each([
  ["/settings", async () => {}],
  ["/admin/people", async () => {}],
  ["/admin/claude", async () => {}],
  [
    "/",
    async () => {
      await userEvent.click(await screen.findByRole("button", { name: m.connect }));
      await userEvent.click(screen.getByRole("button", { name: m.connectGo }));
      await screen.findByLabelText(m.connectCode);
    },
  ],
])("no input on %s carries its own validation rule", async (path, open) => {
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

test("the settings page has an input for every field it describes, and nothing else", async () => {
  api();
  renderApp("/settings");
  const found = await inputs();
  expect(Object.keys(FIELDS).length).toBeGreaterThan(0); // non-vacuity
  expect(found.length).toBe(Object.keys(FIELDS).length);
  expect(document.querySelectorAll("form").length).toBeGreaterThan(1);
  expect(document.querySelectorAll("form[novalidate]").length).toBe(document.querySelectorAll("form").length);
});
