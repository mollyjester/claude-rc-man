import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { MemoryRouter } from "react-router-dom";

import { m } from "./messages";
import { ADMIN_USER, fakeApi, NOTHING_TO_ATTEND, problem, type Routes } from "./test/fakeApi";
import { renderApp } from "./test/render";
import { Versions } from "./Versions";

const PEOPLE = {
  people: [
    { id: 1, email: "alice@example.com", admin: true, state: "active", workspaces: 2 },
    { id: 2, email: "bob@example.com", admin: false, state: "active", workspaces: 0 },
  ],
};

function api(extra: Routes) {
  return fakeApi({
    "GET /api/me": ADMIN_USER,
    "GET /api/attention": NOTHING_TO_ATTEND,
    ...extra,
  });
}

test("people are listed with their role and how many workspaces they have", async () => {
  api({ "GET /api/people": PEOPLE });
  renderApp("/admin/people");
  const alice = await screen.findByRole("row", { name: /alice@example.com/ });
  expect(within(alice).getByText(m.usersAdmin)).toBeInTheDocument();
  expect(within(alice).getByRole("cell", { name: "2" })).toBeInTheDocument();
  const bob = screen.getByRole("row", { name: /bob@example.com/ });
  expect(within(bob).getByText(m.usersUser)).toBeInTheDocument();
  expect(within(bob).getByRole("cell", { name: "0" })).toBeInTheDocument();
  const headers = screen.getAllByRole("columnheader").map((h) => h.textContent);
  expect(headers).toEqual([m.usersEmail, m.usersRole, m.usersWorkspaces, ""]);
});

test("adding someone sends exactly what was entered, and lists them", async () => {
  const people = [...PEOPLE.people];
  const fake = api({
    "GET /api/people": () => ({ people }),
    "POST /api/people": (c) => {
      const body = c.body as { email: string; admin: boolean };
      const person = { id: 3, ...body, state: "active", workspaces: 0 };
      people.push(person);
      return { person };
    },
  });
  renderApp("/admin/people");
  await userEvent.type(await screen.findByLabelText(m.usersEmail), "Carol@example.com");
  await userEvent.click(screen.getByLabelText(m.usersAddAdmin));
  await userEvent.click(screen.getByRole("button", { name: m.usersAdd }));
  await waitFor(() =>
    expect(fake.made("POST", "/api/people")[0].body).toEqual({
      email: "Carol@example.com",
      admin: true,
    }),
  );
  expect(await screen.findByText(m.usersAdded("Carol@example.com"))).toBeInTheDocument();
  expect(await screen.findByRole("row", { name: /Carol@example.com/ })).toBeInTheDocument();
  expect(screen.getByLabelText(m.usersEmail)).toHaveValue("");
  expect(screen.getByLabelText(m.usersAddAdmin)).not.toBeChecked();
});

test("the form asks for an address and the role only", async () => {
  api({ "GET /api/people": PEOPLE });
  renderApp("/admin/people");
  const form = (await screen.findByLabelText(m.usersEmail)).closest("form")!;
  const fields = [...form.querySelectorAll("input")].map((i) => [i.name, i.type]);
  expect(fields).toEqual([
    ["email", "text"],
    ["admin", "checkbox"],
  ]);
});

test("a refused addition names each field by its label", async () => {
  const errors = [
    { field: "email", message: "planted reason for the address" },
    { field: "admin", message: "planted reason for the role" },
  ];
  api({
    "GET /api/people": PEOPLE,
    "POST /api/people": problem(422, "validation_failed", "Some of what was sent is not valid.", {
      errors,
    }),
  });
  renderApp("/admin/people");
  await userEvent.type(await screen.findByLabelText(m.usersEmail), "not an address");
  await userEvent.click(screen.getByRole("button", { name: m.usersAdd }));
  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(`${m.usersEmail}: planted reason for the address`);
  expect(alert).toHaveTextContent(`${m.usersAddAdmin}: planted reason for the role`);
});

test("removing someone asks first, saying their workspaces go too, then removes them", async () => {
  let people = [...PEOPLE.people];
  const fake = api({
    "GET /api/people": () => ({ people }),
    "DELETE /api/people/2": () => {
      people = people.filter((p) => p.id !== 2);
      return { deleted: "bob@example.com" };
    },
  });
  renderApp("/admin/people");
  const bob = await screen.findByRole("row", { name: /bob@example.com/ });
  const remove = within(bob).getByRole("button", { name: m.usersRemove });
  expect(remove).toHaveClass("secondary-danger");
  await userEvent.click(remove);
  const dialog = screen.getByRole("dialog", { name: m.usersRemoveTitle("bob@example.com") });
  expect(within(dialog).getByText(m.usersRemoveBody)).toBeInTheDocument();
  expect(fake.calls.filter((c) => c.method === "DELETE")).toEqual([]);
  await userEvent.click(within(dialog).getByRole("button", { name: m.usersRemoveConfirm }));
  await waitFor(() => expect(fake.made("DELETE", "/api/people/2")).toHaveLength(1));
  expect(await screen.findByText(m.usersRemoved("bob@example.com"))).toBeInTheDocument();
  await waitFor(() =>
    expect(screen.queryByRole("row", { name: /bob@example.com/ })).not.toBeInTheDocument(),
  );
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("Cancel closes the dialog, having removed no one", async () => {
  const fake = api({ "GET /api/people": PEOPLE });
  renderApp("/admin/people");
  const bob = await screen.findByRole("row", { name: /bob@example.com/ });
  await userEvent.click(within(bob).getByRole("button", { name: m.usersRemove }));
  await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: m.cancel }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(fake.calls.filter((c) => c.method === "DELETE")).toEqual([]);
});

test("anyone can be removed, oneself included", async () => {
  api({ "GET /api/people": PEOPLE });
  renderApp("/admin/people");
  const alice = await screen.findByRole("row", { name: /alice@example.com/ });
  expect(within(alice).getByRole("button", { name: m.usersRemove })).toBeInTheDocument();
});

test("a refused removal shows the server's reason", async () => {
  api({
    "GET /api/people": PEOPLE,
    "DELETE /api/people/1": problem(404, "person_not_found", "There is no such person."),
  });
  renderApp("/admin/people");
  const alice = await screen.findByRole("row", { name: /alice/ });
  await userEvent.click(within(alice).getByRole("button", { name: m.usersRemove }));
  await userEvent.click(
    within(screen.getByRole("dialog")).getByRole("button", { name: m.usersRemoveConfirm }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("There is no such person.");
});

test("the audit trail pages towards older events", async () => {
  const page1 = {
    events: [1, 2].map((i) => ({
      id: 10 - i,
      at: "2026-10-01T10:00:00+00:00",
      email: "a@x",
      action: "server.start",
      target: "projects/x",
      result: "ok",
    })),
  };
  const fake = api({ "GET /api/audit": (c) => (c.query.get("before") ? { events: [] } : page1) });
  renderApp("/admin/audit");
  expect(await screen.findAllByText("server.start")).toHaveLength(2);
  expect(screen.getAllByRole("cell", { name: "a@x" })).toHaveLength(2); // who, by address alone
  await userEvent.click(screen.getByRole("button", { name: m.auditOlder }));
  await waitFor(() => expect(fake.made("GET", "/api/audit")[1].query.get("before")).toBe("8"));
});

// The Claude Code versions page is not offered while the server refuses it; it is
// rendered on its own here.
function renderVersions() {
  render(
    <MemoryRouter>
      <Versions />
    </MemoryRouter>,
  );
}

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
  renderVersions();
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
  renderVersions();
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
  renderVersions();
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
