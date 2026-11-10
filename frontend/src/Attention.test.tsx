import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ADMIN } from "./AdminPages";
import { m } from "./messages";
import { ADMIN_USER, fakeApi, ME, NOTHING_TO_ATTEND, type Reply, SETTINGS } from "./test/fakeApi";
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

test("people who are not administrators never ask", async () => {
  const fake = api(ME, UNRECOGNISED);
  renderApp();
  expect(await screen.findByText(m.signedInAs(ME.email))).toBeInTheDocument();
  await userEvent.click(screen.getByRole("link", { name: m.navSettings }));
  await screen.findByRole("heading", { name: m.settingsTitle });
  expect(fake.made("GET", "/api/attention")).toHaveLength(0);
  expect(screen.queryByRole("region", { name: m.attentionTitle })).not.toBeInTheDocument();
});
