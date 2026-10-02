import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { m } from "./messages";
import { ADMIN, CONNECTED, ME, NOTHING_TO_ATTEND, type Reply, fakeApi } from "./test/fakeApi";
import { renderApp } from "./test/render";


function api(me: Reply, attention: Reply = NOTHING_TO_ATTEND) {
  return fakeApi({
    "GET /api/me": me,
    "GET /api/claude": CONNECTED,
    "GET /api/folders": { path: "", entries: [] },
    "GET /api/servers": { servers: [] },
    "GET /api/docs": { pages: [] },
    "GET /api/me/settings": { prefix: "alice" },
    "GET /api/attention": attention,
  });
}

test("administrators are told who is on an unrecognised screen and whose version differs", async () => {
  api(ADMIN, {
    unrecognised: [{ linux: "bob", path: "projects/beta" }],
    versions_differ: ["carol"],
    untested: true,
  });
  renderApp();
  const banner = await screen.findByRole("region", { name: m.attentionTitle });
  expect(within(banner).getByText(m.attentionUnrecognised(["bob: projects/beta"]))).toBeInTheDocument();
  expect(within(banner).getByText(m.attentionVersionsDiffer(["carol"]))).toBeInTheDocument();
  expect(within(banner).getByText(m.attentionUntested)).toBeInTheDocument();
  expect(within(banner).getByRole("link", { name: m.navEveryone })).toHaveAttribute("href", "/admin/servers");
  expect(within(banner).getAllByRole("link", { name: m.navVersions })[0]).toHaveAttribute("href", "/admin/claude");
});

test("it is fetched again on every change of page, never on a timer", async () => {
  const fake = api(ADMIN);
  renderApp();
  await waitFor(() => expect(fake.made("GET", "/api/attention")).toHaveLength(1));
  await userEvent.click(screen.getByRole("link", { name: m.navSettings }));
  await waitFor(() => expect(fake.made("GET", "/api/attention")).toHaveLength(2));
  await userEvent.click(screen.getByRole("link", { name: m.navDocs }));
  await waitFor(() => expect(fake.made("GET", "/api/attention")).toHaveLength(3));
});

test("nothing is shown when nothing needs attention", async () => {
  const fake = api(ADMIN);
  renderApp();
  await waitFor(() => expect(fake.made("GET", "/api/attention")).toHaveLength(1));
  await screen.findByRole("link", { name: m.navEveryone });
  expect(screen.queryByRole("region", { name: m.attentionTitle })).not.toBeInTheDocument();
});

test("people who are not administrators never ask", async () => {
  const fake = api(ME);
  renderApp();
  expect(await screen.findByText(m.signedInAs(ME.email))).toBeInTheDocument();
  await userEvent.click(screen.getByRole("link", { name: m.navSettings }));
  await screen.findByRole("heading", { name: m.settingsTitle });
  expect(fake.made("GET", "/api/attention")).toHaveLength(0);
  expect(screen.queryByRole("region", { name: m.attentionTitle })).not.toBeInTheDocument();
});
