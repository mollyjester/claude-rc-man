import { act, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { m } from "./messages";
import { ACTIONS, LABELS } from "./Servers";
import { fakeApi, ME, problem, type Routes, UNREACHABLE_REPLY } from "./test/fakeApi";
import { sockets } from "./test/fakeSocket";
import { renderApp } from "./test/render";

type Workspace = {
  id: number;
  name: string;
  device: string;
  state: string;
  error: string | null;
  source: string | null;
  source_page: string | null;
  log: string | null;
  server: Record<string, unknown> | null;
  server_error: { code: string; title: string } | null;
  claude_version: string | null;
  rebuild: { state: string | null; log: string | null; error: string | null };
  rebuildable: boolean;
  ports: { port: number; label: string | null; url: string }[];
  stoppable: boolean;
  startable: boolean;
  stopping: boolean;
  starting: boolean;
};

// No rebuild under way, and none asked for before.
const NO_REBUILD = { state: null, log: null, error: null };

function workspace(id: number, name: string, extra: Partial<Workspace> = {}): Workspace {
  return {
    id,
    name,
    device: `rcman-ws-${id}`,
    state: "ready",
    error: null,
    source: null,
    source_page: null,
    log: null,
    server: null,
    server_error: null,
    claude_version: null,
    rebuild: NO_REBUILD,
    rebuildable: false,
    ports: [],
    stoppable: false,
    startable: false,
    stopping: false,
    starting: false,
    ...extra,
  };
}

// The version a server started now would run.
const APPROVED = "2.2.0";

// What /api/workspaces answers: the workspaces, and the approved version.
const listing = (workspaces: Workspace[], claude_approved: string | null = APPROVED) => ({
  workspaces,
  claude_approved,
});

function server(id: number, state: string, extra: Record<string, unknown> = {}) {
  return {
    workspace: id,
    state,
    environment_url: null,
    session_name: null,
    exit_code: null,
    ...extra,
  };
}

// alpha with a server in the given state, and beta with none.
function withServer(state: string, extra: Record<string, unknown> = {}) {
  return [workspace(1, "alpha", { server: server(1, state, extra) }), workspace(2, "beta")];
}

// What git answers for a repository with nothing to commit or push, and its branches.
const STATUS_CLEAN = {
  branch: { name: "main", commit: "0123456789", upstream: "origin/main", ahead: 0, behind: 0 },
  merging: false,
  staged: { files: [], more: 0 },
  not_staged: { files: [], more: 0 },
  author: { name: "Alice", email: "alice@example.com" },
  push_has_remote: true,
  push_to_github: false,
  github_token: null,
  push_ahead: 0,
};
const BRANCHES = { local: { branches: [], more: 0 }, remote: { branches: [], more: 0 } };

// What git answers in a workspace that holds no repository, which is what a new one is.
const NOT_A_REPOSITORY = () => problem(409, "not_a_repository", "planted: not a repository");

function dashboard(workspaces: Workspace[], extra: Routes = {}) {
  return fakeApi({
    "GET /api/me": ME,
    "GET /api/workspaces": listing(workspaces),
    "GET /api/git/status": NOT_A_REPOSITORY,
    "GET /api/git/branches": NOT_A_REPOSITORY,
    ...extra,
  });
}

// The list, which the page's own heading names.
const list = async () => screen.findByRole("region", { name: m.navDashboard });
const panel = async (name: string) => screen.findByRole("region", { name });
const entry = async (name: string) => within(await list()).findByRole("button", { name });
const selectedOf = (search: string) => new URLSearchParams(search).get("workspace");
// A row's trash button, named after its workspace.
const trash = async (name: string) =>
  within(await list()).findByRole("button", { name: m.deleteWorkspaceNamed(name) });

// Opens New workspace's dialog with its button beside the page's heading, and returns the dialog.
async function newWorkspace() {
  await userEvent.click(await screen.findByRole("button", { name: m.newWorkspace }));
  return within(await screen.findByRole("dialog", { name: m.newWorkspace }));
}

// The card of alpha's server, with alpha selected from the address.
async function card() {
  renderApp("/?workspace=1");
  return within(await within(await panel("alpha")).findByRole("article", { name: "alpha" }));
}

// --- the list and the selection ---------------------------------------------------

test("the workspaces are listed, and selecting one opens its panel and puts it in the address", async () => {
  dashboard([workspace(1, "alpha"), workspace(2, "beta")]);
  const { location } = renderApp();
  expect(await screen.findByText(m.workspacePanelEmpty)).toBeInTheDocument();
  expect(await entry("alpha")).not.toHaveAttribute("aria-current");
  await userEvent.click(await entry("beta"));
  const opened = await panel("beta");
  expect(within(opened).getByRole("heading", { name: "beta", level: 2 })).toBeInTheDocument();
  expect(selectedOf(location().search)).toBe("2");
  expect(await entry("beta")).toHaveAttribute("aria-current", "true");
  expect(await entry("alpha")).not.toHaveAttribute("aria-current");
  expect(screen.queryByText(m.workspacePanelEmpty)).not.toBeInTheDocument();
});

test("with no workspaces, the list is an empty state offering New workspace, the page's one", async () => {
  dashboard([]);
  renderApp();
  const shown = within(await list());
  expect(await shown.findByText(m.workspacesEmpty)).toBeInTheDocument();
  expect(shown.getByText(m.workspacesEmptyBody)).toBeInTheDocument();
  const make = shown.getByRole("button", { name: m.newWorkspace });
  expect(make).toHaveClass("primary");
  expect(make.closest(".empty-state")).toContainElement(shown.getByText(m.workspacesEmpty));
  // the heading offers it no second time, and there is no panel to select a workspace for
  expect(screen.getAllByRole("button", { name: m.newWorkspace })).toEqual([make]);
  expect(screen.queryByText(m.workspacePanelEmpty)).not.toBeInTheDocument();
  await userEvent.click(make);
  expect(await screen.findByRole("dialog", { name: m.newWorkspace })).toBeInTheDocument();
});

test("with a workspace selected, the switcher opens the others and selects one, and back clears the selection", async () => {
  dashboard([workspace(1, "alpha"), workspace(2, "beta", { state: "failed" })]);
  const { location } = renderApp("/?workspace=1");
  await panel("alpha");
  const switcher = screen.getByRole("button", { name: m.switchWorkspaceFrom("alpha") });
  await userEvent.click(switcher);
  const sheet = document.getElementById(switcher.getAttribute("aria-controls")!)!;
  await userEvent.click(within(sheet).getByRole("button", { name: /^beta/ }));
  expect(selectedOf(location().search)).toBe("2");
  expect(await panel("beta")).toBeInTheDocument();
  await userEvent.click(screen.getByRole("link", { name: m.backToWorkspaces }));
  expect(selectedOf(location().search)).toBeNull();
  expect(await screen.findByText(m.workspacePanelEmpty)).toBeInTheDocument();
});

// --- making a workspace ------------------------------------------------------------

test("New workspace sends the name as typed, then selects the new workspace", async () => {
  const workspaces = [workspace(1, "alpha")];
  const fake = dashboard([], {
    "GET /api/workspaces": () => listing(workspaces),
    "POST /api/workspaces": (c) => {
      const made = workspace(7, (c.body as { name: string }).name, { state: "creating" });
      workspaces.push(made);
      return { workspace: made };
    },
  });
  const { location } = renderApp();
  const form = await newWorkspace();
  await userEvent.type(form.getByLabelText(m.newWorkspaceName), "Fresh-1");
  const create = form.getByRole("button", { name: m.create });
  expect(create).toHaveClass("primary");
  await userEvent.click(create);
  await waitFor(() => expect(fake.made("POST", "/api/workspaces")).toHaveLength(1));
  expect(fake.made("POST", "/api/workspaces")[0].body).toEqual({ name: "Fresh-1" });
  const made = await panel("Fresh-1");
  expect(within(made).getByText(m.workspaceCreating)).toBeInTheDocument();
  expect(selectedOf(location().search)).toBe("7");
  expect(await entry("Fresh-1")).toHaveAttribute("aria-current", "true");
  // the dialog closed once the workspace was made, and a new one starts empty
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect((await newWorkspace()).getByLabelText(m.newWorkspaceName)).toHaveValue("");
});

const ADDRESS = "https://github.com/octo/widget";

test("an address makes the workspace by cloning it, named after the repository when no name is given", async () => {
  const workspaces: Workspace[] = [];
  const fake = dashboard([], {
    "GET /api/workspaces": () => listing(workspaces),
    "POST /api/git/clone": (c) => {
      const { url } = c.body as { url: string };
      const made = workspace(8, "widget", {
        state: "creating",
        source: url,
        source_page: url,
        log: "Making the workspace",
      });
      workspaces.push(made);
      return { workspace: made };
    },
  });
  const { location } = renderApp();
  const form = await newWorkspace();
  await userEvent.type(form.getByLabelText(m.newWorkspaceSource), ADDRESS);
  await userEvent.click(form.getByRole("button", { name: m.create }));
  await waitFor(() => expect(fake.made("POST", "/api/git/clone")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/clone")[0].body).toEqual({ url: ADDRESS });
  expect(fake.made("POST", "/api/workspaces")).toHaveLength(0);
  const made = await panel("widget");
  expect(within(made).getByText(m.workspaceCreating)).toBeInTheDocument();
  expect(selectedOf(location().search)).toBe("8");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

// --- the panel in each state of the workspace -----------------------------------

test("a workspace being made becomes ready by itself as the page refreshes", () =>
  polling(async () => {
    let state = "creating";
    dashboard([], {
      "GET /api/workspaces": () => listing([workspace(1, "alpha", { state })]),
    });
    renderApp("/?workspace=1", { pollMs: 1000 });
    const opened = await panel("alpha");
    expect(within(opened).getByText(m.workspaceCreating)).toBeInTheDocument();
    state = "ready";
    await pass(1000);
    expect(await within(opened).findByRole("button", { name: m.start })).toBeInTheDocument();
    expect(within(opened).queryByText(m.workspaceCreating)).not.toBeInTheDocument();
  }));

test("a workspace that failed shows why, offers no Start, and can be deleted from its row", async () => {
  dashboard([workspace(1, "alpha", { state: "failed", error: "planted: the image is missing" })]);
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  expect(within(opened).getByText(m.workspaceFailed)).toBeInTheDocument();
  expect(within(opened).getByText("planted: the image is missing")).toBeInTheDocument();
  expect(within(opened).queryByRole("button", { name: m.start })).not.toBeInTheDocument();
  await userEvent.click(await trash("alpha"));
  expect(screen.getByRole("dialog", { name: m.deleteWorkspaceTitle("alpha") })).toBeInTheDocument();
});

const STEPS = "Making the workspace\nChecking git\nCloning https://github.com/octo/widget";

test("a workspace being cloned shows the steps so far, in order, and keeps them as the page refreshes", () =>
  polling(async () => {
    let log = "Making the workspace";
    dashboard([], {
      "GET /api/workspaces": () =>
        listing([workspace(1, "alpha", { state: "creating", source: ADDRESS, log })]),
    });
    renderApp("/?workspace=1", { pollMs: 1000 });
    const opened = await panel("alpha");
    const steps = await within(opened).findByRole("figure", { name: m.workspaceLog });
    expect(steps).toHaveTextContent("Making the workspace");
    log = STEPS;
    await pass(1000);
    await waitFor(() => expect(steps.querySelector("pre")?.textContent).toBe(STEPS));
  }));

// --- the repository's page ---------------------------------------------------------

// The page's link in alpha's panel, found by its full address.
async function sourceLink(page: string) {
  return within(await panel("alpha")).findByRole("link", { name: page });
}

test("a cloned workspace's panel names its repository in its head, a link to its page in a new tab, named by the whole address", async () => {
  dashboard([workspace(1, "alpha", { source: `${ADDRESS}.git/`, source_page: ADDRESS })]);
  renderApp("/?workspace=1");
  const link = await sourceLink(ADDRESS);
  expect(link).toHaveAttribute("href", ADDRESS);
  expect(link).toHaveAttribute("target", "_blank");
  expect(link).toHaveAttribute("rel", "noopener noreferrer");
  // what it shows is the repository, owner first, after its icon
  expect(link).toHaveTextContent(/^octo\/widget$/);
  expect(link.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  const name = within(await panel("alpha")).getByRole("heading", { name: "alpha", level: 2 });
  expect(name.closest(".panel-head")).toContainElement(link);
  // the page the server gave, not the address as it was typed
  const opened = within(await panel("alpha"));
  expect(opened.queryByRole("link", { name: `${ADDRESS}.git/` })).not.toBeInTheDocument();
  expect(opened.queryByText(`${ADDRESS}.git/`)).not.toBeInTheDocument();
});

test("a ready workspace with no server offers Start, which starts one there", async () => {
  const workspaces = [workspace(2, "beta")];
  let answer = () => {};
  const fake = dashboard([], {
    "GET /api/workspaces": () => listing(workspaces),
    "POST /api/servers": () =>
      new Promise((resolve) => {
        answer = () => {
          workspaces[0] = workspace(2, "beta", { server: server(2, "trust_prompt") });
          resolve({ server: workspaces[0].server });
        };
      }),
  });
  renderApp("/?workspace=2");
  const opened = await panel("beta");
  expect(within(opened).getByRole("heading", { name: m.serverTitle })).toBeInTheDocument();
  expect(within(opened).getByText(m.noServer)).toBeInTheDocument();
  const start = within(opened).getByRole("button", { name: m.start });
  expect(start).toHaveClass("primary");
  await userEvent.click(start);
  await waitFor(() => expect(fake.made("POST", "/api/servers")).toHaveLength(1));
  expect(fake.made("POST", "/api/servers")[0].body).toEqual({ workspace: 2 });
  expect(start).toBeDisabled();
  answer();
  expect(await within(opened).findByRole("article", { name: "beta" })).toBeInTheDocument();
  expect(within(opened).queryByRole("button", { name: m.start })).not.toBeInTheDocument();
});

test("a start refused because Claude is not signed in shows the server's reason", async () => {
  dashboard([workspace(2, "beta")], {
    "POST /api/servers": problem(
      409,
      "claude_not_logged_in",
      "Claude is not signed in in this workspace.",
    ),
  });
  renderApp("/?workspace=2");
  const opened = await panel("beta");
  await userEvent.click(within(opened).getByRole("button", { name: m.start }));
  expect(await within(opened).findByRole("alert")).toHaveTextContent(
    "Claude is not signed in in this workspace.",
  );
});

// --- the Claude Code version a server runs ------------------------------------------

// alpha with a server in the given state that started with a Claude Code version.
const running = (version: string | null, state = "ready") =>
  workspace(1, "alpha", { server: server(1, state), claude_version: version });

test("a server on another version than the approved one gets a quiet note, saying what to do", async () => {
  dashboard([running("2.1.0")]);
  const opened = await card();
  expect(opened.getByText(m.serverClaudeVersion("2.1.0"))).toBeInTheDocument();
  const note = await screen.findByText(m.serverVersionDiffers(APPROVED));
  expect(note.closest('[role="alert"]')).toBeNull();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(note.closest("section")).toHaveAccessibleName(m.serverTitle); // in the server's section
});

// --- the Changes section ------------------------------------------------------------

const REPOSITORY_STATUS = {
  branch: { name: "main", commit: "0123456789", upstream: null, ahead: null, behind: null },
  merging: false,
  staged: { files: [], more: 0 },
  not_staged: { files: [{ path: "new.txt", change: "untracked" }], more: 0 },
  author: { name: "Alice", email: "alice@example.com" },
  push_has_remote: false,
  push_to_github: false,
  github_token: null,
  push_ahead: null,
};
const NO_BRANCHES = { local: { branches: [], more: 0 }, remote: { branches: [], more: 0 } };

test("a ready workspace's panel has the Changes section, asking for the workspace by its id", async () => {
  const fake = dashboard([workspace(4, "gamma")], {
    "GET /api/git/status": REPOSITORY_STATUS,
    "GET /api/git/branches": NO_BRANCHES,
  });
  renderApp("/?workspace=4");
  const opened = await panel("gamma");
  const section = await within(opened).findByRole("region", { name: m.changesTitle });
  expect(await within(section).findByText("new.txt")).toBeInTheDocument();
  expect(fake.made("GET", "/api/git/status")[0].query.get("workspace")).toBe("4");
  expect(fake.made("GET", "/api/git/branches")[0].query.get("workspace")).toBe("4");
});

// --- a server's card ----------------------------------------------------------------

test("a server with a session attached is in use, and shows the session and a link to it", async () => {
  dashboard(
    withServer("ready", {
      session_name: "alice-quiet-otter",
      environment_url: "https://claude.ai/code?environment=env_1",
    }),
  );
  const found = await card();
  expect(found.getByText(m.states.ready)).toBeInTheDocument();
  expect(found.getByText("alice-quiet-otter")).toBeInTheDocument();
  expect(found.getByRole("link", { name: m.openSession })).toHaveAttribute(
    "href",
    "https://claude.ai/code?environment=env_1",
  );
});

// The check that a server's card offers exactly the actions given, no more and no fewer.
function expectOffers(found: ReturnType<typeof within>, actions: (typeof ACTIONS)[string]) {
  const offered = found.queryAllByRole("button").map((b: HTMLElement) => b.textContent);
  expect(offered.sort()).toEqual(actions.map((a) => LABELS[a]).sort());
}

test.each(Object.entries(ACTIONS))(
  "a %s server offers exactly its actions",
  async (state, actions) => {
    dashboard(withServer(state, { screen: ["x"], exit_code: 1 }));
    expectOffers(await card(), actions);
  },
);

test("the check of a card's actions fails on a card that offers one more than it is given", async () => {
  dashboard(withServer("trust_prompt"));
  const found = await card();
  expectOffers(found, ACTIONS.trust_prompt); // positive control: the card offers its actions
  expect(() => expectOffers(found, ACTIONS.trust_prompt.slice(1))).toThrow();
});

test("answering the trust question sends the answer for the workspace", async () => {
  const fake = dashboard(withServer("trust_prompt"), {
    "POST /api/servers/confirm": { server: server(1, "connected") },
  });
  const found = await card();
  expect(found.getByText(m.trustTitle)).toBeInTheDocument();
  expect(found.getByText(m.trustBody("alpha"))).toBeInTheDocument();
  await userEvent.click(found.getByRole("button", { name: m.trustYes }));
  await waitFor(() => expect(fake.made("POST", "/api/servers/confirm")).toHaveLength(1));
  expect(fake.made("POST", "/api/servers/confirm")[0].body).toEqual({ workspace: 1, answer: "y" });
});

test("Enable at the Remote Control question sends the answer for the workspace", async () => {
  const fake = dashboard(withServer("remote_control_prompt"), {
    "POST /api/servers/confirm": { server: server(1, "connected") },
  });
  const found = await card();
  expect(found.getByText(m.remoteControlTitle)).toBeInTheDocument();
  await userEvent.click(found.getByRole("button", { name: m.enableRc }));
  await waitFor(() => expect(fake.made("POST", "/api/servers/confirm")).toHaveLength(1));
  expect(fake.made("POST", "/api/servers/confirm")[0].body).toEqual({ workspace: 1, answer: "y" });
});

test.each([
  ["stop", m.stop, "/api/servers/stop", "ready"],
  ["resume", m.resume, "/api/servers/resume", "stopped"],
  ["forget", m.forget, "/api/servers/forget", "stopped"],
  ["start again", m.startAgain, "/api/servers", "trust_declined"],
])("%s calls its endpoint with the workspace", async (_name, label, path, state) => {
  const fake = dashboard(withServer(state), { [`POST ${path}`]: { server: server(1, state) } });
  const found = await card();
  await userEvent.click(found.getByRole("button", { name: label }));
  await waitFor(() => expect(fake.made("POST", path)).toHaveLength(1));
  expect(fake.made("POST", path)[0].body).toEqual({ workspace: 1 });
});

test("an unrecognised screen can be shown as it is", async () => {
  dashboard(withServer("unrecognised", { screen: ["strange line", "another"] }));
  const found = await card();
  await userEvent.click(found.getByRole("button", { name: m.showScreen }));
  expect(found.getByText(/strange line\s+another/)).toBeInTheDocument();
});

// --- deleting a workspace ------------------------------------------------------------

test("deleting the selected workspace selects none and says it was deleted, until another is selected", async () => {
  let workspaces = withServer("ready");
  const fake = dashboard([], {
    "GET /api/workspaces": () => listing(workspaces),
    "DELETE /api/workspaces/1": () => {
      workspaces = workspaces.filter((w) => w.id !== 1);
      return { deleted: 1 };
    },
  });
  const { location } = renderApp("/?workspace=1");
  await panel("alpha");
  await userEvent.click(await trash("alpha"));
  const dialog = await screen.findByRole("dialog");
  const confirm = within(dialog).getByRole("button", { name: m.deleteWorkspaceConfirm });
  await waitFor(() => expect(confirm).toBeEnabled());
  await userEvent.click(confirm);
  expect(await screen.findByRole("status")).toHaveTextContent(m.workspaceDeleted("alpha"));
  expect(fake.made("DELETE", "/api/workspaces/1")).toHaveLength(1);
  expect(selectedOf(location().search)).toBeNull();
  await waitFor(() =>
    expect(
      within(screen.getByRole("region", { name: m.navDashboard })).queryByRole("button", {
        name: "alpha",
      }),
    ).not.toBeInTheDocument(),
  );
  expect(await entry("beta")).toBeInTheDocument(); // positive control: the list is there
  // the notice goes once another workspace is selected
  await userEvent.click(await entry("beta"));
  await panel("beta");
  expect(screen.queryByRole("status")).not.toBeInTheDocument();
});

test("the question names the work that is not committed or not pushed, which it asks git for itself", async () => {
  const fake = dashboard(withServer("ready"), {
    "GET /api/git/status": {
      ...STATUS_CLEAN,
      staged: { files: [{ path: "a.txt", change: "modified" }], more: 0 },
      not_staged: {
        files: [
          { path: "a.txt", change: "modified" },
          { path: "b.txt", change: "untracked" },
        ],
        more: 1,
      },
      push_ahead: 2,
    },
    "GET /api/git/branches": BRANCHES,
  });
  renderApp(); // no workspace selected, so no Changes section has asked
  await userEvent.click(await trash("alpha"));
  const dialog = await screen.findByRole("dialog");
  // a.txt and b.txt, and one more that the list did not show
  expect(await within(dialog).findByText(m.deleteWorkspaceUncommitted(3))).toBeInTheDocument();
  expect(within(dialog).getByText(m.deleteWorkspaceUnpushed(2))).toBeInTheDocument();
  expect(within(dialog).getByText(m.deleteWorkspaceBody)).toBeInTheDocument();
  expect(fake.made("GET", "/api/git/status").map((c) => c.query.get("workspace"))).toEqual(["1"]);
});

// --- polls that fail ------------------------------------------------------------

// The page polling every second, with fake timers that only the test advances.
async function polling(test: () => Promise<void>) {
  vi.useFakeTimers();
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
      "GET /api/workspaces": () => (down ? UNREACHABLE_REPLY : listing([])),
      "POST /api/client-errors": {},
    });
    renderApp("/", { pollMs: 1000 });
    // the first poll was answered: the list shows before it is
    expect(await within(await list()).findByText(m.workspacesEmpty)).toBeInTheDocument();
    expect(screen.queryByText(m.unreachable)).not.toBeInTheDocument();
    down = true;
    await pass(1000);
    expect(await screen.findByRole("alert")).toHaveTextContent(m.unreachable);
    expect(screen.queryByText(m.unexpectedResponse)).not.toBeInTheDocument();
    down = false;
    await pass(1000);
    await waitFor(() => expect(screen.queryByText(m.unreachable)).not.toBeInTheDocument());
    const reported = [
      expect.objectContaining({ kind: "unreachable", method: "GET", path: "/api/workspaces" }),
    ];
    const reports = () => fake.made("POST", "/api/client-errors").map((c) => c.body);
    await waitFor(() => expect(reports()).toEqual(reported));
    await pass(1000); // a later poll works, and nothing more is reported
    expect(fake.made("GET", "/api/workspaces").length).toBeGreaterThanOrEqual(3); // positive control
    expect(reports()).toEqual(reported);
  }));

// --- the panel's tabs ----------------------------------------------------------------

const tabs = async (name = "alpha") => within(await panel(name)).findByRole("tablist");
const tab = async (label: string) => within(await tabs()).getByRole("tab", { name: label });
const tabOf = (search: string) => new URLSearchParams(search).get("tab");

test("choosing Terminal shows it, keeps the choice in the address, and opens one terminal", async () => {
  dashboard(withServer("ready"));
  const { location } = renderApp("/?workspace=1");
  await userEvent.click(await tab(m.tabTerminal));
  expect(await tab(m.tabTerminal)).toHaveAttribute("aria-selected", "true");
  expect(await tab(m.tabOverview)).toHaveAttribute("aria-selected", "false");
  expect(tabOf(location().search)).toBe("terminal");
  expect(selectedOf(location().search)).toBe("1");
  const shown = screen.getByRole("tabpanel", { name: m.tabTerminal });
  expect(within(shown).getByRole("status")).toHaveTextContent(m.terminalConnecting);
  expect(within(await panel("alpha")).queryByRole("region", { name: m.serverTitle })).toBeNull();
  await waitFor(() => expect(sockets()).toHaveLength(1));
  // back to Overview and to Terminal again: the same terminal, kept while it was hidden
  await userEvent.click(await tab(m.tabOverview));
  expect(tabOf(location().search)).toBeNull();
  expect(screen.getByRole("tabpanel", { name: m.tabOverview })).toBeInTheDocument();
  expect(screen.queryByRole("tabpanel", { name: m.tabTerminal })).toBeNull();
  await userEvent.click(await tab(m.tabTerminal));
  expect(sockets()).toHaveLength(1);
  expect(sockets()[0].closedByPage).toBeUndefined();
});

// --- the Container card, and rebuilding a workspace ---------------------------------

// A cloned workspace that may be rebuilt now, with what more is given.
const rebuildable = (extra: Partial<Workspace> = {}) =>
  workspace(1, "alpha", { source: ADDRESS, source_page: ADDRESS, rebuildable: true, ...extra });

// What the server says of the workspace's devcontainer.json.
const changedSays = (changed: boolean | null) => ({
  "GET /api/workspaces/1/devcontainer": { changed },
});

const containerCard = async (name = "alpha") =>
  within(await within(await panel(name)).findByRole("region", { name: m.containerTitle }));

test("a changed devcontainer.json is said in the card, and Rebuild leads as the primary button", async () => {
  const fake = dashboard([rebuildable()], changedSays(true));
  renderApp("/?workspace=1");
  const card = await containerCard();
  expect(await card.findByText(m.devcontainerChanged)).toBeInTheDocument();
  expect(card.getByRole("button", { name: m.rebuild })).toHaveClass("primary");
  expect(fake.made("GET", "/api/workspaces/1/devcontainer").length).toBeGreaterThan(0);
});

test("confirming sends the confirmation, and the card shows the rebuild under way", async () => {
  let current = rebuildable();
  const fake = dashboard([], {
    "GET /api/workspaces": () => listing([current]),
    ...changedSays(true),
    "POST /api/workspaces/1/rebuild": () => {
      current = rebuildable({
        rebuildable: false,
        rebuild: { state: "preparing", log: "Pulling the image", error: null },
      });
      return { workspace: current };
    },
  });
  renderApp("/?workspace=1");
  const card = await containerCard();
  await userEvent.click(await card.findByRole("button", { name: m.rebuild }));
  const dialog = within(await screen.findByRole("dialog", { name: m.rebuildTitle("alpha") }));
  await userEvent.click(dialog.getByRole("button", { name: m.rebuildConfirm }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(fake.made("POST", "/api/workspaces/1/rebuild").map((c) => c.body)).toEqual([
    { confirm: true },
  ]);
  const busy = await card.findByRole("button", { name: m.rebuildBusy });
  expect(busy).toBeDisabled();
  expect(card.getByRole("figure", { name: m.workspaceLog })).toHaveTextContent("Pulling the image");
});

// --- the Ports card -----------------------------------------------------------------

// What the server lists of alpha's forwarded ports: one with a label, one without; ports below
// 1024, which nothing here binds (tests/test_fixed_resources.py).
const PORTS = [
  { port: 680, label: "Pebble Emulator (noVNC)", url: "https://680-1.ws.rcman.example.com/" },
  { port: 300, label: null, url: "https://300-1.ws.rcman.example.com/" },
];

// The listing with forwarding on or off, as the server says.
const forwardingIs = (on: boolean, workspaces: Workspace[]) => ({
  "GET /api/workspaces": { ...listing(workspaces), forwarding: on },
});

const portsCard = async () =>
  within(await within(await panel("alpha")).findByRole("region", { name: m.portsTitle }));

test("each port is shown as its real address, a link that opens a new tab, under its label or its number", async () => {
  const shown = [workspace(1, "alpha", { ports: PORTS })];
  dashboard(shown, forwardingIs(true, shown));
  renderApp("/?workspace=1");
  const card = await portsCard();
  const rows = card.getAllByRole("listitem");
  expect(rows).toHaveLength(PORTS.length);
  rows.forEach((row, at) => {
    const port = PORTS[at];
    expect(within(row).getByText(port.label ?? m.portNumbered(port.port))).toBeInTheDocument();
    const link = within(row).getByRole("link", { name: port.url });
    expect(link).toHaveAccessibleDescription(port.label ?? m.portNumbered(port.port));
    expect(link).toHaveAttribute("href", port.url);
    expect(link).toHaveTextContent(port.url);
    expect(link).toHaveAttribute("target", "_blank");
    expect(link).toHaveAttribute("rel", "noopener");
    expect(link).toHaveAttribute("translate", "no");
  });
  expect(card.queryByRole("button")).toBeNull(); // no Open, no copy: the address is the link
});

// --- stopping and starting a workspace -------------------------------------------------

test("Stop is sent at once, and once; the stopped workspace says so where it is read, and Start leads, sent once", async () => {
  const halted = (extra: Partial<Workspace> = {}) =>
    workspace(1, "alpha", {
      state: "halted",
      startable: true,
      server_error: { code: "workspace_stopped", title: "Planted: stopped." },
      ...extra,
    });
  let current = workspace(1, "alpha", {
    stoppable: true,
    ports: PORTS,
    server: server(1, "connected"),
  });
  let stopped: (answer: unknown) => void = () => {};
  let started: (answer: unknown) => void = () => {};
  const fake = dashboard([], {
    "GET /api/workspaces": () => ({ ...listing([current]), forwarding: true }),
    ...changedSays(null),
    "POST /api/workspaces/1/stop": () => new Promise((resolve) => (stopped = resolve)),
    "POST /api/workspaces/1/start": () => new Promise((resolve) => (started = resolve)),
  });
  renderApp("/?workspace=1");
  const card = await containerCard();

  // Pressed, it stops at once, says it is stopping, and a second press sends nothing more.
  await userEvent.click(await card.findByRole("button", { name: m.stopWorkspace }));
  const stopping = await card.findByRole("button", { name: m.stopWorkspaceBusy });
  expect(stopping).toBeDisabled();
  await userEvent.click(stopping);
  expect(fake.made("POST", "/api/workspaces/1/stop")).toHaveLength(1);
  current = halted();
  stopped({ workspace: current });

  // Stopped: its server, ports and terminal say so, and Start leads, the panel's one primary.
  const opened = within(await panel("alpha"));
  const start = await opened.findByRole("button", { name: m.startWorkspace });
  expect(start).toHaveClass("primary");
  const overview = opened.getByRole("tabpanel", { name: m.tabOverview });
  expect(overview.querySelectorAll(".primary")).toHaveLength(0);
  const serverCard = within(opened.getByRole("region", { name: m.serverTitle }));
  expect(serverCard.getByText(m.serverPower.workspace_stopped)).toBeInTheDocument();
  expect(serverCard.queryByRole("alert")).toBeNull();
  expect((await portsCard()).getByText(m.portsPower.workspace_stopped)).toBeInTheDocument();
  expect(opened.queryByRole("button", { name: m.stopWorkspace })).toBeNull();
  await userEvent.click(await tab(m.tabTerminal));
  expect(opened.getByText(m.terminalPower.workspace_stopped)).toBeInTheDocument();
  expect(sockets()).toEqual([]); // no terminal is asked of a stopped workspace

  // Start says it is starting while its request runs, and while the workspace is starting.
  await userEvent.click(start);
  const starting = await opened.findByRole("button", { name: m.startWorkspaceBusy });
  expect(starting).toHaveAttribute("aria-disabled", "true");
  await userEvent.click(starting);
  expect(fake.made("POST", "/api/workspaces/1/start")).toHaveLength(1);
  current = workspace(1, "alpha", {
    starting: true,
    server_error: { code: "workspace_starting", title: "Planted: starting." },
  });
  started({ workspace: current });
  await waitFor(() =>
    expect(opened.getByText(m.terminalPower.workspace_starting)).toBeInTheDocument(),
  );
  expect(opened.getByText(m.workspacePower.starting)).toBeInTheDocument();
  expect(opened.getByRole("button", { name: m.startWorkspaceBusy })).toHaveAttribute(
    "aria-disabled",
    "true",
  );
  expect(fake.made("POST", "/api/workspaces/1/start")).toHaveLength(1);
});
