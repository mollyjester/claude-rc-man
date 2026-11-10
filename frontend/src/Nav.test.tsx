import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { vi } from "vitest";

import { ADMIN_PAGES } from "./AdminPages";
import { away } from "./App";
import { m } from "./messages";
import { PAGES } from "./Pages";
import { ADMIN_USER, fakeApi, ME, NOTHING_TO_ATTEND, type Reply } from "./test/fakeApi";
import { renderApp } from "./test/render";
import { waitForIt } from "./test/waitForIt";

function api(me: Reply) {
  return fakeApi({
    "POST /api/me/sign-out": { sign_out_url: "/oauth2/sign_out" },
    "GET /api/me": me,
    "GET /api/workspaces": { workspaces: [], claude_approved: "1.0.0" },
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
    "GET /api/attention": NOTHING_TO_ATTEND,
  });
}

// The one navigation of the pages: the sidebar beside the page, and a drawer on a phone.
const sidebar = async () => screen.findByRole("navigation", { name: m.navMain });
// The pages the sidebar offers everyone, from the route table.
const SIDEBAR = PAGES.filter((page) => page.icon !== undefined);

// Opens the account menu, and returns what it holds.
async function account() {
  const button = within(await screen.findByRole("banner")).getByRole("button", {
    name: m.account,
  });
  await userEvent.click(button);
  return within(document.getElementById(button.getAttribute("aria-controls")!)!);
}
// The button that opens the sidebar as a drawer, which the stylesheet shows on a phone only.
const menuButton = async () =>
  within(await screen.findByRole("banner")).getByRole("button", { name: m.navMenu });

test("people who are not administrators are not offered administration", async () => {
  api(ME);
  renderApp();
  const nav = await sidebar();
  expect(within(nav).getByRole("link", { name: m.navSettings })).toBeInTheDocument(); // positive
  expect(within(nav).queryByRole("list", { name: m.navAdmin })).not.toBeInTheDocument();
  expect(screen.queryByText(m.navAdmin)).not.toBeInTheDocument();
  for (const { label } of ADMIN_PAGES) {
    expect(screen.queryByRole("link", { name: label })).not.toBeInTheDocument();
  }
});

test("administrators get a group of the administrators' pages in the sidebar, in order", async () => {
  api(ADMIN_USER);
  renderApp();
  const nav = await sidebar();
  const group = within(nav).getByRole("list", { name: m.navAdmin });
  const links = within(group).getAllByRole("link");
  expect(links).toHaveLength(ADMIN_PAGES.length);
  expect(links.map((link) => [link.textContent, link.getAttribute("href")])).toEqual(
    ADMIN_PAGES.map((page) => [page.label, page.path]),
  );
  for (const link of links) expect(link.querySelector("svg.icon")).not.toBeNull();
  // everyone's pages come first, outside the group
  expect(within(nav).getAllByRole("link")).toHaveLength(SIDEBAR.length + ADMIN_PAGES.length);
  expect(within(group).queryByRole("link", { name: m.navSettings })).not.toBeInTheDocument();
});

test("the menu button opens the sidebar as a drawer, a disclosure of the one navigation", async () => {
  api(ME);
  renderApp();
  const button = await menuButton();
  const nav = await sidebar();
  expect(button).toHaveAttribute("aria-controls", nav.id);
  expect(button).toHaveAttribute("aria-expanded", "false");
  expect(nav).not.toHaveClass("open");
  expect(nav).toHaveAttribute("data-component", "Navigation");
  await userEvent.click(button);
  expect(button).toHaveAttribute("aria-expanded", "true");
  expect(nav).toHaveClass("open");
  expect(document.querySelectorAll("nav.sidebar")).toHaveLength(1); // the same element
  await userEvent.click(button);
  expect(nav).not.toHaveClass("open");
});

test("signing out of rcman keeps sessions running unless asked, then leaves for oauth2-proxy", async () => {
  for (const keep of [true, false]) {
    const fake = api(ME);
    const left = vi.spyOn(away, "to").mockImplementation(() => {});
    const { unmount } = renderApp();
    const menu = await account();
    const box = menu.getByRole("checkbox", { name: m.keepServersOnSignOut });
    expect(box).toBeChecked();
    if (!keep) await userEvent.click(box);
    const button = menu.getByRole("button", { name: m.signOut });
    await userEvent.click(button);
    await waitForIt(() => expect(left).toHaveBeenCalledWith("/oauth2/sign_out"));
    expect(fake.made("POST", "/api/me/sign-out")[0].body).toEqual({ keep_sessions: keep });
    left.mockRestore();
    unmount();
  }
});

test("a link that skips to the content is the first thing Tab reaches, and moves focus to the page", async () => {
  api(ME);
  renderApp();
  await screen.findByRole("main");
  await userEvent.tab();
  const skip = screen.getByRole("link", { name: m.skipToContent });
  expect(skip).toHaveFocus();
  // nothing that can take focus comes before it in the page
  const focusable = document.querySelector("a[href], button, input, select, textarea, [tabindex]");
  expect(focusable).toBe(skip);
  await userEvent.keyboard("{Enter}");
  expect(screen.getByRole("main")).toHaveFocus();
});

test.each([
  ["/", m.navDashboard],
  ["/claude", m.navClaude],
  ["/settings", m.navSettings],
  ["/help", m.navDocs],
  ...ADMIN_PAGES.map((page) => [page.path, page.label]),
])("the page at %s is titled with its own name", async (path, name) => {
  api(ADMIN_USER);
  renderApp(path);
  await screen.findByRole("main");
  await waitForIt(() => expect(document.title).toBe(m.pageTitle(name)));
  expect(document.title).toContain(m.appName);
});
