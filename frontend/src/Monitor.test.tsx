import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";
import { ADMIN } from "./AdminPages";
import { takeReports } from "./clientErrors";
import { MONITOR_POLL_MS } from "./Monitor";
import { m } from "./messages";
import {
  ADMIN_USER,
  fakeApi,
  NOTHING_TO_ATTEND,
  problem,
  type Reply,
  type Routes,
  UNREACHABLE_REPLY,
} from "./test/fakeApi";
import { renderApp } from "./test/render";

const PATH = "/admin/monitor";
const USAGE_AT = "2026-10-23T10:00:00+00:00";
const DISK_AT = "2026-10-23T09:00:00+00:00";

type Snapshot = Record<string, unknown>;

const UNKNOWN = { cpu_percent: null, memory_bytes: null, tasks: null, disk_bytes: null };

// What GET /api/monitor answers with every scope on, after a round of each.
const SNAPSHOT: Snapshot = {
  scopes: { host: true, people: true, containers: true },
  usage: { collected_at: USAGE_AT, error: null },
  disk: { collected_at: DISK_AT, error: null },
  host: {
    cpu_percent: 250.5,
    cpus: 8,
    memory_used_bytes: 6_200_000_000,
    memory_total_bytes: 16_800_000_000,
    tasks: 412,
    disks: [
      {
        path: "/var/lib/rcman",
        roles: ["database", "root"],
        size_bytes: 100_000_000_000,
        used_bytes: 45_000_000_000,
        available_bytes: 50_000_000_000,
      },
      {
        path: "/srv/podman/storage",
        roles: ["podman"],
        size_bytes: null,
        used_bytes: null,
        available_bytes: null,
      },
    ],
  },
  people: [
    {
      id: 1,
      email: "alice@example.com",
      workspaces: 2,
      cpu_percent: 12.5,
      memory_bytes: 1_234_000_000,
      tasks: 40,
      disk_bytes: 3_000_000_000,
    },
    { id: 2, email: "bob@example.com", workspaces: 0, ...UNKNOWN },
  ],
  containers: [
    {
      id: 1,
      name: "alpha",
      owner: "alice@example.com",
      state: "ready",
      cpu_percent: 7.5,
      memory_bytes: 812_000_000,
      tasks: 31,
      disk_bytes: 1_500_000_000,
    },
    { id: 2, name: "beta", owner: "alice@example.com", state: "failed", ...UNKNOWN },
  ],
};

const SETTINGS = {
  usage_interval_seconds: 60,
  disk_interval_seconds: 3600,
  host: true,
  people: true,
  containers: true,
};

function api(snapshot: Reply = SNAPSHOT, extra: Routes = {}) {
  return fakeApi({
    "GET /api/me": ADMIN_USER,
    "GET /api/attention": NOTHING_TO_ATTEND,
    "GET /api/monitor": snapshot,
    "GET /api/monitor/settings": { settings: SETTINGS },
    ...extra,
  });
}

beforeEach(() => void vi.useFakeTimers({ shouldAdvanceTime: true }));
afterEach(() => void vi.useRealTimers());
const pass = (ms: number) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

const page = () => screen.findByRole("heading", { level: 1, name: m.monitorTitle });
const card = (name: string) => screen.findByRole("region", { name });
// The value of a fact, by what its name reads.
const fact = (part: HTMLElement, term: string) =>
  [...part.querySelectorAll("dt")].find((dt) => dt.textContent === term)?.nextElementSibling ??
  null;
// What an element reads, its non-breaking spaces read as plain ones.
const NBSP = /\u00a0/g;
const read = (element: Element | null) => (element?.textContent ?? "").replace(NBSP, " ");
// The live region of the collection: when each round last collected, and how often each runs.
const collected = () =>
  screen
    .getAllByRole("status")
    .find((region) => region.classList.contains("monitor-collected")) as HTMLElement;
// The cells of the row of a table that names something.
const cells = async (table: string, row: RegExp) => {
  const found = within(await card(table)).getByRole("row", { name: row });
  return within(found)
    .getAllByRole("cell")
    .map((cell) => read(cell));
};

test("the page is headed Monitor, and says what it shows", async () => {
  api();
  renderApp(PATH);
  expect(await page()).toBeInTheDocument();
  expect(screen.getByText(m.monitorIntro)).toBeInTheDocument();
});

test("the host card shows CPU beside the cores, memory of its total, processes and each filesystem", async () => {
  api();
  renderApp(PATH);
  const host = await card(m.monitorHost);
  await within(host).findByText(m.monitorCpu, { selector: "dt" });
  expect(read(fact(host, m.monitorCpu))).toBe("250.5% of one core; 8 cores in all");
  expect(read(fact(host, m.monitorMemory))).toBe("6.2 GB used of 16.8 GB");
  expect(read(fact(host, m.monitorProcesses))).toBe("412");
  // the main figures in large tabular numerals
  for (const term of [m.monitorCpu, m.monitorMemory, m.monitorProcesses])
    expect(fact(host, term)?.querySelector(".figure")).not.toBeNull();
  // each filesystem by its path, with the folders it holds in words
  expect(read(fact(host, "/var/lib/rcman"))).toBe(
    `45 GB used of 100 GB, 50 GB free ${m.monitorFilesystemOf("rcman's database and the host's root folder")}`,
  );
  expect(read(fact(host, "/srv/podman/storage"))).toBe(
    `${m.monitorNotKnown} ${m.monitorFilesystemOf("Podman's storage")}`,
  );
});

test("the People table shows what each person's workspaces use together, and Not known for what was not read", async () => {
  api();
  renderApp(PATH);
  const people = await card(m.monitorPeople);
  const headings = within(people)
    .getAllByRole("columnheader")
    .map((h) => h.textContent);
  expect(headings).toEqual([
    m.monitorPerson,
    m.usersWorkspaces,
    m.monitorCpu,
    m.monitorMemoryWithSwap,
    m.monitorProcesses,
    m.monitorDisk,
  ]);
  expect(await cells(m.monitorPeople, /alice/)).toEqual([
    "alice@example.com",
    "2",
    "12.5%",
    "1.23 GB",
    "40",
    "3 GB",
  ]);
  expect(await cells(m.monitorPeople, /bob/)).toEqual([
    "bob@example.com",
    "0",
    m.monitorNotKnown,
    m.monitorNotKnown,
    m.monitorNotKnown,
    m.monitorNotKnown,
  ]);
  // a list of rows on a phone
  expect(within(people).getByRole("table")).toHaveClass("stacked");
});

test("the Containers table shows each workspace's container by its name and owner, with its state", async () => {
  api();
  renderApp(PATH);
  const containers = await card(m.monitorContainers);
  expect(
    within(containers)
      .getAllByRole("columnheader")
      .map((h) => h.textContent),
  ).toEqual([
    m.everyoneWorkspace,
    m.versionsOwner,
    m.monitorState,
    m.monitorCpu,
    m.monitorMemoryWithSwap,
    m.monitorProcesses,
    m.monitorDisk,
  ]);
  expect(await cells(m.monitorContainers, /alpha/)).toEqual([
    "alpha",
    "alice@example.com",
    m.workspaceStates.ready,
    "7.5%",
    "812 MB",
    "31",
    "1.5 GB",
  ]);
  expect(await cells(m.monitorContainers, /beta/)).toEqual([
    "beta",
    "alice@example.com",
    m.workspaceStates.failed,
    m.monitorNotKnown,
    m.monitorNotKnown,
    m.monitorNotKnown,
    m.monitorNotKnown,
  ]);
  expect(within(containers).getByRole("table")).toHaveClass("stacked");
});

test("a host collected before its first round is all Not known", async () => {
  api({
    ...SNAPSHOT,
    usage: { collected_at: null, error: null },
    host: {
      cpu_percent: null,
      cpus: null,
      memory_used_bytes: null,
      memory_total_bytes: null,
      tasks: null,
      disks: [],
    },
  });
  renderApp(PATH);
  const host = await card(m.monitorHost);
  await within(host).findByText(m.monitorCpu, { selector: "dt" });
  for (const term of [m.monitorCpu, m.monitorMemory, m.monitorProcesses])
    expect(read(fact(host, term))).toBe(m.monitorNotKnown);
});

test.each([
  ["host", () => m.monitorHost],
  ["people", () => m.monitorPeople],
  ["containers", () => m.monitorContainers],
] as const)(
  "%s switched off shows an empty state that leads to the Service settings page",
  async (scope, name) => {
    api({ ...SNAPSHOT, scopes: { ...(SNAPSHOT.scopes as object), [scope]: false }, [scope]: null });
    const { location } = renderApp(PATH);
    const off = await card(name());
    expect(await within(off).findByText(m.monitorOff)).toBeInTheDocument();
    expect(within(off).getByText(m.monitorOffBody(name()))).toBeInTheDocument();
    const link = within(off).getByRole("link", { name: m.monitorOffLink });
    expect(link).toHaveAttribute("href", ADMIN.settings.path);
    expect(within(off).queryByRole("table")).not.toBeInTheDocument();
    // the others are shown
    const others = [m.monitorHost, m.monitorPeople, m.monitorContainers].filter(
      (n) => n !== name(),
    );
    for (const other of others)
      expect(within(await card(other)).queryByText(m.monitorOff)).not.toBeInTheDocument();
    // the link leads to the page that holds the monitoring settings
    await userEvent.click(link);
    expect(location().pathname).toBe(ADMIN.settings.path);
    expect(
      await screen.findByRole("heading", { level: 1, name: m.serviceSettingsTitle }),
    ).toBeInTheDocument();
  },
);

test("the page holds no settings form, and reads the settings once, for how often each round runs", async () => {
  const fake = api();
  renderApp(PATH);
  await card(m.monitorHost);
  await pass(MONITOR_POLL_MS * 2);
  await waitFor(() => expect(fake.made("GET", "/api/monitor").length).toBeGreaterThan(1));
  expect(document.querySelector("main form")).toBeNull();
  expect(screen.queryByRole("region", { name: m.monitorSettingsTitle })).not.toBeInTheDocument();
  expect(screen.queryByRole("button", { name: m.monitorSave })).not.toBeInTheDocument();
  expect(fake.made("GET", "/api/monitor/settings")).toHaveLength(1);
  expect(fake.made("PUT", "/api/monitor/settings")).toHaveLength(0);
});

test("the live region says when each round last collected, and how often each runs", async () => {
  api();
  renderApp(PATH);
  await page();
  await waitFor(() =>
    expect(read(collected())).toBe(
      `${m.monitorCollected(m.monitorUsage, "2026-10-23 10:00:00", "1 min")} ${m.monitorCollected(m.monitorDisk, "2026-10-23 09:00:00", "1 h")}`,
    ),
  );
  expect(collected()).toHaveAttribute("role", "status");
});

test("a round that has not collected yet says so", async () => {
  api({
    ...SNAPSHOT,
    usage: { collected_at: null, error: null },
    disk: { collected_at: null, error: null },
  });
  renderApp(PATH);
  await page();
  await waitFor(() =>
    expect(read(collected())).toBe(
      `${m.monitorCollected(m.monitorUsage, null, "1 min")} ${m.monitorCollected(m.monitorDisk, null, "1 h")}`,
    ),
  );
});

test("a poll with the same collection times leaves the live region alone, and a new time changes it", async () => {
  let snapshot: Snapshot = SNAPSHOT;
  const fake = api(() => snapshot);
  renderApp(PATH);
  await page();
  await waitFor(() => expect(read(collected())).toContain("10:00:00"));
  const region = collected();
  const changes: MutationRecord[] = [];
  const watcher = new MutationObserver((found) => changes.push(...found));
  watcher.observe(region, { subtree: true, childList: true, characterData: true });
  // new figures, the same times
  snapshot = { ...SNAPSHOT, host: { ...(SNAPSHOT.host as object), tasks: 999 } };
  const before = fake.made("GET", "/api/monitor").length;
  await pass(MONITOR_POLL_MS);
  await waitFor(() => expect(fake.made("GET", "/api/monitor").length).toBeGreaterThan(before));
  const host = await card(m.monitorHost);
  await waitFor(() => expect(read(fact(host, m.monitorProcesses))).toBe("999")); // the poll was shown
  changes.push(...watcher.takeRecords());
  expect(changes).toEqual([]);
  // a new usage time
  snapshot = { ...snapshot, usage: { collected_at: "2026-10-23T10:01:00+00:00", error: null } };
  await pass(MONITOR_POLL_MS);
  await waitFor(() => expect(read(collected())).toContain("10:01:00"));
  watcher.disconnect();
});

test("a round's failure is shown, and its figures stay with the time they were read", async () => {
  api({
    ...SNAPSHOT,
    usage: { collected_at: USAGE_AT, error: "planted: the user manager did not answer" },
    disk: { collected_at: DISK_AT, error: "planted: podman system df failed" },
  });
  renderApp(PATH);
  expect(await screen.findByText(m.monitorUsageFailed)).toBeInTheDocument();
  expect(screen.getByText("planted: the user manager did not answer")).toBeInTheDocument();
  expect(screen.getByText(m.monitorDiskFailed)).toBeInTheDocument();
  expect(screen.getByText("planted: podman system df failed")).toBeInTheDocument();
  expect(screen.getByText(m.monitorUsageFailed).closest(".message")).toHaveClass("message-problem");
  expect(read(collected())).toContain("10:00:00");
  expect(read(fact(await card(m.monitorHost), m.monitorProcesses))).toBe("412");
});

test("a read that fails is shown, and the figures read before stay until a read works", async () => {
  const replies: Reply[] = [SNAPSHOT, UNREACHABLE_REPLY, SNAPSHOT];
  let asked = 0;
  const fake = api(() => replies[Math.min(asked++, replies.length - 1)]);
  renderApp(PATH);
  const host = await card(m.monitorHost);
  await within(host).findByText(m.monitorCpu, { selector: "dt" });
  expect(screen.queryByText(m.unreachable)).not.toBeInTheDocument();
  await pass(MONITOR_POLL_MS);
  expect(await screen.findByText(m.unreachable)).toBeInTheDocument();
  expect(read(fact(host, m.monitorProcesses))).toBe("412"); // the last figures stay
  await pass(MONITOR_POLL_MS);
  await waitFor(() => expect(screen.queryByText(m.unreachable)).not.toBeInTheDocument());
  // the polls are quiet: nothing noted as a failure for rcman
  expect(takeReports()).toEqual([]);
  expect(fake.made("POST", "/api/client-errors")).toHaveLength(0);
});

test("a first read that fails is shown", async () => {
  api(problem(500, "internal", "planted: the snapshot could not be read"));
  renderApp(PATH);
  expect(await screen.findByText("planted: the snapshot could not be read")).toBeInTheDocument();
});

// A reply a test lets go of when it chooses, as a request still out.
function held() {
  let release: (reply: Reply) => void = () => {};
  const reply = new Promise<Reply>((resolve) => {
    release = resolve;
  });
  return { reply, release: (answer: Reply = SNAPSHOT) => release(answer) };
}

test("a read still out holds back the next: reads never overlap", async () => {
  const first = held();
  const fake = api(() => first.reply);
  renderApp(PATH);
  await page();
  await waitFor(() => expect(fake.made("GET", "/api/monitor")).toHaveLength(1));
  await pass(MONITOR_POLL_MS * 3);
  expect(fake.made("GET", "/api/monitor")).toHaveLength(1);
  first.release();
  await card(m.monitorHost); // the answer was shown
  expect(fake.made("GET", "/api/monitor")).toHaveLength(1);
  await pass(MONITOR_POLL_MS);
  await waitFor(() => expect(fake.made("GET", "/api/monitor")).toHaveLength(2));
});

test("with the settings not read, the live region says when each round collected, and not how often", async () => {
  api(SNAPSHOT, {
    "GET /api/monitor/settings": problem(
      500,
      "internal",
      "planted: the settings could not be read",
    ),
  });
  renderApp(PATH);
  expect(await screen.findByText("planted: the settings could not be read")).toBeInTheDocument();
  await waitFor(() =>
    expect(read(collected())).toBe(
      `${m.monitorCollected(m.monitorUsage, "2026-10-23 10:00:00", null)} ${m.monitorCollected(m.monitorDisk, "2026-10-23 09:00:00", null)}`,
    ),
  );
  expect(read(collected())).not.toContain("every");
  expect(screen.queryByLabelText(m.monitorUsageInterval)).not.toBeInTheDocument();
});
