import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { m } from "./messages";
import { ADMIN_USER, fakeApi, NOTHING_TO_ATTEND, problem, type Routes } from "./test/fakeApi";
import { renderApp } from "./test/render";

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
  expect(await screen.findByText(m.usersAdded("Carol@example.com"))).toBeInTheDocument();
  expect(await screen.findByRole("row", { name: /Carol@example.com/ })).toBeInTheDocument();
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); // done with, it closes
  // asked again, it starts empty
  const again = await openAdd();
  expect(within(again).getByLabelText(m.usersEmail)).toHaveValue("");
  expect(within(again).getByLabelText(m.usersAddAdmin)).not.toBeChecked();
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

test("More actions holds Limits and Remove, and Remove asks about that person", async () => {
  api({ "GET /api/people": PEOPLE });
  renderApp("/admin/people");
  const { panel } = await openMore(BOB);
  const items = within(panel).getAllByRole("button");
  expect(items.map((b) => b.textContent)).toEqual([m.usersLimits, m.usersRemove]);
  await userEvent.click(items[1]);
  expect(await screen.findByRole("dialog", { name: m.usersRemoveTitle(BOB) })).toBeInTheDocument();
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

test("a Limits button on each row opens a dialog, which asks the server for the limits and use", async () => {
  let answer: (report: unknown) => void = () => {};
  const fake = limitsApi({
    "GET /api/people/2/limits": () => new Promise((resolve) => (answer = resolve)),
  });
  renderApp("/admin/people");
  const row = await screen.findByRole("row", { name: /bob@example.com/ });
  const button = within(row).getByRole("button", { name: m.usersLimits });
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

// --- the Claude Code versions page -----------------------------------------------

type Installed = {
  version: string;
  approved: boolean;
  previous: boolean;
  in_use: number;
  failed: boolean;
};
type Drift = {
  workspace: number;
  name: string;
  email: string;
  version: string;
  approved: string;
  failed: boolean;
};
type Failure = { version: string; at: string; by: string; reason: string | null };
type Report = {
  approved: string | null;
  previous: string | null;
  tested: string;
  installed: Installed[];
  drift: Drift[];
  failed: Failure[];
  upgrading: boolean;
  rcman_upgrading?: boolean;
};

const installed = (version: string, extra: Partial<Installed> = {}): Installed => ({
  version,
  approved: false,
  previous: false,
  in_use: 0,
  failed: false,
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
    {
      workspace: 5,
      name: "beta",
      email: "bob@example.com",
      version: "2.1.0",
      approved: "2.2.0",
      failed: false,
    },
  ],
  failed: [],
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

test("the installed versions are listed with their standing and how many running servers use each", async () => {
  versionsApi();
  renderApp("/admin/claude");
  const region = await versionsPage();
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
});

test("a server on another version than the approved one is listed with its owner", async () => {
  versionsApi();
  renderApp("/admin/claude");
  const region = await screen.findByRole("region", { name: m.versionsDriftTitle });
  const row = within(region).getByRole("row", { name: /beta/ });
  expect(
    within(row)
      .getAllByRole("cell")
      .map((c) => c.textContent),
  ).toEqual(["beta", "bob@example.com", "2.1.0", "2.2.0"]);
  expect(within(region).queryByText(m.versionsDriftNone)).not.toBeInTheDocument();
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
  expect(await screen.findByText(m.versionsUpgraded("2.1.0"))).toBeInTheDocument();
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

  // while it runs: said, the form and every Use shut
  const status = await screen.findByText(m.versionsRolling("2.3.0"));
  expect(status).toBeInTheDocument();
  const busy = screen.getByRole("button", { name: m.versionsUpgradeBusy });
  expect(busy).toBeDisabled();
  expect(screen.getByRole("button", { name: m.versionsUseVersion("2.0.0") })).toBeDisabled();
  await userEvent.type(input, "{Enter}"); // Enter in the field submits the form too
  expect(fake.made("POST", "/api/claude/upgrade")).toHaveLength(1);

  slow.give({
    ...approving(REPORT, "2.3.0"),
    installed: [installed("2.3.0", { approved: true }), ...REPORT.installed],
  });
  expect(await screen.findByText(m.versionsUpgraded("2.3.0"))).toBeInTheDocument();
  expect(screen.queryByText(m.versionsRolling("2.3.0"))).not.toBeInTheDocument();
  await waitFor(() => expect(definition(m.versionsApproved)).toHaveTextContent("2.3.0"));
  expect(screen.getByRole("button", { name: m.versionsUpgradeGo })).toBeEnabled();
  expect(input).toHaveValue("");
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

// --- versions marked as failed ------------------------------------------------------------

// 2.0.0 marked as failed by alice, with a reason; 1.9.0 by bob with none, earlier.
const MARK_200: Failure = {
  version: "2.0.0",
  at: "2026-10-20T09:15:00+00:00",
  by: "alice@example.com",
  reason: "Sessions hang after sign-in",
};
const MARK_190: Failure = {
  version: "1.9.0",
  at: "2026-10-01T08:00:00+00:00",
  by: "bob@example.com",
  reason: null,
};

// What a mark changes in a report: the version flagged wherever it is listed, and its mark first.
function marking(report: Report, mark: Failure): Report {
  return {
    ...report,
    installed: report.installed.map((i) =>
      i.version === mark.version ? { ...i, failed: true } : i,
    ),
    drift: report.drift.map((d) => (d.version === mark.version ? { ...d, failed: true } : d)),
    failed: [mark, ...report.failed.filter((f) => f.version !== mark.version)],
  };
}

// What removing a mark changes in a report.
function clearing(report: Report, version: string): Report {
  return {
    ...report,
    installed: report.installed.map((i) => (i.version === version ? { ...i, failed: false } : i)),
    drift: report.drift.map((d) => (d.version === version ? { ...d, failed: false } : d)),
    failed: report.failed.filter((f) => f.version !== version),
  };
}

const MARKED = marking(marking(REPORT, MARK_190), MARK_200);

const failedCard = () => screen.findByRole("region", { name: m.versionsFailedTitle });

async function openMark(from: HTMLElement) {
  await userEvent.click(from);
  return screen.findByRole("dialog", { name: m.versionsMarkAnyFailed });
}

test("the versions marked as failed are listed with why, by whom and when, the newest first", async () => {
  versionsApi({}, MARKED);
  renderApp("/admin/claude");
  const region = await failedCard();
  const rows = within(region).getAllByRole("row").slice(1);
  expect(rows.map((r) => within(r).getAllByRole("cell")[0].textContent)).toEqual([
    "2.0.0",
    "1.9.0",
  ]);
  const cells = within(rows[0])
    .getAllByRole("cell")
    .map((c) => c.textContent);
  expect(cells.slice(0, 4)).toEqual([
    "2.0.0",
    "Sessions hang after sign-in",
    "alice@example.com",
    "2026-10-20 09:15:00",
  ]);
  expect(within(rows[1]).getByText(m.versionsNoReason)).toBeInTheDocument();
  expect(
    within(rows[1]).getByRole("button", { name: m.versionsClearFailedVersion("1.9.0") }),
  ).toBeInTheDocument();
  expect(within(region).queryByText(m.versionsFailedNone)).not.toBeInTheDocument();
});

test("Mark as failed opens the dialog with the row's version, and sends the version and the reason", async () => {
  let report = REPORT;
  const fake = versionsApi(
    {
      "PUT /api/claude/failures": (c) =>
        (report = marking(report, {
          version: "2.0.0",
          at: "2026-10-24T10:00:00+00:00",
          by: "alice@example.com",
          reason: (c.body as { reason?: string }).reason ?? null,
        })),
    },
    () => report,
  );
  renderApp("/admin/claude");
  const region = await versionsPage();
  const dialog = await openMark(
    within(region).getByRole("button", { name: m.versionsMarkFailedVersion("2.0.0") }),
  );
  expect(within(dialog).getByLabelText(m.versionsVersion)).toHaveValue("2.0.0");
  const reason = within(dialog).getByLabelText(m.versionsReason);
  await userEvent.type(reason, "Sessions hang");
  await userEvent.click(within(dialog).getByRole("button", { name: m.versionsMarkFailed }));
  await waitFor(() => expect(fake.made("PUT", "/api/claude/failures")).toHaveLength(1));
  expect(fake.made("PUT", "/api/claude/failures")[0].body).toEqual({
    version: "2.0.0",
    reason: "Sessions hang",
  });
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(await screen.findByText(m.versionsMarked("2.0.0"))).toBeInTheDocument();
  const card = await failedCard();
  expect(within(card).getByRole("row", { name: /2\.0\.0/ })).toHaveTextContent("Sessions hang");
  const row = within(region).getByRole("row", { name: /2\.0\.0/ });
  expect(within(row).getByText(m.versionsStandingFailed)).toBeInTheDocument();
  expect(within(row).queryByRole("button")).not.toBeInTheDocument();
});

test("Remove mark sends the version at once, and Use is offered for it again", async () => {
  let report = MARKED;
  const fake = versionsApi(
    { "DELETE /api/claude/failures/2.0.0": () => (report = clearing(report, "2.0.0")) },
    () => report,
  );
  renderApp("/admin/claude");
  const card = await failedCard();
  await userEvent.click(
    within(card).getByRole("button", { name: m.versionsClearFailedVersion("2.0.0") }),
  );
  await waitFor(() => expect(fake.made("DELETE", "/api/claude/failures/2.0.0")).toHaveLength(1));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); // no question asked
  expect(await screen.findByText(m.versionsCleared("2.0.0"))).toBeInTheDocument();
  const region = await versionsPage();
  expect(
    await within(region).findByRole("button", { name: m.versionsUseVersion("2.0.0") }),
  ).toBeInTheDocument();
  expect(within(card).queryByRole("row", { name: /2\.0\.0/ })).not.toBeInTheDocument();
  expect(within(card).getByRole("row", { name: /1\.9\.0/ })).toBeInTheDocument();
});
