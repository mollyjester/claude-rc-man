import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { api, SIGNED_OUT, terminalUrl, UNREACHABLE } from "./api";
import { takeReports } from "./clientErrors";
import { m } from "./messages";
import { PROTOCOL } from "./terminalSocket";
import { THEME } from "./terminalTheme";
import { fakeApi, ME, problem, SIGNED_OUT_REPLY } from "./test/fakeApi";
import { resized, schemeListening, switchScheme } from "./test/fakeLayout";
import { sockets } from "./test/fakeSocket";
import { views } from "./test/fakeXterm";
import { renderApp } from "./test/render";

function workspace(id: number, name: string, state = "ready") {
  return {
    id,
    name,
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
const terminalTab = async () =>
  within(await panel("alpha")).findByRole("tab", { name: m.tabTerminal });
const overviewTab = async () =>
  within(await panel("alpha")).findByRole("tab", { name: m.tabOverview });

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

test("the socket is opened through api.openTerminal, at this page's own address for the workspace", async () => {
  page();
  const openTerminal = vi.spyOn(api, "openTerminal");
  try {
    const { socket } = await opened();
    expect(openTerminal).toHaveBeenCalledExactlyOnceWith(1);
    expect(socket.url).toBe(`ws://${window.location.host}${PROTOCOL.path}?${PROTOCOL.query}=1`);
    expect(socket.binaryType).toBe("arraybuffer");
    await says(m.terminalConnecting);
  } finally {
    openTerminal.mockRestore();
  }
});

test("the address is ws for a page on http and wss for one on https, at the page's own host", () => {
  const at = (href: string) => new URL(href) as unknown as Location;
  expect(terminalUrl(3, at("http://rcman.test/x?y=1"))).toBe(
    `ws://rcman.test${PROTOCOL.path}?${PROTOCOL.query}=3`,
  );
  expect(terminalUrl(3, at("https://rcman.test/"))).toBe(
    `wss://rcman.test${PROTOCOL.path}?${PROTOCOL.query}=3`,
  );
});

test("the first frame is a resize to the screen's fitted size, before anything typed", async () => {
  page();
  const { socket, view } = await opened();
  view.size = { cols: 132, rows: 41 };
  view.type("typed while connecting");
  expect(socket.sent).toEqual([]); // nothing is sent before the socket opens
  act(() => socket.accept());
  expect(socket.frames()).toEqual([{ type: PROTOCOL.resize, cols: 132, rows: 41 }]);
  expect(typeof socket.sent[0]).toBe("string");
  act(() => view.type("ls\r"));
  expect(socket.sent).toHaveLength(2);
  expect(socket.typed()).toBe("ls\r");
});

test("connected, it says so, and what rcman sends is drawn and what is typed goes as bytes", async () => {
  page();
  const { socket, view } = await connected();
  expect(view.inputEnabled).toBe(true);
  act(() => socket.sendBytes("hello\r\n"));
  act(() => socket.sendBytes("café"));
  expect(view.written).toBe("hello\r\ncafé");
  act(() => view.type("é\r"));
  const typed = socket.sent.at(-1) as Uint8Array;
  expect(typed).toBeInstanceOf(Uint8Array); // a binary frame, not text
  expect([...typed]).toEqual([0xc3, 0xa9, 0x0d]);
  // what xterm.js hands on as bytes in a string is sent as those bytes
  act(() => view.typeBinary("\x1b[M \xff!"));
  expect([...(socket.sent.at(-1) as Uint8Array)]).toEqual([0x1b, 0x5b, 0x4d, 0x20, 0xff, 0x21]);
  expect(socket.frames()).toHaveLength(1); // only the first resize was text
});

// --- what ends it ------------------------------------------------------------------------

// The reasons after which rcman would refuse another terminal on the workspace, and those
// after which it would not.
const REFUSED_AGAIN = ["workspace_gone", "signed_out", "not_allowed"];
const OPEN_AGAIN = PROTOCOL.reasons.filter((r) => !REFUSED_AGAIN.includes(r));

test("the reasons are split between those that offer Reconnect and those that do not", () => {
  expect(OPEN_AGAIN).toEqual(["terminal_replaced", "service_closing"]);
  for (const reason of REFUSED_AGAIN) expect(PROTOCOL.reasons).toContain(reason);
});

test.each([404, 409])(
  "a workspace refused with %i shows the problem, offers no Reconnect, and asks for the workspaces again",
  async (status_) => {
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
  },
);

test.each(OPEN_AGAIN)(
  "a terminal closed for %s says so in words, and offers Reconnect",
  async (reason) => {
    const fake = page();
    const { socket } = await connected();
    const asked = fake.made("GET", "/api/workspaces").length;
    act(() => {
      socket.sendText({ type: PROTOCOL.closed, reason });
      socket.serverClose(
        reason === "service_closing" ? PROTOCOL.close_closing : PROTOCOL.close_normal,
      );
    });
    await says(m.terminalClosed[reason]);
    expect(await reconnect()).toBeInTheDocument();
    expect(fake.made("GET", "/api/workspaces")).toHaveLength(asked);
  },
);

test.each(REFUSED_AGAIN)(
  "a terminal closed for %s says so in words, offers no Reconnect, and asks for the workspaces again",
  async (reason) => {
    const fake = page();
    const { socket } = await connected();
    const asked = fake.made("GET", "/api/workspaces").length;
    act(() => {
      socket.sendText({ type: PROTOCOL.closed, reason });
      socket.serverClose(PROTOCOL.close_normal);
    });
    await says(m.terminalClosed[reason]);
    await waitFor(() => expect(fake.made("GET", "/api/workspaces")).toHaveLength(asked + 1));
    expect(screen.queryByRole("button", { name: m.terminalReconnect })).not.toBeInTheDocument();
  },
);

test("every reason a terminal is closed for has words of its own, and no words are for another", () => {
  expect(Object.keys(m.terminalClosed).sort()).toEqual([...PROTOCOL.reasons].sort());
  expect(new Set(Object.values(m.terminalClosed)).size).toBe(PROTOCOL.reasons.length);
});

test("a reason with no words is shown as it is", async () => {
  page();
  const { socket } = await connected();
  act(() => {
    socket.sendText({ type: PROTOCOL.closed, reason: "planted_reason" });
    socket.serverClose(PROTOCOL.close_normal);
  });
  await says(m.terminalClosedOther("planted_reason"));
});

test("a shell that exited says so, with its exit code", async () => {
  page();
  const { socket } = await connected();
  act(() => {
    socket.sendText({ type: PROTOCOL.exit, code: 7 });
    socket.serverClose(PROTOCOL.close_normal);
  });
  await says(m.terminalExited(7));
  expect(await reconnect()).toBeInTheDocument();
});

test("a socket closed with no frame is a lost connection, but rcman stopping says that", async () => {
  page();
  const first = await connected();
  act(() => first.socket.serverClose(1006));
  await says(m.terminalLost);
  await userEvent.click(await reconnect());
  await waitFor(() => expect(sockets()).toHaveLength(2));
  act(() => sockets()[1].accept());
  await says(m.terminalConnected);
  act(() => sockets()[1].serverClose(PROTOCOL.close_closing)); // uvicorn's own close as it stops
  await says(m.terminalClosed.service_closing);
});

test("a terminal taken over elsewhere is not opened again by itself; Reconnect opens a new one", async () => {
  page();
  const { socket, view } = await connected();
  act(() => socket.sendBytes("old output"));
  act(() => {
    socket.sendText({ type: PROTOCOL.closed, reason: "terminal_replaced" });
    socket.serverClose(PROTOCOL.close_normal);
  });
  await says(m.terminalClosed.terminal_replaced);
  await new Promise((resolve) => setTimeout(resolve, 200));
  expect(sockets()).toHaveLength(1);
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

test("a terminal that opened and closed is not noted as a failure, and asks for nothing", async () => {
  const fake = page();
  const { socket } = await connected();
  const asked = fake.made("GET", "/api/workspaces").length;
  act(() => {
    socket.sendText({ type: PROTOCOL.exit, code: 0 });
    socket.serverClose(PROTOCOL.close_normal);
  });
  await says(m.terminalExited(0));
  expect(fake.made("GET", "/api/workspaces")).toHaveLength(asked);
  expect(takeReports()).toEqual([]);
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

test("leaving the workspace while it connects notes no failure and asks for nothing", async () => {
  const fake = page();
  const { socket } = await opened(); // never accepted
  const asked = fake.made("GET", "/api/workspaces").length;
  await userEvent.click(
    within(await screen.findByRole("region", { name: m.navDashboard })).getByRole("button", {
      name: "beta",
    }),
  );
  await panel("beta");
  await waitFor(() => expect(socket.readyState).toBe(socket.CLOSED)); // its close event came
  await new Promise((resolve) => setTimeout(resolve, 50));
  expect(fake.made("GET", "/api/workspaces")).toHaveLength(asked);
  expect(takeReports()).toEqual([]);
});

test("Reconnect puts focus in the screen, and the old socket's bytes are not drawn", async () => {
  page();
  const { socket, view } = await connected();
  act(() => socket.serverClose(1006));
  const focused = view.focuses;
  await userEvent.click(await reconnect());
  expect(view.focuses).toBeGreaterThan(focused);
  await waitFor(() => expect(sockets()).toHaveLength(2));
  act(() => sockets()[1].accept());
  act(() => socket.sendBytes("stale"));
  act(() => sockets()[1].sendBytes("fresh"));
  expect(view.written).toBe("fresh");
});

// A regression check, not a guard of its own: the socket's effect waits for the screen, which
// comes only once xterm.js has loaded, after the mount whose effects strict mode runs twice,
// so it holds whatever the effect does.
test("in React's strict mode, which runs every effect twice, one socket is opened", async () => {
  page();
  renderApp("/?workspace=1&tab=terminal", { strict: true });
  await waitFor(() => expect(sockets().length).toBeGreaterThan(0));
  await new Promise((resolve) => setTimeout(resolve, 100));
  expect(sockets()).toHaveLength(1);
  expect(views().filter((v) => !v.disposed)).toHaveLength(1);
});

test("leaving the page closes the socket", async () => {
  page({
    "GET /api/me/settings": {
      prefix: "",
      author_name: "",
      author_email: "",
      effective_author: null,
    },
  });
  const { socket, navigate } = await connected();
  await navigate("/settings");
  await waitFor(() => expect(socket.closedByPage).toBe(PROTOCOL.close_normal));
});

test("the screen is fitted again when the tab is shown again, and the new size is sent", async () => {
  page();
  const { socket, view } = await connected();
  await userEvent.click(await overviewTab());
  expect(socket.closedByPage).toBeUndefined(); // kept while the other tab is shown
  view.size = { cols: 100, rows: 30 };
  const fits = view.fits;
  await userEvent.click(await terminalTab());
  expect(view.fits).toBeGreaterThan(fits);
  expect(socket.frames().at(-1)).toEqual({ type: PROTOCOL.resize, cols: 100, rows: 30 });
  expect(sockets()).toHaveLength(1);
});

test("the screen is fitted whenever its box changes size, and only a new size is sent", async () => {
  page();
  const { socket, view } = await connected();
  const fits = view.fits;
  act(() => resized());
  expect(view.fits).toBeGreaterThan(fits);
  expect(socket.frames()).toHaveLength(1); // the same size: nothing sent
  view.size = { cols: 60, rows: 20 };
  act(() => resized());
  expect(socket.frames()).toEqual([
    { type: PROTOCOL.resize, cols: 80, rows: 24 },
    { type: PROTOCOL.resize, cols: 60, rows: 20 },
  ]);
});

test("the screen's colours are read from the page, and read again when the colour scheme changes", async () => {
  page();
  const root = document.documentElement.style;
  root.setProperty(THEME.background, "#010203");
  root.setProperty(THEME.foreground, "#040506");
  try {
    const { view } = await connected();
    expect(view.themes).toEqual([
      expect.objectContaining({ background: "#010203", foreground: "#040506" }),
    ]);
    root.setProperty(THEME.background, "#0a0b0c");
    act(() => switchScheme(true));
    expect(view.themes).toHaveLength(2);
    expect(view.themes[1]).toEqual(
      expect.objectContaining({ background: "#0a0b0c", foreground: "#040506" }),
    );
  } finally {
    root.removeProperty(THEME.background);
    root.removeProperty(THEME.foreground);
  }
});
