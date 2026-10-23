import { act, cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { m } from "./messages";
import { ADMIN_USER, fakeApi, NOTHING_TO_ATTEND, problem, type Routes } from "./test/fakeApi";
import { expectRefused } from "./test/fields";
import { renderApp } from "./test/render";
import { UPGRADE_POLL_MS } from "./Versions";

const NONE = { memory: null, cpu: null, pids: null };

const PEOPLE = {
  people: [
    {
      id: 1,
      email: "alice@example.com",
      admin: true,
      state: "active",
      workspaces: 2,
      limits: { memory: "4G", cpu: "1.5", pids: "500" },
    },
    {
      id: 2,
      email: "bob@example.com",
      admin: false,
      state: "active",
      workspaces: 0,
      limits: NONE,
    },
  ],
};

function api(extra: Routes) {
  return fakeApi({
    "GET /api/me": ADMIN_USER,
    "GET /api/attention": NOTHING_TO_ATTEND,
    ...extra,
  });
}

// Opens the dialog that adds a person, with the button beside the page's heading.
async function openAdd() {
  await userEvent.click(await screen.findByRole("button", { name: m.usersAdd }));
  return screen.findByRole("dialog", { name: m.usersAdd });
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
  expect(headers).toEqual([
    m.usersEmail,
    m.usersRole,
    m.usersWorkspaces,
    m.usersMemory,
    m.usersCpu,
    m.usersProcesses,
    m.usersActions,
  ]);
  // the actions' column is named too, though only heard
  expect(screen.getByRole("columnheader", { name: m.usersActions })).toBeInTheDocument();
});

test("each person's stored limits are listed, and no limit is said so", async () => {
  api({ "GET /api/people": PEOPLE });
  renderApp("/admin/people");
  const alice = await screen.findByRole("row", { name: /alice@example.com/ });
  const cells = within(alice)
    .getAllByRole("cell")
    .map((c) => c.textContent);
  expect(cells.slice(3, 6)).toEqual(["4G", "1.5", "500"]);
  const bob = screen.getByRole("row", { name: /bob@example.com/ });
  const bobs = within(bob)
    .getAllByRole("cell")
    .map((c) => c.textContent);
  expect(bobs.slice(3, 6)).toEqual([m.usersNoLimit, m.usersNoLimit, m.usersNoLimit]);
});

test("adding someone sends exactly what was entered, and lists them", async () => {
  const people = [...PEOPLE.people];
  const fake = api({
    "GET /api/people": () => ({ people }),
    "POST /api/people": (c) => {
      const body = c.body as { email: string; admin: boolean };
      const person = { id: 3, ...body, state: "active", workspaces: 0, limits: NONE };
      people.push(person);
      return { person };
    },
  });
  renderApp("/admin/people");
  const dialog = await openAdd();
  await userEvent.type(within(dialog).getByLabelText(m.usersEmail), "Carol@example.com");
  await userEvent.click(within(dialog).getByLabelText(m.usersAddAdmin));
  await userEvent.click(within(dialog).getByRole("button", { name: m.usersAddGo }));
  await waitFor(() =>
    expect(fake.made("POST", "/api/people")[0].body).toEqual({
      email: "Carol@example.com",
      admin: true,
    }),
  );
  const added = await screen.findByText(m.usersAdded("Carol@example.com"));
  expect(added.closest(".message")).toHaveClass("message-success");
  expect(await screen.findByRole("row", { name: /Carol@example.com/ })).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); // done with, it closes
  // asked again, it starts empty
  const again = await openAdd();
  expect(within(again).getByLabelText(m.usersEmail)).toHaveValue("");
  expect(within(again).getByLabelText(m.usersAddAdmin)).not.toBeChecked();
});

test("the page's heading row holds its title and Add person, its one primary button, which opens the form in a dialog", async () => {
  api({ "GET /api/people": PEOPLE });
  renderApp("/admin/people");
  const title = await screen.findByRole("heading", { level: 1, name: m.usersTitle });
  const head = title.parentElement!;
  expect(head).toHaveClass("page-head");
  const add = within(head).getByRole("button", { name: m.usersAdd });
  expect(add).toHaveClass("primary");
  expect(screen.queryByLabelText(m.usersEmail)).not.toBeInTheDocument(); // not before it is asked for
  await userEvent.click(add);
  expect(await screen.findByRole("dialog", { name: m.usersAdd })).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: m.cancel }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(add).toHaveFocus();
});

test("the people are a table in a card, each row's Limits a subtle button and Remove a danger outline", async () => {
  api({ "GET /api/people": PEOPLE });
  renderApp("/admin/people");
  const bob = await screen.findByRole("row", { name: /bob@example.com/ });
  const card = bob.closest("table")!.closest(".card")!;
  expect(card).not.toBeNull();
  expect(card).toHaveClass("flush");
  expect(within(bob).getByRole("button", { name: m.usersLimits })).toHaveClass("subtle");
  expect(within(bob).getByRole("button", { name: m.usersRemove })).toHaveClass("secondary-danger");
});

// --- the More actions menu, which holds a row's actions on a phone ----------------------

// A row's More actions button, by the person it is for.
const moreFor = (row: HTMLElement, email: string) =>
  within(row).getByRole("button", { name: m.usersMoreActionsFor(email) });

// Opens a row's More actions menu, and returns its panel.
async function openMore(email: string) {
  const row = await screen.findByRole("row", { name: new RegExp(email) });
  const more = moreFor(row, email);
  await userEvent.click(more);
  expect(more).toHaveAttribute("aria-expanded", "true");
  const panel = document.getElementById(more.getAttribute("aria-controls")!)!;
  expect(panel).toBeInTheDocument();
  return { row, more, panel };
}

test("each row has a More actions button named for its person, a subtle icon button, closed", async () => {
  api({ "GET /api/people": PEOPLE });
  renderApp("/admin/people");
  for (const email of ["alice@example.com", BOB]) {
    const row = await screen.findByRole("row", { name: new RegExp(email) });
    const more = moreFor(row, email);
    expect(more).toHaveClass("subtle", "icon-button");
    expect(more).toHaveAttribute("aria-expanded", "false");
    expect(more.closest("td")).toHaveClass("row-actions");
  }
});

test("More actions opens a menu of Limits and Remove, Remove in danger's text, marked as the menu's", async () => {
  api({ "GET /api/people": PEOPLE });
  renderApp("/admin/people");
  const { panel } = await openMore(BOB);
  expect(panel).toHaveAttribute("data-component", "PersonMenu");
  expect(panel).toHaveClass("popover");
  const items = within(panel).getAllByRole("button");
  expect(items.map((b) => b.textContent)).toEqual([m.usersLimits, m.usersRemove]);
  expect(items[1]).toHaveClass("danger-item");
  expect(items[0]).not.toHaveClass("danger-item");
});

test("Escape closes the menu and puts focus back on More actions, and a press outside closes it", async () => {
  api({ "GET /api/people": PEOPLE });
  renderApp("/admin/people");
  const { more, panel } = await openMore(BOB);
  within(panel).getByRole("button", { name: m.usersLimits }).focus();
  await userEvent.keyboard("{Escape}");
  expect(more).toHaveAttribute("aria-expanded", "false");
  expect(panel).not.toBeInTheDocument();
  expect(more).toHaveFocus();

  await userEvent.click(more);
  expect(more).toHaveAttribute("aria-expanded", "true");
  fireEvent.pointerDown(screen.getByRole("heading", { level: 1, name: m.usersTitle }));
  expect(more).toHaveAttribute("aria-expanded", "false");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("Limits in the menu closes it and opens that person's limits, and closing them puts focus back on More actions", async () => {
  limitsApi();
  renderApp("/admin/people");
  const { more, panel } = await openMore(BOB);
  await userEvent.click(within(panel).getByRole("button", { name: m.usersLimits }));
  expect(more).toHaveAttribute("aria-expanded", "false");
  const dialog = screen.getByRole("dialog", { name: m.limitsTitle(BOB) });
  await within(dialog).findByRole("form", { name: PERSON_SCOPE });
  await userEvent.click(within(dialog).getByRole("button", { name: m.close }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(more).toHaveFocus();
});

test("Remove in the menu closes it and asks about that person, and Cancel puts focus back on More actions", async () => {
  const fake = api({ "GET /api/people": PEOPLE });
  renderApp("/admin/people");
  const { more, panel } = await openMore(BOB);
  await userEvent.click(within(panel).getByRole("button", { name: m.usersRemove }));
  expect(more).toHaveAttribute("aria-expanded", "false");
  const dialog = screen.getByRole("dialog", { name: m.usersRemoveTitle(BOB) });
  await userEvent.click(within(dialog).getByRole("button", { name: m.cancel }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(more).toHaveFocus();
  expect(fake.calls.filter((c) => c.method === "DELETE")).toEqual([]);
});

test.each([
  [
    "the people",
    "/admin/people",
    () => api({ "GET /api/people": PEOPLE }),
    /alice@example.com/,
    [m.usersWorkspaces, m.usersMemory, m.usersCpu, m.usersProcesses],
  ],
  [
    "the audit trail",
    "/admin/audit",
    () => api({ "GET /api/audit": { events: [{ ...EVENT_ONE }] } }),
    /a@x/,
    [],
  ],
])(
  "%s keep a table's roles said outright, for a phone draws its rows as a list, and show a column's name beside a value only where the value alone does not say what it is",
  async (_, path, serve, row, labelled) => {
    serve();
    renderApp(path);
    const found = await screen.findByRole("row", { name: row });
    const table = found.closest("table")!;
    expect(table).toHaveClass("stacked");
    expect(table).toHaveAttribute("role", "table");
    for (const group of table.querySelectorAll("thead, tbody"))
      expect(group).toHaveAttribute("role", "rowgroup");
    for (const tr of table.querySelectorAll("tr")) expect(tr).toHaveAttribute("role", "row");
    for (const th of table.querySelectorAll("th"))
      expect(th).toHaveAttribute("role", "columnheader");
    for (const td of table.querySelectorAll("td")) expect(td).toHaveAttribute("role", "cell");
    const labels = [...found.querySelectorAll("td[data-label]")].map((td) =>
      td.getAttribute("data-label"),
    );
    expect(labels).toEqual(labelled);
    for (const td of found.querySelectorAll("td[data-label]")) expect(td).toHaveClass("named");
  },
);

test("the form asks for an address and the role only", async () => {
  api({ "GET /api/people": PEOPLE });
  renderApp("/admin/people");
  const dialog = await openAdd();
  const form = within(dialog).getByLabelText(m.usersEmail).closest("form")!;
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
  const dialog = await openAdd();
  await userEvent.type(within(dialog).getByLabelText(m.usersEmail), "not an address");
  await userEvent.click(within(dialog).getByRole("button", { name: m.usersAddGo }));
  await within(dialog).findByRole("alert");
  expectRefused(within(dialog).getByLabelText(m.usersEmail), "planted reason for the address");
  expectRefused(within(dialog).getByLabelText(m.usersAddAdmin), "planted reason for the role");
  expect(within(dialog).getByLabelText(m.usersEmail)).toHaveFocus();
  expect(within(dialog).getByLabelText(m.usersEmail)).toHaveValue("not an address"); // kept
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
  expect(await screen.findAllByText(m.auditActions["server.start"])).toHaveLength(2);
  expect(screen.getAllByRole("cell", { name: "a@x" })).toHaveLength(2); // who, by address alone
  await userEvent.click(screen.getByRole("button", { name: m.auditOlder }));
  await waitFor(() => expect(fake.made("GET", "/api/audit")[1].query.get("before")).toBe("8"));
});

test("the audit trail is a table in a card, with Older at the card's foot", async () => {
  api({ "GET /api/audit": { events: [{ ...EVENT_ONE }] } });
  renderApp("/admin/audit");
  const title = await screen.findByRole("heading", { level: 1, name: m.auditTitle });
  expect(title.parentElement).toHaveClass("page-head");
  const row = await screen.findByRole("row", { name: /a@x/ });
  const card = row.closest(".card")!;
  expect(card).toHaveClass("flush");
  const older = within(card as HTMLElement).getByRole("button", { name: m.auditOlder });
  expect(older.closest(".card-foot")).not.toBeNull();
});

test("until the first page of the trail is had the page says it is loading, and a refusal shows no card", async () => {
  let answer: (reply: unknown) => void = () => {};
  api({ "GET /api/audit": () => new Promise((resolve) => (answer = resolve)) });
  const { unmount } = renderApp("/admin/audit");
  await screen.findByRole("heading", { level: 1, name: m.auditTitle }); // the page, not the app, loading
  expect(screen.getByText(m.loading)).toBeInTheDocument();
  expect(screen.queryByText(m.auditEmpty)).not.toBeInTheDocument();
  await act(async () => answer({ events: [] }));
  expect(await screen.findByText(m.auditEmpty)).toBeInTheDocument();
  expect(screen.queryByText(m.loading)).not.toBeInTheDocument();
  unmount();
  api({ "GET /api/audit": problem(500, "internal_error", "planted: no trail") });
  renderApp("/admin/audit");
  expect(await screen.findByRole("alert")).toHaveTextContent("planted: no trail");
  expect(document.querySelector("main .card")).toBeNull();
});

test("what was done on the People page is said in a place that is there before it", async () => {
  api({ "GET /api/people": PEOPLE, "POST /api/people": { person: {} } });
  renderApp("/admin/people");
  await screen.findByRole("row", { name: /bob@example.com/ });
  const status = screen.getByRole("status");
  expect(status).toBeEmptyDOMElement();
  const dialog = await openAdd();
  await userEvent.type(within(dialog).getByLabelText(m.usersEmail), "carol@example.com");
  await userEvent.click(within(dialog).getByRole("button", { name: m.usersAddGo }));
  await waitFor(() => expect(status).toHaveTextContent(m.usersAdded("carol@example.com")));
  expect(screen.getByRole("status")).toBe(status);
});

test("with nothing recorded the card says so, with no table", async () => {
  api({ "GET /api/audit": { events: [] } });
  renderApp("/admin/audit");
  const empty = await screen.findByText(m.auditEmpty);
  expect(empty.closest(".card")).not.toBeNull();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: m.auditOlder })).not.toBeInTheDocument();
});

const EVENT_ONE = {
  id: 5,
  at: "2026-10-01T10:05:09+00:00",
  email: "a@x",
  action: "server.start",
  target: "projects/x",
  result: "ok",
};

const EVENT = {
  id: 5,
  at: "2026-10-01T10:05:09+00:00",
  email: "a@x",
  action: "server.start",
  target: "projects/x",
  result: "ok",
};

async function auditRow(event: Omit<Partial<typeof EVENT>, "target"> & { target?: string | null }) {
  api({ "GET /api/audit": { events: [{ ...EVENT, ...event }] } });
  renderApp("/admin/audit");
  return within(await screen.findByRole("row", { name: /a@x/ }));
}

test("the audit trail names each action in plain words, and an action it has no words for by its code", async () => {
  let row = await auditRow({ action: "person.add" });
  expect(row.getByText(m.auditActions["person.add"])).toBeInTheDocument();
  expect(row.queryByText("person.add")).not.toBeInTheDocument();
  cleanup();
  row = await auditRow({ action: "something.new" });
  expect(row.getByText("something.new")).toBeInTheDocument();
});

test("a result that worked is said so, one that did not is marked as failed beside its code, and one in between says what", async () => {
  let row = await auditRow({ result: "ok" });
  expect(row.getByText(m.auditResults.ok)).toBeInTheDocument();
  expect(row.queryByText(m.auditFailed)).not.toBeInTheDocument();
  cleanup();
  row = await auditRow({ result: "not_a_repository" });
  const failed = row.getByText(m.auditFailed);
  expect(failed.closest("td")).toHaveClass("result-failed");
  expect(failed).toHaveClass("badge", "badge-problem"); // a danger pill
  expect(row.getByText("not_a_repository")).toBeInTheDocument(); // the code, not worded
  cleanup();
  row = await auditRow({ result: "committed_not_pushed" });
  expect(row.getByText(m.auditResults.committed_not_pushed)).toBeInTheDocument();
  expect(row.queryByText(m.auditFailed)).not.toBeInTheDocument();
});

test("a date is shown the same everywhere, in UTC, and is not broken across lines", async () => {
  const row = await auditRow({ at: "2026-10-01T12:05:09+02:00" });
  const when = row.getByText("2026-10-01 10:05:09");
  expect(when.tagName).toBe("TIME");
  expect(when).toHaveAttribute("datetime", "2026-10-01T12:05:09+02:00");
  expect(when.closest("td")).toHaveClass("when");
  expect(screen.getByRole("columnheader", { name: m.auditWhen })).toBeInTheDocument();
});

test("a target is read as it is, a list of settings as a list, and none as none", async () => {
  let row = await auditRow({
    action: "settings.update",
    target: '{"prefix": "mine", "author_name": "Ann"}',
  });
  expect(row.getByText("prefix: mine, author_name: Ann")).toBeInTheDocument();
  expect(row.queryByText(/[{}]/)).not.toBeInTheDocument();
  cleanup();
  row = await auditRow({ target: null });
  expect(row.getByText(m.auditNoTarget)).toBeInTheDocument();
  cleanup();
  row = await auditRow({ target: "{not json" }); // not a list of settings: as it is
  expect(row.getByText("{not json")).toBeInTheDocument();
});

// --- the Limits dialog ------------------------------------------------------------------

type Used = { memory: string; memory_bytes: number; tasks: number } | null;
type Held = { memory: string | null; cpu: string | null; pids: string | null };
type Scope = { id: number; name: string; state: string; limits: Held; usage: Used };

const used = (memory: string, tasks: number): Used => ({
  memory,
  memory_bytes: 1024 * 1024 * 1024,
  tasks,
});

// What GET /api/people/2/limits answers for bob: a limit of his own, one workspace that is
// ready with limits of its own and its use, and one still being made whose use cannot be read.
const BOB = "bob@example.com";
const WORKSPACES: Scope[] = [
  {
    id: 7,
    name: "alpha",
    state: "ready",
    limits: { memory: "2G", cpu: null, pids: "300" },
    usage: used("1G", 12),
  },
  { id: 8, name: "beta", state: "creating", limits: NONE, usage: null },
];
const REPORT_OF_BOB = {
  person: { limits: { memory: "4G", cpu: null, pids: "500" }, usage: used("1500M", 40) },
  workspaces: WORKSPACES,
};
const PERSON_SCOPE = m.limitsPersonScope(BOB);
const ALPHA_SCOPE = m.limitsWorkspaceScope("alpha", null);
const BETA_SCOPE = m.limitsWorkspaceScope("beta", m.workspaceStates.creating);

function limitsApi(extra: Routes = {}) {
  return api({
    "GET /api/people": PEOPLE,
    "GET /api/people/2/limits": REPORT_OF_BOB,
    ...extra,
  });
}

// Opens bob's Limits dialog and waits for its forms.
async function openLimits() {
  const row = await screen.findByRole("row", { name: /bob@example.com/ });
  await userEvent.click(within(row).getByRole("button", { name: m.usersLimits }));
  const dialog = screen.getByRole("dialog", { name: m.limitsTitle(BOB) });
  await within(dialog).findByRole("form", { name: PERSON_SCOPE });
  return dialog;
}

const form = (dialog: HTMLElement, name: string) => within(dialog).getByRole("form", { name });
const field = (scope: HTMLElement, label: string) => within(scope).getByLabelText(label);

async function retype(input: HTMLElement, text: string) {
  await userEvent.clear(input);
  if (text) await userEvent.type(input, text);
}

test("a Limits button on each row opens a dialog like Remove's, which asks the server for the limits and use", async () => {
  let answer: (report: unknown) => void = () => {};
  const fake = limitsApi({
    "GET /api/people/2/limits": () => new Promise((resolve) => (answer = resolve)),
  });
  renderApp("/admin/people");
  const row = await screen.findByRole("row", { name: /bob@example.com/ });
  const button = within(row).getByRole("button", { name: m.usersLimits });
  expect(button).not.toHaveClass("secondary-danger");
  expect(button).not.toHaveClass("primary");
  expect(fake.made("GET", "/api/people/2/limits")).toEqual([]);
  await userEvent.click(button);
  const dialog = screen.getByRole("dialog", { name: m.limitsTitle(BOB) });
  expect(within(dialog).getByRole("heading", { name: m.limitsTitle(BOB) })).toBeInTheDocument();
  expect(within(dialog).getByText(m.loading)).toBeInTheDocument();
  expect(within(dialog).queryAllByRole("form")).toEqual([]);
  await act(async () => answer(REPORT_OF_BOB));
  await within(dialog).findByRole("form", { name: PERSON_SCOPE });
  expect(fake.made("GET", "/api/people/2/limits")).toHaveLength(1);
  expect(within(dialog).queryByText(m.loading)).not.toBeInTheDocument();
  expect(
    within(dialog)
      .getAllByRole("heading", { level: 3 })
      .map((h) => h.textContent),
  ).toEqual([PERSON_SCOPE, ALPHA_SCOPE, BETA_SCOPE]);
  expect(within(dialog).getAllByRole("form")).toHaveLength(3); // each named by its heading
});

test("the Limits dialog is a large one, each scope a part of it divided by a stroke, each hint one short line", async () => {
  limitsApi();
  renderApp("/admin/people");
  const dialog = await openLimits();
  expect(dialog).toHaveClass("dialog", "large");
  for (const scope of within(dialog).getAllByRole("form")) expect(scope).toHaveClass("limits");
  const hints = Object.values(m.limitsHints);
  expect(hints).toHaveLength(3);
  for (const hint of hints) expect(hint.length, hint).toBeLessThanOrEqual(44);
});

test("the dialog leads with the fields, one hint under each, and keeps the long explanation folded", async () => {
  limitsApi();
  renderApp("/admin/people");
  const dialog = await openLimits();
  const person = form(dialog, PERSON_SCOPE);
  for (const [name, label] of [
    ["memory", m.usersMemory],
    ["cpu", m.usersCpu],
    ["pids", m.usersProcesses],
  ] as const) {
    expect(field(person, label)).toHaveAccessibleDescription(m.limitsHints[name]);
  }
  const how = within(dialog).getByText(m.limitsHowTitle).closest("details")!;
  expect(how).not.toHaveAttribute("open");
  // the fields come before the explanation, and nothing of it is above them
  expect(person.compareDocumentPosition(how) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  const paragraphs = [...dialog.querySelectorAll(":scope > p")].map((p) => p.textContent);
  expect(paragraphs).not.toContain(m.limitsMemoryHard);
  await userEvent.click(within(dialog).getByText(m.limitsHowTitle));
  expect(how).toHaveAttribute("open");
  for (const text of [
    m.limitsHeldToPerson,
    m.limitsMemoryHard,
    m.limitsBelowUse,
    m.limitsNotLimited,
  ]) {
    expect(within(how).getByText(text)).toBeInTheDocument();
  }
});

test("each scope's fields hold its stored limits, empty where there is none, beside what it uses", async () => {
  limitsApi();
  renderApp("/admin/people");
  const dialog = await openLimits();
  const person = form(dialog, PERSON_SCOPE);
  expect(field(person, m.usersMemory)).toHaveValue("4G");
  expect(field(person, m.usersCpu)).toHaveValue("");
  expect(field(person, m.usersProcesses)).toHaveValue("500");
  expect(person).toHaveTextContent(m.limitsUsage("1500M", 40));
  const alpha = form(dialog, ALPHA_SCOPE);
  expect(field(alpha, m.usersMemory)).toHaveValue("2G");
  expect(field(alpha, m.usersProcesses)).toHaveValue("300");
  expect(alpha).toHaveTextContent(m.limitsUsage("1G", 12));
  const beta = form(dialog, BETA_SCOPE);
  expect(field(beta, m.usersMemory)).toHaveValue("");
  expect([...person.querySelectorAll("input")].map((i) => [i.name, i.type])).toEqual([
    ["memory", "text"],
    ["cpu", "text"],
    ["pids", "text"],
  ]);
});

test("a workspace whose use could not be read says so plainly", async () => {
  limitsApi();
  renderApp("/admin/people");
  const dialog = await openLimits();
  const beta = form(dialog, BETA_SCOPE);
  expect(beta).toHaveTextContent(m.limitsUsageUnknown);
  expect(form(dialog, ALPHA_SCOPE)).not.toHaveTextContent(m.limitsUsageUnknown);
});

test("saving a person sends exactly what was typed, null for a field left empty, and only for that person", async () => {
  const fake = limitsApi({
    "PUT /api/people/2/limits": (c) => ({ limits: c.body }),
  });
  renderApp("/admin/people");
  const dialog = await openLimits();
  const person = form(dialog, PERSON_SCOPE);
  await retype(field(person, m.usersMemory), "");
  await retype(field(person, m.usersCpu), "1.50");
  await userEvent.click(
    within(person).getByRole("button", { name: m.limitsSaveFor(PERSON_SCOPE) }),
  );
  await waitFor(() => expect(fake.made("PUT", "/api/people/2/limits")).toHaveLength(1));
  expect(fake.made("PUT", "/api/people/2/limits")[0].body).toEqual({
    memory: null,
    cpu: "1.50",
    pids: "500",
  });
  expect(fake.calls.filter((c) => c.method === "PUT")).toHaveLength(1);
});

test("saving a workspace sends its three fields to its own route, and no other scope's", async () => {
  const fake = limitsApi({
    "PUT /api/people/2/workspaces/7/limits": (c) => ({ limits: c.body }),
  });
  renderApp("/admin/people");
  const dialog = await openLimits();
  const alpha = form(dialog, ALPHA_SCOPE);
  await retype(field(alpha, m.usersMemory), "512M");
  await retype(field(alpha, m.usersProcesses), "");
  await userEvent.click(within(alpha).getByRole("button", { name: m.limitsSaveFor(ALPHA_SCOPE) }));
  await waitFor(() =>
    expect(fake.made("PUT", "/api/people/2/workspaces/7/limits")).toHaveLength(1),
  );
  expect(fake.made("PUT", "/api/people/2/workspaces/7/limits")[0].body).toEqual({
    memory: "512M",
    cpu: null,
    pids: null,
  });
  expect(fake.calls.filter((c) => c.method === "PUT")).toHaveLength(1);
});

test("a workspace that is not ready can be given limits", async () => {
  const fake = limitsApi({
    "PUT /api/people/2/workspaces/8/limits": (c) => ({ limits: c.body }),
  });
  renderApp("/admin/people");
  const dialog = await openLimits();
  const beta = form(dialog, BETA_SCOPE);
  await retype(field(beta, m.usersCpu), "2");
  await userEvent.click(within(beta).getByRole("button", { name: m.limitsSaveFor(BETA_SCOPE) }));
  await waitFor(() =>
    expect(fake.made("PUT", "/api/people/2/workspaces/8/limits")[0].body).toEqual({
      memory: null,
      cpu: "2",
      pids: null,
    }),
  );
});

test("while a scope saves it is busy and says so, then it says it is saved, shows what the server stored, and the table follows", async () => {
  let stored = NONE as Held;
  let finish: (reply: unknown) => void = () => {};
  const fake = limitsApi({
    "GET /api/people": () => ({
      people: [PEOPLE.people[0], { ...PEOPLE.people[1], limits: stored }],
    }),
    "PUT /api/people/2/limits": () => {
      stored = { memory: "1G", cpu: null, pids: null };
      return new Promise((resolve) => {
        finish = resolve;
      });
    },
  });
  renderApp("/admin/people");
  const dialog = await openLimits();
  const person = form(dialog, PERSON_SCOPE);
  const save = within(person).getByRole("button", { name: m.limitsSaveFor(PERSON_SCOPE) });
  await retype(field(person, m.usersMemory), "1g");
  await userEvent.click(save);
  expect(save).toBeDisabled();
  expect(person).toHaveAttribute("aria-busy", "true");
  expect(within(person).getByRole("status")).toHaveTextContent(m.limitsSaving);
  expect(fake.made("PUT", "/api/people/2/limits")).toHaveLength(1);
  await act(async () => finish({ limits: { memory: "1G", cpu: null, pids: null } }));
  await waitFor(() => expect(save).toBeEnabled());
  expect(person).not.toHaveAttribute("aria-busy", "true");
  expect(within(person).getByRole("status")).toHaveTextContent(m.limitsSaved(PERSON_SCOPE));
  expect(field(person, m.usersMemory)).toHaveValue("1G"); // as the server keeps it
  expect(field(person, m.usersProcesses)).toHaveValue(""); // the stored set, not what was there
  const row = await screen.findByRole("row", { name: /bob@example.com/ });
  await waitFor(() => expect(within(row).getByRole("cell", { name: "1G" })).toBeInTheDocument());
  expect(screen.getByRole("dialog", { name: m.limitsTitle(BOB) })).toBeInTheDocument(); // open still
});

test("after a save the use is read again, and what was typed in other fields stays", async () => {
  let asked = 0;
  limitsApi({
    "GET /api/people/2/limits": () => {
      asked += 1;
      return asked === 1
        ? REPORT_OF_BOB
        : { ...REPORT_OF_BOB, person: { ...REPORT_OF_BOB.person, usage: used("2G", 55) } };
    },
    "PUT /api/people/2/limits": (c) => ({ limits: c.body }),
  });
  renderApp("/admin/people");
  const dialog = await openLimits();
  const alpha = form(dialog, ALPHA_SCOPE);
  await retype(field(alpha, m.usersMemory), "1500M"); // typed, not saved
  const person = form(dialog, PERSON_SCOPE);
  expect(person).toHaveTextContent(m.limitsUsage("1500M", 40));
  await userEvent.click(
    within(person).getByRole("button", { name: m.limitsSaveFor(PERSON_SCOPE) }),
  );
  await waitFor(() => expect(person).toHaveTextContent(m.limitsUsage("2G", 55)));
  expect(field(alpha, m.usersMemory)).toHaveValue("1500M");
  expect(within(person).getByRole("status")).toHaveTextContent(m.limitsSaved(PERSON_SCOPE));
});

test("after a refusal the use is read again, and what was typed is kept", async () => {
  let asked = 0;
  limitsApi({
    "GET /api/people/2/limits": () => {
      asked += 1;
      return asked === 1
        ? REPORT_OF_BOB
        : { ...REPORT_OF_BOB, person: { ...REPORT_OF_BOB.person, usage: used("3G", 70) } };
    },
    "PUT /api/people/2/limits": problem(409, "limit_below_use", "planted below use", {}),
  });
  renderApp("/admin/people");
  const dialog = await openLimits();
  const person = form(dialog, PERSON_SCOPE);
  await retype(field(person, m.usersCpu), "0.5");
  await userEvent.click(
    within(person).getByRole("button", { name: m.limitsSaveFor(PERSON_SCOPE) }),
  );
  await waitFor(() => expect(person).toHaveTextContent(m.limitsUsage("3G", 70)));
  expect(await within(person).findByRole("alert")).toHaveTextContent("planted below use");
  expect(field(person, m.usersCpu)).toHaveValue("0.5");
});

test("while a scope saves, Close and Escape wait, and Close says why", async () => {
  let finish: (reply: unknown) => void = () => {};
  limitsApi({
    "PUT /api/people/2/limits": () => new Promise((resolve) => (finish = resolve)),
  });
  renderApp("/admin/people");
  const dialog = await openLimits();
  const person = form(dialog, PERSON_SCOPE);
  const close = within(dialog).getByRole("button", { name: m.close });
  expect(close).toBeEnabled();
  expect(close).not.toHaveAccessibleDescription(m.limitsCloseWaits);
  await userEvent.click(
    within(person).getByRole("button", { name: m.limitsSaveFor(PERSON_SCOPE) }),
  );
  expect(close).toBeDisabled();
  expect(close).toHaveAccessibleDescription(m.limitsCloseWaits);
  await userEvent.keyboard("{Escape}");
  expect(screen.getByRole("dialog", { name: m.limitsTitle(BOB) })).toBeInTheDocument();
  await act(async () => finish({ limits: { memory: "4G", cpu: null, pids: "500" } }));
  await waitFor(() => expect(close).toBeEnabled());
  expect(close).not.toHaveAccessibleDescription(m.limitsCloseWaits);
  await userEvent.click(close);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("a use the server cannot tell, in part or whole, is said to be unknown", async () => {
  limitsApi({
    "GET /api/people/2/limits": {
      person: {
        limits: NONE,
        usage: { memory: null, memory_bytes: null, tasks: 4 },
      },
      workspaces: [{ ...WORKSPACES[0], usage: { memory: "1G", memory_bytes: 1, tasks: null } }],
    },
  });
  renderApp("/admin/people");
  const dialog = await openLimits();
  expect(form(dialog, PERSON_SCOPE)).toHaveTextContent(m.limitsUsageUnknown);
  expect(form(dialog, ALPHA_SCOPE)).toHaveTextContent(m.limitsUsageUnknown);
});

test("a refused field is shown beside its own scope's fields by its label, and in no other scope", async () => {
  limitsApi({
    "PUT /api/people/2/workspaces/7/limits": problem(
      422,
      "validation_failed",
      "Some of what was sent is not valid.",
      {
        errors: [
          { field: "memory", message: "planted reason for memory" },
          { field: "cpu", message: "planted reason for cpu" },
          { field: "pids", message: "planted reason for pids" },
        ],
      },
    ),
  });
  renderApp("/admin/people");
  const dialog = await openLimits();
  const alpha = form(dialog, ALPHA_SCOPE);
  await userEvent.click(within(alpha).getByRole("button", { name: m.limitsSaveFor(ALPHA_SCOPE) }));
  await within(alpha).findByRole("alert");
  expectRefused(field(alpha, m.usersMemory), "planted reason for memory");
  expectRefused(field(alpha, m.usersCpu), "planted reason for cpu");
  expectRefused(field(alpha, m.usersProcesses), "planted reason for pids");
  expect(field(alpha, m.usersMemory)).toHaveFocus();
  // the hint stays with the field, after the reason
  expect(field(alpha, m.usersMemory)).toHaveAccessibleDescription(
    expect.stringContaining(m.limitsHints.memory),
  );
  expect(within(form(dialog, PERSON_SCOPE)).queryByRole("alert")).not.toBeInTheDocument();
  expect(within(alpha).getByRole("button", { name: m.limitsSaveFor(ALPHA_SCOPE) })).toBeEnabled();
  expect(within(alpha).getByRole("status")).not.toHaveTextContent(m.limitsSaved(ALPHA_SCOPE));
});

test("a refusal below the use is shown under the field it names, with the title that names the use, and what was typed is kept", async () => {
  const title = "The memory limit is below the 3G in use.";
  limitsApi({
    "PUT /api/people/2/limits": problem(409, "limit_below_use", title, {
      limit: "memory",
      used: "3G",
    }),
  });
  renderApp("/admin/people");
  const dialog = await openLimits();
  const person = form(dialog, PERSON_SCOPE);
  await retype(field(person, m.usersMemory), "256M");
  await userEvent.click(
    within(person).getByRole("button", { name: m.limitsSaveFor(PERSON_SCOPE) }),
  );
  await within(person).findByRole("alert");
  expectRefused(field(person, m.usersMemory), title);
  expect(field(person, m.usersProcesses)).not.toHaveAttribute("aria-invalid");
  expect(field(person, m.usersMemory)).toHaveValue("256M");
});

test("limits that could not be applied are shown, and a later save clears the refusal", async () => {
  let answer: unknown = problem(502, "limits_not_applied", "rcman could not apply the limits.");
  limitsApi({ "PUT /api/people/2/limits": () => answer });
  renderApp("/admin/people");
  const dialog = await openLimits();
  const person = form(dialog, PERSON_SCOPE);
  const save = within(person).getByRole("button", { name: m.limitsSaveFor(PERSON_SCOPE) });
  await userEvent.click(save);
  expect(await within(person).findByRole("alert")).toHaveTextContent(
    "rcman could not apply the limits.",
  );
  answer = { limits: { memory: "4G", cpu: null, pids: "500" } };
  await userEvent.click(save);
  await waitFor(() => expect(within(person).queryByRole("alert")).not.toBeInTheDocument());
  expect(within(person).getByRole("status")).toHaveTextContent(m.limitsSaved(PERSON_SCOPE));
});

test("a refusal in one scope is replaced by the next scope's save", async () => {
  limitsApi({
    "PUT /api/people/2/limits": problem(409, "limit_below_use", "planted below use", {}),
    "PUT /api/people/2/workspaces/7/limits": (c) => ({ limits: c.body }),
  });
  renderApp("/admin/people");
  const dialog = await openLimits();
  await userEvent.click(
    within(form(dialog, PERSON_SCOPE)).getByRole("button", { name: m.limitsSaveFor(PERSON_SCOPE) }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("planted below use");
  await userEvent.click(
    within(form(dialog, ALPHA_SCOPE)).getByRole("button", { name: m.limitsSaveFor(ALPHA_SCOPE) }),
  );
  await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
});

test("when the limits cannot be had the refusal is shown, with no forms, and the dialog can be closed", async () => {
  limitsApi({
    "GET /api/people/2/limits": problem(
      503,
      "runtime_unavailable",
      "Containers are not available.",
    ),
  });
  renderApp("/admin/people");
  const row = await screen.findByRole("row", { name: /bob@example.com/ });
  await userEvent.click(within(row).getByRole("button", { name: m.usersLimits }));
  const dialog = screen.getByRole("dialog", { name: m.limitsTitle(BOB) });
  expect(await within(dialog).findByRole("alert")).toHaveTextContent(
    "Containers are not available.",
  );
  expect(within(dialog).queryAllByRole("form")).toEqual([]);
  expect(within(dialog).queryByText(m.loading)).not.toBeInTheDocument();
  await userEvent.click(within(dialog).getByRole("button", { name: m.close }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

test("closing the dialog puts focus back on the row's Limits button, by Close and by Escape", async () => {
  limitsApi();
  renderApp("/admin/people");
  const dialog = await openLimits();
  expect(dialog).toHaveFocus(); // focus moved into the dialog as it opened
  await userEvent.click(within(dialog).getByRole("button", { name: m.close }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  const row = screen.getByRole("row", { name: /bob@example.com/ });
  const button = within(row).getByRole("button", { name: m.usersLimits });
  expect(button).toHaveFocus();

  await userEvent.click(button);
  await within(screen.getByRole("dialog")).findByRole("form", { name: PERSON_SCOPE });
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(button).toHaveFocus();
});

test("Limits and Remove each close the other's dialog, and Remove still removes", async () => {
  let people = [...PEOPLE.people];
  const fake = limitsApi({
    "GET /api/people": () => ({ people }),
    "DELETE /api/people/2": () => {
      people = people.filter((p) => p.id !== 2);
      return { deleted: BOB };
    },
  });
  renderApp("/admin/people");
  const row = await screen.findByRole("row", { name: /bob@example.com/ });
  await userEvent.click(within(row).getByRole("button", { name: m.usersRemove }));
  await userEvent.click(within(row).getByRole("button", { name: m.usersLimits }));
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  expect(screen.getByRole("dialog", { name: m.limitsTitle(BOB) })).toBeInTheDocument();
  await userEvent.click(within(row).getByRole("button", { name: m.usersRemove }));
  const remove = screen.getByRole("dialog", { name: m.usersRemoveTitle(BOB) });
  expect(screen.getAllByRole("dialog")).toHaveLength(1);
  await userEvent.click(within(remove).getByRole("button", { name: m.usersRemoveConfirm }));
  await waitFor(() => expect(fake.made("DELETE", "/api/people/2")).toHaveLength(1));
  expect(await screen.findByText(m.usersRemoved(BOB))).toBeInTheDocument();
});

test("a person who is no longer there is refused when the limits are asked for", async () => {
  limitsApi({
    "GET /api/people/2/limits": problem(404, "person_not_found", "There is no such person."),
  });
  renderApp("/admin/people");
  const row = await screen.findByRole("row", { name: /bob@example.com/ });
  await userEvent.click(within(row).getByRole("button", { name: m.usersLimits }));
  expect(await screen.findByRole("alert")).toHaveTextContent("There is no such person.");
});

// --- the Claude Code versions page -----------------------------------------------

type Installed = { version: string; approved: boolean; previous: boolean; in_use: number };
type Drift = { workspace: number; name: string; email: string; version: string; approved: string };
type Report = {
  approved: string | null;
  previous: string | null;
  tested: string;
  installed: Installed[];
  drift: Drift[];
  upgrading: boolean;
  rcman_upgrading?: boolean;
};

const installed = (version: string, extra: Partial<Installed> = {}): Installed => ({
  version,
  approved: false,
  previous: false,
  in_use: 0,
  ...extra,
});

// 2.2.0 is approved and runs on one server, 2.1.0 is the previous one and runs on two, and
// the tested version is 2.1.0; one server runs the previous version.
const REPORT: Report = {
  approved: "2.2.0",
  previous: "2.1.0",
  tested: "2.1.0",
  installed: [
    installed("2.2.0", { approved: true, in_use: 1 }),
    installed("2.1.0", { previous: true, in_use: 2 }),
    installed("2.0.0"),
  ],
  drift: [
    { workspace: 5, name: "beta", email: "bob@example.com", version: "2.1.0", approved: "2.2.0" },
  ],
  upgrading: false,
};

// What a rollout of a version changes in a report: it becomes the approved one, the approved one
// the previous one.
function approving(report: Report, version: string): Report {
  return {
    ...report,
    approved: version,
    previous: report.approved,
    installed: report.installed.map((i) => ({
      ...i,
      approved: i.version === version,
      previous: i.version === report.approved,
    })),
    drift: [],
  };
}

function versionsApi(extra: Routes = {}, report: Report | (() => Report) = REPORT) {
  return api({
    "GET /api/claude/versions": () => (typeof report === "function" ? report() : report),
    "GET /api/claude/latest": { latest: null },
    ...extra,
  });
}

// A reply that is given when the test says so, to see the page while a request is out.
function later<T>() {
  let give: (reply: T) => void = () => {};
  const reply = new Promise<T>((resolve) => {
    give = resolve;
  });
  return { reply, give };
}

const versionsPage = async () => {
  await screen.findByRole("heading", { name: m.versionsTitle });
  return screen.findByRole("region", { name: m.versionsInstalledTitle });
};
const definition = (term: string) => screen.getByText(term, { selector: "dt" }).nextElementSibling;

test("the page shows the approved, previous and tested versions", async () => {
  versionsApi();
  renderApp("/admin/claude");
  await versionsPage();
  expect(definition(m.versionsApproved)).toHaveTextContent("2.2.0");
  expect(definition(m.versionsPrevious)).toHaveTextContent("2.1.0");
  expect(definition(m.versionsTested)).toHaveTextContent("2.1.0");
});

test("the versions are facts in a card, under the page's heading row", async () => {
  versionsApi();
  renderApp("/admin/claude");
  await versionsPage();
  const title = screen.getByRole("heading", { level: 1, name: m.versionsTitle });
  expect(title.parentElement).toHaveClass("page-head");
  const facts = screen.getByText(m.versionsApproved, { selector: "dt" }).closest(".card")!;
  expect(facts).not.toBeNull();
  expect(
    within(facts as HTMLElement).getByRole("heading", { level: 2, name: m.versionsFactsTitle }),
  ).toBeInTheDocument();
});

test("each table is in a card of its own, named by its heading", async () => {
  versionsApi();
  renderApp("/admin/claude");
  for (const name of [m.versionsInstalledTitle, m.versionsDriftTitle]) {
    const region = await screen.findByRole("region", { name });
    expect(region).toHaveClass("card", "flush");
    expect(within(region).getByRole("heading", { level: 2, name })).toBeInTheDocument();
    expect(within(region).getByRole("table").closest(".card")).toBe(region);
  }
});

test.each([
  ["the approved version is not the tested one", REPORT, "2.2.0", "2.1.0"],
  ["the approved version is the tested one", { ...REPORT, tested: "2.2.0" }, "2.2.0", "2.2.0"],
  ["nothing is approved", { ...REPORT, approved: null }, m.versionsNone, "2.1.0"],
])("no message is shown when %s", async (_state, report, approved, tested) => {
  versionsApi({}, report);
  renderApp("/admin/claude");
  await versionsPage();
  // positive control: this case's report is the one shown
  expect(definition(m.versionsApproved)).toHaveTextContent(approved);
  expect(definition(m.versionsTested)).toHaveTextContent(tested);
  expect(document.querySelector(".message")).toBeNull(); // no message bar of any kind
});

test("a version nothing is approved or recorded for is shown as none", async () => {
  versionsApi({}, { ...REPORT, approved: null, previous: null, installed: [], drift: [] });
  renderApp("/admin/claude");
  await versionsPage();
  expect(definition(m.versionsApproved)).toHaveTextContent(m.versionsNone);
  expect(definition(m.versionsPrevious)).toHaveTextContent(m.versionsNone);
});

test("the installed versions are listed with their standing and how many running servers use each", async () => {
  versionsApi();
  renderApp("/admin/claude");
  const region = await versionsPage();
  const headers = within(region)
    .getAllByRole("columnheader")
    .map((h) => h.textContent);
  expect(headers).toEqual([
    m.versionsVersion,
    m.versionsStanding,
    m.versionsServersUsing,
    m.versionsActions,
  ]);
  const approved = within(region).getByRole("row", { name: /2\.2\.0/ });
  expect(within(approved).getByText(m.versionsStandingApproved)).toBeInTheDocument();
  expect(within(approved).getByText(m.versionsServers(1))).toBeInTheDocument();
  expect(within(approved).queryByRole("button")).not.toBeInTheDocument(); // already in use
  const previous = within(region).getByRole("row", { name: /2\.1\.0/ });
  expect(within(previous).getByText(m.versionsStandingPrevious)).toBeInTheDocument();
  expect(within(previous).getByText(m.versionsServers(2))).toBeInTheDocument();
  const unused = within(region).getByRole("row", { name: /2\.0\.0/ });
  expect(within(unused).getByText(m.versionsServers(0))).toBeInTheDocument();
  expect(within(unused).queryByText(m.versionsStandingApproved)).not.toBeInTheDocument();
  expect(within(unused).queryByText(m.versionsStandingPrevious)).not.toBeInTheDocument();
  expect(m.versionsServers(1)).not.toBe(m.versionsServers(2)); // one server, two servers
});

test("a server on another version than the approved one is listed with its owner", async () => {
  versionsApi();
  renderApp("/admin/claude");
  const region = await screen.findByRole("region", { name: m.versionsDriftTitle });
  const headers = within(region)
    .getAllByRole("columnheader")
    .map((h) => h.textContent);
  expect(headers).toEqual([
    m.everyoneWorkspace,
    m.versionsOwner,
    m.versionsRunning,
    m.versionsApproved,
  ]);
  const row = within(region).getByRole("row", { name: /beta/ });
  expect(
    within(row)
      .getAllByRole("cell")
      .map((c) => c.textContent),
  ).toEqual(["beta", "bob@example.com", "2.1.0", "2.2.0"]);
  expect(within(region).queryByText(m.versionsDriftNone)).not.toBeInTheDocument();
});

test("with no server on another version, the page says so", async () => {
  versionsApi({}, { ...REPORT, drift: [] });
  renderApp("/admin/claude");
  const region = await screen.findByRole("region", { name: m.versionsDriftTitle });
  expect(within(region).getByText(m.versionsDriftNone)).toBeInTheDocument();
  expect(within(region).queryByRole("table")).not.toBeInTheDocument();
});

test("Use rolls an installed version out again, and shows the report as it is then", async () => {
  let report = REPORT;
  const fake = versionsApi(
    {
      "POST /api/claude/upgrade": (c) =>
        (report = approving(report, (c.body as { version: string }).version)),
    },
    () => report,
  );
  renderApp("/admin/claude");
  const region = await versionsPage();
  const row = within(region).getByRole("row", { name: /2\.1\.0/ });
  await userEvent.click(within(row).getByRole("button", { name: m.versionsUseVersion("2.1.0") }));
  await waitFor(() => expect(fake.made("POST", "/api/claude/upgrade")).toHaveLength(1));
  expect(fake.made("POST", "/api/claude/upgrade")[0].body).toEqual({ version: "2.1.0" });
  expect(await screen.findByText(m.versionsUpgraded("2.1.0"))).toHaveAttribute("role", "status");
  await waitFor(() => expect(definition(m.versionsApproved)).toHaveTextContent("2.1.0"));
  expect(definition(m.versionsPrevious)).toHaveTextContent("2.2.0");
  expect(
    within(within(region).getByRole("row", { name: /2\.1\.0/ })).queryByRole("button"),
  ).not.toBeInTheDocument();
});

test("rolling out a version sends it, says it is working while the request lasts, and refuses a second press", async () => {
  const slow = later<Report>();
  let report = REPORT;
  const fake = versionsApi(
    { "POST /api/claude/upgrade": () => slow.reply.then((r) => (report = r)) },
    () => report,
  );
  renderApp("/admin/claude");
  const input = await screen.findByLabelText(m.versionsUpgrade);
  await userEvent.type(input, "2.3.0");
  const submit = screen.getByRole("button", { name: m.versionsUpgradeGo });
  await userEvent.click(submit);
  await waitFor(() => expect(fake.made("POST", "/api/claude/upgrade")).toHaveLength(1));
  expect(fake.made("POST", "/api/claude/upgrade")[0].body).toEqual({ version: "2.3.0" });

  // while it runs: said in a status, the form and every Use shut
  const status = await screen.findByText(m.versionsRolling("2.3.0"));
  expect(status).toHaveAttribute("role", "status");
  const busy = screen.getByRole("button", { name: m.versionsUpgradeBusy });
  expect(busy).toBeDisabled();
  expect(screen.getByRole("button", { name: m.versionsUseVersion("2.0.0") })).toBeDisabled();
  await userEvent.type(input, "{Enter}"); // Enter in the field submits the form too
  expect(fake.made("POST", "/api/claude/upgrade")).toHaveLength(1);

  slow.give({
    ...approving(REPORT, "2.3.0"),
    installed: [installed("2.3.0", { approved: true }), ...REPORT.installed],
  });
  expect(await screen.findByText(m.versionsUpgraded("2.3.0"))).toHaveAttribute("role", "status");
  expect(screen.queryByText(m.versionsRolling("2.3.0"))).not.toBeInTheDocument();
  await waitFor(() => expect(definition(m.versionsApproved)).toHaveTextContent("2.3.0"));
  expect(screen.getByRole("button", { name: m.versionsUpgradeGo })).toBeEnabled();
  expect(input).toHaveValue("");
});

test("a refused version names the field by its label", async () => {
  const errors = [{ field: "version", message: "planted reason for the version" }];
  versionsApi({
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
  await screen.findByRole("alert");
  expectRefused(screen.getByLabelText(m.versionsUpgrade), "planted reason for the version");
  expect(screen.getByLabelText(m.versionsUpgrade)).toHaveFocus();
});

test("failed checks are shown as the server named them, with what each ran and got back, and the form is free again", async () => {
  const fake = versionsApi({
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
  expect(alert).toHaveTextContent(`${m.versionsFailedChecks} sign in screen, logout`);
  const signIn = within(alert).getByRole("figure", { name: "sign in screen" });
  expect(signIn).toHaveTextContent("planted sign-in screen");
  expect(signIn.nextElementSibling).toHaveTextContent(m.versionsReportMore(2));
  const logout = within(alert).getByRole("figure", { name: "logout" });
  expect(logout).toHaveTextContent("planted logout");
  expect(screen.queryByText(m.versionsRolling("2.3.0"))).not.toBeInTheDocument();
  expect(screen.queryByText(m.versionsUpgraded("2.3.0"))).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: m.versionsUpgradeGo })).toBeEnabled();
  expect(definition(m.versionsApproved)).toHaveTextContent("2.2.0"); // nothing changed
});

test.each([
  [
    "a version that could not be had",
    409,
    "claude_install_failed",
    "Claude Code has no such version.",
  ],
  ["another rollout under way", 409, "claude_upgrade_busy", "Another version is being rolled out."],
  ["checks that could not be run", 502, "claude_canary_failed", "The checks could not be run."],
  [
    "a download that failed on the network",
    502,
    "claude_install_failed",
    "rcman could not download it.",
  ],
])("%s is shown with the server's words", async (_, status, code, title) => {
  versionsApi({
    "POST /api/claude/upgrade": problem(
      status,
      code,
      title,
      code === "claude_install_failed"
        ? { kind: status === 502 ? "network" : "no_such_version" }
        : {},
    ),
  });
  renderApp("/admin/claude");
  await userEvent.type(await screen.findByLabelText(m.versionsUpgrade), "9.9.9");
  await userEvent.click(screen.getByRole("button", { name: m.versionsUpgradeGo }));
  expect(await screen.findByRole("alert")).toHaveTextContent(title);
  expect(screen.getByRole("button", { name: m.versionsUpgradeGo })).toBeEnabled();
  expect(definition(m.versionsApproved)).toHaveTextContent("2.2.0");
});

test("a failed Use is shown the same way", async () => {
  versionsApi({
    "POST /api/claude/upgrade": problem(
      502,
      "claude_canary_failed",
      "The checks could not be run.",
    ),
  });
  renderApp("/admin/claude");
  const region = await versionsPage();
  await userEvent.click(
    within(region).getByRole("button", { name: m.versionsUseVersion("2.0.0") }),
  );
  expect(await screen.findByRole("alert")).toHaveTextContent("The checks could not be run.");
  expect(within(region).getByRole("button", { name: m.versionsUseVersion("2.0.0") })).toBeEnabled();
});

test("the newest release is suggested, and rolled out only when asked", async () => {
  let report = REPORT;
  const fake = versionsApi(
    {
      "GET /api/claude/latest": { latest: "2.5.0" },
      "POST /api/claude/upgrade": (c) =>
        (report = approving(report, (c.body as { version: string }).version)),
    },
    () => report,
  );
  renderApp("/admin/claude");
  const region = await screen.findByRole("region", { name: m.versionsLatestTitle });
  expect(await within(region).findByText(m.versionsLatest("2.5.0"))).toBeInTheDocument();
  expect(fake.made("POST", "/api/claude/upgrade")).toHaveLength(0); // a suggestion only
  await userEvent.click(
    within(region).getByRole("button", { name: m.versionsRolloutVersion("2.5.0") }),
  );
  await waitFor(() => expect(fake.made("POST", "/api/claude/upgrade")).toHaveLength(1));
  expect(fake.made("POST", "/api/claude/upgrade")[0].body).toEqual({ version: "2.5.0" });
  expect(await screen.findByText(m.versionsUpgraded("2.5.0"))).toBeInTheDocument();
  await waitFor(() => expect(definition(m.versionsApproved)).toHaveTextContent("2.5.0"));
  expect(within(region).getByText(m.versionsLatestApproved)).toBeInTheDocument(); // nothing newer
  expect(within(region).queryByRole("button")).not.toBeInTheDocument();
});

test("when the approved version is the newest release, there is nothing to suggest", async () => {
  versionsApi({ "GET /api/claude/latest": { latest: "2.2.0" } });
  renderApp("/admin/claude");
  const region = await screen.findByRole("region", { name: m.versionsLatestTitle });
  expect(await within(region).findByText(m.versionsLatestApproved)).toBeInTheDocument();
  expect(within(region).queryByRole("button")).not.toBeInTheDocument();
});

test("when the newest release is not known, the page says so and suggests nothing", async () => {
  const fake = versionsApi({ "GET /api/claude/latest": { latest: null } });
  renderApp("/admin/claude");
  const region = await screen.findByRole("region", { name: m.versionsLatestTitle });
  expect(await within(region).findByText(m.versionsLatestUnknown)).toBeInTheDocument();
  expect(within(region).queryByRole("button")).not.toBeInTheDocument();
  expect(fake.made("GET", "/api/claude/latest").length).toBeGreaterThan(0);
});

test("while another rollout runs, nothing is offered to roll out", async () => {
  versionsApi({ "GET /api/claude/latest": { latest: "2.5.0" } }, { ...REPORT, upgrading: true });
  renderApp("/admin/claude");
  const region = await versionsPage();
  expect(await screen.findByText(m.versionsBusyElsewhere)).toHaveAttribute("role", "status");
  expect(screen.getByRole("button", { name: m.versionsUpgradeGo })).toBeDisabled();
  expect(
    within(region).getByRole("button", { name: m.versionsUseVersion("2.0.0") }),
  ).toBeDisabled();
  expect(
    await screen.findByRole("button", { name: m.versionsRolloutVersion("2.5.0") }),
  ).toBeDisabled();
});

test("while rcman is being upgraded, nothing is offered to roll out, and the page says why", async () => {
  versionsApi(
    { "GET /api/claude/latest": { latest: "2.5.0" } },
    { ...REPORT, rcman_upgrading: true },
  );
  renderApp("/admin/claude");
  const region = await versionsPage();
  expect(await screen.findByText(m.versionsRcmanUpgrading)).toHaveAttribute("role", "status");
  expect(screen.getByRole("button", { name: m.versionsUpgradeGo })).toBeDisabled();
  expect(
    within(region).getByRole("button", { name: m.versionsUseVersion("2.0.0") }),
  ).toBeDisabled();
  expect(
    await screen.findByRole("button", { name: m.versionsRolloutVersion("2.5.0") }),
  ).toBeDisabled();
});

test("a report that cannot be had is shown with the server's reason", async () => {
  api({
    "GET /api/claude/versions": problem(500, "internal_error", "planted: no report"),
    "GET /api/claude/latest": { latest: null },
  });
  renderApp("/admin/claude");
  expect(await screen.findByRole("alert")).toHaveTextContent("planted: no report");
  expect(screen.queryByRole("region", { name: m.versionsInstalledTitle })).not.toBeInTheDocument();
});

test("the version is sent without the spaces around it", async () => {
  const fake = versionsApi({ "POST /api/claude/upgrade": REPORT });
  renderApp("/admin/claude");
  await userEvent.type(await screen.findByLabelText(m.versionsUpgrade), "  2.3.0 ");
  await userEvent.click(screen.getByRole("button", { name: m.versionsUpgradeGo }));
  await waitFor(() => expect(fake.made("POST", "/api/claude/upgrade")).toHaveLength(1));
  expect(fake.made("POST", "/api/claude/upgrade")[0].body).toEqual({ version: "2.3.0" });
});

test("a refusal because another rollout runs reads the report again, and the page then says so", async () => {
  let upgrading = false;
  const fake = versionsApi(
    {
      "POST /api/claude/upgrade": () => {
        upgrading = true;
        return problem(409, "claude_upgrade_busy", "Another version is being rolled out.");
      },
    },
    () => ({ ...REPORT, upgrading }),
  );
  renderApp("/admin/claude");
  await userEvent.type(await screen.findByLabelText(m.versionsUpgrade), "2.3.0");
  expect(screen.queryByText(m.versionsBusyElsewhere)).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: m.versionsUpgradeGo }));
  expect(await screen.findByText(m.versionsBusyElsewhere)).toBeInTheDocument();
  expect(fake.made("GET", "/api/claude/versions")).toHaveLength(2);
  expect(screen.getByRole("alert")).toHaveTextContent("Another version is being rolled out.");
});

describe("while a rollout of another administrator runs", () => {
  beforeEach(() => void vi.useFakeTimers({ shouldAdvanceTime: true }));
  afterEach(() => void vi.useRealTimers());
  const pass = (ms: number) =>
    act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });

  test("the report is read again at intervals until it says the rollout is over, and then no more", async () => {
    let upgrading = true;
    const fake = versionsApi({}, () => ({ ...REPORT, upgrading }));
    renderApp("/admin/claude");
    expect(await screen.findByText(m.versionsBusyElsewhere)).toBeInTheDocument();
    const asked = () => fake.made("GET", "/api/claude/versions").length;
    expect(asked()).toBe(1);
    await pass(UPGRADE_POLL_MS);
    expect(asked()).toBe(2);
    upgrading = false;
    await pass(UPGRADE_POLL_MS);
    await waitFor(() =>
      expect(screen.queryByText(m.versionsBusyElsewhere)).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: m.versionsUpgradeGo })).toBeEnabled();
    const settled = asked();
    expect(settled).toBe(3);
    await pass(UPGRADE_POLL_MS * 3);
    expect(asked()).toBe(settled);
  });

  test("it stops when the page is left", async () => {
    const fake = versionsApi({}, { ...REPORT, upgrading: true });
    const { unmount } = renderApp("/admin/claude");
    await screen.findByText(m.versionsBusyElsewhere);
    unmount();
    const asked = fake.made("GET", "/api/claude/versions").length;
    await pass(UPGRADE_POLL_MS * 3);
    expect(fake.made("GET", "/api/claude/versions")).toHaveLength(asked);
  });
});

test("the report is not read again at intervals while no rollout runs", async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  try {
    const fake = versionsApi();
    renderApp("/admin/claude");
    await versionsPage();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(UPGRADE_POLL_MS * 3);
    });
    expect(fake.made("GET", "/api/claude/versions")).toHaveLength(1);
  } finally {
    vi.useRealTimers();
  }
});

// --- tables wider than a phone ----------------------------------------------------------

test.each([
  ["the people", "/admin/people", () => api({ "GET /api/people": PEOPLE })],
  ["the installed versions", "/admin/claude", () => versionsApi()],
])(
  "%s are in a box that scrolls sideways, which is named and can take focus",
  async (_, path, serve) => {
    serve();
    renderApp(path);
    const [table] = await screen.findAllByRole("table");
    const box = table.closest(".table-scroll")!;
    expect(box).not.toBeNull();
    expect(box).toHaveAttribute("tabindex", "0");
    expect(within(box as HTMLElement).getByRole("table")).toBe(table);
    expect(box).toHaveAccessibleName(m.tableTitle);
    expect(screen.getAllByRole("region", { name: m.tableTitle })).toContain(box);
  },
);

test("the servers on another version are in a box that scrolls sideways", async () => {
  versionsApi();
  renderApp("/admin/claude");
  const tables = await screen.findAllByRole("table");
  expect(tables.length).toBeGreaterThan(1); // positive control: the installed and the drifting
  for (const table of tables) expect(table.closest(".table-scroll")).not.toBeNull();
});

test("the field for the address of a person to add shows an example", async () => {
  api({ "GET /api/people": PEOPLE });
  renderApp("/admin/people");
  const dialog = await openAdd();
  expect(within(dialog).getByLabelText(m.usersEmail)).toHaveAttribute(
    "placeholder",
    m.examples.email,
  );
});
