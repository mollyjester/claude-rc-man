import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { m } from "./messages";
import {
  ADMIN_USER,
  CONNECTED,
  fakeApi,
  NOTHING_TO_ATTEND,
  problem,
  type Routes,
} from "./test/fakeApi";
import { renderApp } from "./test/render";

const USERS = {
  users: [
    { linux: "alice", email: "alice@example.com", admin: true, account: true },
    { linux: "bob", email: "bob@example.com", admin: false, account: false },
  ],
};

function api(extra: Routes) {
  return fakeApi({
    "GET /api/me": ADMIN_USER,
    "GET /api/claude": CONNECTED,
    "GET /api/attention": NOTHING_TO_ATTEND,
    ...extra,
  });
}

test("people are listed with role and account", async () => {
  api({ "GET /api/users": USERS });
  renderApp("/admin/people");
  const bob = await screen.findByRole("row", { name: /bob/ });
  expect(within(bob).getByText(m.usersUser)).toBeInTheDocument();
  expect(within(bob).getByText(m.usersAccountMissing)).toBeInTheDocument();
});

test("adding someone sends exactly what was entered", async () => {
  const fake = api({
    "GET /api/users": USERS,
    "POST /api/users": { user: { linux: "carol" }, created: true },
  });
  renderApp("/admin/people");
  await userEvent.type(await screen.findByLabelText(m.usersLinux), "carol");
  await userEvent.type(screen.getByLabelText(m.usersEmail), "carol@example.com");
  await userEvent.click(screen.getByLabelText(m.usersAddAdmin));
  await userEvent.click(screen.getByRole("button", { name: m.usersAdd }));
  await waitFor(() =>
    expect(fake.made("POST", "/api/users")[0].body).toEqual({
      linux: "carol",
      email: "carol@example.com",
      admin: true,
      adopt: false,
    }),
  );
  expect(await screen.findByText(m.usersAdded("carol"))).toBeInTheDocument();
});

test("a refused addition names each field by its label", async () => {
  const errors = [
    { field: "linux", message: "planted reason for the name" },
    { field: "email", message: "planted reason for the address" },
  ];
  api({
    "GET /api/users": USERS,
    "POST /api/users": problem(422, "validation_failed", "Some of what was sent is not valid.", {
      errors,
    }),
  });
  renderApp("/admin/people");
  await userEvent.type(await screen.findByLabelText(m.usersLinux), "Bad!");
  await userEvent.click(screen.getByRole("button", { name: m.usersAdd }));
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(`${m.usersLinux}: planted reason for the name`);
  expect(alert).toHaveTextContent(`${m.usersEmail}: planted reason for the address`);
});

test("removing someone asks about the home folder, keeping it unless told otherwise", async () => {
  const fake = api({
    "GET /api/users": USERS,
    "DELETE /api/users/bob": { deleted: "bob", home_deleted: false },
  });
  renderApp("/admin/people");
  const bob = await screen.findByRole("row", { name: /bob/ });
  await userEvent.click(within(bob).getByRole("button", { name: m.usersRemove }));
  const dialog = screen.getByRole("dialog", { name: m.usersRemoveTitle("bob") });
  expect(within(dialog).getByLabelText(m.usersKeepHome)).toBeChecked();
  await userEvent.click(within(dialog).getByRole("button", { name: m.usersRemoveConfirm }));
  await waitFor(() =>
    expect(fake.made("DELETE", "/api/users/bob")[0].query.get("home")).toBe("keep"),
  );
});

test("removing with the home folder", async () => {
  const fake = api({
    "GET /api/users": USERS,
    "DELETE /api/users/bob": { deleted: "bob", home_deleted: true },
  });
  renderApp("/admin/people");
  const bob = await screen.findByRole("row", { name: /bob/ });
  await userEvent.click(within(bob).getByRole("button", { name: m.usersRemove }));
  const dialog = screen.getByRole("dialog");
  await userEvent.click(within(dialog).getByLabelText(m.usersDeleteHome));
  await userEvent.click(within(dialog).getByRole("button", { name: m.usersRemoveConfirm }));
  await waitFor(() =>
    expect(fake.made("DELETE", "/api/users/bob")[0].query.get("home")).toBe("delete"),
  );
});

test("a refused removal shows the server's reason", async () => {
  api({
    "GET /api/users": USERS,
    "DELETE /api/users/alice": problem(409, "last_admin", "That is the last administrator."),
  });
  renderApp("/admin/people");
  const alice = await screen.findByRole("row", { name: /alice/ });
  await userEvent.click(within(alice).getByRole("button", { name: m.usersRemove }));
  await userEvent.click(
    within(screen.getByRole("dialog")).getByRole("button", { name: m.usersRemoveConfirm }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("That is the last administrator.");
});

test("the audit trail pages towards older events", async () => {
  const page1 = {
    events: [1, 2].map((i) => ({
      id: 10 - i,
      at: "2026-10-01T10:00:00+00:00",
      email: "a@x",
      linux: "a",
      action: "server.start",
      target: "projects/x",
      result: "ok",
    })),
  };
  const fake = api({ "GET /api/audit": (c) => (c.query.get("before") ? { events: [] } : page1) });
  renderApp("/admin/audit");
  expect(await screen.findAllByText("server.start")).toHaveLength(2);
  await userEvent.click(screen.getByRole("button", { name: m.auditOlder }));
  await waitFor(() => expect(fake.made("GET", "/api/audit")[1].query.get("before")).toBe("8"));
});

test("versions show who differs and whether the approved one is tested", async () => {
  api({
    "GET /api/claude/versions": {
      approved: "2.2.0",
      tested: "2.1.0",
      untested: true,
      differs: ["bob"],
      users: [
        { linux: "alice", installed: "2.2.0" },
        { linux: "bob", installed: "2.1.0" },
      ],
    },
  });
  renderApp("/admin/claude");
  expect(await screen.findByText(m.versionsUntested)).toBeInTheDocument();
  const bob = screen.getByRole("row", { name: /bob/ });
  expect(within(bob).getByText(new RegExp(m.versionsDiffers))).toBeInTheDocument();
});

test("a refused version names the field by its label", async () => {
  const errors = [{ field: "version", message: "planted reason for the version" }];
  api({
    "GET /api/claude/versions": {
      approved: "2.1.0",
      tested: "2.1.0",
      untested: false,
      differs: [],
      users: [],
    },
    "POST /api/claude/upgrade": problem(
      422,
      "validation_failed",
      "Some of what was sent is not valid.",
      { errors },
    ),
  });
  renderApp("/admin/claude");
  await userEvent.type(await screen.findByLabelText(m.versionsUpgrade), "latest");
  await userEvent.click(screen.getByRole("button", { name: m.versionsUpgradeGo }));
  expect(await screen.findByRole("alert")).toHaveTextContent(
    `${m.versionsUpgrade}: planted reason for the version`,
  );
});

test("rolling out a version sends it and shows failed checks as the server named them", async () => {
  const fake = api({
    "GET /api/claude/versions": {
      approved: "2.1.0",
      tested: "2.1.0",
      untested: false,
      differs: [],
      users: [],
    },
    "POST /api/claude/upgrade": problem(
      409,
      "claude_checks_failed",
      "That version failed rcman's checks.",
      {
        failed: ["sign_in_screen", "logout"],
        report: {
          sign_in_screen: { lines: ["ran: claude auth login", "planted sign-in screen"], more: 2 },
          logout: { lines: ["ran: claude auth logout", "planted logout"], more: 0 },
        },
      },
    ),
  });
  renderApp("/admin/claude");
  await userEvent.type(await screen.findByLabelText(m.versionsUpgrade), "2.3.0");
  await userEvent.click(screen.getByRole("button", { name: m.versionsUpgradeGo }));
  await waitFor(() =>
    expect(fake.made("POST", "/api/claude/upgrade")[0].body).toEqual({ version: "2.3.0" }),
  );
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("That version failed rcman's checks.");
  expect(alert).toHaveTextContent("sign_in_screen");
  expect(alert).toHaveTextContent("logout");
  const signIn = within(alert).getByRole("figure", { name: "sign_in_screen" });
  expect(signIn).toHaveTextContent("planted sign-in screen");
  expect(signIn.nextElementSibling).toHaveTextContent(m.versionsReportMore(2));
  const logout = within(alert).getByRole("figure", { name: "logout" });
  expect(logout).toHaveTextContent("planted logout");
});
