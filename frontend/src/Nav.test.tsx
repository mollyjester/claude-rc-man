import { fireEvent, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { vi } from "vitest";

import { ADMIN, ADMIN_PAGES } from "./AdminPages";
import { away } from "./App";
import { m } from "./messages";
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

const nav = () => screen.getByRole("banner");
// The Admin button, once the page knows who is signed in.
const adminButton = async () =>
  within(await screen.findByRole("banner")).getByRole("button", { name: m.navAdmin });

test("people who are not administrators are not offered administration", async () => {
  api(ME);
  renderApp();
  expect(await screen.findByText(m.signedInAs(ME.email))).toBeInTheDocument();
  expect(within(nav()).getByRole("link", { name: m.navSettings })).toBeInTheDocument();
  expect(within(nav()).queryByRole("button", { name: m.navAdmin })).not.toBeInTheDocument();
  for (const { label } of ADMIN_PAGES) {
    expect(screen.queryByRole("link", { name: label })).not.toBeInTheDocument();
  }
});

test("administrators get one Admin entry, which leads to every administrators' page", async () => {
  api(ADMIN_USER);
  renderApp();
  const admin = await adminButton();
  expect(within(nav()).getAllByRole("button", { name: m.navAdmin })).toHaveLength(1);
  expect(admin).toHaveAttribute("aria-expanded", "false");
  for (const { label } of ADMIN_PAGES) {
    expect(screen.queryByRole("link", { name: label })).not.toBeInTheDocument();
  }

  // a disclosure: a caret, which the stylesheet turns by aria-expanded, outside its name
  expect(admin.querySelector(".caret")).toHaveAttribute("aria-hidden", "true");
  expect(admin).toHaveAccessibleName(m.navAdmin);

  await userEvent.click(admin);
  expect(admin).toHaveAttribute("aria-expanded", "true");
  const menu = document.getElementById(admin.getAttribute("aria-controls")!)!;
  const links = within(menu).getAllByRole("link");
  expect(links).toHaveLength(ADMIN_PAGES.length);
  for (const { path, label } of ADMIN_PAGES) {
    expect(within(menu).getByRole("link", { name: label })).toHaveAttribute("href", path);
  }
});

test("choosing a page opens it and closes the menu, which then marks itself as where you are", async () => {
  api(ADMIN_USER);
  const { location } = renderApp();
  const admin = await adminButton();
  expect(admin).not.toHaveClass("active");
  await userEvent.click(admin);
  const [page] = ADMIN_PAGES;
  await userEvent.click(screen.getByRole("link", { name: page.label }));
  expect(location().pathname).toBe(page.path);
  expect(admin).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByRole("link", { name: page.label })).not.toBeInTheDocument();
  expect(admin).toHaveClass("active");
});

test("the menu closes when the page changes, and when the page already open is chosen", async () => {
  api(ADMIN_USER);
  const { navigate } = renderApp(ADMIN_PAGES[0].path);
  const admin = await adminButton();
  await userEvent.click(admin);
  expect(admin).toHaveAttribute("aria-expanded", "true");
  await userEvent.click(screen.getByRole("link", { name: ADMIN_PAGES[0].label }));
  expect(admin).toHaveAttribute("aria-expanded", "false");

  await userEvent.click(admin);
  expect(admin).toHaveAttribute("aria-expanded", "true");
  await navigate("/settings");
  expect(admin).toHaveAttribute("aria-expanded", "false");
});

test("the menu stays open while focus moves inside it, and closes when Tab takes focus out of it", async () => {
  api(ADMIN_USER);
  renderApp();
  const admin = await adminButton();
  await userEvent.click(admin);
  const menu = document.getElementById(admin.getAttribute("aria-controls")!)!;
  for (const { label } of ADMIN_PAGES) {
    await userEvent.tab();
    expect(screen.getByRole("link", { name: label })).toHaveFocus();
    expect(admin).toHaveAttribute("aria-expanded", "true");
  }
  await userEvent.tab(); // past the last page: out of the menu
  expect(menu.contains(document.activeElement)).toBe(false);
  expect(document.activeElement).not.toBe(document.body); // something outside took it
  expect(admin).toHaveAttribute("aria-expanded", "false");
});

test("a press outside the menu closes it, though focus stays where it was, and a press inside does not", async () => {
  api(ADMIN_USER);
  renderApp();
  const admin = await adminButton();
  await userEvent.click(admin);
  const menu = document.getElementById(admin.getAttribute("aria-controls")!)!;
  fireEvent.pointerDown(menu);
  expect(admin).toHaveAttribute("aria-expanded", "true"); // positive control: a press inside
  fireEvent.pointerDown(screen.getByText(m.signedInAs(ADMIN_USER.email)));
  expect(admin).toHaveFocus(); // the press moved no focus
  expect(admin).toHaveAttribute("aria-expanded", "false");
});

test("Escape closes the menu and returns to its button", async () => {
  api(ADMIN_USER);
  renderApp();
  const admin = await adminButton();
  await userEvent.click(admin);
  const first = screen.getByRole("link", { name: ADMIN_PAGES[0].label });
  first.focus();
  await userEvent.keyboard("{Escape}");
  expect(admin).toHaveAttribute("aria-expanded", "false");
  expect(first).not.toBeVisible();
  expect(admin).toHaveFocus();
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
  await userEvent.click(await adminButton());
  await userEvent.click(screen.getByRole("link", { name: m.navVersions }));
  expect(location().pathname).toBe("/admin/claude");
  expect(await screen.findByRole("heading", { name: m.versionsTitle })).toBeInTheDocument();
});

test("people who are not administrators are sent on from the Claude Code versions page", async () => {
  const fake = api(ME);
  const { location } = renderApp("/admin/claude");
  await screen.findByRole("region", { name: m.workspacesTitle }); // the Workspaces page
  expect(location().pathname).toBe("/");
  expect(fake.made("GET", "/api/claude/versions")).toHaveLength(0);
  expect(screen.queryByRole("heading", { name: m.versionsTitle })).not.toBeInTheDocument();
});

test("the navigation leads to the Workspaces page", async () => {
  api(ME);
  renderApp("/settings");
  await userEvent.click(
    within(await screen.findByRole("banner")).getByRole("link", { name: m.navDashboard }),
  );
  expect(await screen.findByRole("region", { name: m.workspacesTitle })).toBeInTheDocument();
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
  const link = await within(await screen.findByRole("banner")).findByRole("link", {
    name: m.navClaude,
  });
  expect(link).toHaveAttribute("href", "/claude");
});

test("signing out of rcman keeps sessions running unless asked, then leaves for oauth2-proxy", async () => {
  for (const keep of [true, false]) {
    const fake = api(ME);
    const left = vi.spyOn(away, "to").mockImplementation(() => {});
    const { unmount } = renderApp();
    const banner = await screen.findByRole("banner");
    const box = within(banner).getByRole("checkbox", { name: m.keepServersOnSignOut });
    expect(box).toBeChecked();
    if (!keep) await userEvent.click(box);
    const button = within(banner).getByRole("button", { name: m.signOut });
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
  const banner = await screen.findByRole("banner");
  const button = within(banner).getByRole("button", { name: m.signOut });
  expect(button).not.toHaveClass("primary");
  expect(button).not.toHaveClass("danger");
  const box = within(banner).getByRole("checkbox", { name: m.keepServersOnSignOut });
  expect(box.closest("form")).toBe(button.closest("form"));
  expect(m.signOut).not.toBe(m.claudeSignOut); // the two sign-outs are told apart by name
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
