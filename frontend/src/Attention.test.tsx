import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import { ADMIN } from "./AdminPages";
import { m } from "./messages";

import {
  ADMIN_USER,
  fakeApi,
  ME,
  NOTHING_TO_ATTEND,
  problem,
  type Reply,
  SETTINGS,
} from "./test/fakeApi";
import { renderApp } from "./test/render";

function api(me: Reply, attention: Reply = NOTHING_TO_ATTEND) {
  return fakeApi({
    "GET /api/me": me,
    "GET /api/workspaces": { workspaces: [] },
    "GET /api/docs": { pages: [] },
    "GET /api/me/settings": SETTINGS,
    "GET /api/attention": attention,
  });
}

const UNRECOGNISED = {
  unrecognised: [
    { email: "bob@example.com", workspace: "beta" },
    { email: "carol@example.com", workspace: "gamma" },
  ],
};

test("administrators are told whose workspace's server is on an unrecognised screen, with a link to everyone's workspaces", async () => {
  api(ADMIN_USER, UNRECOGNISED);
  renderApp();
  const banner = await screen.findByRole("region", { name: m.attentionTitle });
  expect(
    within(banner).getByText(
      m.attentionUnrecognised(["bob@example.com: beta", "carol@example.com: gamma"]),
      { exact: false },
    ),
  ).toBeInTheDocument();
  const links = within(banner).getAllByRole("link");
  expect(links).toHaveLength(1);
  expect(links[0]).toHaveAttribute("href", ADMIN.everyone.path);
  expect(links[0]).toHaveTextContent(ADMIN.everyone.label);
});

test("it is fetched again on every change of page", async () => {
  const fake = api(ADMIN_USER);
  renderApp();
  await waitFor(() => expect(fake.made("GET", "/api/attention")).toHaveLength(1));
  await userEvent.click(screen.getByRole("link", { name: m.navSettings }));
  await waitFor(() => expect(fake.made("GET", "/api/attention")).toHaveLength(2));
  const nav = screen.getByRole("navigation", { name: m.navMain });
  await userEvent.click(within(nav).getByRole("link", { name: m.navDocs }));
  await waitFor(() => expect(fake.made("GET", "/api/attention")).toHaveLength(3));
});

test("nothing is shown when nothing needs attention", async () => {
  const fake = api(ADMIN_USER);
  renderApp();
  await waitFor(() => expect(fake.made("GET", "/api/attention")).toHaveLength(1));
  await screen.findByRole("list", { name: m.navAdmin });
  expect(screen.queryByRole("region", { name: m.attentionTitle })).not.toBeInTheDocument();
});

test("people who are not administrators never ask", async () => {
  const fake = api(ME, UNRECOGNISED);
  renderApp();
  expect(await screen.findByText(m.signedInAs(ME.email))).toBeInTheDocument();
  await userEvent.click(screen.getByRole("link", { name: m.navSettings }));
  await screen.findByRole("heading", { name: m.settingsTitle });
  expect(fake.made("GET", "/api/attention")).toHaveLength(0);
  expect(screen.queryByRole("region", { name: m.attentionTitle })).not.toBeInTheDocument();
});

test("however long a page stays open, it is not fetched again", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    const fake = api(ADMIN_USER);
    renderApp();
    await waitFor(() => expect(fake.made("GET", "/api/attention")).toHaveLength(1));
    await vi.advanceTimersByTimeAsync(24 * 60 * 60 * 1000); // a day: past any polling interval
    expect(fake.made("GET", "/api/attention")).toHaveLength(1);
  } finally {
    vi.useRealTimers();
  }
});

test("when it cannot be fetched, the server's reason is shown", async () => {
  api(ADMIN_USER, problem(500, "database_unavailable", "planted: the database is unavailable."));
  renderApp();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "planted: the database is unavailable.",
  );
});
