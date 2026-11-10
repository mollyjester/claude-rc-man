import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { browser } from "./browser";
import { takeReports } from "./clientErrors";
import { m } from "./messages";
import {
  ADMIN_USER,
  fakeApi,
  NOTHING_TO_ATTEND,
  type Reply,
  type Routes,
  UNREACHABLE_REPLY,
} from "./test/fakeApi";
import { renderApp } from "./test/render";
import { GUIDE_ROLLBACK, GUIDE_WEB_UPGRADE, RCMAN_POLL_MS } from "./Upgrade";

const PATH = "/admin/rcman";
const RELEASE = "0123456789abcdef0123456789abcdef01234567";
const NEXT = "fedcba9876543210fedcba9876543210fedcba98";

type Report = Record<string, unknown>;

// What GET /api/rcman/upgrade answers with the option on, nothing recorded and nothing in the way.
const READY: Report = {
  release: RELEASE,
  enabled: true,
  available: true,
  reason: null,
  state: "unknown",
  started: null,
  finished: null,
  from: null,
  to: null,
  error: null,
};
const refused = (reason: string, title: string) => ({
  type: "urn:rcman:problem:rcman_upgrade_refused",
  title,
  status: 409,
  code: "rcman_upgrade_refused",
  reason,
});
const OFF: Report = {
  ...READY,
  enabled: false,
  available: false,
  reason: refused("disabled", "planted: it is off"),
};
const REQUESTED: Report = {
  ...READY,
  available: false,
  reason: refused("under_way", "planted: under way"),
  state: "requested",
};
const RUNNING: Report = {
  ...REQUESTED,
  state: "running",
  started: "2026-10-19T10:00:00+00:00",
  from: RELEASE,
};
const SUCCEEDED: Report = {
  ...READY,
  release: NEXT,
  state: "succeeded",
  started: "2026-10-19T10:00:00+00:00",
  finished: "2026-10-19T10:05:00+00:00",
  from: RELEASE,
  to: NEXT,
};
const FAILED: Report = {
  ...READY,
  state: "failed",
  started: "2026-10-19T10:00:00+00:00",
  finished: "2026-10-19T10:01:00+00:00",
  from: RELEASE,
  error: "install.sh: planted: the clone is not clean",
};

function api(report: Reply, extra: Routes = {}) {
  return fakeApi({
    "GET /api/me": ADMIN_USER,
    "GET /api/attention": NOTHING_TO_ATTEND,
    "GET /api/rcman/upgrade": report,
    ...extra,
  });
}

const page = () => screen.findByRole("heading", { level: 1, name: m.upgradeRcman });
const fact = (term: string) => screen.getByText(term, { selector: "dt" }).nextElementSibling;
const upgradeButton = () => screen.queryByRole("button", { name: m.upgradeRcman });

async function openDialog() {
  await userEvent.click(await screen.findByRole("button", { name: m.upgradeRcman }));
  return screen.findByRole("dialog", { name: m.upgradeRcman });
}

test("the release rcman runs is a fact, its commit as code that is not a link", async () => {
  api(READY);
  renderApp(PATH);
  await page();
  const release = await waitFor(() => fact(m.upgradeRelease)!);
  const code = release.querySelector("code")!;
  expect(code).toHaveTextContent(RELEASE);
  expect(code).toHaveAttribute("translate", "no");
  expect(release.querySelector("a")).toBeNull();
  expect(fact(m.upgradeLast)).toHaveTextContent(m.upgradeStates.unknown);
});

test("with the option off the page explains it, links the guide and offers no upgrade", async () => {
  api(OFF);
  renderApp(PATH);
  await page();
  const link = await screen.findByRole("link", { name: m.upgradeOffLink });
  expect(link).toHaveAttribute("href", GUIDE_WEB_UPGRADE);
  // the link ends the sentence that explains it
  expect(link.closest("p")).toHaveTextContent(`${m.upgradeOff} ${m.upgradeOffLink}.`);
  expect(upgradeButton()).not.toBeInTheDocument();
  expect(screen.queryByText("planted: it is off")).not.toBeInTheDocument();
});

test("while the request is out, the dialog's button keeps its label and waits; then the page follows the upgrade", async () => {
  let give: (reply: Report) => void = () => {};
  const fake = api(READY, {
    "POST /api/rcman/upgrade": () =>
      new Promise<Report>((resolve) => {
        give = resolve;
      }),
  });
  renderApp(PATH);
  const dialog = await openDialog();
  const confirm = within(dialog).getByRole("button", { name: m.upgradeRcman });
  await userEvent.click(confirm);
  await waitFor(() => expect(confirm).toBeDisabled());
  expect(confirm).toHaveTextContent(m.upgradeRcman);
  expect(within(dialog).getByRole("button", { name: m.closeDialog })).toBeDisabled();
  await userEvent.click(confirm);
  expect(fake.made("POST", "/api/rcman/upgrade")).toHaveLength(1);
  await act(async () => give(REQUESTED));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(await screen.findByText(m.upgradeRequested)).toBeInTheDocument();
  expect(upgradeButton()).not.toBeInTheDocument();
});

test("a failed upgrade shows its error and says to roll back from a shell, linking the guide", async () => {
  api(FAILED);
  renderApp(PATH);
  expect(await screen.findByText(FAILED.error as string)).toBeInTheDocument();
  const link = screen.getByRole("link", { name: m.upgradeRollBackLink });
  expect(link).toHaveAttribute("href", GUIDE_ROLLBACK);
  expect(link.closest("p")).toHaveTextContent(`${m.upgradeRollBack} ${m.upgradeRollBackLink}.`);
  expect(fact(m.upgradeLast)).toHaveTextContent(m.upgradeStates.failed);
  // one may be asked for again
  expect(upgradeButton()).toBeInTheDocument();
});

describe("following an upgrade", () => {
  beforeEach(() => void vi.useFakeTimers());
  afterEach(() => void vi.useRealTimers());
  const pass = (ms: number) =>
    act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });

  test("it polls quietly through the restart, then offers Reload page", async () => {
    const replies: Reply[] = [RUNNING, UNREACHABLE_REPLY, UNREACHABLE_REPLY, RUNNING, SUCCEEDED];
    let asked = 0;
    const fake = api(() => replies[Math.min(asked++, replies.length - 1)]);
    const reload = vi.spyOn(browser, "reload").mockImplementation(() => {});
    renderApp(PATH);
    expect(await screen.findByText(m.upgradeRunning)).toBeInTheDocument();
    await pass(RCMAN_POLL_MS);
    expect(await screen.findByText(m.upgradeRestarting)).toBeInTheDocument();
    await pass(RCMAN_POLL_MS);
    await pass(RCMAN_POLL_MS);
    expect(await screen.findByText(m.upgradeRunning)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    await pass(RCMAN_POLL_MS);
    expect(await screen.findByText(m.upgradeSucceeded)).toBeInTheDocument();
    expect(takeReports()).toEqual([]); // nothing noted as a failure for rcman
    expect(fake.made("POST", "/api/client-errors")).toHaveLength(0);
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(screen.getByRole("button", { name: m.upgradeReload }));
    expect(reload).toHaveBeenCalledTimes(1);
    const polls = fake.made("GET", "/api/rcman/upgrade").length;
    await pass(RCMAN_POLL_MS * 3);
    expect(fake.made("GET", "/api/rcman/upgrade")).toHaveLength(polls); // and then no more
    reload.mockRestore();
  });
});
