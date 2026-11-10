import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { api, SIGNED_OUT, UNREACHABLE } from "./api";
import { takeReports } from "./clientErrors";
import { m } from "./messages";
import { PROTOCOL } from "./terminalSocket";
import { fakeApi, ME, problem, SIGNED_OUT_REPLY } from "./test/fakeApi";
import { schemeListening } from "./test/fakeLayout";
import { sockets } from "./test/fakeSocket";
import { views } from "./test/fakeXterm";
import { renderApp } from "./test/render";
import { pendingTimersRun } from "./test/turn";

function workspace(id: number, name: string, state = "ready") {
  return {
    id,
    name,
    device: `rcman-ws-${id}`,
    state,
    error: null,
    source: null,
    source_page: null,
    log: null,
    server: null,
    server_error: null,
    claude_version: null,
  };
}

const NOT_A_REPOSITORY = () => problem(409, "not_a_repository", "planted: not a repository");

// alpha and beta, both ready; what /api/workspaces answers can be changed by the test.
function page(extra: Record<string, unknown> = {}) {
  return fakeApi({
    "GET /api/me": ME,
    "GET /api/workspaces": { workspaces: [workspace(1, "alpha"), workspace(2, "beta")] },
    "GET /api/git/status": NOT_A_REPOSITORY,
    "GET /api/git/branches": NOT_A_REPOSITORY,
    ...extra,
  });
}

const panel = (name: string) => screen.findByRole("region", { name });

// alpha's Terminal tab, opened from the address: its socket, once the page has opened it.
async function opened(path = "/?workspace=1&tab=terminal") {
  const rendered = renderApp(path);
  await waitFor(() => expect(sockets()).toHaveLength(1));
  return { ...rendered, socket: sockets()[0], view: views()[0] };
}

// The same, with rcman's end accepting it.
async function connected(path?: string) {
  const found = await opened(path);
  act(() => found.socket.accept());
  expect(await screen.findByText(m.terminalConnected)).toBeInTheDocument();
  return found;
}

const status = async () => within(await panel("alpha")).findByRole("status");
// The status line says `text`, waited for: the line is there from the first, saying the
// terminal connects, before it says anything else.
const says = (text: string) => waitFor(async () => expect(await status()).toHaveTextContent(text));
const reconnect = () => screen.findByRole("button", { name: m.terminalReconnect });

// --- opening -----------------------------------------------------------------------------

test("the socket is opened at this page's own address for the workspace, sends the screen's size first, then what is typed as bytes, and draws what rcman sends", async () => {
  page();
  const openTerminal = vi.spyOn(api, "openTerminal");
  try {
    const { socket, view } = await opened();
    expect(openTerminal).toHaveBeenCalledExactlyOnceWith(1);
    expect(socket.url).toBe(`ws://${window.location.host}${PROTOCOL.path}?${PROTOCOL.query}=1`);
    expect(socket.binaryType).toBe("arraybuffer");
    await says(m.terminalConnecting);
    view.size = { cols: 132, rows: 41 };
    act(() => socket.accept());
    expect(await screen.findByText(m.terminalConnected)).toBeInTheDocument();
    expect(socket.frames()).toEqual([{ type: PROTOCOL.resize, cols: 132, rows: 41 }]);
    act(() => socket.sendBytes("café"));
    expect(view.written).toBe("café");
    act(() => view.type("é\r"));
    const typed = socket.sent.at(-1) as Uint8Array;
    expect(typed).toBeInstanceOf(Uint8Array); // a binary frame, not text
    expect([...typed]).toEqual([0xc3, 0xa9, 0x0d]);
  } finally {
    openTerminal.mockRestore();
  }
});

// --- what ends it ------------------------------------------------------------------------

test("a workspace refused shows the problem, offers no Reconnect, and asks for the workspaces again", async () => {
  const status_ = 404;
  const fake = page();
  const { socket, view } = await opened();
  act(() => socket.accept());
  const asked = fake.made("GET", "/api/workspaces").length;
  act(() => {
    socket.sendText({
      type: PROTOCOL.problem,
      problem: {
        type: "urn:x",
        status: status_,
        code: "planted_code",
        title: "planted: refused",
      },
    });
    socket.serverClose(PROTOCOL.close_problem + status_);
  });
  const alert = await within(await panel("alpha")).findByRole("alert");
  expect(alert).toHaveTextContent("planted: refused");
  await says(m.terminalNotOpened);
  await waitFor(() => expect(fake.made("GET", "/api/workspaces")).toHaveLength(asked + 1));
  expect(screen.queryByRole("button", { name: m.terminalReconnect })).not.toBeInTheDocument();
  expect(view.inputEnabled).toBe(false);
  expect(takeReports()).toEqual([]); // a problem rcman sent is not reported
});

test("a terminal taken over elsewhere is not opened again by itself; Reconnect opens a new one", async () => {
  page();
  const { socket, view } = await connected();
  act(() => socket.sendBytes("old output"));
  vi.useFakeTimers(); // so that whatever the close leaves waiting on a timer is run below
  try {
    act(() => {
      socket.sendText({ type: PROTOCOL.closed, reason: "terminal_replaced" });
      socket.serverClose(PROTOCOL.close_normal);
    });
    await says(m.terminalClosed.terminal_replaced);
    await pendingTimersRun(); // what follows the close has run
    expect(sockets()).toHaveLength(1);
  } finally {
    vi.useRealTimers();
  }
  view.type("typed while closed");
  expect(socket.typed()).toBe("");
  await userEvent.click(await reconnect());
  await waitFor(() => expect(sockets()).toHaveLength(2));
  expect(sockets()[1].url).toBe(socket.url);
  expect(view.resets).toBe(1); // the new shell starts on a clear screen, in the same view
  expect(views()).toHaveLength(1);
  await says(m.terminalConnecting);
  expect(screen.queryByRole("button", { name: m.terminalReconnect })).not.toBeInTheDocument();
  act(() => sockets()[1].accept());
  expect(sockets()[1].frames()).toEqual([{ type: PROTOCOL.resize, cols: 80, rows: 24 }]);
});

test("a socket that never opened is noted and the workspaces are asked for again, which says why", async () => {
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  try {
    let signedOut = false;
    const fake = page({
      "GET /api/workspaces": () =>
        signedOut
          ? SIGNED_OUT_REPLY
          : { workspaces: [workspace(1, "alpha"), workspace(2, "beta")] },
    });
    const { socket } = await opened();
    const asked = fake.made("GET", "/api/workspaces").length;
    signedOut = true;
    act(() => socket.serverClose(1006)); // a refused upgrade: the page is told no more
    await says(m.terminalNotOpened);
    expect(await screen.findByText(m.signedOut)).toBeInTheDocument();
    expect(fake.made("GET", "/api/workspaces")).toHaveLength(asked + 1); // one refresh
    expect(takeReports().map((r) => [r.kind, r.method, r.path])).toEqual([
      [UNREACHABLE, "GET", PROTOCOL.path],
      [SIGNED_OUT, "GET", "/api/workspaces"],
    ]);
    expect(await reconnect()).toBeInTheDocument();
    expect(sockets()).toHaveLength(1);
  } finally {
    consoleError.mockRestore();
  }
});

// --- its life on the page ----------------------------------------------------------------

test("leaving the workspace closes its socket and ends its screen", async () => {
  page();
  const { socket, view } = await connected();
  await userEvent.click(
    within(await screen.findByRole("region", { name: m.navDashboard })).getByRole("button", {
      name: "beta",
    }),
  );
  await panel("beta");
  expect(socket.closedByPage).toBe(PROTOCOL.close_normal);
  expect(view.disposed).toBe(true);
  expect(schemeListening()).toBe(0);
  expect(sockets()).toHaveLength(1); // beta's Terminal is not opened by itself
});
