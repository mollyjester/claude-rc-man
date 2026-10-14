import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { useEffect, useState } from "react";

import { api as client } from "./api";
import { ClaudeConnection, CONNECTION_STATES, type Connection, START_REASON } from "./Claude";
import { m } from "./messages";
import {
  CONNECTED,
  fakeApi,
  NOT_CONNECTED,
  problem,
  type Reply,
  type Routes,
} from "./test/fakeApi";
import { renderAlone } from "./test/render";

// The Claude account's section is on no page while signing in to Claude in a
// workspace is not there; here it is loaded once and shown on its own.
function Account() {
  const [connection, setConnection] = useState<Connection | null>(null);
  useEffect(() => {
    client.get<Connection>("/api/claude").then(setConnection);
  }, []);
  return <ClaudeConnection connection={connection} onChange={setConnection} />;
}

function api(claude: Reply, extra: Routes = {}) {
  return fakeApi({
    "GET /api/claude": claude,
    ...extra,
  });
}

test("every state of the account says whether a server can start, and why not", () => {
  expect(Object.keys(START_REASON).sort()).toEqual([...CONNECTION_STATES].sort());
  // non-vacuity: some states let a server start and some do not
  const reasons = Object.values(START_REASON);
  expect(reasons).toContain(null);
  const blocked = reasons.filter((r) => r !== null);
  expect(blocked.length).toBeGreaterThan(0);
  for (const reason of blocked) {
    for (const said of [reason.start, reason.resume, reason.startAgain]) expect(said).toMatch(/\S/);
  }
});

test("an expiring or expired sign-in still starts a server: the server refuses only an account not signed in", () => {
  expect(START_REASON.expiring).toBeNull();
  expect(START_REASON.expired).toBeNull();
  expect(START_REASON.connected).toBeNull();
  expect(START_REASON.not_connected).toBe(m.notConnected);
});

test("connecting: link, then code, then connected", async () => {
  let connected = false;
  const fake = api(() => (connected ? CONNECTED : NOT_CONNECTED), {
    "POST /api/claude/login": { url: "https://claude.com/cai/oauth/authorize?x=1" },
    "POST /api/claude/code": () => {
      connected = true;
      return CONNECTED;
    },
  });
  renderAlone(<Account />);
  expect(await screen.findByText(m.claudeNotConnected)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: m.connect }));
  await userEvent.click(screen.getByRole("button", { name: m.connectGo }));
  const link = await screen.findByRole("link", { name: m.connectLink });
  expect(link).toHaveAttribute("href", "https://claude.com/cai/oauth/authorize?x=1");
  expect(link).toHaveAttribute("target", "_blank");
  expect(fake.made("POST", "/api/claude/login")[0].body).toEqual({});
  await userEvent.type(screen.getByLabelText(m.connectCode), "CODE#STATE");
  await userEvent.click(screen.getByRole("button", { name: m.connectSubmit }));
  await waitFor(() =>
    expect(fake.made("POST", "/api/claude/code")[0].body).toEqual({ code: "CODE#STATE" }),
  );
  expect(await screen.findByText(m.claudeConnectedAs("alice@claude.test"))).toBeInTheDocument();
});

test("another Claude address can be given", async () => {
  const fake = api(NOT_CONNECTED, { "POST /api/claude/login": { url: "https://claude.com/x" } });
  renderAlone(<Account />);
  await userEvent.click(await screen.findByRole("button", { name: m.connect }));
  await userEvent.type(screen.getByLabelText(m.connectEmail), "work@example.org");
  await userEvent.click(screen.getByRole("button", { name: m.connectGo }));
  await waitFor(() =>
    expect(fake.made("POST", "/api/claude/login")[0].body).toEqual({ email: "work@example.org" }),
  );
});

test("a refused code shows the server's problem", async () => {
  api(NOT_CONNECTED, {
    "POST /api/claude/login": { url: "https://claude.com/x" },
    "POST /api/claude/code": problem(409, "login_failed", "Signing in to Claude failed."),
  });
  renderAlone(<Account />);
  await userEvent.click(await screen.findByRole("button", { name: m.connect }));
  await userEvent.click(screen.getByRole("button", { name: m.connectGo }));
  await userEvent.type(await screen.findByLabelText(m.connectCode), "BAD");
  await userEvent.click(screen.getByRole("button", { name: m.connectSubmit }));
  expect(await screen.findByRole("alert")).toHaveTextContent("Signing in to Claude failed.");
  // Each code works once and Claude's sign-in has ended: offer a fresh link.
  expect(screen.getByRole("button", { name: m.connectGo })).toBeInTheDocument();
  expect(screen.queryByLabelText(m.connectCode)).not.toBeInTheDocument();
});

test("an unrecognised sign-in screen is shown as it is", async () => {
  api(NOT_CONNECTED, {
    "POST /api/claude/login": problem(502, "claude_unrecognised_output", "Unrecognised output.", {
      screen: ["odd screen"],
    }),
  });
  renderAlone(<Account />);
  await userEvent.click(await screen.findByRole("button", { name: m.connect }));
  await userEvent.click(screen.getByRole("button", { name: m.connectGo }));
  expect(await screen.findByText("odd screen")).toBeInTheDocument();
});

test("an expiring sign-in warns and offers to connect again", async () => {
  api({ ...CONNECTED, connection: "expiring" });
  renderAlone(<Account />);
  expect(await screen.findByText(/Your Claude sign-in expires/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: m.reconnect })).toBeInTheDocument();
});

test("an expired sign-in says so", async () => {
  api({ ...CONNECTED, connection: "expired" });
  renderAlone(<Account />);
  expect(await screen.findByText(m.claudeExpired)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: m.reconnect })).toBeInTheDocument();
});
