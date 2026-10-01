import { screen } from "@testing-library/react";

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
  for (const label of [m.navUsers, m.navAudit, m.navVersions]) {
    expect(screen.queryByRole("link", { name: label })).not.toBeInTheDocument();
  }
  expect(screen.getByRole("link", { name: m.navSettings })).toBeInTheDocument();
});

test("administrators are", async () => {
  api(ADMIN);
  renderApp();
  for (const label of [m.navUsers, m.navAudit, m.navVersions]) {
    expect(await screen.findByRole("link", { name: label })).toBeInTheDocument();
  }
});

test("an account rcman refuses sees the server's reason", async () => {
  const { problem } = await import("./test/fakeApi");
  fakeApi({ "GET /api/me": problem(403, "not_allowed", "This account may not use rcman.") });
  renderApp();
  expect(await screen.findByRole("alert")).toHaveTextContent("This account may not use rcman.");
});
