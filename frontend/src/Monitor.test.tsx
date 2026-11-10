import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { MONITOR_POLL_MS } from "./Monitor";
import { m } from "./messages";
import { ADMIN_USER, fakeApi, NOTHING_TO_ATTEND, type Reply, type Routes } from "./test/fakeApi";
import { renderApp } from "./test/render";

const PATH = "/admin/monitor";
const USAGE_AT = "2026-10-23T10:00:00+00:00";
const DISK_AT = "2026-10-23T09:00:00+00:00";

type Snapshot = Record<string, unknown>;

const UNKNOWN = { cpu_percent: null, memory_bytes: null, tasks: null, disk_bytes: null };

// What GET /api/monitor answers with every scope on, after a round of each.
const SNAPSHOT: Snapshot = {
  scopes: { host: true, people: true, containers: true, storage: true },
  usage: { collected_at: USAGE_AT, error: null },
  disk: { collected_at: DISK_AT, error: null },
  refreshing: false,
  // rcman's own use of the disk; one part not known, and so not its total
  storage: {
    images_bytes: 13_500_000_000,
    workspace_volumes_bytes: 10_600_000_000,
    other_volumes_bytes: 250_000_000,
    claude_versions_bytes: null,
    total_bytes: null,
  },
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
    { id: 3, name: "gamma", owner: "alice@example.com", state: "halted", ...UNKNOWN },
  ],
};

const SETTINGS = {
  usage_interval_seconds: 60,
  disk_interval_seconds: 3600,
  host: true,
  people: true,
  containers: true,
  storage: true,
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

beforeEach(() => void vi.useFakeTimers());
afterEach(() => void vi.useRealTimers());
const pass = (ms: number) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

const card = (name: string) => screen.findByRole("region", { name });
// The value of a fact, by what its name reads.
const fact = (part: HTMLElement, term: string) =>
  [...part.querySelectorAll("dt")].find((dt) => dt.textContent === term)?.nextElementSibling ??
  null;
// What an element reads, its non-breaking spaces read as plain ones.
const NBSP = /\u00a0/g;
const read = (element: Element | null) => (element?.textContent ?? "").replace(NBSP, " ");
// The cells of the row of a table that names something.
const cells = async (table: string, row: RegExp) => {
  const found = within(await card(table)).getByRole("row", { name: row });
  return within(found)
    .getAllByRole("cell")
    .map((cell) => read(cell));
};

test("the host card shows CPU, memory, processes and each whole filesystem, and the Storage card rcman's own use by part", async () => {
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
  // each filesystem said to be the whole of it, not rcman's share
  expect(within(host).getByText(m.monitorFilesystemsLine)).toBeInTheDocument();
  // rcman's own use, by part, and their total; a part not known reads so, and so does the total
  const storage = await card(m.monitorStorage);
  expect(within(storage).getByText(m.monitorStorageLine)).toBeInTheDocument();
  expect([...storage.querySelectorAll("dt")].map((dt) => dt.textContent)).toEqual([
    m.monitorImages,
    m.monitorWorkspaceVolumes,
    m.monitorOtherVolumes,
    m.monitorClaudeVersions,
    m.monitorStorageTotal,
  ]);
  expect(read(fact(storage, m.monitorImages))).toBe("13.5 GB");
  expect(read(fact(storage, m.monitorWorkspaceVolumes))).toBe("10.6 GB");
  expect(read(fact(storage, m.monitorOtherVolumes))).toBe("250 MB");
  expect(read(fact(storage, m.monitorClaudeVersions))).toBe(m.monitorNotKnown);
  expect(read(fact(storage, m.monitorStorageTotal))).toBe(m.monitorNotKnown);
  expect(fact(storage, m.monitorImages)?.querySelector(".figure")).not.toBeNull();
});

test("the People table shows what each person's workspaces use together, and Not known for what was not read", async () => {
  api();
  renderApp(PATH);
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
});

test("the Containers table shows each workspace's container by its name and owner, with its state", async () => {
  api();
  renderApp(PATH);
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
  // a stopped workspace is shown as stopped, its figures not known
  expect(m.workspaceStates.halted).toBe("Stopped");
  expect(await cells(m.monitorContainers, /gamma/)).toEqual([
    "gamma",
    "alice@example.com",
    m.workspaceStates.halted,
    m.monitorNotKnown,
    m.monitorNotKnown,
    m.monitorNotKnown,
    m.monitorNotKnown,
  ]);
});

// A reply a test lets go of when it chooses, as a request still out.
function held() {
  let release: (reply: Reply) => void = () => {};
  const reply = new Promise<Reply>((resolve) => {
    release = resolve;
  });
  return { reply, release: (answer: Reply = SNAPSHOT) => release(answer) };
}

// The Refresh button beside the page's title, by the name it keeps while it is busy too.
const refreshButton = () => screen.findByRole("button", { name: m.monitorRefresh });

test("Refresh posts, says Refreshing… and is disabled until the snapshot stops refreshing", async () => {
  let snapshot: Snapshot = SNAPSHOT;
  const posted = held();
  const fake = api(() => snapshot, {
    "POST /api/monitor/refresh": () => posted.reply,
  });
  renderApp(PATH);
  const button = await refreshButton();
  await card(m.monitorHost);
  expect(button).toHaveAttribute("aria-disabled", "false");
  const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
  await user.click(button);
  await waitFor(() => expect(fake.made("POST", "/api/monitor/refresh")).toHaveLength(1));
  // while the request is out: busy
  expect(button).toHaveAttribute("aria-disabled", "true");
  expect(button).toHaveTextContent(m.monitorRefreshing);
  // the rounds run: the page reads again at once, and stays busy while they do
  snapshot = { ...SNAPSHOT, refreshing: true };
  const before = fake.made("GET", "/api/monitor").length;
  posted.release({ refreshing: true });
  await waitFor(() => expect(fake.made("GET", "/api/monitor").length).toBe(before + 1));
  await pass(0);
  expect(button).toHaveAttribute("aria-disabled", "true");
  expect(button).toHaveTextContent(m.monitorRefreshing);
  await user.click(button); // pressing again does nothing
  expect(fake.made("POST", "/api/monitor/refresh")).toHaveLength(1);
  // the rounds are in: the new figures, and the button is free
  snapshot = { ...SNAPSHOT, host: { ...(SNAPSHOT.host as object), tasks: 999 } };
  await pass(MONITOR_POLL_MS);
  await waitFor(() => expect(button).toHaveAttribute("aria-disabled", "false"));
  expect(read(fact(await card(m.monitorHost), m.monitorProcesses))).toBe("999");
});
