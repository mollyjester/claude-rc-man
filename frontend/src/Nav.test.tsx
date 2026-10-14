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
    "GET /api/workspaces": { workspaces: [] },
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

test("the administration pages are People, everyone's workspaces and the audit trail, in that order", () => {
  expect(ADMIN_PAGES.map((p) => p.label)).toEqual([m.navUsers, m.navEveryone, m.navAudit]);
  expect(ADMIN.everyone.path).toBe("/admin/workspaces");
  expect(ADMIN_PAGES).toEqual(Object.values(ADMIN));
});

test("the Claude Code versions page, which the server refuses, is not offered", async () => {
  api(ADMIN_USER);
  const { location } = renderApp("/admin/claude");
  await screen.findByRole("region", { name: m.workspacesTitle }); // sent on to the Workspaces page
  expect(location().pathname).toBe("/");
  await userEvent.click(await adminButton());
  expect(screen.getAllByRole("link", { name: m.navUsers })).toHaveLength(1); // positive control
  expect(screen.queryByRole("link", { name: m.versionsTitle })).not.toBeInTheDocument();
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
    const box = within(banner).getByRole("checkbox", { name: m.keepSessions });
    expect(box).toBeChecked();
    if (!keep) await userEvent.click(box);
    await userEvent.click(within(banner).getByRole("button", { name: m.signOut }));
    await vi.waitFor(() => expect(left).toHaveBeenCalledWith("/oauth2/sign_out"));
    expect(fake.made("POST", "/api/me/sign-out")[0].body).toEqual({ keep_sessions: keep });
    left.mockRestore();
    unmount();
  }
});
