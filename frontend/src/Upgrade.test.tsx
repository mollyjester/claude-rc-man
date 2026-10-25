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
  problem,
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

test("the page is among the administrators' pages, named by a noun as they are, and headed by what it does", async () => {
  api(READY);
  renderApp("/admin/people");
  const nav = await screen.findByRole("navigation", { name: m.navMain });
  const link = await within(nav).findByRole("link", { name: m.navRcman });
  expect(link).toHaveAttribute("href", PATH);
  expect(m.navRcman).not.toBe(m.upgradeRcman);
  await userEvent.click(link);
  expect(await page()).toBeInTheDocument();
});

test("Upgrade rcman is the page's one primary button, beside its heading", async () => {
  api(READY);
  renderApp(PATH);
  const button = await screen.findByRole("button", { name: m.upgradeRcman });
  expect(button).toHaveClass("primary");
  const head = button.closest(".page-head");
  expect(head).not.toBeNull();
  expect(head).toContainElement(screen.getByRole("heading", { level: 1, name: m.upgradeRcman }));
  expect(document.querySelectorAll("main button.primary")).toHaveLength(1);
});

test("the release rcman runs is a fact, its commit as code that is not a link", async () => {
  api(READY);
  renderApp(PATH);
  await page();
  const release = await waitFor(() => fact(m.upgradeRelease)!);
  expect(release.closest("dl")).toHaveClass("facts");
  const code = release.querySelector("code")!;
  expect(code).toHaveTextContent(RELEASE);
  expect(code).toHaveAttribute("translate", "no");
  expect(release.querySelector("a")).toBeNull();
  expect(fact(m.upgradeLast)).toHaveTextContent(m.upgradeStates.unknown);
});

test("a release that is not known is said so", async () => {
  api({ ...READY, release: null });
  renderApp(PATH);
  await page();
  await waitFor(() => expect(fact(m.upgradeRelease)).toHaveTextContent(m.upgradeReleaseUnknown));
  expect(fact(m.upgradeRelease)!.querySelector("code")).toBeNull();
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

test("where an upgrade cannot be asked for now, the server's reason is in the button's place", async () => {
  api({ ...READY, available: false, reason: refused("creating", "planted: one is being made") });
  renderApp(PATH);
  expect(await screen.findByText("planted: one is being made")).toBeInTheDocument();
  expect(upgradeButton()).not.toBeInTheDocument();
});

test("Upgrade rcman asks first, saying what it cuts off, and Cancel asks for nothing", async () => {
  const fake = api(READY);
  renderApp(PATH);
  const dialog = await openDialog();
  for (const consequence of m.upgradeConsequences)
    expect(within(dialog).getByText(consequence)).toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: m.upgradeRcman })).toHaveClass("primary");
  await userEvent.click(within(dialog).getByRole("button", { name: m.cancel }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(fake.made("POST", "/api/rcman/upgrade")).toHaveLength(0);
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
  // the button that had focus is gone: focus is on the card that says how it goes
  const heading = screen.getByRole("heading", { level: 2, name: m.upgradeFactsTitle });
  await waitFor(() => expect(heading).toHaveFocus());
  expect(heading).toHaveAttribute("tabindex", "-1");
});

test("a refused request is shown in the dialog, which stays open", async () => {
  api(READY, {
    "POST /api/rcman/upgrade": problem(409, "rcman_upgrade_refused", "planted: refused", {
      reason: "rolling_out",
    }),
  });
  renderApp(PATH);
  const dialog = await openDialog();
  await userEvent.click(within(dialog).getByRole("button", { name: m.upgradeRcman }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("planted: refused");
  expect(screen.getByRole("dialog", { name: m.upgradeRcman })).toBeInTheDocument();
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

test("a request that never started says so, and how to roll back is not offered as if it ran", async () => {
  api({ ...READY, state: "not_started" });
  renderApp(PATH);
  expect(await screen.findByText(m.upgradeNotStarted)).toBeInTheDocument();
  expect(screen.queryByRole("link", { name: m.upgradeRollBackLink })).not.toBeInTheDocument();
});

test("a finished upgrade's facts are shown, its commits as code", async () => {
  api(SUCCEEDED);
  renderApp(PATH);
  await page();
  await waitFor(() => expect(fact(m.upgradeLast)).toHaveTextContent(m.upgradeStates.succeeded));
  expect(fact(m.upgradeFrom)!.querySelector("code")).toHaveTextContent(RELEASE);
  expect(fact(m.upgradeTo)!.querySelector("code")).toHaveTextContent(NEXT);
  expect(fact(m.upgradeFinished)).toHaveTextContent("2026-10-19 10:05:00");
  // opened afresh after it, the page is the new one: nothing to reload
  expect(screen.queryByRole("button", { name: m.upgradeReload })).not.toBeInTheDocument();
});

describe("following an upgrade", () => {
  beforeEach(() => void vi.useFakeTimers({ shouldAdvanceTime: true }));
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
    await userEvent.click(screen.getByRole("button", { name: m.upgradeReload }));
    expect(reload).toHaveBeenCalledTimes(1);
    const polls = fake.made("GET", "/api/rcman/upgrade").length;
    await pass(RCMAN_POLL_MS * 3);
    expect(fake.made("GET", "/api/rcman/upgrade")).toHaveLength(polls); // and then no more
    reload.mockRestore();
  });

  test("an upgrade that fails while followed shows its error", async () => {
    let state = RUNNING;
    api(() => state);
    renderApp(PATH);
    expect(await screen.findByText(m.upgradeRunning)).toBeInTheDocument();
    state = FAILED;
    await pass(RCMAN_POLL_MS);
    const error = await screen.findByText(FAILED.error as string);
    expect(screen.queryByText(m.upgradeRunning)).not.toBeInTheDocument();
    // said where the progress was, so that it is announced
    expect(screen.getByRole("status")).toContainElement(error);
    expect(screen.getByRole("status")).toContainElement(
      screen.getByRole("link", { name: m.upgradeRollBackLink }),
    );
  });

  test.each([
    ["succeeded", SUCCEEDED, m.upgradeSucceeded],
    [
      "not started",
      { ...REQUESTED, state: "not_started", available: true, reason: null },
      m.upgradeNotStarted,
    ],
  ])(
    "an upgrade that ended %s while followed is announced where its progress was",
    async (_, end, words) => {
      let state: Report = RUNNING;
      api(() => state);
      renderApp(PATH);
      expect(await screen.findByText(m.upgradeRunning)).toBeInTheDocument();
      state = end;
      await pass(RCMAN_POLL_MS);
      expect(screen.getByRole("status")).toContainElement(await screen.findByText(words));
    },
  );

  test("the progress is the page's one live region", async () => {
    api(RUNNING);
    renderApp(PATH);
    const progress = await screen.findByText(m.upgradeRunning);
    const main = screen.getByRole("main");
    const live = main.querySelectorAll('[role="status"], [role="alert"], [aria-live]');
    expect(live).toHaveLength(1);
    expect(live[0]).toContainElement(progress);
  });

  test("nothing is polled while no upgrade is under way, nor once the page is left", async () => {
    const fake = api(READY);
    const { unmount } = renderApp(PATH);
    await page();
    await waitFor(() => expect(fake.made("GET", "/api/rcman/upgrade")).toHaveLength(1));
    await pass(RCMAN_POLL_MS * 3);
    expect(fake.made("GET", "/api/rcman/upgrade")).toHaveLength(1);
    unmount();
    const running = api(RUNNING);
    const second = renderApp(PATH);
    await screen.findByText(m.upgradeRunning);
    second.unmount();
    const asked = running.made("GET", "/api/rcman/upgrade").length;
    await pass(RCMAN_POLL_MS * 3);
    expect(running.made("GET", "/api/rcman/upgrade")).toHaveLength(asked);
  });
});
