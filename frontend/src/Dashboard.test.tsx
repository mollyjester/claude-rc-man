import { cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { CONNECTION_STATES, START_REASON } from "./Claude";
import { ACTIONS, KIND, LABELS, PRIMARY } from "./Servers";
import { m } from "./messages";
import { CONNECTED, ME, type Routes, fakeApi, problem } from "./test/fakeApi";
import { renderApp } from "./test/render";

const entry = (path: string, has_children = false) => ({ name: path.split("/").pop(), path, has_children });

// A folder whose name holds what an address treats specially.
const ODD = "projects/gamma/a&b #c?d e";

// Each folder's contents, by its path; "" is the home folder.
const FOLDERS: Record<string, { path: string; entries: unknown[] }> = {
  "": { path: "", entries: [entry("projects", true), entry("proj")] },
  projects: {
    path: "projects",
    entries: [entry("projects/alpha"), entry("projects/beta"), entry("projects/gamma", true)],
  },
  "projects/gamma": { path: "projects/gamma", entries: [entry("projects/gamma/deep"), entry(ODD, true)] },
  [ODD]: { path: ODD, entries: [entry(`${ODD}/inner`)] },
};

function server(path: string, state: string, extra: Record<string, unknown> = {}) {
  return { path, state, environment_url: null, session_name: null, exit_code: null, ...extra };
}

function dashboard(servers: unknown[], extra: Routes = {}) {
  return fakeApi({
    "GET /api/me": ME,
    "GET /api/claude": CONNECTED,
    "GET /api/folders": (c) => FOLDERS[c.query.get("path") ?? ""] ?? problem(404, "no_such_folder", "No such folder."),
    "GET /api/servers": { servers },
    ...extra,
  });
}

const tree = async () => screen.findByRole("region", { name: m.foldersTitle });
const summary = async () => screen.findByRole("region", { name: m.yourServersTitle });
const panel = async (path: string) => screen.findByRole("region", { name: path });
const folder = async (name: string) => within(await tree()).findByRole("button", { name });
const folderOf = (search: string) => new URLSearchParams(search).get("folder");

async function expand(name: string) {
  await userEvent.click(await within(await tree()).findByRole("button", { name: m.expand(name) }));
}

async function select(name: string) {
  await userEvent.click(await folder(name));
}

// The card of the server in a folder, with that folder selected from the address.
async function card(path = "projects/alpha") {
  renderApp(`/?folder=${path}`);
  return within(await within(await panel(path)).findByRole("article", { name: path }));
}

function expectNoDot(button: HTMLElement) {
  expect(button).toHaveAccessibleDescription("");
  expect(button.closest("li")!.querySelector(":scope > .dot")).toBeNull();
}

test("selecting a folder opens its panel, with the breadcrumb and its server", async () => {
  const fake = dashboard([server("projects/alpha", "ready")]);
  renderApp();
  expect(await screen.findByRole("region", { name: m.home })).toHaveTextContent(m.panelEmpty);
  expect(fake.calls.filter((c) => c.path === "/api/folders" && c.query.get("path"))).toHaveLength(0);
  await expand("projects");
  await select("alpha");

  const opened = await panel("projects/alpha");
  const crumbs = within(opened).getByRole("navigation", { name: m.breadcrumb });
  expect(within(crumbs).getByRole("link", { name: m.home })).toHaveAttribute("href", "/");
  expect(within(crumbs).getByRole("link", { name: "projects" })).toHaveAttribute("href", "/?folder=projects");
  expect(within(crumbs).getByText("alpha")).toHaveAttribute("aria-current", "page");
  expect(within(crumbs).queryByRole("link", { name: "alpha" })).not.toBeInTheDocument();
  expect(within(opened).getByRole("heading", { name: m.serverTitle })).toBeInTheDocument();
  expect(within(opened).getByRole("article", { name: "projects/alpha" })).toBeInTheDocument();
  expect(await folder("alpha")).toHaveAttribute("aria-current", "true");
  expect(await folder("beta")).not.toHaveAttribute("aria-current");
  expect(screen.queryByRole("region", { name: m.home })).not.toBeInTheDocument();
});

test("a folder with no server offers Start, which starts one there", async () => {
  const servers: unknown[] = [];
  let answer = () => {};
  const fake = dashboard([], {
    "GET /api/servers": () => ({ servers }),
    "POST /api/servers": () =>
      new Promise((resolve) => {
        answer = () => {
          servers.push(server("projects/beta", "trust_prompt"));
          resolve({ server: servers[0] });
        };
      }),
  });
  renderApp();
  await expand("projects");
  await select("beta");
  const opened = await panel("projects/beta");
  const start = await within(opened).findByRole("button", { name: m.start });
  expect(start).toHaveClass("primary");
  await userEvent.click(start);
  await waitFor(() => expect(fake.made("POST", "/api/servers")).toHaveLength(1));
  expect(fake.made("POST", "/api/servers")[0].body).toEqual({ path: "projects/beta" });
  expect(start).toBeDisabled();
  answer();
  expect(await within(opened).findByRole("article", { name: "projects/beta" })).toBeInTheDocument();
  expect(within(opened).queryByRole("button", { name: m.start })).not.toBeInTheDocument();
});

const NOT_CONNECTED = { connection: "not_connected", account: null, login_expires_at: null };

test.each(CONNECTION_STATES)("with the Claude account %s, Start is offered only if a server can start", async (state) => {
  dashboard([], { "GET /api/claude": { ...CONNECTED, connection: state } });
  renderApp("/?folder=projects/beta");
  const opened = await panel("projects/beta");
  const reason = START_REASON[state];
  if (reason === null) {
    expect(await within(opened).findByRole("button", { name: m.start })).toBeInTheDocument();
  } else {
    expect(await within(opened).findByText(reason.start)).toBeInTheDocument();
    expect(within(opened).queryByRole("button", { name: m.start })).not.toBeInTheDocument();
  }
});

test("until the Claude account's state is known, neither Start nor a reason is shown", async () => {
  dashboard([], { "GET /api/claude": () => new Promise(() => {}) });
  renderApp("/?folder=projects/beta");
  const opened = await panel("projects/beta");
  expect(await within(await summary()).findByText(m.yourServersEmpty)).toBeInTheDocument(); // the servers are listed
  expect(within(opened).getByText(m.loading)).toBeInTheDocument();
  expect(within(opened).queryByRole("button", { name: m.start })).not.toBeInTheDocument();
  for (const reason of Object.values(START_REASON)) {
    if (reason) expect(within(opened).queryByText(reason.start)).not.toBeInTheDocument();
  }
});

test("once the account is connected on the page, Start is offered", async () => {
  let connected = false;
  dashboard([], {
    "GET /api/claude": () => (connected ? CONNECTED : NOT_CONNECTED),
    "POST /api/claude/login": { url: "https://claude.com/x" },
    "POST /api/claude/code": () => {
      connected = true;
      return CONNECTED;
    },
  });
  renderApp("/?folder=projects/beta");
  const opened = await panel("projects/beta");
  expect(await within(opened).findByText(m.notConnected.start)).toBeInTheDocument();
  await userEvent.click(screen.getByRole("button", { name: m.connect }));
  await userEvent.click(screen.getByRole("button", { name: m.connectGo }));
  await userEvent.type(await screen.findByLabelText(m.connectCode), "CODE");
  await userEvent.click(screen.getByRole("button", { name: m.connectSubmit }));
  expect(await within(opened).findByRole("button", { name: m.start })).toBeInTheDocument();
  expect(within(opened).queryByText(m.notConnected.start)).not.toBeInTheDocument();
});

test.each([
  ["stopped", "resume"],
  ["exited", "resume"],
  ["trust_declined", "startAgain"],
  ["logged_out", "startAgain"],
] as const)("a %s server offers %s only while the account is connected, and says why instead", async (state, action) => {
  const said = m.notConnected[action];
  dashboard([server("projects/alpha", state, { screen: ["x"], exit_code: 1 })], { "GET /api/claude": NOT_CONNECTED });
  const waiting = await card();
  expect(waiting.getByRole("button", { name: m.forget })).toBeInTheDocument(); // the card is there
  expect(waiting.queryByRole("button", { name: LABELS[action] })).not.toBeInTheDocument();
  expect(waiting.getByText(said)).toBeInTheDocument();
  expect(waiting.queryByText(m.nextStep[state])).not.toBeInTheDocument();
  cleanup();

  dashboard([server("projects/alpha", state, { screen: ["x"], exit_code: 1 })]);
  const offered = await card();
  expect(offered.getByRole("button", { name: LABELS[action] })).toBeInTheDocument();
  expect(offered.getByText(m.nextStep[state])).toBeInTheDocument();
  expect(offered.queryByText(said)).not.toBeInTheDocument();
});

test("a folder that is not there still opens, and Start shows the server's reason", async () => {
  dashboard([], { "POST /api/servers": problem(404, "no_such_folder", "No such folder.") });
  renderApp("/?folder=gone/away");
  const opened = await panel("gone/away");
  await userEvent.click(await within(opened).findByRole("button", { name: m.start }));
  expect(await within(opened).findByRole("alert")).toHaveTextContent("No such folder.");
});

test("the selected folder is kept in the address, which opens it again", async () => {
  dashboard([]);
  const { location } = renderApp("/?folder=projects/alpha");
  expect(await panel("projects/alpha")).toBeInTheDocument();
  await select("beta");
  expect(await panel("projects/beta")).toBeInTheDocument();
  expect(folderOf(location().search)).toBe("projects/beta");
});

test("an empty folder in the address is no folder", async () => {
  dashboard([]);
  renderApp("/?folder=");
  expect(await screen.findByRole("region", { name: m.home })).toBeInTheDocument();
});

test("the back button returns to the folder selected before, then to none", async () => {
  dashboard([]);
  const { location, navigate } = renderApp();
  await expand("projects");
  await select("alpha");
  await panel("projects/alpha");
  const entries = location().key;
  await select("alpha"); // selecting it again changes nothing
  expect(location().key).toBe(entries);
  await select("beta");
  await panel("projects/beta");

  await navigate(-1);
  expect(await panel("projects/alpha")).toBeInTheDocument();
  expect(folderOf(location().search)).toBe("projects/alpha");
  await navigate(-1);
  expect(await screen.findByRole("region", { name: m.home })).toBeInTheDocument();
  expect(location().search).toBe("");
});

test("the breadcrumb selects a folder above, and Home selects none", async () => {
  dashboard([]);
  const { location } = renderApp("/?folder=projects/alpha");
  const crumbs = within(await panel("projects/alpha")).getByRole("navigation", { name: m.breadcrumb });
  await userEvent.click(within(crumbs).getByRole("link", { name: "projects" }));
  expect(await panel("projects")).toBeInTheDocument();
  expect(folderOf(location().search)).toBe("projects");
  expect(await folder("projects")).toHaveAttribute("aria-current", "true");

  const above = within(await panel("projects")).getByRole("navigation", { name: m.breadcrumb });
  await userEvent.click(within(above).getByRole("link", { name: m.home }));
  expect(await screen.findByRole("region", { name: m.home })).toBeInTheDocument();
  expect(location().search).toBe("");
});

test("a folder selected from the address is shown in the tree", async () => {
  const fake = dashboard([]);
  renderApp("/?folder=projects/alpha");
  await waitFor(() =>
    expect(fake.made("GET", "/api/folders").map((c) => c.query.get("path"))).toContain("projects"),
  );
  expect(await folder("alpha")).toHaveAttribute("aria-current", "true");
  expect(within(await tree()).getByRole("button", { name: m.collapse("projects") })).toBeInTheDocument();
});

test("a folder whose name holds &, #, ? and spaces keeps its place in the address", async () => {
  const address = "/?folder=projects/gamma/a%26b%20%23c%3Fd%20e";
  dashboard([]);
  const { location } = renderApp(`${address}/inner`);
  const crumbs = within(await panel(`${ODD}/inner`)).getByRole("navigation", { name: m.breadcrumb });
  const link = within(crumbs).getByRole("link", { name: "a&b #c?d e" });
  expect(link).toHaveAttribute("href", address);
  await userEvent.click(link);
  expect(await panel(ODD)).toBeInTheDocument();
  expect(location().search).toBe(address.slice(1));
  cleanup();

  dashboard([]);
  renderApp(address); // as a reload would
  expect(await panel(ODD)).toBeInTheDocument();
  expect(await folder("a&b #c?d e")).toHaveAttribute("aria-current", "true");
});

test("until the servers are listed, the panel says it is loading", async () => {
  dashboard([], { "GET /api/servers": () => new Promise(() => {}) });
  renderApp("/?folder=projects/beta");
  const opened = await panel("projects/beta");
  expect(await screen.findByText(m.claudeConnectedAs("alice@claude.test"))).toBeInTheDocument(); // the account is known
  expect(within(opened).getByText(m.loading)).toBeInTheDocument();
  expect(within(opened).queryByRole("button", { name: m.start })).not.toBeInTheDocument();
});

test("a closed folder holding a server with a problem and one waiting shows the problem", async () => {
  dashboard([server("projects/alpha", "exited"), server("projects/beta", "trust_prompt")]);
  renderApp();
  expect(await folder("projects")).toHaveAccessibleDescription(m.kinds.problem);
});

test.each(Object.entries(KIND))("a folder whose server is %s carries the %s dot", async (state, kind) => {
  dashboard([server("projects/alpha", state)]);
  renderApp();
  await expand("projects");
  const alpha = await folder("alpha");
  expect(alpha).toHaveAccessibleDescription(m.kinds[kind]);
  // its shape is the kind's class, its meaning a title on hover, and the
  // description is read once, through the name it describes
  const dot = alpha.closest("li")!.querySelector(":scope > .dot")!;
  expect(dot).toHaveClass(`dot-${kind}`);
  expect(dot).toHaveAttribute("title", m.kinds[kind]);
  const description = document.getElementById(alpha.getAttribute("aria-describedby")!)!;
  expect(description).toHaveTextContent(m.kinds[kind]);
  expect(description).toHaveAttribute("hidden");
  expectNoDot(await folder("beta"));
});

test("the check for no dot fails on a folder that has one", async () => {
  dashboard([server("projects/beta", "ready")]);
  renderApp();
  await expand("projects");
  const beta = await folder("beta");
  expect(beta).toHaveAccessibleDescription(m.kinds.running); // the plant took effect
  expect(() => expectNoDot(beta)).toThrow();
});

test("a closed folder carries the most urgent dot of the servers inside it", async () => {
  dashboard([server("projects/alpha", "ready"), server("projects/beta", "trust_prompt")]);
  renderApp();
  expect(await folder("projects")).toHaveAccessibleDescription(m.kinds.waiting);
  expectNoDot(await folder("proj"));

  await expand("projects");
  expectNoDot(await folder("projects"));
  expect(await folder("alpha")).toHaveAccessibleDescription(m.kinds.running);
  expect(await folder("beta")).toHaveAccessibleDescription(m.kinds.waiting);
  expectNoDot(await folder("proj"));

  await userEvent.click(within(await tree()).getByRole("button", { name: m.collapse("projects") }));
  expect(await folder("projects")).toHaveAccessibleDescription(m.kinds.waiting);
  expectNoDot(await folder("proj"));
});

test("the summary lists every server, and selects its folder", async () => {
  const servers = [server("projects/alpha", "ready"), server("projects/gamma/deep", "exited")];
  const fake = dashboard(servers);
  const { location } = renderApp();
  const list = await summary();
  await within(list).findByRole("button", { name: "projects/alpha" });
  expect(within(list).getAllByRole("button")).toHaveLength(servers.length);
  for (const { path, state } of servers) {
    const item = within(list).getByRole("button", { name: path }).closest("li")!;
    expect(within(item as HTMLElement).getByText(m.states[state])).toHaveClass("badge");
  }

  expect(within(list).getByRole("button", { name: "projects/gamma/deep" })).not.toHaveAttribute("aria-current");
  await userEvent.click(within(list).getByRole("button", { name: "projects/gamma/deep" }));
  expect(within(await panel("projects/gamma/deep")).getByRole("article")).toBeInTheDocument();
  expect(folderOf(location().search)).toBe("projects/gamma/deep");
  expect(within(list).getByRole("button", { name: "projects/gamma/deep" })).toHaveAttribute("aria-current", "true");
  expect(within(list).getByRole("button", { name: "projects/alpha" })).not.toHaveAttribute("aria-current");
  await waitFor(() =>
    expect(fake.made("GET", "/api/folders").map((c) => c.query.get("path"))).toContain("projects/gamma"),
  );
  expect(await folder("deep")).toHaveAttribute("aria-current", "true");
});

test("with no servers, the summary says so", async () => {
  dashboard([]);
  renderApp();
  expect(await folder("projects")).toBeInTheDocument();
  const list = await summary();
  expect(await within(list).findByText(m.yourServersEmpty)).toBeInTheDocument();
  expect(within(list).queryAllByRole("button")).toEqual([]);
});

test("a server with a session attached is in use, and shows the session and a link to it", async () => {
  dashboard([
    server("projects/alpha", "ready", {
      session_name: "alice-quiet-otter",
      environment_url: "https://claude.ai/code?environment=env_1",
    }),
  ]);
  const found = await card();
  expect(found.getByText("In use")).toBeInTheDocument();
  expect(found.getByText("alice-quiet-otter")).toBeInTheDocument();
  expect(found.getByRole("link", { name: m.openSession })).toHaveAttribute(
    "href",
    "https://claude.ai/code?environment=env_1",
  );
});

test("a connected server with no session yet is ready to accept sessions and links to them", async () => {
  dashboard([
    server("projects/alpha", "connected", { environment_url: "https://claude.ai/code?environment=env_1" }),
  ]);
  const found = await card();
  expect(found.getByText("Ready to accept sessions")).toBeInTheDocument();
  expect(found.getByRole("link", { name: m.openSession })).toHaveAttribute(
    "href",
    "https://claude.ai/code?environment=env_1",
  );
});

test("a stopped server keeps its environment but offers no link to it", async () => {
  dashboard([
    server("projects/alpha", "stopped", { environment_url: "https://claude.ai/code?environment=env_1" }),
  ]);
  const found = await card();
  expect(found.getByText(m.states.stopped)).toBeInTheDocument();
  expect(found.queryByRole("link", { name: m.openSession })).not.toBeInTheDocument();
});

test.each(Object.entries(ACTIONS))("a %s server offers exactly its actions", async (state, actions) => {
  dashboard([server("projects/alpha", state, { screen: ["x"], exit_code: 1 })]);
  const found = await card();
  const offered = found.queryAllByRole("button").map((b) => b.textContent);
  expect(offered.sort()).toEqual(actions.map((a) => LABELS[a]).sort());
});

test.each(Object.keys(ACTIONS))("a %s server shows its state as a badge and says what to do", async (state) => {
  dashboard([server("projects/alpha", state, { screen: ["x"], exit_code: 1 })]);
  const found = await card();
  const badge = found.getByText(m.states[state], { exact: true });
  expect(badge).toHaveClass("badge", `badge-${KIND[state]}`);
  expect(found.getByText(m.nextStep[state])).toBeInTheDocument();
});

test("a state rcman does not know says to look at the screen", async () => {
  dashboard([server("projects/alpha", "something_new", { screen: ["x"] })]);
  const found = await card();
  expect(found.getByText("something_new")).toHaveClass("badge", "badge-problem");
  expect(found.getByText(m.nextStepUnknown)).toBeInTheDocument();
  expect(found.getByRole("button", { name: m.showScreen })).toHaveClass("secondary");
});

test.each(Object.keys(ACTIONS))("a %s server has one primary action, and Stop and Forget are secondary", async (state) => {
  dashboard([
    server("projects/alpha", state, { screen: ["x"], exit_code: 1, environment_url: "https://claude.ai/code?environment=env_1" }),
  ]);
  const found = await card();
  const primary = PRIMARY[state];
  const primaries = (await panel("projects/alpha")).querySelectorAll(".primary");
  if (primary === null) {
    expect(primaries).toHaveLength(0);
  } else {
    expect(primaries).toHaveLength(1);
    expect(primaries[0]).toHaveTextContent(primary === "openSession" ? m.openSession : LABELS[primary]);
  }
  for (const label of [m.stop, m.forget]) {
    const button = found.queryByRole("button", { name: label });
    if (button) expect(button).toHaveClass("secondary");
  }
  // non-vacuity: the buttons that are not primary are there, and secondary
  for (const button of found.getAllByRole("button")) {
    if (!button.classList.contains("primary")) expect(button).toHaveClass("secondary");
  }
});

test("answering the trust question sends the answer", async () => {
  const fake = dashboard([server("projects/alpha", "trust_prompt")], {
    "POST /api/servers/confirm": { server: server("projects/alpha", "connected") },
  });
  const found = await card();
  expect(found.getByText(m.trustTitle)).toBeInTheDocument();
  await userEvent.click(found.getByRole("button", { name: m.trustYes }));
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
  const found = await card();
  await userEvent.click(found.getByRole("button", { name: label }));
  await waitFor(() => expect(fake.made("POST", path)).toHaveLength(1));
  expect(fake.made("POST", path)[0].body).toEqual({ path: "projects/alpha" });
});

test("a server that stopped by itself says so, whatever its exit status, and can be resumed", async () => {
  for (const exit_code of [0, 1, 137]) {
    const fake = dashboard([server("projects/alpha", "exited", { exit_code, screen: ["x"] })], {
      "POST /api/servers/resume": { server: server("projects/alpha", "connected") },
    });
    const found = await card();
    expect(found.getByText("Stopped by itself")).toBeInTheDocument();
    await userEvent.click(found.getByRole("button", { name: m.resume }));
    await waitFor(() => expect(fake.made("POST", "/api/servers/resume")).toHaveLength(1));
    expect(fake.made("POST", "/api/servers/resume")[0].body).toEqual({ path: "projects/alpha" });
    cleanup();
  }
});

test("an unrecognised screen can be shown as it is", async () => {
  dashboard([server("projects/alpha", "unrecognised", { screen: ["strange line", "another"] })]);
  const found = await card();
  await userEvent.click(found.getByRole("button", { name: m.showScreen }));
  expect(found.getByText(/strange line\s+another/)).toBeInTheDocument();
});

test("a refused action shows the server's problem", async () => {
  dashboard([server("projects/alpha", "ready")], {
    "POST /api/servers/stop": problem(409, "not_running", "No server is running in this folder."),
  });
  const found = await card();
  await userEvent.click(found.getByRole("button", { name: m.stop }));
  expect(await found.findByRole("alert")).toHaveTextContent("No server is running in this folder.");
});
