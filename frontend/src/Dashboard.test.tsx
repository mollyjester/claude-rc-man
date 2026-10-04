import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { CONNECTION_STATES, START_REASON } from "./Claude";
import { m } from "./messages";
import { ACTIONS, KIND, LABELS, PRIMARY } from "./Servers";
import {
  type Call,
  CONNECTED,
  fakeApi,
  ME,
  NOT_CONNECTED,
  problem,
  type Routes,
  UNREACHABLE_REPLY,
} from "./test/fakeApi";
import { renderApp } from "./test/render";

const entry = (path: string, has_children = false) => ({
  name: path.split("/").pop(),
  path,
  has_children,
});

// A folder whose name holds what an address treats specially.
const ODD = "projects/gamma/a&b #c?d e";

// Each folder's contents, by its path; "" is the home folder.
const FOLDERS: Record<string, { path: string; entries: unknown[] }> = {
  "": { path: "", entries: [entry("projects", true), entry("proj")] },
  projects: {
    path: "projects",
    entries: [entry("projects/alpha"), entry("projects/beta"), entry("projects/gamma", true)],
  },
  "projects/gamma": {
    path: "projects/gamma",
    entries: [entry("projects/gamma/deep"), entry(ODD, true)],
  },
  [ODD]: { path: ODD, entries: [entry(`${ODD}/inner`)] },
};

function server(path: string, state: string, extra: Record<string, unknown> = {}) {
  return { path, state, environment_url: null, session_name: null, exit_code: null, ...extra };
}

function dashboard(servers: unknown[], extra: Routes = {}) {
  return fakeApi({
    "GET /api/me": ME,
    "GET /api/claude": CONNECTED,
    "GET /api/folders": (c) =>
      FOLDERS[c.query.get("path") ?? ""] ?? problem(404, "no_such_folder", "No such folder."),
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
  expect(fake.calls.filter((c) => c.path === "/api/folders" && c.query.get("path"))).toHaveLength(
    0,
  );
  await expand("projects");
  await select("alpha");

  const opened = await panel("projects/alpha");
  const crumbs = within(opened).getByRole("navigation", { name: m.breadcrumb });
  expect(within(crumbs).getByRole("link", { name: m.home })).toHaveAttribute("href", "/");
  expect(within(crumbs).getByRole("link", { name: "projects" })).toHaveAttribute(
    "href",
    "/?folder=projects",
  );
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
  expect(within(opened).getByText(m.noServer)).toBeInTheDocument();
  await userEvent.click(start);
  await waitFor(() => expect(fake.made("POST", "/api/servers")).toHaveLength(1));
  expect(fake.made("POST", "/api/servers")[0].body).toEqual({ path: "projects/beta" });
  expect(start).toBeDisabled();
  answer();
  expect(await within(opened).findByRole("article", { name: "projects/beta" })).toBeInTheDocument();
  expect(within(opened).queryByRole("button", { name: m.start })).not.toBeInTheDocument();
});

test.each(CONNECTION_STATES)(
  "with the Claude account %s, Start is offered only if a server can start",
  async (state) => {
    dashboard([], { "GET /api/claude": { ...CONNECTED, connection: state } });
    renderApp("/?folder=projects/beta");
    const opened = await panel("projects/beta");
    const reason = START_REASON[state];
    if (reason === null) {
      expect(await within(opened).findByRole("button", { name: m.start })).toBeInTheDocument();
    } else {
      expect(await within(opened).findByText(reason.start)).toBeInTheDocument();
      expect(within(opened).getByText(m.noServer)).toBeInTheDocument();
      expect(within(opened).queryByRole("button", { name: m.start })).not.toBeInTheDocument();
    }
  },
);

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
] as const)(
  "a %s server offers %s only while the account is connected, and says why instead",
  async (state, action) => {
    const said = m.notConnected[action];
    dashboard([server("projects/alpha", state, { screen: ["x"], exit_code: 1 })], {
      "GET /api/claude": NOT_CONNECTED,
    });
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
  },
);

test("a state allowing more than one action that starts Claude offers none of them while it cannot", async () => {
  // No state rc-helper reports allows two such actions today; one planted here would.
  ACTIONS.planted = ["resume", "startAgain", "forget"];
  try {
    dashboard([server("projects/alpha", "planted")], { "GET /api/claude": NOT_CONNECTED });
    const waiting = await card();
    expect(waiting.getByRole("button", { name: m.forget })).toBeInTheDocument(); // the card is there
    expect(waiting.queryByRole("button", { name: m.resume })).not.toBeInTheDocument();
    expect(waiting.queryByRole("button", { name: m.startAgain })).not.toBeInTheDocument();
    // the sentence gives the reason for each of them
    const said = waiting.getByText(new RegExp(m.notConnected.resume));
    expect(said).toHaveTextContent(m.notConnected.startAgain);
  } finally {
    delete ACTIONS.planted;
  }
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
  const crumbs = within(await panel("projects/alpha")).getByRole("navigation", {
    name: m.breadcrumb,
  });
  await userEvent.click(within(crumbs).getByRole("link", { name: "projects" }));
  expect(await panel("projects")).toBeInTheDocument();
  expect(folderOf(location().search)).toBe("projects");
  expect(await folder("projects")).toHaveAttribute("aria-current", "true");

  const above = within(await panel("projects")).getByRole("navigation", { name: m.breadcrumb });
  await userEvent.click(within(above).getByRole("link", { name: m.home }));
  expect(await screen.findByRole("region", { name: m.home })).toBeInTheDocument();
  expect(location().search).toBe("");
});

test.each([
  ["projects/alpha/", ["projects"], "alpha"],
  ["/projects", [], "projects"],
  ["projects//alpha", ["projects"], "alpha"],
])("the breadcrumb and heading of %s leave out its empty parts", async (path, above, name) => {
  dashboard([]);
  renderApp(`/?folder=${path}`);
  const opened = await panel(path);
  const crumbs = within(opened).getByRole("navigation", { name: m.breadcrumb });
  // each step but the last ends with the slash after it
  expect(
    within(crumbs)
      .getAllByRole("listitem")
      .map((li) => li.textContent),
  ).toEqual([`${m.home}/`, ...above.map((folder) => `${folder}/`), name]);
  expect(within(opened).getByRole("heading", { level: 2 })).toHaveTextContent(name);
});

test("a folder that is only a slash still opens, under Home", async () => {
  dashboard([]);
  renderApp("/?folder=/");
  const opened = await panel("/");
  const crumbs = within(opened).getByRole("navigation", { name: m.breadcrumb });
  expect(
    within(crumbs)
      .getAllByRole("listitem")
      .map((li) => li.textContent),
  ).toEqual([m.home]);
  expect(within(opened).getByRole("heading", { level: 2 })).toHaveTextContent("/");
});

test("a folder whose contents could not be fetched is asked again when reopened, not when closed", async () => {
  let failing = true;
  const fake = dashboard([], {
    "GET /api/folders": (c) => {
      const path = c.query.get("path") ?? "";
      if (path === "projects" && failing)
        return problem(500, "helper_failed", "rcman's helper failed.");
      return FOLDERS[path];
    },
  });
  const asked = () =>
    fake.made("GET", "/api/folders").filter((c) => c.query.get("path") === "projects").length;
  renderApp();
  await expand("projects");
  expect(await within(await tree()).findByRole("alert")).toHaveTextContent(
    "rcman's helper failed.",
  );
  expect(asked()).toBe(1);

  failing = false;
  await userEvent.click(within(await tree()).getByRole("button", { name: m.collapse("projects") }));
  expect(within(await tree()).queryByRole("alert")).not.toBeInTheDocument();
  expect(asked()).toBe(1);
  await expand("projects");
  expect(await folder("alpha")).toBeInTheDocument();
  expect(asked()).toBe(2);
});

test("a folder selected from the address is shown in the tree", async () => {
  const fake = dashboard([]);
  renderApp("/?folder=projects/alpha");
  await waitFor(() =>
    expect(fake.made("GET", "/api/folders").map((c) => c.query.get("path"))).toContain("projects"),
  );
  expect(await folder("alpha")).toHaveAttribute("aria-current", "true");
  expect(
    within(await tree()).getByRole("button", { name: m.collapse("projects") }),
  ).toBeInTheDocument();
});

test("a folder whose name holds &, #, ? and spaces keeps its place in the address", async () => {
  const address = "/?folder=projects/gamma/a%26b%20%23c%3Fd%20e";
  dashboard([]);
  const { location } = renderApp(`${address}/inner`);
  const crumbs = within(await panel(`${ODD}/inner`)).getByRole("navigation", {
    name: m.breadcrumb,
  });
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

test.each(Object.entries(KIND))(
  "a folder whose server is %s carries the %s dot",
  async (state, kind) => {
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
  },
);

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
    // a narrow line breaks the path after a slash, never inside a name
    const button = within(list).getByRole("button", { name: path });
    // each folder's name with the slash after it is one part; e2e/paths.spec.ts
    // checks in a browser that the parts are never broken inside
    const parts = [...button.querySelectorAll(".path-part")].map((part) => part.textContent);
    expect(parts).toEqual(
      path.split("/").map((name, i, names) => (i < names.length - 1 ? `${name}/` : name)),
    );
    expect(button).toHaveAccessibleName(path);
    const item = button.closest("li")!;
    expect(within(item as HTMLElement).getByText(m.states[state])).toHaveClass("badge");
  }

  expect(within(list).getByRole("button", { name: "projects/gamma/deep" })).not.toHaveAttribute(
    "aria-current",
  );
  await userEvent.click(within(list).getByRole("button", { name: "projects/gamma/deep" }));
  expect(within(await panel("projects/gamma/deep")).getByRole("article")).toBeInTheDocument();
  expect(folderOf(location().search)).toBe("projects/gamma/deep");
  expect(within(list).getByRole("button", { name: "projects/gamma/deep" })).toHaveAttribute(
    "aria-current",
    "true",
  );
  expect(within(list).getByRole("button", { name: "projects/alpha" })).not.toHaveAttribute(
    "aria-current",
  );
  await waitFor(() =>
    expect(fake.made("GET", "/api/folders").map((c) => c.query.get("path"))).toContain(
      "projects/gamma",
    ),
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
  // the link is among the card's actions, beside Stop
  const actions = found.getByRole("button", { name: m.stop }).parentElement!;
  expect(actions).toHaveClass("actions");
  expect(actions).toContainElement(found.getByRole("link", { name: m.openSession }));
  expect(found.getByRole("link", { name: m.openSession })).toHaveAttribute(
    "href",
    "https://claude.ai/code?environment=env_1",
  );
});

test("a connected server with no session yet is ready to accept sessions and links to them", async () => {
  dashboard([
    server("projects/alpha", "connected", {
      environment_url: "https://claude.ai/code?environment=env_1",
    }),
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
    server("projects/alpha", "stopped", {
      environment_url: "https://claude.ai/code?environment=env_1",
    }),
  ]);
  const found = await card();
  expect(found.getByText(m.states.stopped)).toBeInTheDocument();
  expect(found.queryByRole("link", { name: m.openSession })).not.toBeInTheDocument();
});

test.each(Object.entries(ACTIONS))(
  "a %s server offers exactly its actions",
  async (state, actions) => {
    dashboard([server("projects/alpha", state, { screen: ["x"], exit_code: 1 })]);
    const found = await card();
    const offered = found.queryAllByRole("button").map((b) => b.textContent);
    expect(offered.sort()).toEqual(actions.map((a) => LABELS[a]).sort());
  },
);

test.each(Object.keys(ACTIONS))(
  "a %s server shows its state as a badge and says what to do",
  async (state) => {
    dashboard([server("projects/alpha", state, { screen: ["x"], exit_code: 1 })]);
    const found = await card();
    const badge = found.getByText(m.states[state], { exact: true });
    expect(badge).toHaveClass("badge", `badge-${KIND[state]}`);
    expect(found.getByText(m.nextStep[state])).toBeInTheDocument();
  },
);

test("a state rcman does not know says to look at the screen", async () => {
  dashboard([server("projects/alpha", "something_new", { screen: ["x"] })]);
  const found = await card();
  expect(found.getByText("something_new")).toHaveClass("badge", "badge-problem");
  expect(found.getByText(m.nextStepUnknown)).toBeInTheDocument();
  expect(found.getByRole("button", { name: m.showScreen })).toHaveClass("secondary");
});

test.each(Object.keys(ACTIONS))(
  "a %s server has one primary action, and Stop and Forget are secondary",
  async (state) => {
    dashboard([
      server("projects/alpha", state, {
        screen: ["x"],
        exit_code: 1,
        environment_url: "https://claude.ai/code?environment=env_1",
      }),
    ]);
    const found = await card();
    const primary = PRIMARY[state];
    const primaries = (await panel("projects/alpha")).querySelectorAll(".primary");
    if (primary === null) {
      expect(primaries).toHaveLength(0);
    } else {
      expect(primaries).toHaveLength(1);
      expect(primaries[0]).toHaveTextContent(
        primary === "openSession" ? m.openSession : LABELS[primary],
      );
    }
    for (const label of [m.stop, m.forget]) {
      const button = found.queryByRole("button", { name: label });
      if (button) expect(button).toHaveClass("secondary");
    }
    // non-vacuity: the buttons that are not primary are there, and secondary
    for (const button of found.getAllByRole("button")) {
      if (!button.classList.contains("primary")) expect(button).toHaveClass("secondary");
    }
  },
);

test("answering the trust question sends the answer", async () => {
  const fake = dashboard([server("projects/alpha", "trust_prompt")], {
    "POST /api/servers/confirm": { server: server("projects/alpha", "connected") },
  });
  const found = await card();
  expect(found.getByText(m.trustTitle)).toBeInTheDocument();
  await userEvent.click(found.getByRole("button", { name: m.trustYes }));
  await waitFor(() => expect(fake.made("POST", "/api/servers/confirm")).toHaveLength(1));
  expect(fake.made("POST", "/api/servers/confirm")[0].body).toEqual({
    path: "projects/alpha",
    answer: "y",
  });
});

test.each([
  ["stop", m.stop, "/api/servers/stop", "ready"],
  ["resume", m.resume, "/api/servers/resume", "stopped"],
  ["forget", m.forget, "/api/servers/forget", "stopped"],
  ["resume", m.resume, "/api/servers/resume", "exited"],
  ["start again", m.startAgain, "/api/servers", "trust_declined"],
])("%s calls its endpoint with the folder", async (_name, label, path, state) => {
  const fake = dashboard([server("projects/alpha", state)], {
    [`POST ${path}`]: { server: server("projects/alpha", state) },
  });
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

test("Show screen is a disclosure, which hides the screen again", async () => {
  dashboard([server("projects/alpha", "exited", { exit_code: 1, screen: ["last words"] })]);
  const found = await card();
  const disclosure = found.getByRole("button", { name: m.showScreen });
  expect(disclosure).not.toHaveAttribute("aria-pressed");
  expect(disclosure.querySelector(".caret")).toHaveAttribute("aria-hidden", "true");
  const screenOf = () => document.getElementById(disclosure.getAttribute("aria-controls")!);
  expect(disclosure).toHaveAttribute("aria-expanded", "false");
  expect(screenOf()).not.toBeVisible();

  await userEvent.click(disclosure);
  expect(disclosure).toHaveAttribute("aria-expanded", "true");
  expect(disclosure).toHaveAccessibleName(m.showScreen); // the name stays; its state says what it does
  expect(screenOf()).toBeVisible();
  expect(screenOf()).toHaveTextContent("last words");
  // a figure, so its name reaches assistive technology, which a pre's own name does not
  expect(found.getByRole("figure", { name: m.screenTitle("projects/alpha") })).toBe(screenOf());

  await userEvent.click(disclosure);
  expect(disclosure).toHaveAttribute("aria-expanded", "false");
  expect(disclosure).toHaveAccessibleName(m.showScreen);
  expect(screenOf()).not.toBeVisible();
});

test("a refused action shows the server's problem", async () => {
  dashboard([server("projects/alpha", "ready")], {
    "POST /api/servers/stop": problem(409, "not_running", "No server is running in this folder."),
  });
  const found = await card();
  await userEvent.click(found.getByRole("button", { name: m.stop }));
  expect(await found.findByRole("alert")).toHaveTextContent("No server is running in this folder.");
});

// --- new and delete folders -----------------------------------------------------------

// A home whose folders change as they are made and deleted, starting as FOLDERS.
function changingFolders() {
  const folders: Record<string, { path: string; entries: ReturnType<typeof entry>[] }> =
    structuredClone(FOLDERS) as never;
  const parentOf = (path: string) =>
    path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
  const routes: Routes = {
    "GET /api/folders": (c) =>
      folders[c.query.get("path") ?? ""] ??
      problem(404, "path_not_found", "The folder does not exist."),
    "POST /api/folders": (c) => {
      const { parent, name } = c.body as { parent: string; name: string };
      const created = parent ? `${parent}/${name}` : name;
      folders[parent] ??= { path: parent, entries: [] };
      folders[parent].entries.push(entry(created));
      folders[created] = { path: created, entries: [] };
      const above = folders[parentOf(parent)];
      if (parent && above)
        above.entries = above.entries.map((e) =>
          e.path === parent ? { ...e, has_children: true } : e,
        );
      return { created };
    },
    "DELETE /api/folders": (c) => {
      const path = c.query.get("path")!;
      const parent = parentOf(path);
      folders[parent].entries = folders[parent].entries.filter((e) => e.path !== path);
      delete folders[path];
      return { deleted: path, link: false, stopped: [], forgotten: [] };
    },
  };
  return routes;
}

const asked = (fake: ReturnType<typeof fakeApi>, path: string) =>
  fake.made("GET", "/api/folders").filter((c) => (c.query.get("path") ?? "") === path).length;

test("the Home panel offers New folder, and no Delete folder", async () => {
  dashboard([]);
  renderApp();
  const home = await screen.findByRole("region", { name: m.home });
  expect(within(home).getByRole("button", { name: m.newFolder })).toBeInTheDocument();
  expect(within(home).queryByRole("button", { name: m.deleteFolder })).not.toBeInTheDocument();
});

test("a folder's panel has New folder and Delete folder in its header, and the tree has neither", async () => {
  dashboard([server("projects/alpha", "ready")]);
  renderApp("/?folder=projects/alpha");
  const opened = await panel("projects/alpha");
  const newFolder = await within(opened).findByRole("button", { name: m.newFolder });
  const deleteFolder = await within(opened).findByRole("button", { name: m.deleteFolder });
  expect(newFolder).toHaveClass("secondary");
  expect(deleteFolder).toHaveClass("secondary-danger");
  // in the header: before the Claude server section, and outside the server's card
  const section = within(opened).getByRole("heading", { name: m.serverTitle });
  for (const button of [newFolder, deleteFolder]) {
    expect(button.compareDocumentPosition(section) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(button).not.toHaveClass("primary");
    expect(button).not.toHaveClass("danger"); // a form's lead only: this one just opens the dialog
  }
  expect(within(await tree()).getByRole("button", { name: "alpha" })).toBeInTheDocument(); // the tree is there
  expect(within(await tree()).queryByRole("button", { name: m.newFolder })).not.toBeInTheDocument();
  expect(
    within(await tree()).queryByRole("button", { name: m.deleteFolder }),
  ).not.toBeInTheDocument();
});

test("New folder in Home makes the folder there and selects it", async () => {
  const fake = dashboard([], changingFolders());
  const { location } = renderApp();
  await folder("projects");
  const home = await screen.findByRole("region", { name: m.home });
  await userEvent.click(within(home).getByRole("button", { name: m.newFolder }));
  await userEvent.type(within(home).getByLabelText(m.newFolderName), "fresh");
  await userEvent.click(within(home).getByRole("button", { name: m.create }));

  expect(await panel("fresh")).toBeInTheDocument();
  expect(fake.made("POST", "/api/folders")[0].body).toEqual({ parent: "", name: "fresh" });
  expect(folderOf(location().search)).toBe("fresh");
  expect(await folder("fresh")).toHaveAttribute("aria-current", "true");
  expect(asked(fake, "")).toBe(2);
});

test("New folder in a folder makes it there, selects it, and the folder shows it has folders now", async () => {
  const fake = dashboard([], changingFolders());
  const { location, navigate } = renderApp("/?folder=projects/alpha");
  const opened = await panel("projects/alpha");
  await folder("alpha");
  expect(
    within(await tree()).queryByRole("button", { name: m.expand("alpha") }),
  ).not.toBeInTheDocument();
  await userEvent.click(within(opened).getByRole("button", { name: m.newFolder }));
  await userEvent.type(within(opened).getByLabelText(m.newFolderName), "inner");
  await userEvent.click(within(opened).getByRole("button", { name: m.create }));

  expect(await panel("projects/alpha/inner")).toBeInTheDocument();
  expect(fake.made("POST", "/api/folders")[0].body).toEqual({
    parent: "projects/alpha",
    name: "inner",
  });
  expect(folderOf(location().search)).toBe("projects/alpha/inner");
  expect(await folder("inner")).toHaveAttribute("aria-current", "true");
  // the folder above now has a folder in it: its entry, in its own parent's listing, was fetched again
  expect(asked(fake, "projects")).toBe(2);
  expect(
    within(await tree()).getByRole("button", { name: m.collapse("alpha") }),
  ).toBeInTheDocument();
  await navigate(-1); // the new folder was added to the history
  expect(await panel("projects/alpha")).toBeInTheDocument();
});

test("a refused name shows the server's reason in the form and keeps it", async () => {
  // whatever the server says about the field, shown as it was sent
  const sent = { field: "name", message: "planted reason, as the server sent it" };
  const fake = dashboard([], {
    "POST /api/folders": problem(422, "validation_failed", "Some of what was sent is not valid.", {
      errors: [sent],
    }),
  });
  const { location } = renderApp("/?folder=projects");
  const opened = await panel("projects");
  await userEvent.click(within(opened).getByRole("button", { name: m.newFolder }));
  await userEvent.type(within(opened).getByLabelText(m.newFolderName), ".hidden");
  await userEvent.click(within(opened).getByRole("button", { name: m.create }));
  expect(await within(opened).findByRole("alert")).toHaveTextContent(
    `${m.newFolderName}: ${sent.message}`,
  );
  expect(within(opened).getByLabelText(m.newFolderName)).toHaveValue(".hidden");
  expect(within(opened).getByRole("button", { name: m.create })).toBeEnabled();
  expect(fake.made("POST", "/api/folders")).toHaveLength(1);
  expect(folderOf(location().search)).toBe("projects");
});

test("Cancel closes the new folder's form, having made nothing", async () => {
  const fake = dashboard([]);
  renderApp("/?folder=projects");
  const opened = await panel("projects");
  await userEvent.click(within(opened).getByRole("button", { name: m.newFolder }));
  await userEvent.type(within(opened).getByLabelText(m.newFolderName), "x");
  await userEvent.click(within(opened).getByRole("button", { name: m.cancel }));
  expect(within(opened).queryByLabelText(m.newFolderName)).not.toBeInTheDocument();
  expect(fake.made("POST", "/api/folders")).toHaveLength(0);
});

async function openDelete(path: string) {
  const opened = await panel(path);
  await userEvent.click(await within(opened).findByRole("button", { name: m.deleteFolder }));
  return within(await screen.findByRole("dialog", { name: m.deleteFolderTitle(path) }));
}

test("Delete folder asks first, naming the folder and the servers in it, and calls nothing until confirmed", async () => {
  const servers = [
    server("projects/alpha", "ready"),
    server("projects/alpha/deep", "stopped"),
    server("projects/alpha2", "ready"),
  ];
  const fake = dashboard(servers);
  renderApp("/?folder=projects/alpha");
  const dialog = await openDelete("projects/alpha");
  expect(
    dialog.getByRole("heading", { name: m.deleteFolderTitle("projects/alpha") }),
  ).toBeInTheDocument();
  expect(dialog.getByText(m.deleteFolderBody)).toBeInTheDocument();
  expect(dialog.getByText(m.deleteFolderServers(2))).toBeInTheDocument();
  const listed = dialog.getByRole("list", { name: m.deleteFolderServersList });
  expect(
    within(listed)
      .getAllByRole("listitem")
      .map((li) => li.textContent),
  ).toEqual(["projects/alpha", "projects/alpha/deep"]);
  expect(dialog.getByRole("button", { name: m.deleteFolderConfirm })).toHaveClass("danger");
  expect(fake.made("DELETE", "/api/folders")).toHaveLength(0);

  await userEvent.click(dialog.getByRole("button", { name: m.cancel }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(fake.made("DELETE", "/api/folders")).toHaveLength(0);
});

test.each([0, 1, 3])(
  "the confirmation says so when %s servers will be stopped and forgotten",
  async (count) => {
    const servers = ["", "/a", "/b"]
      .slice(0, count)
      .map((below) => server(`projects/beta${below}`, "ready"));
    dashboard(servers);
    renderApp("/?folder=projects/beta");
    const dialog = await openDelete("projects/beta");
    expect(dialog.getByText(m.deleteFolderServers(count))).toBeInTheDocument();
    expect(dialog.queryAllByRole("listitem")).toHaveLength(count);
  },
);

test("the count of servers is said in words that fit it", () => {
  expect(new Set([0, 1, 3].map(m.deleteFolderServers)).size).toBe(3);
  expect(m.deleteFolderServers(3)).toContain("3");
});

test("confirming deletes the folder, refreshes, and selects the folder above in its place", async () => {
  const fake = dashboard([server("projects/alpha", "ready")], changingFolders());
  const { location, navigate } = renderApp();
  await expand("projects");
  await select("alpha");
  const dialog = await openDelete("projects/alpha");
  const listed = fake.made("GET", "/api/servers").length;
  await userEvent.click(dialog.getByRole("button", { name: m.deleteFolderConfirm }));

  expect(await panel("projects")).toBeInTheDocument();
  expect(fake.made("DELETE", "/api/folders")[0].query.get("path")).toBe("projects/alpha");
  expect(folderOf(location().search)).toBe("projects");
  expect(await screen.findByRole("status")).toHaveTextContent(m.folderDeleted("projects/alpha"));
  expect(fake.made("GET", "/api/servers").length).toBeGreaterThan(listed);
  await waitFor(() =>
    expect(
      within(screen.getByRole("region", { name: m.foldersTitle })).queryByRole("button", {
        name: "alpha",
      }),
    ).not.toBeInTheDocument(),
  );
  expect(await folder("beta")).toBeInTheDocument(); // the rest of the tree is there
  expect(asked(fake, "projects")).toBe(2);
  expect(asked(fake, "")).toBe(2);
  await navigate(-1); // it replaced the deleted folder in the history
  expect(await screen.findByRole("region", { name: m.home })).toBeInTheDocument();
});

test("a folder made where one was deleted starts empty and closed", async () => {
  dashboard([], changingFolders());
  renderApp("/?folder=projects");
  await expand("projects");
  await expand("gamma");
  expect(await folder("deep")).toBeInTheDocument(); // positive control: gamma was open, with deep in it
  await select("gamma");
  const dialog = await openDelete("projects/gamma");
  await userEvent.click(dialog.getByRole("button", { name: m.deleteFolderConfirm }));
  const projects = await panel("projects");
  await waitFor(() =>
    expect(
      within(screen.getByRole("region", { name: m.foldersTitle })).queryByRole("button", {
        name: "gamma",
      }),
    ).not.toBeInTheDocument(),
  );

  await userEvent.click(within(projects).getByRole("button", { name: m.newFolder }));
  await userEvent.type(within(projects).getByLabelText(m.newFolderName), "gamma");
  await userEvent.click(within(projects).getByRole("button", { name: m.create }));
  expect(await panel("projects/gamma")).toBeInTheDocument();
  expect(await folder("gamma")).toHaveAttribute("aria-current", "true");
  expect(
    within(await tree()).queryByRole("button", { name: m.collapse("gamma") }),
  ).not.toBeInTheDocument();
  expect(within(await tree()).queryByRole("button", { name: "deep" })).not.toBeInTheDocument();
});

test("deleting a folder at the top selects Home", async () => {
  const fake = dashboard([], changingFolders());
  const { location } = renderApp("/?folder=proj");
  const dialog = await openDelete("proj");
  await userEvent.click(dialog.getByRole("button", { name: m.deleteFolderConfirm }));
  expect(await screen.findByRole("region", { name: m.home })).toBeInTheDocument();
  expect(location().search).toBe("");
  expect(screen.getByRole("status")).toHaveTextContent(m.folderDeleted("proj"));
  await waitFor(() => expect(asked(fake, "")).toBe(2));
  expect(within(await tree()).queryByRole("button", { name: "proj" })).not.toBeInTheDocument();
});

test("while it deletes, the dialog's buttons wait", async () => {
  let answer = () => {};
  dashboard([], {
    "DELETE /api/folders": () =>
      new Promise((resolve) => {
        answer = () =>
          resolve({ deleted: "projects/beta", link: false, stopped: [], forgotten: [] });
      }),
  });
  renderApp("/?folder=projects/beta");
  const dialog = await openDelete("projects/beta");
  await userEvent.click(dialog.getByRole("button", { name: m.deleteFolderConfirm }));
  expect(await dialog.findByRole("button", { name: m.deleting })).toBeDisabled();
  expect(dialog.getByRole("button", { name: m.cancel })).toBeDisabled();
  answer();
  expect(await panel("projects")).toBeInTheDocument();
});

test("a delete that fails says which server, and the servers and tree are fetched again", async () => {
  const stuck = server("projects/alpha", "unrecognised", { screen: ["still going"] });
  const fake = dashboard([stuck], {
    "DELETE /api/folders": problem(
      409,
      "delete_stop_failed",
      "A server in the folder did not stop.",
      {
        server: stuck,
        stopped: [],
      },
    ),
  });
  const { location } = renderApp("/?folder=projects/alpha");
  const dialog = await openDelete("projects/alpha");
  const listed = fake.made("GET", "/api/servers").length;
  await userEvent.click(dialog.getByRole("button", { name: m.deleteFolderConfirm }));
  const alert = await dialog.findByRole("alert");
  expect(alert).toHaveTextContent("A server in the folder did not stop.");
  expect(alert).toHaveTextContent(`${m.problemServer} projects/alpha`);
  expect(alert).toHaveTextContent("still going");
  await waitFor(() => expect(fake.made("GET", "/api/servers").length).toBeGreaterThan(listed));
  await waitFor(() => expect(asked(fake, "projects")).toBe(2));
  expect(dialog.getByRole("button", { name: m.deleteFolderConfirm })).toBeEnabled();
  expect(folderOf(location().search)).toBe("projects/alpha");
});

test("Delete folder is not offered until the servers are listed", async () => {
  dashboard([], { "GET /api/servers": () => new Promise(() => {}) });
  renderApp("/?folder=projects/beta");
  const opened = await panel("projects/beta");
  expect(await within(opened).findByRole("button", { name: m.newFolder })).toBeInTheDocument(); // the header is there
  expect(within(opened).queryByRole("button", { name: m.deleteFolder })).not.toBeInTheDocument();
});

test("a repository carries a marker its name is described by, and another folder none", async () => {
  const marked = (path: string) => ({ ...entry(path), repository: path !== "projects/beta" });
  dashboard([server("projects/alpha", "ready")], {
    "GET /api/folders": (c) =>
      c.query.get("path") === "projects"
        ? {
            path: "projects",
            entries: [marked("projects/alpha"), marked("projects/beta"), marked("projects/gamma")],
          }
        : FOLDERS[c.query.get("path") ?? ""],
  });
  renderApp();
  await expand("projects");
  const alpha = await folder("alpha");
  expect(alpha).toHaveAccessibleDescription(`${m.kinds.running} ${m.repository}`);
  expect(alpha.closest("li")!.querySelector(":scope > .repository-marker")).toHaveTextContent(
    m.repositoryMark,
  );
  const gamma = await folder("gamma");
  expect(gamma).toHaveAccessibleDescription(m.repository); // no dot, but the marker
  const beta = await folder("beta");
  expect(beta.closest("li")!.querySelector(":scope > .repository-marker")).toBeNull();
  expectNoDot(beta);
});

// --- clone from GitHub ------------------------------------------------------------------

const CLONED = "https://github.com/owner/fresh-clone";

// A home whose folders change as they are made, deleted and cloned into, starting as
// FOLDERS; a clone makes the folder git would name, which is listed as a repository.
function cloning() {
  const routes = changingFolders();
  const list = routes["GET /api/folders"] as (c: Call) => { entries?: { path: string }[] };
  const make = routes["POST /api/folders"] as (c: Call) => { created: string };
  const clones = new Set<string>();
  return {
    ...routes,
    "GET /api/folders": (c: Call) => {
      const found = list(c);
      if (!found.entries) return found;
      return {
        ...found,
        entries: found.entries.map((e) => ({ ...e, repository: clones.has(e.path) })),
      };
    },
    "POST /api/git/clone": (c: Call) => {
      const { parent, url } = c.body as { parent: string; url: string };
      const { created } = make({ ...c, body: { parent, name: url.split("/").pop() } });
      clones.add(created);
      return { created };
    },
  } satisfies Routes;
}

// The same, each clone answered only once its answer() is called.
function cloningLater() {
  const routes = cloning();
  const clone = routes["POST /api/git/clone"];
  const answers: (() => void)[] = [];
  return {
    answer: () => answers.shift()!(),
    routes: {
      ...routes,
      "POST /api/git/clone": (c: Call) =>
        new Promise((resolve) => answers.push(() => resolve(clone(c)))),
    } satisfies Routes,
  };
}

async function openClone(region: HTMLElement) {
  await userEvent.click(within(region).getByRole("button", { name: m.cloneFromGithub }));
  return within(region).getByLabelText(m.cloneUrl);
}

test.each([
  ["Home", "/", m.home],
  ["a folder", "/?folder=projects/alpha", "projects/alpha"],
])(
  "Clone from GitHub sits after New folder in the header of %s, as secondary, and not in the tree",
  async (_, path, name) => {
    dashboard([server("projects/alpha", "ready")]);
    renderApp(path);
    const region = await screen.findByRole("region", { name });
    const clone = within(region).getByRole("button", { name: m.cloneFromGithub });
    const newFolder = within(region).getByRole("button", { name: m.newFolder });
    expect(clone).toHaveClass("secondary");
    expect(clone).not.toHaveClass("primary");
    expect(
      newFolder.compareDocumentPosition(clone) & Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
    expect(clone.closest('[role="group"]')).toBe(newFolder.closest('[role="group"]'));
    expect(await folder("projects")).toBeInTheDocument(); // the tree is there
    expect(
      within(await tree()).queryByRole("button", { name: m.cloneFromGithub }),
    ).not.toBeInTheDocument();
  },
);

test("Clone from GitHub in a folder sends the address and the folder, then selects the clone", async () => {
  const fake = dashboard([], cloning());
  const { location } = renderApp("/?folder=projects/alpha");
  const opened = await panel("projects/alpha");
  await folder("alpha");
  await userEvent.type(await openClone(opened), CLONED);
  await userEvent.click(within(opened).getByRole("button", { name: m.clone }));

  expect(await panel("projects/alpha/fresh-clone")).toBeInTheDocument();
  expect(fake.made("POST", "/api/git/clone").map((c) => c.body)).toEqual([
    { parent: "projects/alpha", url: CLONED },
  ]);
  expect(folderOf(location().search)).toBe("projects/alpha/fresh-clone");
  const cloned = await folder("fresh-clone");
  expect(cloned).toHaveAttribute("aria-current", "true");
  expect(cloned).toHaveAccessibleDescription(m.repository);
});

test("Clone from GitHub in Home clones into the home folder and selects the clone", async () => {
  const fake = dashboard([], cloning());
  const { location } = renderApp();
  await folder("projects");
  const home = await screen.findByRole("region", { name: m.home });
  await userEvent.type(await openClone(home), CLONED);
  await userEvent.click(within(home).getByRole("button", { name: m.clone }));

  expect(await panel("fresh-clone")).toBeInTheDocument();
  expect(fake.made("POST", "/api/git/clone")[0].body).toEqual({ parent: "", url: CLONED });
  expect(folderOf(location().search)).toBe("fresh-clone");
  expect(asked(fake, "")).toBe(2);
});

test("the clone form says git names the folder, and links to the GitHub token in Settings", async () => {
  dashboard([]);
  renderApp("/?folder=projects");
  const opened = await panel("projects");
  await openClone(opened);
  const link = within(opened).getByRole("link", { name: m.cloneNote.link });
  expect(link).toHaveAttribute("href", "/settings");
  expect(link.closest("p")).toHaveTextContent(
    `${m.cloneNote.before}${m.cloneNote.link}${m.cloneNote.after}`,
  );
});

test("while it clones, the form's buttons wait, and only one clone is asked for", async () => {
  const later = cloningLater();
  const fake = dashboard([], later.routes);
  renderApp("/?folder=projects");
  const opened = await panel("projects");
  await userEvent.type(await openClone(opened), CLONED);
  await userEvent.click(within(opened).getByRole("button", { name: m.clone }));
  const busy = await within(opened).findByRole("button", { name: m.cloning });
  expect(busy).toBeDisabled();
  expect(within(opened).getByRole("button", { name: m.cancel })).toBeDisabled();
  await userEvent.click(busy);
  expect(fake.made("POST", "/api/git/clone")).toHaveLength(1);
  later.answer();
  expect(await panel("projects/fresh-clone")).toBeInTheDocument();
});

test("a refused address is shown under its label in the form, which keeps it", async () => {
  const sent = { field: "url", message: "planted reason, as the server sent it" };
  dashboard([], {
    "POST /api/git/clone": problem(
      422,
      "validation_failed",
      "Some of what was sent is not valid.",
      { errors: [sent] },
    ),
  });
  const { location } = renderApp("/?folder=projects");
  const opened = await panel("projects");
  await userEvent.type(await openClone(opened), "https://example.com/x");
  await userEvent.click(within(opened).getByRole("button", { name: m.clone }));
  expect(await within(opened).findByRole("alert")).toHaveTextContent(
    `${m.cloneUrl}: ${sent.message}`,
  );
  expect(within(opened).getByLabelText(m.cloneUrl)).toHaveValue("https://example.com/x");
  expect(within(opened).getByRole("button", { name: m.clone })).toBeEnabled();
  expect(folderOf(location().search)).toBe("projects");
});

test("git's refusal of a clone is shown in git's words", async () => {
  dashboard([], {
    "POST /api/git/clone": problem(409, "git_failed", "git refused. What it said is below.", {
      git: ["remote: Repository not found.", "fatal: repository not found"],
      git_more: 0,
    }),
  });
  renderApp("/?folder=projects");
  const opened = await panel("projects");
  await userEvent.type(await openClone(opened), CLONED);
  await userEvent.click(within(opened).getByRole("button", { name: m.clone }));
  const alert = await within(opened).findByRole("alert");
  expect(alert).toHaveTextContent("git refused. What it said is below.");
  expect(within(alert).getByLabelText(m.gitSaid)).toHaveTextContent(
    "remote: Repository not found.",
  );
});

test("Cancel closes the clone form, having asked for nothing", async () => {
  const fake = dashboard([]);
  renderApp("/?folder=projects");
  const opened = await panel("projects");
  await userEvent.type(await openClone(opened), CLONED);
  await userEvent.click(within(opened).getByRole("button", { name: m.cancel }));
  expect(within(opened).queryByLabelText(m.cloneUrl)).not.toBeInTheDocument();
  expect(fake.made("POST", "/api/git/clone")).toHaveLength(0);
});

test("a clone that ends once another folder is selected leaves that one selected, and shows the clone", async () => {
  const later = cloningLater();
  const fake = dashboard([], later.routes);
  const { location } = renderApp("/?folder=projects/alpha");
  const opened = await panel("projects/alpha");
  await userEvent.type(await openClone(opened), CLONED);
  await userEvent.click(within(opened).getByRole("button", { name: m.clone }));
  await within(opened).findByRole("button", { name: m.cloning });
  await select("beta");
  expect(await panel("projects/beta")).toBeInTheDocument();
  const before = asked(fake, "projects/alpha");
  later.answer();
  await waitFor(() => expect(asked(fake, "projects/alpha")).toBe(before + 1));
  expect(folderOf(location().search)).toBe("projects/beta");
  await expand("alpha");
  const cloned = await folder("fresh-clone");
  expect(cloned).not.toHaveAttribute("aria-current", "true");
  expect(cloned).toHaveAccessibleDescription(m.repository);
  expect(folderOf(location().search)).toBe("projects/beta");
});

test("while it makes a folder, New folder's buttons and the header's other actions wait, and only one folder is asked for", async () => {
  let answer = () => {};
  const fake = dashboard([], {
    "POST /api/folders": () =>
      new Promise((resolve) => {
        answer = () => resolve({ created: "projects/fresh" });
      }),
  });
  renderApp("/?folder=projects");
  const opened = await panel("projects");
  await userEvent.click(within(opened).getByRole("button", { name: m.newFolder }));
  await userEvent.type(within(opened).getByLabelText(m.newFolderName), "fresh");
  await userEvent.click(within(opened).getByRole("button", { name: m.create }));
  const busy = await within(opened).findByRole("button", { name: m.creating });
  expect(busy).toBeDisabled();
  expect(within(opened).getByRole("button", { name: m.cancel })).toBeDisabled();
  const header = within(within(opened).getByRole("group", { name: m.folderActions }));
  for (const name of [m.newFolder, m.cloneFromGithub, m.deleteFolder]) {
    expect(header.getByRole("button", { name })).toBeDisabled();
  }
  await userEvent.click(busy);
  await userEvent.click(header.getByRole("button", { name: m.cloneFromGithub }));
  expect(within(opened).queryByLabelText(m.cloneUrl)).not.toBeInTheDocument(); // its form stays
  expect(fake.made("POST", "/api/folders")).toHaveLength(1);
  answer();
  expect(await panel("projects/fresh")).toBeInTheDocument();
});

test.each([
  [m.newFolder, m.newFolderName, m.create, "POST /api/folders"],
  [m.cloneFromGithub, m.cloneUrl, m.clone, "POST /api/git/clone"],
])(
  "while %s runs, the header's other actions wait, so its form stays",
  async (opener, label, submit, route) => {
    let answer = () => {};
    dashboard([], {
      [route]: () =>
        new Promise((resolve) => {
          answer = () =>
            resolve(problem(409, "folder_exists", "Something with that name is there already."));
        }),
    });
    renderApp("/?folder=projects");
    const opened = await panel("projects");
    const actions = within(within(opened).getByRole("group", { name: m.folderActions }));
    await userEvent.click(actions.getByRole("button", { name: opener }));
    await userEvent.type(within(opened).getByLabelText(label), "x");
    await userEvent.click(within(opened).getByRole("button", { name: submit }));
    await waitFor(() => expect(within(opened).getByLabelText(label)).toBeInTheDocument());
    const header = await within(opened).findByRole("group", { name: m.folderActions });
    await waitFor(() =>
      expect(
        within(header)
          .getAllByRole("button")
          .every((b) => (b as HTMLButtonElement).disabled),
      ).toBe(true),
    );
    expect(within(header).getAllByRole("button")).toHaveLength(3); // New folder, Clone from GitHub, Delete folder
    answer();
    expect(await within(opened).findByRole("alert")).toHaveTextContent(
      "Something with that name is there already.",
    );
    for (const button of within(header).getAllByRole("button")) expect(button).toBeEnabled();
  },
);

// --- polls that fail ------------------------------------------------------------

// The Servers page polling every second, with fake timers that still advance by themselves.
async function polling(test: () => Promise<void>) {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const consoleError = vi.spyOn(console, "error").mockImplementation(() => {});
  try {
    await test();
  } finally {
    consoleError.mockRestore();
    vi.useRealTimers();
  }
}

const pass = (ms: number) =>
  act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });

test("a poll that cannot reach rcman says so, and the message goes once a later poll works", () =>
  polling(async () => {
    let down = false;
    const fake = dashboard([], {
      "GET /api/servers": () => (down ? UNREACHABLE_REPLY : { servers: [] }),
      "POST /api/client-errors": {},
    });
    renderApp("/", { pollMs: 1000 });
    await summary();
    expect(screen.queryByText(m.unreachable)).not.toBeInTheDocument();
    down = true;
    await pass(1000);
    expect(await screen.findByRole("alert")).toHaveTextContent(m.unreachable);
    expect(screen.queryByText(m.unexpectedResponse)).not.toBeInTheDocument();
    down = false;
    await pass(1000);
    await waitFor(() => expect(screen.queryByText(m.unreachable)).not.toBeInTheDocument());
    await waitFor(() =>
      expect(fake.made("POST", "/api/client-errors").map((c) => c.body)).toEqual([
        expect.objectContaining({ kind: "unreachable", method: "GET", path: "/api/servers" }),
      ]),
    );
  }));

test("a failed Claude account load stays while polls work", () =>
  polling(async () => {
    const fake = dashboard([], {
      "GET /api/claude": problem(502, "helper_unavailable", "planted Claude account failure"),
    });
    renderApp("/", { pollMs: 1000 });
    expect(await screen.findByRole("alert")).toHaveTextContent("planted Claude account failure");
    const polled = fake.made("GET", "/api/servers").length;
    await pass(2500);
    expect(fake.made("GET", "/api/servers").length).toBeGreaterThanOrEqual(polled + 2); // positive control
    expect(screen.getByRole("alert")).toHaveTextContent("planted Claude account failure");
  }));

test("a failing poll that ends after a later one worked is not shown", () =>
  polling(async () => {
    let asked = 0;
    let failLate = () => {};
    dashboard([], {
      "GET /api/servers": () => {
        asked += 1;
        if (asked !== 2) return { servers: [server("projects/alpha", "ready")] };
        return new Promise((resolve) => {
          failLate = () => resolve(UNREACHABLE_REPLY);
        });
      },
      "POST /api/client-errors": {},
    });
    renderApp("/", { pollMs: 1000 });
    await summary();
    await pass(1000); // the second poll, which hangs
    await pass(1000); // the third, which works
    expect(asked).toBeGreaterThanOrEqual(3);
    await act(async () => {
      failLate();
    });
    await pass(0);
    expect(console.error).toHaveBeenCalledWith(
      "rcman:",
      "unreachable",
      "GET",
      "/api/servers",
      expect.any(TypeError),
    ); // positive control: the late poll did fail
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(
      within(await summary()).getByRole("button", { name: "projects/alpha" }),
    ).toBeInTheDocument();
  }));

test("a Claude account load that cannot reach rcman is tried again once a poll works", () =>
  polling(async () => {
    let claudeDown = true;
    const fake = dashboard([], {
      "GET /api/claude": () => (claudeDown ? UNREACHABLE_REPLY : CONNECTED),
      "POST /api/client-errors": {},
    });
    renderApp("/", { pollMs: 1000 });
    expect(await screen.findByRole("alert")).toHaveTextContent(m.unreachable);
    claudeDown = false;
    await pass(1000);
    await waitFor(() => expect(screen.queryByRole("alert")).not.toBeInTheDocument());
    expect(
      await screen.findByText(m.claudeConnectedAs(CONNECTED.account.email)),
    ).toBeInTheDocument();
    const loads = fake.made("GET", "/api/claude").length;
    expect(loads).toBeGreaterThanOrEqual(2);
    await pass(2000);
    expect(fake.made("GET", "/api/claude")).toHaveLength(loads); // not again once it worked
  }));

test("the same failure of the account load and the poll is shown once", () =>
  polling(async () => {
    const fake = dashboard([], {
      "GET /api/claude": UNREACHABLE_REPLY,
      "GET /api/servers": UNREACHABLE_REPLY,
    });
    renderApp("/", { pollMs: 0 });
    await waitFor(() => {
      expect(fake.made("GET", "/api/claude")).toHaveLength(1);
      expect(fake.made("GET", "/api/servers")).toHaveLength(1);
    });
    await pass(0);
    expect(await screen.findByRole("alert")).toHaveTextContent(m.unreachable);
    expect(screen.getAllByRole("alert")).toHaveLength(1);
  }));

test("different failures of the account load and the poll are both shown", () =>
  polling(async () => {
    dashboard([], {
      "GET /api/claude": problem(502, "helper_unavailable", "planted Claude account failure"),
      "GET /api/servers": UNREACHABLE_REPLY,
    });
    renderApp("/", { pollMs: 0 });
    await waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(2));
    const texts = screen.getAllByRole("alert").map((a) => a.textContent);
    expect(texts).toEqual(
      expect.arrayContaining([
        expect.stringContaining("planted Claude account failure"),
        expect.stringContaining(m.unreachable),
      ]),
    );
  }));
