import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { vi } from "vitest";

import { ADMIN, ADMIN_PAGES } from "./AdminPages";
import { away } from "./App";
import { m } from "./messages";
import { PAGES } from "./Pages";
import { ADMIN_USER, fakeApi, ME, NOTHING_TO_ATTEND, type Reply } from "./test/fakeApi";
import { renderApp } from "./test/render";

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
      upgrading: false,
    },
    "GET /api/claude/latest": { latest: null },
    "GET /api/attention": NOTHING_TO_ATTEND,
  });
}

const banner = () => screen.getByRole("banner");
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

test("the sidebar offers every page of the route table, each with its icon, in order", async () => {
  api(ME);
  renderApp();
  const nav = await sidebar();
  expect(SIDEBAR.length).toBeGreaterThan(1); // non-vacuity
  expect(SIDEBAR.map((page) => page.name)).toEqual([
    m.navDashboard,
    m.navClaude,
    m.navSettings,
    m.navDocs,
  ]);
  const links = within(nav).getAllByRole("link");
  expect(links.map((link) => [link.textContent, link.getAttribute("href")])).toEqual(
    SIDEBAR.map((page) => [page.name, page.path]),
  );
  for (const link of links) expect(link.querySelector("svg.icon")).not.toBeNull();
  // one navigation of the pages, not one for the sidebar and another for the drawer
  expect(screen.getAllByRole("navigation", { name: m.navMain })).toHaveLength(1);
  expect(document.querySelectorAll("nav.sidebar")).toHaveLength(1);
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

test("the sidebar marks the page where you are, a help page and an administrators' page among them", async () => {
  api(ADMIN_USER);
  const { navigate } = renderApp("/settings");
  const nav = await sidebar();
  const current = () =>
    within(nav)
      .getAllByRole("link")
      .filter((link) => link.getAttribute("aria-current") === "page")
      .map((link) => link.textContent);
  expect(current()).toEqual([m.navSettings]);
  await navigate("/help/workspaces");
  expect(current()).toEqual([m.navDocs]);
  await navigate(ADMIN.versions.path);
  expect(current()).toEqual([m.navVersions]);
  await navigate("/");
  expect(current()).toEqual([m.navDashboard]);
});

test("the top bar names rcman, leading home, and the page where you are, and links to Help", async () => {
  api(ADMIN_USER);
  const { navigate } = renderApp("/settings");
  const bar = await screen.findByRole("banner");
  expect(within(bar).getByRole("link", { name: m.appName })).toHaveAttribute("href", "/");
  const crumb = () => bar.querySelector(".crumb")?.textContent;
  expect(crumb()).toBe(m.navSettings);
  await navigate("/help/workspaces");
  expect(crumb()).toBe(m.navDocs);
  await navigate(ADMIN.people.path);
  expect(crumb()).toBe(m.navUsers);
  await navigate("/");
  expect(crumb()).toBe(m.navDashboard);
  const help = within(bar).getByRole("link", { name: m.navDocs });
  expect(help).toHaveAttribute("href", "/help");
  expect(help.querySelector("svg.icon")).not.toBeNull();
});

test("the account menu's button is the person's avatar: the first letter of their address", async () => {
  api(ME);
  renderApp();
  const button = within(await screen.findByRole("banner")).getByRole("button", {
    name: m.account,
  });
  const avatar = button.querySelector(".avatar")!;
  expect(avatar).toHaveTextContent(ME.email[0].toUpperCase());
  expect(avatar).toHaveAttribute("aria-hidden", "true");
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

test("choosing a page in the drawer opens it and closes the drawer, the page already open too", async () => {
  api(ADMIN_USER);
  const { location } = renderApp();
  const button = await menuButton();
  const nav = await sidebar();
  await userEvent.click(button);
  const [page] = ADMIN_PAGES;
  await userEvent.click(within(nav).getByRole("link", { name: page.label }));
  expect(location().pathname).toBe(page.path);
  expect(button).toHaveAttribute("aria-expanded", "false");
  await userEvent.click(button);
  await userEvent.click(within(nav).getByRole("link", { name: page.label }));
  expect(button).toHaveAttribute("aria-expanded", "false");
});

test("the drawer closes when the page changes, on Escape back to its button, and on a press outside it", async () => {
  api(ME);
  const { navigate } = renderApp();
  const button = await menuButton();
  const nav = await sidebar();
  await userEvent.click(button);
  await navigate("/settings");
  expect(button).toHaveAttribute("aria-expanded", "false");

  await userEvent.click(button);
  within(nav).getByRole("link", { name: m.navClaude }).focus();
  await userEvent.keyboard("{Escape}");
  expect(button).toHaveAttribute("aria-expanded", "false");
  expect(button).toHaveFocus();

  await userEvent.click(button);
  fireEvent.pointerDown(nav);
  expect(button).toHaveAttribute("aria-expanded", "true"); // positive control: a press inside
  fireEvent.pointerDown(screen.getByRole("main"));
  expect(button).toHaveAttribute("aria-expanded", "false");
});

test("opening the drawer puts focus on the page where you are, or on the first page", async () => {
  api(ADMIN_USER);
  const { navigate } = renderApp("/settings");
  const button = await menuButton();
  const nav = await sidebar();
  await userEvent.click(button);
  expect(within(nav).getByRole("link", { name: m.navSettings })).toHaveFocus();
  await userEvent.keyboard("{Escape}");
  expect(button).toHaveFocus();
  // an administrators' page, in its group
  await navigate(ADMIN.versions.path);
  await userEvent.click(button);
  expect(within(nav).getByRole("link", { name: m.navVersions })).toHaveFocus();
});

test("choosing a page in the drawer puts focus on the page, not nowhere; in the sidebar it stays", async () => {
  api(ME);
  const { location } = renderApp();
  const button = await menuButton();
  const nav = await sidebar();
  await userEvent.click(button);
  await userEvent.click(within(nav).getByRole("link", { name: m.navClaude }));
  expect(location().pathname).toBe("/claude");
  expect(screen.getByRole("main")).toHaveFocus();
  // the same page chosen again from the drawer: still the page
  await userEvent.click(button);
  await userEvent.click(within(nav).getByRole("link", { name: m.navClaude }));
  expect(screen.getByRole("main")).toHaveFocus();
  // beside the page, as on a desktop, the link keeps it, as any link does
  const settings = within(nav).getByRole("link", { name: m.navSettings });
  await userEvent.click(settings);
  expect(location().pathname).toBe("/settings");
  expect(settings).toHaveFocus();
});

test("the drawer stays open while focus moves through it, and closes when Tab takes focus out of it", async () => {
  api(ME);
  renderApp();
  const button = await menuButton();
  const nav = await sidebar();
  await userEvent.click(button);
  const links = within(nav).getAllByRole("link");
  links[links.length - 1].focus();
  expect(button).toHaveAttribute("aria-expanded", "true");
  await userEvent.tab(); // past the last page: out of the navigation, into the page
  expect(nav.contains(document.activeElement)).toBe(false);
  expect(document.activeElement).not.toBe(document.body); // something outside took it
  expect(button).toHaveAttribute("aria-expanded", "false");
});

test("the administration pages are People, everyone's workspaces, the Claude Code versions and the audit trail, in that order", () => {
  expect(ADMIN_PAGES.map((p) => p.label)).toEqual([
    m.navUsers,
    m.navEveryone,
    m.navVersions,
    m.navAudit,
  ]);
  expect(ADMIN.everyone.path).toBe("/admin/workspaces");
  expect(ADMIN.versions.path).toBe("/admin/claude");
  expect(ADMIN_PAGES).toEqual(Object.values(ADMIN));
});

test("the Claude Code versions page is offered to administrators, and opens", async () => {
  api(ADMIN_USER);
  const { location } = renderApp();
  await userEvent.click(within(await sidebar()).getByRole("link", { name: m.navVersions }));
  expect(location().pathname).toBe("/admin/claude");
  expect(await screen.findByRole("heading", { name: m.versionsTitle })).toBeInTheDocument();
});

test("people who are not administrators are sent on from the Claude Code versions page", async () => {
  const fake = api(ME);
  const { location } = renderApp("/admin/claude");
  await screen.findByRole("region", { name: m.navDashboard }); // the Workspaces page
  expect(location().pathname).toBe("/");
  expect(fake.made("GET", "/api/claude/versions")).toHaveLength(0);
  expect(screen.queryByRole("heading", { name: m.versionsTitle })).not.toBeInTheDocument();
});

test("the navigation leads to the Workspaces page", async () => {
  api(ME);
  renderApp("/settings");
  await userEvent.click(within(await sidebar()).getByRole("link", { name: m.navDashboard }));
  expect(await screen.findByRole("region", { name: m.navDashboard })).toBeInTheDocument();
});

test("an account rcman refuses sees the server's reason", async () => {
  const { problem } = await import("./test/fakeApi");
  fakeApi({ "GET /api/me": problem(403, "not_allowed", "This account may not use rcman.") });
  renderApp();
  expect(await screen.findByRole("alert")).toHaveTextContent("This account may not use rcman.");
});

test("everyone reaches the Claude page from the navigation", async () => {
  api(ME);
  renderApp();
  const link = within(await sidebar()).getByRole("link", { name: m.navClaude });
  expect(link).toHaveAttribute("href", "/claude");
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
    await vi.waitFor(() => expect(left).toHaveBeenCalledWith("/oauth2/sign_out"));
    expect(fake.made("POST", "/api/me/sign-out")[0].body).toEqual({ keep_sessions: keep });
    left.mockRestore();
    unmount();
  }
});

test("Sign out is a plain button, in one form with the box that says what happens to the servers", async () => {
  api(ME);
  renderApp();
  const menu = await account();
  const button = menu.getByRole("button", { name: m.signOut });
  expect(button).not.toHaveClass("primary");
  expect(button).not.toHaveClass("danger");
  const box = menu.getByRole("checkbox", { name: m.keepServersOnSignOut });
  expect(box.closest("form")).toBe(button.closest("form"));
  expect(m.signOut).not.toBe(m.claudeSignOut); // the two sign-outs are told apart by name
});

test("the account menu holds who is signed in, and signing out, shown only once it is opened", async () => {
  api(ME);
  renderApp();
  const button = within(await screen.findByRole("banner")).getByRole("button", {
    name: m.account,
  });
  expect(button).toHaveAttribute("aria-expanded", "false");
  // closed: none of it can be reached
  expect(within(banner()).queryByRole("button", { name: m.signOut })).not.toBeInTheDocument();
  expect(within(banner()).queryByRole("checkbox")).not.toBeInTheDocument();
  const menu = await account();
  expect(button).toHaveAttribute("aria-expanded", "true");
  expect(menu.getByText(m.signedInAs(ME.email))).toBeInTheDocument();
  expect(menu.getByRole("checkbox", { name: m.keepServersOnSignOut })).toBeInTheDocument();
  expect(menu.getByRole("button", { name: m.signOut })).toBeInTheDocument();
  await userEvent.keyboard("{Escape}");
  expect(button).toHaveAttribute("aria-expanded", "false");
  expect(button).toHaveFocus();
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
  await vi.waitFor(() => expect(document.title).toBe(m.pageTitle(name)));
  expect(document.title).toContain(m.appName);
});

test("the Workspaces page has its own top heading", async () => {
  api(ME);
  renderApp();
  expect(
    await screen.findByRole("heading", { level: 1, name: m.navDashboard }),
  ).toBeInTheDocument();
  expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
});
