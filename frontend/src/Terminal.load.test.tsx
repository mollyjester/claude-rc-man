// The Terminal tab while xterm.js loads, and where it cannot load. src/xtermView.ts is loaded
// with a dynamic import, which the page caches; each test here has a module registry of its
// own (vi.resetModules), in which src/xtermView.ts is what the test says, and imports the
// page afresh from it.
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { m } from "./messages";
import { fakeApi, ME, problem } from "./test/fakeApi";
import { sockets } from "./test/fakeSocket";

const ready = (id: number, name: string) => ({
  id,
  name,
  state: "ready",
  error: null,
  source: null,
  source_page: null,
  log: null,
  server: null,
  server_error: null,
  claude_version: null,
});

function page() {
  const NOT_A_REPOSITORY = () => problem(409, "not_a_repository", "planted: not a repository");
  return fakeApi({
    "GET /api/me": ME,
    "GET /api/workspaces": { workspaces: [ready(1, "alpha")] },
    "GET /api/git/status": NOT_A_REPOSITORY,
    "GET /api/git/branches": NOT_A_REPOSITORY,
  });
}

// The page, imported afresh with src/xtermView.ts as `factory` gives it; what to clean up
// with, and the page's own reports.
async function fresh(factory: () => Promise<unknown>) {
  vi.resetModules();
  vi.doMock("./xtermView", factory);
  const { renderApp } = await import("./test/render");
  const { cleanup } = await import("@testing-library/react");
  const { takeReports } = await import("./clientErrors");
  const { browser } = await import("./browser");
  return { renderApp, cleanup, takeReports, browser };
}

afterEach(() => {
  vi.doUnmock("./xtermView");
});

const alpha = () => screen.findByRole("region", { name: "alpha" });

test("xterm.js that cannot be loaded says so, offers to reload the page and not Reconnect, and is reported", async () => {
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  const { renderApp, cleanup, takeReports, browser } = await fresh(() =>
    Promise.reject(new TypeError("planted: Failed to fetch dynamically imported module")),
  );
  try {
    page();
    renderApp("/?workspace=1&tab=terminal");
    const panel = within(await alpha());
    // the status line says the terminal connects first, so its words are waited for
    await waitFor(() => expect(panel.getByRole("status")).toHaveTextContent(m.terminalNotLoaded));
    expect(panel.queryByRole("button", { name: m.terminalReconnect })).not.toBeInTheDocument();
    const reload = vi.spyOn(browser, "reload").mockImplementation(() => {});
    await userEvent.click(panel.getByRole("button", { name: m.reloadPage }));
    expect(reload).toHaveBeenCalledOnce();
    expect(takeReports().map((r) => r.kind)).toEqual(["page_error"]);
    expect(sockets()).toEqual([]);
  } finally {
    cleanup();
    consoleError.mockRestore();
  }
});

test("a screen is made only once its tab is shown, though xterm.js loaded while it was hidden", async () => {
  let loaded: () => void = () => {};
  const gate = new Promise<void>((resolve) => {
    loaded = resolve;
  });
  const { renderApp, cleanup } = await fresh(async () => {
    await gate;
    return import("./test/fakeXterm");
  });
  const { views } = await import("./test/fakeXterm");
  try {
    page();
    renderApp("/?workspace=1&tab=terminal");
    const panel = within(await alpha());
    await waitFor(() => expect(panel.getByRole("status")).toHaveTextContent(m.terminalConnecting));
    await userEvent.click(panel.getByRole("tab", { name: m.tabOverview }));
    loaded();
    await new Promise((resolve) => setTimeout(resolve, 100));
    expect(views()).toEqual([]); // nothing made where it cannot be measured
    expect(sockets()).toEqual([]);
    await userEvent.click(panel.getByRole("tab", { name: m.tabTerminal }));
    await waitFor(() => expect(views()).toHaveLength(1));
    await waitFor(() => expect(sockets()).toHaveLength(1));
  } finally {
    cleanup();
  }
});
