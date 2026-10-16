import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { m } from "./messages";
import { ADMIN_USER, fakeApi, NOTHING_TO_ATTEND, problem, type Routes } from "./test/fakeApi";
import { renderApp } from "./test/render";
import { UPGRADE_POLL_MS } from "./Versions";

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

test("it warns when the approved version is not the tested one, and only then", async () => {
  versionsApi();
  const { unmount } = renderApp("/admin/claude");
  expect(await screen.findByText(m.versionsUntested)).toBeInTheDocument();
  unmount();

  versionsApi({}, { ...REPORT, tested: "2.2.0" });
  renderApp("/admin/claude");
  await versionsPage();
  expect(definition(m.versionsTested)).toHaveTextContent("2.2.0"); // positive control: it loaded
  expect(screen.queryByText(m.versionsUntested)).not.toBeInTheDocument();
});

test("a version nothing is approved or recorded for is shown as none", async () => {
  versionsApi({}, { ...REPORT, approved: null, previous: null, installed: [], drift: [] });
  renderApp("/admin/claude");
  await versionsPage();
  expect(definition(m.versionsApproved)).toHaveTextContent(m.versionsNone);
  expect(definition(m.versionsPrevious)).toHaveTextContent(m.versionsNone);
  expect(screen.queryByText(m.versionsUntested)).not.toBeInTheDocument(); // nothing to differ
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
  expect(await screen.findByRole("alert")).toHaveTextContent(
    `${m.versionsUpgrade}: planted reason for the version`,
  );
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
  expect(alert).toHaveTextContent(`${m.versionsFailedChecks} sign_in_screen, logout`);
  const signIn = within(alert).getByRole("figure", { name: "sign_in_screen" });
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
