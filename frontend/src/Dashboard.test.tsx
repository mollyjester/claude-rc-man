import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { ACTIONS, KIND, PRIMARY } from "./Servers";
import { m } from "./messages";
import { CONNECTED, ME, type Routes, fakeApi, problem } from "./test/fakeApi";
import { renderApp } from "./test/render";

const TREE = {
  path: "",
  entries: [{ name: "projects", path: "projects", has_children: true }],
};
const PROJECTS = {
  path: "projects",
  entries: [
    { name: "alpha", path: "projects/alpha", has_children: false },
    { name: "beta", path: "projects/beta", has_children: false },
  ],
};

function server(path: string, state: string, extra: Record<string, unknown> = {}) {
  return { path, state, environment_url: null, session_name: null, exit_code: null, ...extra };
}

function dashboard(servers: unknown[], extra: Routes = {}) {
  return fakeApi({
    "GET /api/me": ME,
    "GET /api/claude": CONNECTED,
    "GET /api/folders": (c) => (c.query.get("path") === "projects" ? PROJECTS : TREE),
    "GET /api/servers": { servers },
    ...extra,
  });
}

test("the folder tree loads lazily and offers start on folders without a server", async () => {
  const fake = dashboard([server("projects/beta", "ready")], {
    "POST /api/servers": { server: server("projects/alpha", "trust_prompt") },
  });
  renderApp();
  await userEvent.click(await screen.findByRole("button", { name: m.expand("projects") }));
  const alpha = await screen.findByRole("button", { name: m.startIn("projects/alpha") });
  expect(screen.queryByRole("button", { name: m.startIn("projects/beta") })).not.toBeInTheDocument();
  await userEvent.click(alpha);
  await waitFor(() => expect(fake.made("POST", "/api/servers")).toHaveLength(1));
  expect(fake.made("POST", "/api/servers")[0].body).toEqual({ path: "projects/alpha" });
});

test("a server with a session attached is in use, and shows the session and a link to it", async () => {
  dashboard([
    server("projects/alpha", "ready", {
      session_name: "alice-quiet-otter",
      environment_url: "https://claude.ai/code?environment=env_1",
    }),
  ]);
  renderApp();
  const card = await screen.findByRole("article", { name: "projects/alpha" });
  expect(within(card).getByText("In use")).toBeInTheDocument();
  expect(within(card).getByText("alice-quiet-otter")).toBeInTheDocument();
  expect(within(card).getByRole("link", { name: m.openSession })).toHaveAttribute(
    "href",
    "https://claude.ai/code?environment=env_1",
  );
});

test("a connected server with no session yet is ready to accept sessions and links to them", async () => {
  dashboard([
    server("projects/alpha", "connected", { environment_url: "https://claude.ai/code?environment=env_1" }),
  ]);
  renderApp();
  const card = await screen.findByRole("article", { name: "projects/alpha" });
  expect(within(card).getByText("Ready to accept sessions")).toBeInTheDocument();
  expect(within(card).getByRole("link", { name: m.openSession })).toHaveAttribute(
    "href",
    "https://claude.ai/code?environment=env_1",
  );
});

test("a stopped server keeps its environment but offers no link to it", async () => {
  dashboard([
    server("projects/alpha", "stopped", { environment_url: "https://claude.ai/code?environment=env_1" }),
  ]);
  renderApp();
  const card = await screen.findByRole("article", { name: "projects/alpha" });
  expect(within(card).getByText(m.states.stopped)).toBeInTheDocument();
  expect(within(card).queryByRole("link", { name: m.openSession })).not.toBeInTheDocument();
});

const LABELS: Record<string, string> = {
  stop: m.stop,
  resume: m.resume,
  startAgain: m.startAgain,
  forget: m.forget,
  showScreen: m.showScreen,
  trustYes: m.trustYes,
  trustNo: m.trustNo,
};

test.each(Object.entries(ACTIONS))("a %s server offers exactly its actions", async (state, actions) => {
  dashboard([server("projects/alpha", state, { screen: ["x"], exit_code: 1 })]);
  renderApp();
  const card = await screen.findByRole("article", { name: "projects/alpha" });
  const offered = within(card)
    .queryAllByRole("button")
    .map((b) => b.textContent);
  expect(offered.sort()).toEqual(actions.map((a) => LABELS[a]).sort());
});

test.each(Object.keys(ACTIONS))("a %s server shows its state as a badge and says what to do", async (state) => {
  dashboard([server("projects/alpha", state, { screen: ["x"], exit_code: 1 })]);
  renderApp();
  const card = await screen.findByRole("article", { name: "projects/alpha" });
  const badge = within(card).getByText(m.states[state], { exact: true });
  expect(badge).toHaveClass("badge", `badge-${KIND[state]}`);
  expect(within(card).getByText(m.nextStep[state])).toBeInTheDocument();
});

test("a state rcman does not know says to look at the screen", async () => {
  dashboard([server("projects/alpha", "something_new", { screen: ["x"] })]);
  renderApp();
  const card = await screen.findByRole("article", { name: "projects/alpha" });
  expect(within(card).getByText("something_new")).toHaveClass("badge", "badge-problem");
  expect(within(card).getByText(m.nextStepUnknown)).toBeInTheDocument();
  expect(within(card).getByRole("button", { name: m.showScreen })).toHaveClass("secondary");
});

test.each(Object.keys(ACTIONS))("a %s server has one primary action, and Stop and Forget are secondary", async (state) => {
  dashboard([
    server("projects/alpha", state, { screen: ["x"], exit_code: 1, environment_url: "https://claude.ai/code?environment=env_1" }),
  ]);
  renderApp();
  const card = await screen.findByRole("article", { name: "projects/alpha" });
  const primary = PRIMARY[state];
  const primaries = card.querySelectorAll(".primary");
  if (primary === null) {
    expect(primaries).toHaveLength(0);
  } else {
    expect(primaries).toHaveLength(1);
    expect(primaries[0]).toHaveTextContent(primary === "openSession" ? m.openSession : LABELS[primary]);
  }
  for (const label of [m.stop, m.forget]) {
    const button = within(card).queryByRole("button", { name: label });
    if (button) expect(button).toHaveClass("secondary");
  }
  // non-vacuity: the buttons that are not primary are there, and secondary
  for (const button of within(card).getAllByRole("button")) {
    if (!button.classList.contains("primary")) expect(button).toHaveClass("secondary");
  }
});

test("answering the trust question sends the answer", async () => {
  const fake = dashboard([server("projects/alpha", "trust_prompt")], {
    "POST /api/servers/confirm": { server: server("projects/alpha", "connected") },
  });
  renderApp();
  const card = await screen.findByRole("article", { name: "projects/alpha" });
  expect(within(card).getByText(m.trustTitle)).toBeInTheDocument();
  await userEvent.click(within(card).getByRole("button", { name: m.trustYes }));
  await waitFor(() => expect(fake.made("POST", "/api/servers/confirm")).toHaveLength(1));
  expect(fake.made("POST", "/api/servers/confirm")[0].body).toEqual({ path: "projects/alpha", answer: "y" });
});

test.each([
  ["stop", m.stop, "/api/servers/stop", "ready"],
  ["resume", m.resume, "/api/servers/resume", "stopped"],
  ["forget", m.forget, "/api/servers/forget", "stopped"],
  ["resume", m.resume, "/api/servers/resume", "exited"],
  ["start again", m.startAgain, "/api/servers", "trust_declined"],
])("%s calls its endpoint with the folder", async (_name, label, path, state) => {
  const fake = dashboard([server("projects/alpha", state)], { [`POST ${path}`]: { server: server("projects/alpha", state) } });
  renderApp();
  const card = await screen.findByRole("article", { name: "projects/alpha" });
  await userEvent.click(within(card).getByRole("button", { name: label }));
  await waitFor(() => expect(fake.made("POST", path)).toHaveLength(1));
  expect(fake.made("POST", path)[0].body).toEqual({ path: "projects/alpha" });
});

test("a server that stopped by itself says so, whatever its exit status, and can be resumed", async () => {
  for (const exit_code of [0, 1, 137]) {
    const fake = dashboard([server("projects/alpha", "exited", { exit_code, screen: ["x"] })], {
      "POST /api/servers/resume": { server: server("projects/alpha", "connected") },
    });
    const { unmount } = renderApp();
    const card = await screen.findByRole("article", { name: "projects/alpha" });
    expect(within(card).getByText("Stopped by itself")).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: m.resume }));
    await waitFor(() => expect(fake.made("POST", "/api/servers/resume")).toHaveLength(1));
    expect(fake.made("POST", "/api/servers/resume")[0].body).toEqual({ path: "projects/alpha" });
    unmount();
  }
});

test("an unrecognised screen can be shown as it is", async () => {
  dashboard([server("projects/alpha", "unrecognised", { screen: ["strange line", "another"] })]);
  renderApp();
  const card = await screen.findByRole("article", { name: "projects/alpha" });
  await userEvent.click(within(card).getByRole("button", { name: m.showScreen }));
  expect(within(card).getByText(/strange line\s+another/)).toBeInTheDocument();
});

test("a refused action shows the server's problem", async () => {
  dashboard([server("projects/alpha", "ready")], {
    "POST /api/servers/stop": problem(409, "not_running", "No server is running in this folder."),
  });
  renderApp();
  const card = await screen.findByRole("article", { name: "projects/alpha" });
  await userEvent.click(within(card).getByRole("button", { name: m.stop }));
  expect(await within(card).findByRole("alert")).toHaveTextContent("No server is running in this folder.");
});

test("an empty list says so", async () => {
  dashboard([]);
  renderApp();
  expect(await screen.findByText(m.serversEmpty)).toBeInTheDocument();
});
