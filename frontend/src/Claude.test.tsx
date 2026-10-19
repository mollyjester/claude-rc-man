import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ClaudePage } from "./Claude";
import { m } from "./messages";
import {
  CONNECTED,
  fakeApi,
  NOT_CONNECTED,
  problem,
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

test("the account's state is read once, as the page opens, never polled", async () => {
  const fake = api(CONNECTED);
  renderAlone(<Account />);
  expect(await accountTitle("alice@claude.test")).toBeInTheDocument();
  await new Promise((done) => setTimeout(done, 50));
  expect(fake.made("GET", "/api/claude")).toHaveLength(1);
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

test("signing out of Claude without keeping sessions stops them and signs out at once", async () => {
  const fake = api(CONNECTED, { "POST /api/claude/logout": NOT_CONNECTED });
  renderAlone(<Account />);
  await userEvent.click(await screen.findByRole("checkbox", { name: m.keepSessions }));
  await userEvent.click(screen.getByRole("button", { name: m.claudeSignOut }));
  await waitFor(() =>
    expect(fake.made("POST", "/api/claude/logout")[0].body).toEqual({ keep_sessions: false }),
  );
  expect(await screen.findByText(m.claudeNotConnected)).toBeInTheDocument();
});

test("a sign-in that disappeared on its own is said so, instead of the ordinary words, with the connect form offered", async () => {
  api({ ...NOT_CONNECTED, signed_out_unexpectedly: true });
  renderAlone(<Account />);
  expect(await screen.findByText(m.claudeSignedOutUnexpectedly)).toBeInTheDocument();
  expect(screen.queryByText(m.claudeNotConnected)).not.toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: m.connect }));
  expect(await screen.findByRole("button", { name: m.connectGo })).toBeInTheDocument();
});

test("an account that was never connected says the ordinary words, not the unexpected ones", async () => {
  api({ ...NOT_CONNECTED, signed_out_unexpectedly: false });
  renderAlone(<Account />);
  expect(await screen.findByText(m.claudeNotConnected)).toBeInTheDocument();
  expect(screen.queryByText(m.claudeSignedOutUnexpectedly)).not.toBeInTheDocument();
});

test("connecting answers with the account's state, and the words go once it is connected", async () => {
  api(
    { ...NOT_CONNECTED, signed_out_unexpectedly: true },
    {
      "POST /api/claude/login": { url: "https://claude.com/x" },
      "POST /api/claude/code": { ...CONNECTED, signed_out_unexpectedly: false },
    },
  );
  renderAlone(<Account />);
  await userEvent.click(await screen.findByRole("button", { name: m.connect }));
  await userEvent.click(screen.getByRole("button", { name: m.connectGo }));
  await userEvent.type(await screen.findByLabelText(m.connectCode), "abc");
  await userEvent.click(screen.getByRole("button", { name: m.connectSubmit }));
  expect(await accountTitle("alice@claude.test")).toBeInTheDocument();
  expect(screen.queryByText(m.claudeSignedOutUnexpectedly)).not.toBeInTheDocument();
});

test("an account not connected is offered no sign-out", async () => {
  api(NOT_CONNECTED);
  renderAlone(<Account />);
  expect(await screen.findByText(m.claudeNotConnected)).toBeInTheDocument();
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

test("signing out of Claude, and finishing a sign-out at once, are drawn as what they are: danger", async () => {
  api(CONNECTED);
  const { unmount } = renderAlone(<Account />);
  expect(await screen.findByRole("button", { name: m.claudeSignOut })).toHaveClass("danger");
  unmount();
  api(SIGNING_OUT);
  renderAlone(<Account />);
  expect(await screen.findByRole("button", { name: m.claudeSignOutNow })).toHaveClass("danger");
});

// --- the sheet -------------------------------------------------------------------------

test("the account is a sheet in a card: its address as the title, its state as a pill, its facts as rows", async () => {
  api(CONNECTED);
  renderAlone(<Account />);
  const title = await accountTitle("alice@claude.test");
  const sheet = title.closest("section")!;
  expect(sheet).toHaveClass("card", "claude-sheet");
  expect(sheet).toHaveAccessibleName("alice@claude.test");
  expect(pill()).toHaveTextContent(m.claudeStates.connected);
  expect(pill()).toHaveClass("badge", "badge-running");
  expect(fact(m.claudeOrg)).toHaveTextContent("Org");
  expect(fact(m.claudePlan)).toHaveTextContent("max");
  expect(fact(m.claudeExpiresAt)).toHaveTextContent(
    new Date(CONNECTED.login_expires_at).toLocaleString(),
  );
  // connected and nothing to say: no message bar, no offer to connect
  expect(sheet.querySelector(".message")).toBeNull();
  expect(screen.queryByRole("button", { name: m.reconnect })).not.toBeInTheDocument();
  // the page's title is the page-head's, as on every page
  expect(screen.getByRole("heading", { level: 1, name: m.claudeTitle }).parentElement).toHaveClass(
    "page-head",
  );
});

test("an account not connected has no facts, says so in an info bar, and offers to connect as the sheet's one primary action", async () => {
  api(NOT_CONNECTED);
  renderAlone(<Account />);
  const title = await accountTitle(m.claudeNoAccount);
  const sheet = title.closest("section")!;
  expect(sheet.querySelector("dl")).toBeNull();
  expect(within(sheet).getByText(m.claudeNotConnected).closest(".message")).toHaveClass(
    "message-info",
  );
  expect(within(sheet).getByRole("button", { name: m.connect })).toHaveClass("primary");
});

test.each([
  ["expiring", { ...CONNECTED, connection: "expiring" }, "badge-waiting", "message-warning"],
  ["expired", { ...CONNECTED, connection: "expired" }, "badge-problem", "message-problem"],
  ["signing out", SIGNING_OUT, "badge-waiting", "message-warning"],
] as const)(
  "an account %s says so as its pill and in a message bar of its kind",
  async (_, reply, badge, bar) => {
    api(reply);
    renderAlone(<Account />);
    await waitFor(() => expect(pill()).not.toBeNull());
    expect(pill()).toHaveClass("badge", badge);
    const bars = document.querySelectorAll(".claude-sheet .message");
    expect(bars).toHaveLength(1);
    expect(bars[0]).toHaveClass(bar);
  },
);

test("an account signed out on its own says so in a warning's message bar, with no pill", async () => {
  api({ ...NOT_CONNECTED, signed_out_unexpectedly: true });
  renderAlone(<Account />);
  const title = await accountTitle(m.claudeNoAccount);
  const sheet = title.closest("section")!;
  const bars = sheet.querySelectorAll(".message");
  expect(bars).toHaveLength(1);
  expect(bars[0]).toHaveClass("message-warning");
  expect(bars[0]).toHaveTextContent(m.claudeSignedOutUnexpectedly);
  expect(pill()).toBeNull();
});

test("the sign-out is a section of its own at the sheet's foot, after the facts, with its one line", async () => {
  api(CONNECTED);
  renderAlone(<Account />);
  const zone = await screen.findByRole("region", { name: m.claudeSignOutTitle });
  expect(zone).toHaveClass("danger-zone");
  expect(within(zone).getByText(m.claudeSignOutLine)).toBeInTheDocument();
  expect(within(zone).getByRole("checkbox", { name: m.keepSessions })).toBeChecked();
  expect(within(zone).getByRole("button", { name: m.claudeSignOut })).toHaveClass("danger");
  const sheet = zone.closest(".claude-sheet")!;
  expect(sheet.lastElementChild).toBe(zone);
  const facts = sheet.querySelector("dl")!;
  expect(facts.compareDocumentPosition(zone) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test("while signing out, the section at the sheet's foot offers to finish at once", async () => {
  api(SIGNING_OUT);
  renderAlone(<Account />);
  const zone = await screen.findByRole("region", { name: m.claudeSignOutTitle });
  expect(within(zone).getByRole("button", { name: m.claudeSignOutNow })).toHaveClass("danger");
  expect(within(zone).queryByRole("button", { name: m.claudeSignOut })).not.toBeInTheDocument();
});

test("a sign-out that waits keeps its section when the sign-in has meanwhile gone", async () => {
  api({ ...NOT_CONNECTED, signing_out: true });
  renderAlone(<Account />);
  const zone = await screen.findByRole("region", { name: m.claudeSignOutTitle });
  expect(within(zone).getByRole("button", { name: m.claudeSignOutNow })).toHaveClass("danger");
});

test("an account not connected has no pill: its title says so", async () => {
  api(NOT_CONNECTED);
  renderAlone(<Account />);
  await accountTitle(m.claudeNoAccount);
  expect(pill()).toBeNull();
});

test("a fact Claude did not give is said to be unknown in words of the Claude page's own", async () => {
  api({ ...CONNECTED, account: { ...CONNECTED.account, org_name: null } });
  renderAlone(<Account />);
  await accountTitle("alice@claude.test");
  expect(fact(m.claudeOrg)).toHaveTextContent(m.claudeUnknown);
});

test("the Claude address to connect is asked for by a short label with one line of help under it", async () => {
  api(NOT_CONNECTED, { "POST /api/claude/login": { url: "https://claude.com/x" } });
  renderAlone(<Account />);
  await userEvent.click(await screen.findByRole("button", { name: m.connect }));
  const email = screen.getByLabelText(m.connectEmail);
  expect(email).toHaveAccessibleDescription(m.connectEmailHelp);
  expect(m.connectEmail).not.toMatch(/\(/); // the help is not in the label
});
