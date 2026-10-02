import { screen } from "@testing-library/react";

import { ADMIN_PAGES } from "./App";
import { m } from "./messages";
import { ADMIN, CONNECTED, ME, type Reply, fakeApi } from "./test/fakeApi";
import { renderApp } from "./test/render";

function api(me: Reply) {
  return fakeApi({
    "GET /api/me": me,
    "GET /api/claude": CONNECTED,
    "GET /api/folders": { path: "", entries: [] },
    "GET /api/servers": { servers: [] },
  });
}

test("people who are not administrators are not offered administration", async () => {
  api(ME);
  renderApp();
  expect(await screen.findByText(m.signedInAs(ME.email))).toBeInTheDocument();
  for (const { label } of ADMIN_PAGES) {
    expect(screen.queryByRole("link", { name: label })).not.toBeInTheDocument();
  }
  expect(screen.getByRole("link", { name: m.navSettings })).toBeInTheDocument();
});

test("administrators are", async () => {
  api(ADMIN);
  renderApp();
  for (const { path, label } of ADMIN_PAGES) {
    expect(await screen.findByRole("link", { name: label })).toHaveAttribute("href", path);
  }
});

test("the administration pages include everyone's servers", () => {
  const labels = ADMIN_PAGES.map((p) => p.label);
  // non-vacuity: the table holds every page administrators are known to have
  for (const label of [m.navUsers, m.navAudit, m.navVersions, m.navEveryone]) {
    expect(labels).toContain(label);
  }
  expect(ADMIN_PAGES.find((p) => p.label === m.navEveryone)?.path).toBe("/admin/servers");
});

test("an account rcman refuses sees the server's reason", async () => {
  const { problem } = await import("./test/fakeApi");
  fakeApi({ "GET /api/me": problem(403, "not_allowed", "This account may not use rcman.") });
  renderApp();
  expect(await screen.findByRole("alert")).toHaveTextContent("This account may not use rcman.");
});
