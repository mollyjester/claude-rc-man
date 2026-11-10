import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ClaudePage } from "./Claude";
import { m } from "./messages";
import {
  CONNECTED,
  fakeApi,
  NOT_CONNECTED,
  type Reply,
  type Routes,
  SIGNING_OUT,
} from "./test/fakeApi";
import { renderAlone } from "./test/render";

// The Claude page: the account's state, read once as the page opens.
const Account = ClaudePage;

// The sheet's title: the account's address, or that none is connected.
const accountTitle = (name: string) => screen.findByRole("heading", { level: 2, name });
// The account's state, as a pill in the sheet's head.
const pill = () => document.querySelector(".claude-sheet .sheet-head .badge");
// What the sheet says of a fact, by its name.
const fact = (term: string) => screen.getByText(term, { selector: "dt" }).nextElementSibling;

function api(claude: Reply, extra: Routes = {}) {
  return fakeApi({
    "GET /api/claude": claude,
    ...extra,
  });
}

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
  expect(await accountTitle("alice@claude.test")).toBeInTheDocument();
  expect(pill()).toHaveTextContent(m.claudeStates.connected);
});

test("signing out of Claude keeps sessions running unless asked otherwise", async () => {
  const fake = api(CONNECTED, { "POST /api/claude/logout": SIGNING_OUT });
  renderAlone(<Account />);
  const keep = await screen.findByRole("checkbox", { name: m.keepSessions });
  expect(keep).toBeChecked();
  await userEvent.click(screen.getByRole("button", { name: m.claudeSignOut }));
  await waitFor(() =>
    expect(fake.made("POST", "/api/claude/logout")[0].body).toEqual({ keep_sessions: true }),
  );
  expect(await screen.findByText(m.claudeSigningOut)).toBeInTheDocument();
  // No new server can start meanwhile; connecting again ends the wait.
  expect(screen.getByRole("button", { name: m.reconnect })).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: m.claudeSignOut })).not.toBeInTheDocument();
});

test("a sign-out that waits can be finished at once, stopping the servers", async () => {
  const fake = api(SIGNING_OUT, { "POST /api/claude/logout": NOT_CONNECTED });
  renderAlone(<Account />);
  expect(await screen.findByText(m.claudeSigningOut)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: m.claudeSignOutNow }));
  await waitFor(() =>
    expect(fake.made("POST", "/api/claude/logout")[0].body).toEqual({ keep_sessions: false }),
  );
  expect(await screen.findByText(m.claudeNotConnected)).toBeInTheDocument();
});

// --- the sheet -------------------------------------------------------------------------

test("a connected account is shown by its address, its state and its facts", async () => {
  api(CONNECTED);
  renderAlone(<Account />);
  const title = await accountTitle("alice@claude.test");
  const sheet = title.closest("section")!;
  expect(sheet).toHaveTextContent(m.claudeStates.connected);
  expect(fact(m.claudeOrg)).toHaveTextContent("Org");
  expect(fact(m.claudePlan)).toHaveTextContent("max");
  expect(fact(m.claudeExpiresAt)).toHaveTextContent(
    new Date(CONNECTED.login_expires_at).toLocaleString(),
  );
  // connected: no offer to connect again
  expect(screen.queryByRole("button", { name: m.reconnect })).not.toBeInTheDocument();
});
