import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { m } from "./messages";
import { ACTIONS, KIND, type Kind, LABELS, PRIMARY, WORKSPACE_KIND } from "./Servers";
import { fakeApi, ME, problem, type Routes, UNREACHABLE_REPLY } from "./test/fakeApi";
import { sockets } from "./test/fakeSocket";
import { expectRefused } from "./test/fields";
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

// What a row's name describes it with: its dot's kind, where it has one, then its device name.
const described = (id: number, kind?: Kind) =>
  [kind && m.kinds[kind], `rcman-ws-${id}`].filter(Boolean).join(" ");

function expectNoDot(button: HTMLElement) {
  expect(button.textContent).toMatch(/rcman-ws-\d+$/); // positive control: the row's device
  const device = button.textContent!.match(/rcman-ws-\d+$/)![0];
  expect(button).toHaveAccessibleDescription(device);
  expect(button.closest("li")!.querySelector(".dot")).toBeNull();
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

test.each([
  ["beside the heading", () => withServer("ready")],
  ["in the empty list", () => []],
])(
  "while rcman is being upgraded, New workspace %s is not offered, and the page says why",
  async (_, workspaces) => {
    dashboard([], {
      "GET /api/workspaces": { ...listing(workspaces()), rcman_upgrading: true },
    });
    renderApp();
    const why = await screen.findByText(m.workspacesUpgrading);
    const buttons = await screen.findAllByRole("button", { name: m.newWorkspace });
    expect(buttons.length).toBeGreaterThan(0); // positive control: the button is there
    for (const button of buttons) {
      expect(button).toBeDisabled();
      expect(button).toHaveAccessibleDescription(m.workspacesUpgrading);
    }
    expect(why).toBeInTheDocument();
  },
);

test("with no upgrade of rcman under way, New workspace is offered and nothing is said of one", async () => {
  dashboard([], { "GET /api/workspaces": { ...listing([]), rcman_upgrading: false } });
  renderApp();
  expect(await screen.findByRole("button", { name: m.newWorkspace })).toBeEnabled();
  expect(screen.queryByText(m.workspacesUpgrading)).not.toBeInTheDocument();
});

test("the heading row counts the workspaces, for a phone to show beside New", async () => {
  dashboard(withServer("ready"));
  renderApp();
  await entry("alpha");
  const heading = screen.getByRole("heading", { level: 1, name: m.navDashboard });
  expect(within(heading.parentElement!).getByText(m.workspaceCount(2))).toHaveClass("count-line");
  expect(m.workspaceCount(1)).toBe("1 workspace");
});

test("the empty state's words name no control the page has not got", () => {
  expect(`${m.workspacesEmpty} ${m.workspacesEmptyBody}`).not.toMatch(/\+/);
});

test("each workspace is a row of the list, the selected one marked as a row, its dot leading its name", async () => {
  dashboard(withServer("ready"));
  renderApp("/?workspace=2");
  const alpha = await entry("alpha");
  const beta = await entry("beta");
  expect(alpha.closest("li")).toHaveClass("list-row");
  expect(beta.closest("li")).toHaveClass("list-row", "selected");
  expect(alpha.closest("li")).not.toHaveClass("selected");
  // the dot is first in the name's button, and a row with none keeps its room, so names line up
  expect(alpha.firstElementChild).toHaveClass("dot-slot");
  expect(alpha.firstElementChild?.querySelector(".dot")).toHaveClass("dot-running");
  expect(beta.firstElementChild).toHaveClass("dot-slot");
  expect(beta.firstElementChild?.querySelector(".dot")).toBeNull();
});

test("each row shows its own device name under its name, with no label, the name still the row's", async () => {
  dashboard([workspace(1, "alpha", { device: "planted-device-a" }), workspace(2, "beta")]);
  renderApp();
  const alpha = await entry("alpha");
  const beta = await entry("beta");
  const device = within(alpha).getByText("planted-device-a");
  expect(device).toHaveClass("device", "muted");
  expect(device).toHaveAttribute("translate", "no");
  expect(device.parentElement).toContainElement(within(alpha).getByText("alpha")); // under it
  expect(within(beta).getByText("rcman-ws-2")).toHaveClass("device");
  expect(alpha).not.toHaveTextContent("rcman-ws-2");
  expect(alpha.textContent).toBe("alphaplanted-device-a"); // no label
  expect(alpha).toHaveAccessibleDescription("planted-device-a");
});

test("the switcher's sheet shows each workspace's device name, the choice named by the workspace", async () => {
  dashboard([workspace(1, "alpha"), workspace(2, "beta", { device: "planted-device-b" })]);
  renderApp("/?workspace=1");
  await panel("alpha");
  const switcher = screen.getByRole("button", { name: m.switchWorkspaceFrom("alpha") });
  await userEvent.click(switcher);
  const sheet = within(document.getElementById(switcher.getAttribute("aria-controls")!)!);
  const beta = sheet.getByRole("button", { name: "beta" });
  const device = within(beta).getByText("planted-device-b");
  expect(device).toHaveClass("device", "muted");
  expect(device).toHaveAttribute("translate", "no");
  expect(beta).toHaveAccessibleDescription("planted-device-b");
  expect(within(sheet.getByRole("button", { name: "alpha" })).getByText("rcman-ws-1")).toHaveClass(
    "device",
  );
});

test("with a workspace selected, the bar over its panel leads back to the list, and its switcher, named by the workspace, opens the others as a sheet", async () => {
  dashboard([workspace(1, "alpha"), workspace(2, "beta", { state: "failed" })]);
  const { location } = renderApp("/?workspace=1");
  await panel("alpha");
  // on a phone the bar takes the list's place (src/styles.css); here, it is beside the list
  const back = screen.getByRole("link", { name: m.backToWorkspaces });
  expect(back.textContent).toBe(""); // an icon only, named for where it goes
  expect(back.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  const switcher = screen.getByRole("button", { name: m.switchWorkspaceFrom("alpha") });
  expect(switcher).toHaveTextContent(/^alpha$/); // it shows the name its own name holds
  expect(switcher.parentElement).toContainElement(back); // one bar, the two side by side
  expect(switcher).toHaveAttribute("aria-expanded", "false");
  await userEvent.click(switcher);
  const sheet = document.getElementById(switcher.getAttribute("aria-controls")!)!;
  expect(sheet).not.toHaveAttribute("hidden");
  expect(sheet).toHaveClass("sheet");
  expect(within(sheet).getByText(m.switchWorkspace)).toBeInTheDocument(); // the sheet's title
  const choices = within(sheet).getAllByRole("button");
  expect(choices.map((c) => c.textContent)).toEqual([
    "alpharcman-ws-1",
    `betarcman-ws-2 ${m.workspaceStates.failed}`,
  ]);
  const current = within(sheet).getByRole("button", { name: "alpha" });
  expect(current).toHaveAttribute("aria-current", "true");
  expect(current).toHaveFocus(); // the sheet opens where you are
  await userEvent.click(within(sheet).getByRole("button", { name: /^beta/ }));
  expect(selectedOf(location().search)).toBe("2");
  expect(await panel("beta")).toBeInTheDocument();
  const now = screen.getByRole("button", { name: m.switchWorkspaceFrom("beta") });
  expect(now).toHaveAttribute("aria-expanded", "false");
  expect(now).toHaveFocus(); // back on the switcher once one was chosen
  await userEvent.click(screen.getByRole("link", { name: m.backToWorkspaces }));
  expect(selectedOf(location().search)).toBeNull();
  expect(await screen.findByText(m.workspacePanelEmpty)).toBeInTheDocument();
  // with none selected, there is nothing to go back from
  expect(screen.queryByRole("link", { name: m.backToWorkspaces })).not.toBeInTheDocument();
  expect(screen.queryByText(m.switchWorkspace)).not.toBeInTheDocument();
});

test("the switcher's sheet closes on Escape, focus back on the switcher", async () => {
  dashboard([workspace(1, "alpha"), workspace(2, "beta")]);
  renderApp("/?workspace=1");
  await panel("alpha");
  const switcher = screen.getByRole("button", { name: m.switchWorkspaceFrom("alpha") });
  await userEvent.click(switcher);
  expect(switcher).toHaveAttribute("aria-expanded", "true");
  await userEvent.keyboard("{Escape}");
  expect(switcher).toHaveAttribute("aria-expanded", "false");
  expect(switcher).toHaveFocus();
});

test("the selected workspace is kept in the address, which opens it again", async () => {
  dashboard([workspace(1, "alpha"), workspace(2, "beta")]);
  renderApp("/?workspace=2");
  expect(await panel("beta")).toBeInTheDocument();
  expect(await entry("beta")).toHaveAttribute("aria-current", "true");
});

test("the back button returns to the workspace selected before, then to none", async () => {
  dashboard([workspace(1, "alpha"), workspace(2, "beta")]);
  const { location, navigate } = renderApp();
  await userEvent.click(await entry("alpha"));
  await userEvent.click(await entry("beta"));
  expect(selectedOf(location().search)).toBe("2");
  await navigate(-1);
  expect(await panel("alpha")).toBeInTheDocument();
  await navigate(-1);
  expect(await screen.findByText(m.workspacePanelEmpty)).toBeInTheDocument();
});

test("a workspace in the address that is not listed says so", async () => {
  dashboard([workspace(1, "alpha")]);
  renderApp("/?workspace=99");
  expect(await screen.findByText(m.workspaceUnknown)).toBeInTheDocument();
  expect(await entry("alpha")).not.toHaveAttribute("aria-current");
});

test("until the workspaces are listed, the panel says it is loading", async () => {
  dashboard([], { "GET /api/workspaces": () => new Promise(() => {}) });
  renderApp("/?workspace=1");
  await list(); // the page is there, not the app's own loading
  expect(screen.getByText(m.loading)).toBeInTheDocument();
  expect(screen.queryByText(m.workspaceUnknown)).not.toBeInTheDocument();
});

test("the page asks nothing about a Claude account", async () => {
  const fake = dashboard(withServer("ready"));
  renderApp("/?workspace=1");
  await panel("alpha");
  expect(fake.made("GET", "/api/workspaces").length).toBeGreaterThan(0); // positive control
  expect(fake.calls.filter((c) => c.path.startsWith("/api/claude"))).toEqual([]);
  expect(screen.queryByRole("region", { name: m.claudeTitle })).not.toBeInTheDocument();
});

// --- dots and states in the list -------------------------------------------------

test.each(Object.entries(KIND))(
  "a workspace whose server is %s carries the %s dot",
  async (state, kind) => {
    dashboard(withServer(state));
    renderApp();
    const alpha = await entry("alpha");
    expect(alpha).toHaveAccessibleDescription(described(1, kind));
    // its shape is the kind's class, its meaning a title on hover, and the
    // description is read once, through the name it describes
    const dot = alpha.closest("li")!.querySelector(".dot")!;
    expect(dot).toHaveClass(`dot-${kind}`);
    expect(dot).toHaveAttribute("title", m.kinds[kind]);
    const [dotId] = alpha.getAttribute("aria-describedby")!.split(" ");
    const description = document.getElementById(dotId)!;
    expect(description).toHaveTextContent(m.kinds[kind]);
    expect(description).toHaveAttribute("hidden");
    expectNoDot(await entry("beta"));
  },
);

test("the check for no dot fails on a workspace that has one", async () => {
  dashboard(withServer("ready"));
  renderApp();
  const alpha = await entry("alpha");
  expect(alpha).toHaveAccessibleDescription(described(1, "running")); // the plant took effect
  expect(() => expectNoDot(alpha)).toThrow();
});

test("a workspace that failed, or whose server could not be read, has the problem dot", async () => {
  dashboard([
    workspace(1, "alpha", { state: "failed", error: "planted failure" }),
    workspace(2, "beta", { server_error: { code: "workspace_failed", title: "Planted." } }),
    workspace(3, "gamma"),
  ]);
  renderApp();
  expect(await entry("alpha")).toHaveAccessibleDescription(described(1, "problem"));
  expect(await entry("beta")).toHaveAccessibleDescription(described(2, "problem"));
  expectNoDot(await entry("gamma"));
});

test("a workspace whose container a rebuild replaces just now carries no problem dot", async () => {
  dashboard([
    workspace(1, "alpha", {
      source: ADDRESS,
      rebuild: { ...NO_REBUILD, state: "switching" },
      server_error: { code: "workspace_rebuilding", title: "Planted: being rebuilt." },
    }),
    workspace(2, "beta", { server_error: { code: "workspace_failed", title: "Planted." } }),
  ]);
  renderApp();
  expect(await entry("beta")).toHaveAccessibleDescription(described(2, "problem")); // control
  expectNoDot(await entry("alpha"));
});

test.each(["creating", "failed", "deleting"])(
  "a workspace that is %s says so in the list",
  async (state) => {
    dashboard([workspace(1, "alpha", { state }), workspace(2, "beta")]);
    renderApp();
    const item = (await entry("alpha")).closest("li") as HTMLElement;
    expect(within(item).getByText(m.workspaceStates[state])).toBeInTheDocument();
    const ready = (await entry("beta")).closest("li") as HTMLElement;
    expect(within(ready).queryByText(m.workspaceStates.ready)).not.toBeInTheDocument();
  },
);

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

test("New workspace, a primary button with its icon beside the heading, opens its dialog, focus in its name, and Escape or Cancel closes it, focus back on it", async () => {
  dashboard([workspace(1, "alpha")]);
  renderApp();
  const opener = await screen.findByRole("button", { name: m.newWorkspace });
  expect(opener).toHaveClass("primary");
  expect(opener).toHaveAccessibleName(m.newWorkspace);
  // its words in full, and the short form a phone shows, hidden from assistive technology
  expect(within(opener).getByText(m.newWorkspace)).toHaveClass("label-long");
  expect(within(opener).getByText(m.newWorkspaceShort)).toHaveAttribute("aria-hidden", "true");
  expect(opener.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
  // beside the page's own heading, the list's name
  const heading = screen.getByRole("heading", { level: 1, name: m.navDashboard });
  expect(heading.parentElement).toContainElement(opener);
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument(); // not there until asked for
  expect(screen.queryByLabelText(m.newWorkspaceName)).not.toBeInTheDocument();
  for (const close of [
    () => userEvent.keyboard("{Escape}"),
    (dialog: ReturnType<typeof within>) =>
      userEvent.click(dialog.getByRole("button", { name: m.cancel })),
  ]) {
    const dialog = await newWorkspace();
    expect(dialog.getByLabelText(m.newWorkspaceName)).toHaveFocus();
    await userEvent.type(dialog.getByLabelText(m.newWorkspaceName), "half-typed");
    await close(dialog);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  }
});

test("each help line of New workspace is one short sentence, the whole story left to the help pages", () => {
  for (const help of [m.newWorkspaceNameHelp, m.newWorkspaceSourceHelp]) {
    expect(help.length).toBeGreaterThan(0);
    expect(help.length).toBeLessThanOrEqual(70);
    expect(help.match(/[.:;] [A-Z]/g) ?? []).toEqual([]);
  }
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

test("an address and a name make the cloned workspace with that name", async () => {
  const workspaces: Workspace[] = [];
  const fake = dashboard([], {
    "GET /api/workspaces": () => listing(workspaces),
    "POST /api/git/clone": (c) => {
      const made = workspace(8, (c.body as { name: string }).name, {
        state: "creating",
        source: ADDRESS,
        source_page: ADDRESS,
      });
      workspaces.push(made);
      return { workspace: made };
    },
  });
  renderApp();
  const form = await newWorkspace();
  await userEvent.type(form.getByLabelText(m.newWorkspaceSource), ADDRESS);
  await userEvent.type(form.getByLabelText(m.newWorkspaceName), "mine");
  await userEvent.click(form.getByRole("button", { name: m.create }));
  await waitFor(() => expect(fake.made("POST", "/api/git/clone")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/clone")[0].body).toEqual({ url: ADDRESS, name: "mine" });
  await panel("mine");
});

test("a pasted space around the address and the name is not sent", async () => {
  const fake = dashboard([], {
    "POST /api/git/clone": (c) => ({
      workspace: workspace(8, (c.body as { name: string }).name, { state: "creating" }),
    }),
  });
  renderApp();
  const form = await newWorkspace();
  await userEvent.type(form.getByLabelText(m.newWorkspaceSource), `  ${ADDRESS} `);
  await userEvent.type(form.getByLabelText(m.newWorkspaceName), " mine  ");
  await userEvent.click(form.getByRole("button", { name: m.create }));
  await waitFor(() => expect(fake.made("POST", "/api/git/clone")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/clone")[0].body).toEqual({ url: ADDRESS, name: "mine" });
});

test("an address typed as spaces only is no address", async () => {
  const fake = dashboard([], { "POST /api/workspaces": problem(422, "validation_failed", "x") });
  renderApp();
  const form = await newWorkspace();
  await userEvent.type(form.getByLabelText(m.newWorkspaceSource), "   ");
  await userEvent.click(form.getByRole("button", { name: m.create }));
  await waitFor(() => expect(fake.made("POST", "/api/workspaces")).toHaveLength(1));
  expect(fake.made("POST", "/api/git/clone")).toHaveLength(0);
});

test("each help line of the new workspace form describes its field", async () => {
  dashboard([]);
  renderApp();
  const form = await newWorkspace();
  expect(form.getByLabelText(m.newWorkspaceName)).toHaveAccessibleDescription(
    m.newWorkspaceNameHelp,
  );
  expect(form.getByLabelText(m.newWorkspaceSource)).toHaveAccessibleDescription(
    m.newWorkspaceSourceHelp,
  );
});

test("with no address a name makes an empty workspace, and an address typed and cleared is not sent", async () => {
  const fake = dashboard([], {
    "POST /api/workspaces": (c) => ({
      workspace: workspace(9, (c.body as { name: string }).name, { state: "creating" }),
    }),
  });
  renderApp();
  const form = await newWorkspace();
  await userEvent.type(form.getByLabelText(m.newWorkspaceSource), "https://x");
  await userEvent.clear(form.getByLabelText(m.newWorkspaceSource));
  await userEvent.type(form.getByLabelText(m.newWorkspaceName), "empty");
  await userEvent.click(form.getByRole("button", { name: m.create }));
  await waitFor(() => expect(fake.made("POST", "/api/workspaces")).toHaveLength(1));
  expect(fake.made("POST", "/api/workspaces")[0].body).toEqual({ name: "empty" });
  expect(fake.made("POST", "/api/git/clone")).toHaveLength(0);
});

test("a refused address is shown under its label in the form, which keeps what was typed", async () => {
  const fake = dashboard([], {
    "POST /api/git/clone": problem(
      422,
      "validation_failed",
      "Some of what was sent is not valid.",
      { errors: [{ field: "url", message: "planted reason for the address" }] },
    ),
  });
  renderApp();
  const form = await newWorkspace();
  await userEvent.type(form.getByLabelText(m.newWorkspaceSource), "ftp://nope");
  await userEvent.type(form.getByLabelText(m.newWorkspaceName), "kept");
  await userEvent.click(form.getByRole("button", { name: m.create }));
  await form.findByRole("alert");
  expect(screen.getByRole("dialog", { name: m.newWorkspace })).toBeInTheDocument(); // still open
  expectRefused(form.getByLabelText(m.newWorkspaceSource), "planted reason for the address");
  expect(form.getByLabelText(m.newWorkspaceName)).not.toHaveAttribute("aria-invalid");
  expect(form.getByLabelText(m.newWorkspaceSource)).toHaveFocus();
  expect(form.getByLabelText(m.newWorkspaceSource)).toHaveValue("ftp://nope");
  expect(form.getByLabelText(m.newWorkspaceName)).toHaveValue("kept");
  expect(fake.made("POST", "/api/git/clone")).toHaveLength(1);
});

test("a name taken for a cloned workspace is shown under the name's label", async () => {
  dashboard([], {
    "POST /api/git/clone": problem(
      409,
      "workspace_name_taken",
      "You already have a workspace of that name.",
    ),
  });
  renderApp();
  const form = await newWorkspace();
  await userEvent.type(form.getByLabelText(m.newWorkspaceSource), ADDRESS);
  await userEvent.type(form.getByLabelText(m.newWorkspaceName), "alpha");
  await userEvent.click(form.getByRole("button", { name: m.create }));
  expect(await form.findByRole("alert")).toHaveTextContent(
    "You already have a workspace of that name.",
  );
});

test("a refused name is shown under its label in the form, which keeps it", async () => {
  const fake = dashboard([], {
    "POST /api/workspaces": problem(
      422,
      "validation_failed",
      "Some of what was sent is not valid.",
      {
        errors: [{ field: "name", message: "planted reason for the name" }],
      },
    ),
  });
  renderApp();
  const form = await newWorkspace();
  await userEvent.type(form.getByLabelText(m.newWorkspaceName), "bad name!");
  await userEvent.click(form.getByRole("button", { name: m.create }));
  await form.findByRole("alert");
  expect(screen.getByRole("dialog", { name: m.newWorkspace })).toBeInTheDocument(); // still open
  expectRefused(form.getByLabelText(m.newWorkspaceName), "planted reason for the name");
  expect(form.getByLabelText(m.newWorkspaceName)).toHaveFocus();
  expect(form.getByLabelText(m.newWorkspaceName)).toHaveValue("bad name!");
  expect(fake.made("POST", "/api/workspaces")).toHaveLength(1);
});

test("while it is being made, its button waits, and only one workspace is asked for", async () => {
  let answer = () => {};
  const fake = dashboard([], {
    "POST /api/workspaces": () =>
      new Promise((resolve) => {
        answer = () => resolve({ workspace: workspace(3, "slow", { state: "creating" }) });
      }),
  });
  renderApp();
  const form = await newWorkspace();
  await userEvent.type(form.getByLabelText(m.newWorkspaceName), "slow");
  await userEvent.click(form.getByRole("button", { name: m.create }));
  const busy = await form.findByRole("button", { name: m.creating });
  expect(busy).toBeDisabled();
  await userEvent.click(busy);
  expect(fake.made("POST", "/api/workspaces")).toHaveLength(1);
  answer();
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
});

// --- the panel in each state of the workspace -----------------------------------

test("a workspace being made says so, and offers neither Start nor Delete", async () => {
  dashboard([workspace(1, "alpha", { state: "creating" })]);
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  expect(within(opened).getByText(m.workspaceCreating)).toBeInTheDocument();
  expect(within(opened).queryAllByRole("button")).toEqual([]);
});

test("a workspace being made becomes ready by itself as the page refreshes", async () => {
  let state = "creating";
  dashboard([], {
    "GET /api/workspaces": () => listing([workspace(1, "alpha", { state })]),
  });
  renderApp("/?workspace=1", { pollMs: 50 });
  const opened = await panel("alpha");
  expect(within(opened).getByText(m.workspaceCreating)).toBeInTheDocument();
  state = "ready";
  expect(await within(opened).findByRole("button", { name: m.start })).toBeInTheDocument();
  expect(within(opened).queryByText(m.workspaceCreating)).not.toBeInTheDocument();
});

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

test("a workspace being cloned shows the steps so far, in order, and keeps them as the page refreshes", async () => {
  let log = "Making the workspace";
  dashboard([], {
    "GET /api/workspaces": () =>
      listing([workspace(1, "alpha", { state: "creating", source: ADDRESS, log })]),
  });
  renderApp("/?workspace=1", { pollMs: 50 });
  const opened = await panel("alpha");
  const steps = await within(opened).findByRole("figure", { name: m.workspaceLog });
  expect(steps).toHaveTextContent("Making the workspace");
  log = STEPS;
  await waitFor(() => expect(steps.querySelector("pre")?.textContent).toBe(STEPS));
});

test("a workspace being made with no steps yet shows no log", async () => {
  dashboard([workspace(1, "alpha", { state: "creating" })]);
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  expect(within(opened).getByText(m.workspaceCreating)).toBeInTheDocument();
  expect(within(opened).queryByRole("figure")).not.toBeInTheDocument();
});

test("a workspace being cloned can be deleted, as the way to cancel, and deleting it asks first", async () => {
  const workspaces = [workspace(1, "alpha", { state: "creating", source: ADDRESS, log: STEPS })];
  const fake = dashboard([], {
    "GET /api/workspaces": () => listing(workspaces),
    "DELETE /api/workspaces/1": () => {
      workspaces.length = 0;
      return {};
    },
  });
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  expect(within(opened).getByText(m.workspaceCloningCancel)).toBeInTheDocument();
  expect(within(opened).queryByRole("button", { name: m.start })).not.toBeInTheDocument();
  const open = await trash("alpha");
  expect(open).toHaveClass("secondary-danger");
  await userEvent.click(open);
  expect(fake.made("DELETE", "/api/workspaces/1")).toHaveLength(0);
  await userEvent.click(
    within(await screen.findByRole("dialog")).getByRole("button", {
      name: m.deleteWorkspaceConfirm,
    }),
  );
  await waitFor(() => expect(fake.made("DELETE", "/api/workspaces/1")).toHaveLength(1));
  expect(await screen.findByText(m.workspaceDeleted("alpha"))).toBeInTheDocument();
});

test("a workspace being deleted offers nothing, a clone that is being cancelled included", async () => {
  dashboard([workspace(1, "alpha", { state: "deleting", source: ADDRESS, log: STEPS })]);
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  expect(within(opened).getByText(m.workspaceDeleting)).toBeInTheDocument();
  expect(within(opened).queryAllByRole("button")).toEqual([]);
});

const CLONE_ERROR = [
  "GitHub asked for a sign-in: the repository is private, or the address is wrong.",
  "git clone failed, exit code 128:",
  "Cloning into 'widget'...",
  "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
].join("\n");

test("a clone that failed shows what was done and why, with git's lines kept apart", async () => {
  dashboard([
    workspace(1, "alpha", {
      state: "failed",
      source: ADDRESS,
      log: `${STEPS}\nThe clone failed`,
      error: CLONE_ERROR,
    }),
  ]);
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  expect(within(opened).getByText(m.workspaceFailed)).toBeInTheDocument();
  const steps = within(opened).getByRole("figure", { name: m.workspaceLog });
  expect(steps.querySelector("pre")?.textContent).toBe(`${STEPS}\nThe clone failed`);
  const said = opened.querySelector("pre.workspace-error");
  expect(said?.textContent).toBe(CLONE_ERROR); // every line break kept
  expect(said).toHaveClass("screen"); // a block of its own, as a screen is
  expect(within(opened).queryByText(m.workspaceCloningCancel)).not.toBeInTheDocument();
  expect(within(opened).queryByRole("button", { name: m.start })).not.toBeInTheDocument();
});

test("a failed clone says what went wrong in a sentence, and keeps the raw words behind Details", async () => {
  dashboard([
    workspace(1, "alpha", { state: "failed", source: ADDRESS, log: STEPS, error: CLONE_ERROR }),
  ]);
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  const sentence = within(opened).getByText(CLONE_ERROR.split("\n")[0]);
  expect(sentence.closest("details")).toBeNull(); // in the open, above the raw words
  const details = opened.querySelector("details")!;
  expect(details).not.toHaveAttribute("open");
  expect(within(details).getByText(m.detailsTitle).tagName).toBe("SUMMARY");
  expect(details.querySelector("pre.workspace-error")?.textContent).toBe(CLONE_ERROR);
  expect(sentence.compareDocumentPosition(details) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test.each([
  [
    "a clone git failed with no cause named",
    ADDRESS,
    "git clone failed, exit code 1:\nfatal: odd",
    m.workspaceFailedClone,
  ],
  ["a workspace that could not be made, many lines", null, "one\ntwo", m.workspaceFailedMake],
  ["a clone that could not be made, many lines", ADDRESS, "one\ntwo", m.workspaceFailedClone],
])("%s is told in a plain sentence of the page's own", async (_, source, error, sentence) => {
  dashboard([workspace(1, "alpha", { state: "failed", source, error })]);
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  expect(within(opened).getByText(sentence)).toBeInTheDocument();
  expect(opened.querySelector("details pre.workspace-error")?.textContent).toBe(error);
});

test.each([
  [
    "runtime_gone",
    "The workspace's container is gone from the runtime.\nrestore could not find it\nlooked in two places",
  ],
  ["default_image_gone", "The default image is gone.\nrebuild it first"],
  ["an unknown first line with no clone line", "Something rcman has no words for\nmore\nlines"],
])(
  "a workspace that failed with %s is told in a sentence, with the server's words behind Details",
  async (_, error) => {
    dashboard([workspace(1, "alpha", { state: "failed", source: null, error })]);
    renderApp("/?workspace=1");
    const opened = await panel("alpha");
    expect(within(opened).getByText(m.workspaceFailedMake)).toBeInTheDocument();
    expect(opened.querySelector("details pre.workspace-error")?.textContent).toBe(error);
  },
);

test("a failure told in one line is shown as it is, with no Details", async () => {
  dashboard([
    workspace(1, "alpha", { state: "failed", error: "Cloning took too long, so it was stopped." }),
  ]);
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  expect(within(opened).getByText("Cloning took too long, so it was stopped.")).toBeInTheDocument();
  expect(opened.querySelector("details")).toBeNull();
});

test("the steps are labelled where they are seen", async () => {
  dashboard([workspace(1, "alpha", { state: "creating", source: ADDRESS, log: STEPS })]);
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  const steps = await within(opened).findByRole("figure", { name: m.workspaceLog });
  expect(within(steps).getByText(m.workspaceLog).tagName).toBe("FIGCAPTION");
});

test("a workspace that is ready shows no log", async () => {
  dashboard([workspace(1, "alpha", { source: ADDRESS, log: null })]);
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  await within(opened).findByRole("button", { name: m.start });
  expect(within(opened).queryByRole("figure", { name: m.workspaceLog })).not.toBeInTheDocument();
});

test("a workspace being deleted says so, and offers nothing", async () => {
  dashboard([workspace(1, "alpha", { state: "deleting" })]);
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  expect(within(opened).getByText(m.workspaceDeleting)).toBeInTheDocument();
  expect(within(opened).queryAllByRole("button")).toEqual([]);
});

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
  expect(link).toHaveClass("repo-link");
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

test.each(["creating", "ready", "failed", "deleting"])(
  "the panel's head of a workspace that is %s names its device, labelled Device",
  async (state) => {
    dashboard([workspace(1, "alpha", { state, device: "planted-device-a" }), workspace(2, "beta")]);
    renderApp("/?workspace=1");
    const name = within(await panel("alpha")).getByRole("heading", { name: "alpha", level: 2 });
    const head = within(name.closest(".panel-head") as HTMLElement);
    const device = head.getByText("planted-device-a");
    expect(device.tagName).toBe("CODE");
    expect(device).toHaveAttribute("translate", "no");
    expect(device.parentElement).toHaveTextContent(`${m.device} planted-device-a`);
    expect(head.queryByText("rcman-ws-2")).not.toBeInTheDocument();
  },
);

test("a page that is no address is shown whole", async () => {
  dashboard([workspace(1, "alpha", { source: "x", source_page: "not an address" })]);
  renderApp("/?workspace=1");
  expect(await sourceLink("not an address")).toHaveTextContent(/^not an address$/);
});

test.each(["creating", "ready", "failed", "deleting"])(
  "the panel's head shows a workspace that is %s as a pill beside its name",
  async (state) => {
    dashboard([workspace(1, "alpha", { state })]);
    renderApp("/?workspace=1");
    const name = within(await panel("alpha")).getByRole("heading", { name: "alpha", level: 2 });
    const pill = within(name.closest(".panel-head") as HTMLElement).getByText(
      m.workspaceStates[state],
    );
    expect(pill).toHaveClass("badge", `badge-${WORKSPACE_KIND[state]}`);
  },
);

test("Overview holds the Claude server and the Changes as two sibling cards, neither inside the other", async () => {
  dashboard([workspace(1, "alpha")]);
  renderApp("/?workspace=1");
  const opened = within(await panel("alpha"));
  const server = await opened.findByRole("region", { name: m.serverTitle });
  const changes = await opened.findByRole("region", { name: m.changesTitle });
  for (const card of [server, changes]) {
    expect(card).toHaveClass("card");
    expect(card.querySelector(".card")).toBeNull();
  }
  expect(server.parentElement).toBe(changes.parentElement);
});

test.each([
  ["creating", { log: STEPS }],
  ["failed", { log: STEPS, error: CLONE_ERROR }],
  ["ready", {}],
  ["deleting", { log: STEPS }],
])("a cloned workspace that is %s links to its repository's page", async (state, extra) => {
  dashboard([workspace(1, "alpha", { state, source: ADDRESS, source_page: ADDRESS, ...extra })]);
  renderApp("/?workspace=1");
  expect(await sourceLink(ADDRESS)).toHaveAttribute("href", ADDRESS);
});

test.each([
  ["an empty workspace", null],
  ["a workspace whose address has no page", "git@github.com:octo/widget.git"],
])("%s links nothing", async (_, source) => {
  // the same panel with a page has the link, so its absence below can be seen
  dashboard([workspace(1, "alpha", { source: ADDRESS, source_page: ADDRESS })]);
  renderApp("/?workspace=1");
  await sourceLink(ADDRESS);
  expect(within(await panel("alpha")).getAllByRole("link")).toHaveLength(1);
  cleanup();
  dashboard([workspace(1, "alpha", { source, source_page: null })]);
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  await within(opened).findByRole("button", { name: m.start });
  expect(within(opened).queryAllByRole("link")).toEqual([]);
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

test("a server that could not be read is shown with the reason, and Start is not offered", async () => {
  dashboard([
    workspace(1, "alpha", {
      server_error: { code: "workspace_failed", title: "planted: the server could not be read" },
    }),
  ]);
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  expect(within(opened).getByRole("alert")).toHaveTextContent(
    "planted: the server could not be read",
  );
  expect(within(opened).queryByRole("button", { name: m.start })).not.toBeInTheDocument();
  expect(within(opened).queryByText(m.noServer)).not.toBeInTheDocument();
  expect(within(opened).getByText(m.serverUnreadNextStep)).toBeInTheDocument(); // what to do next
});

// --- the Claude Code version a server runs ------------------------------------------

// alpha with a server in the given state that started with a Claude Code version.
const running = (version: string | null, state = "ready") =>
  workspace(1, "alpha", { server: server(1, state), claude_version: version });

test("a server's card says which Claude Code version it runs, and says no more when that is the approved one", async () => {
  dashboard([running(APPROVED)]);
  const opened = await card();
  expect(opened.getByText(m.serverClaudeVersion(APPROVED))).toBeInTheDocument();
  expect(screen.queryByText(m.serverVersionDiffers(APPROVED))).not.toBeInTheDocument();
});

test("a server on another version than the approved one gets a quiet note, saying what to do", async () => {
  dashboard([running("2.1.0")]);
  const opened = await card();
  expect(opened.getByText(m.serverClaudeVersion("2.1.0"))).toBeInTheDocument();
  const note = await screen.findByText(m.serverVersionDiffers(APPROVED));
  expect(note).toHaveClass("muted");
  expect(note.closest('[role="alert"]')).toBeNull();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  expect(note.closest("section")).toHaveAccessibleName(m.serverTitle); // in the server's section
});

test("the note is made for a version approved after a rollback too, and names no direction", async () => {
  dashboard([running("2.3.0")]);
  await card();
  expect(await screen.findByText(m.serverVersionDiffers(APPROVED))).toBeInTheDocument();
  expect(m.serverVersionDiffers(APPROVED)).not.toMatch(/newer|older/i);
});

test.each(Object.keys(ACTIONS))(
  "the note about restarting for the approved version is made for a server in state %s only if it is running",
  async (state) => {
    dashboard([running("2.1.0", state)]);
    renderApp("/?workspace=1");
    const opened = await panel("alpha");
    await within(opened).findByRole("article", { name: "alpha" });
    // a server that does not run has nothing to stop and start again
    const note = within(opened).queryByText(m.serverVersionDiffers(APPROVED));
    if (ACTIONS[state].includes("stop")) expect(note).toBeInTheDocument();
    else expect(note).not.toBeInTheDocument();
  },
);

test("the note goes once the server runs the approved version", async () => {
  let version = "2.1.0";
  dashboard([], { "GET /api/workspaces": () => listing([running(version)]) });
  renderApp("/?workspace=1", { pollMs: 50 });
  expect(await screen.findByText(m.serverVersionDiffers(APPROVED))).toBeInTheDocument();
  version = APPROVED;
  await waitFor(() =>
    expect(screen.queryByText(m.serverVersionDiffers(APPROVED))).not.toBeInTheDocument(),
  );
  expect(screen.getByText(m.serverClaudeVersion(APPROVED))).toBeInTheDocument(); // positive control: refreshed
});

test("no version and no note where the server has none recorded, or nothing is approved", async () => {
  dashboard([running(null, "stopped")]);
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  await within(opened).findByRole("article", { name: "alpha" });
  expect(within(opened).queryByText(/Claude Code \d/)).not.toBeInTheDocument();
  expect(within(opened).queryByText(m.serverVersionDiffers(APPROVED))).not.toBeInTheDocument();
  cleanup();

  dashboard([], { "GET /api/workspaces": () => listing([running("2.1.0")], null) });
  renderApp("/?workspace=1");
  const again = await panel("alpha");
  expect(await within(again).findByText(m.serverClaudeVersion("2.1.0"))).toBeInTheDocument();
  expect(within(again).queryByText(/is approved/)).not.toBeInTheDocument();
});

test("a workspace with no server shows no version", async () => {
  dashboard([workspace(2, "beta")]);
  renderApp("/?workspace=2");
  const opened = await panel("beta");
  await within(opened).findByText(m.noServer);
  expect(within(opened).queryByText(/Claude Code \d/)).not.toBeInTheDocument();
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

test("the Changes section is there whether or not a server runs in the workspace", async () => {
  dashboard([workspace(1, "alpha", { server: server(1, "stopped") })], {
    "GET /api/git/status": REPOSITORY_STATUS,
    "GET /api/git/branches": NO_BRANCHES,
  });
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  expect(await within(opened).findByRole("region", { name: m.changesTitle })).toBeInTheDocument();
  expect(within(opened).getByRole("region", { name: m.serverTitle })).toBeInTheDocument();
});

test("a workspace that holds no repository says so quietly in its panel", async () => {
  dashboard([workspace(1, "alpha")]);
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  expect(await within(opened).findByText(m.notARepository)).toBeInTheDocument();
  expect(screen.queryByRole("alert")).not.toBeInTheDocument();
});

test.each(["creating", "failed", "deleting"])(
  "a workspace that is %s has no Changes section and git is not asked",
  async (state) => {
    const fake = dashboard([workspace(1, "alpha", { state, error: "planted" })]);
    renderApp("/?workspace=1");
    const opened = await panel("alpha");
    // the panel is up: its state is said, and the section is not there
    expect(within(opened).getByRole("heading", { name: "alpha", level: 2 })).toBeInTheDocument();
    expect(within(opened).queryByRole("region", { name: m.changesTitle })).not.toBeInTheDocument();
    expect(fake.made("GET", "/api/git/status")).toHaveLength(0);
  },
);

test("the Changes section is checked again as the page refreshes", async () => {
  const fake = dashboard([workspace(1, "alpha")], {
    "GET /api/git/status": REPOSITORY_STATUS,
    "GET /api/git/branches": NO_BRANCHES,
  });
  renderApp("/?workspace=1", { pollMs: 50 });
  await panel("alpha");
  await waitFor(() => expect(fake.made("GET", "/api/git/status").length).toBeGreaterThanOrEqual(3));
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
  dashboard(
    withServer("connected", { environment_url: "https://claude.ai/code?environment=env_1" }),
  );
  const found = await card();
  expect(found.getByText(m.states.connected)).toBeInTheDocument();
  expect(found.getByRole("link", { name: m.openSession })).toHaveAttribute(
    "href",
    "https://claude.ai/code?environment=env_1",
  );
});

test("a stopped server keeps its environment but offers no link to it", async () => {
  dashboard(withServer("stopped", { environment_url: "https://claude.ai/code?environment=env_1" }));
  const found = await card();
  expect(found.getByText(m.states.stopped)).toBeInTheDocument();
  expect(found.queryByRole("link", { name: m.openSession })).not.toBeInTheDocument();
});

test.each(Object.entries(ACTIONS))(
  "a %s server offers exactly its actions",
  async (state, actions) => {
    dashboard(withServer(state, { screen: ["x"], exit_code: 1 }));
    const found = await card();
    const offered = found.queryAllByRole("button").map((b) => b.textContent);
    expect(offered.sort()).toEqual(actions.map((a) => LABELS[a]).sort());
  },
);

test.each(Object.keys(ACTIONS))(
  "a %s server shows its state as a badge and says what to do",
  async (state) => {
    dashboard(withServer(state, { screen: ["x"], exit_code: 1 }));
    const found = await card();
    const badge = found.getByText(m.states[state], { exact: true });
    expect(badge).toHaveClass("badge", `badge-${KIND[state]}`);
    expect(found.getByText(m.nextStep[state])).toBeInTheDocument();
  },
);

test("a state rcman does not know says to look at the screen", async () => {
  dashboard(withServer("something_new", { screen: ["x"] }));
  const found = await card();
  expect(found.getByText("something_new")).toHaveClass("badge", "badge-problem");
  expect(found.getByText(m.nextStepUnknown)).toBeInTheDocument();
  expect(found.getByRole("button", { name: m.showScreen })).toHaveClass("secondary");
});

test.each(Object.keys(ACTIONS))(
  "a %s server has one primary action, and Stop and Forget are secondary",
  async (state) => {
    dashboard(
      withServer(state, {
        screen: ["x"],
        exit_code: 1,
        environment_url: "https://claude.ai/code?environment=env_1",
      }),
    );
    const found = await card();
    const primary = PRIMARY[state];
    const primaries = (await panel("alpha")).querySelectorAll(".primary");
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

test("a server at the Remote Control question shows it with Enable, Not now and Stop", async () => {
  dashboard(withServer("remote_control_prompt"));
  const found = await card();
  expect(found.getByText(m.states.remote_control_prompt)).toHaveClass("badge");
  expect(found.getByText(m.remoteControlTitle)).toBeInTheDocument();
  expect(found.getByText(m.remoteControlBody)).toBeInTheDocument();
  const enable = found.getByRole("button", { name: m.enableRc });
  expect(enable).toHaveClass("primary");
  expect(found.getByRole("button", { name: m.notNow })).toHaveClass("secondary");
  expect(found.getByRole("button", { name: m.stop })).toHaveClass("secondary");
  // not the trust question's
  expect(found.queryByText(m.trustTitle)).not.toBeInTheDocument();
  expect(found.queryByRole("button", { name: m.trustYes })).not.toBeInTheDocument();
  expect(found.queryByRole("button", { name: m.trustNo })).not.toBeInTheDocument();
});

test("a server at the trust question does not show the Remote Control question", async () => {
  dashboard(withServer("trust_prompt"));
  const found = await card();
  expect(found.getByText(m.trustTitle)).toBeInTheDocument(); // positive control
  expect(found.queryByText(m.remoteControlTitle)).not.toBeInTheDocument();
  expect(found.queryByRole("button", { name: m.enableRc })).not.toBeInTheDocument();
  expect(found.queryByRole("button", { name: m.notNow })).not.toBeInTheDocument();
});

test.each([
  ["Enable", m.enableRc, "/api/servers/confirm", { workspace: 1, answer: "y" }],
  ["Not now", m.notNow, "/api/servers/confirm", { workspace: 1, answer: "n" }],
  ["Stop", m.stop, "/api/servers/stop", { workspace: 1 }],
])("%s at the Remote Control question sends its request", async (_name, label, path, body) => {
  const fake = dashboard(withServer("remote_control_prompt"), {
    [`POST ${path}`]: { server: server(1, "connected") },
  });
  const found = await card();
  await userEvent.click(found.getByRole("button", { name: label }));
  await waitFor(() => expect(fake.made("POST", path)).toHaveLength(1));
  expect(fake.made("POST", path)[0].body).toEqual(body);
});

test.each([
  ["stop", m.stop, "/api/servers/stop", "ready"],
  ["resume", m.resume, "/api/servers/resume", "stopped"],
  ["forget", m.forget, "/api/servers/forget", "stopped"],
  ["resume", m.resume, "/api/servers/resume", "exited"],
  ["start again", m.startAgain, "/api/servers", "trust_declined"],
  ["start again", m.startAgain, "/api/servers", "logged_out"],
])("%s calls its endpoint with the workspace", async (_name, label, path, state) => {
  const fake = dashboard(withServer(state), { [`POST ${path}`]: { server: server(1, state) } });
  const found = await card();
  await userEvent.click(found.getByRole("button", { name: label }));
  await waitFor(() => expect(fake.made("POST", path)).toHaveLength(1));
  expect(fake.made("POST", path)[0].body).toEqual({ workspace: 1 });
});

test("a server that stopped by itself says so, whatever its exit status, and can be resumed", async () => {
  for (const exit_code of [0, 1, 137]) {
    const fake = dashboard(withServer("exited", { exit_code, screen: ["x"] }), {
      "POST /api/servers/resume": { server: server(1, "connected") },
    });
    const found = await card();
    expect(found.getByText(m.states.exited)).toBeInTheDocument();
    expect(found.getByText(m.exitCode(exit_code))).toBeInTheDocument();
    await userEvent.click(found.getByRole("button", { name: m.resume }));
    await waitFor(() => expect(fake.made("POST", "/api/servers/resume")).toHaveLength(1));
    expect(fake.made("POST", "/api/servers/resume")[0].body).toEqual({ workspace: 1 });
    cleanup();
  }
});

test("an unrecognised screen can be shown as it is", async () => {
  dashboard(withServer("unrecognised", { screen: ["strange line", "another"] }));
  const found = await card();
  await userEvent.click(found.getByRole("button", { name: m.showScreen }));
  expect(found.getByText(/strange line\s+another/)).toBeInTheDocument();
});

test("a reconnected server offers Show screen and Stop, and no Open in claude.ai", async () => {
  dashboard(
    withServer("reconnected", {
      screen: ["", "[10:09:14] Reconnected after 3s"],
      // planted: even with an environment named, nothing says it takes sessions
      environment_url: "https://claude.ai/code?environment=env_1",
    }),
  );
  const found = await card();
  expect(found.getByText(m.states.reconnected, { exact: true })).toHaveClass("badge");
  expect(found.getByText(m.nextStep.reconnected)).toBeInTheDocument();
  const offered = found.queryAllByRole("button").map((b) => b.textContent);
  expect(offered.sort()).toEqual([m.showScreen, m.stop].sort());
  expect(found.queryByRole("link", { name: m.openSession })).not.toBeInTheDocument();
  await userEvent.click(found.getByRole("button", { name: m.showScreen }));
  expect(found.getByText(/Reconnected after 3s/)).toBeInTheDocument();
});

test("Show screen is a disclosure, which hides the screen again", async () => {
  dashboard(withServer("exited", { exit_code: 1, screen: ["last words"] }));
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
  expect(found.getByRole("figure", { name: m.screenTitle("alpha") })).toBe(screenOf());

  await userEvent.click(disclosure);
  expect(disclosure).toHaveAttribute("aria-expanded", "false");
  expect(disclosure).toHaveAccessibleName(m.showScreen);
  expect(screenOf()).not.toBeVisible();
});

test("a refused action shows the server's problem", async () => {
  dashboard(withServer("ready"), {
    "POST /api/servers/stop": problem(409, "not_running", "No server is running here."),
  });
  const found = await card();
  await userEvent.click(found.getByRole("button", { name: m.stop }));
  expect(await found.findByRole("alert")).toHaveTextContent("No server is running here.");
});

// --- deleting a workspace ------------------------------------------------------------

test("every row has a trash button named after its workspace, a danger outline with an icon only", async () => {
  dashboard([
    workspace(1, "alpha"),
    workspace(2, "beta", { state: "creating" }),
    workspace(3, "gamma", { state: "deleting" }),
  ]);
  renderApp();
  for (const name of ["alpha", "beta", "gamma"]) {
    const button = await trash(name);
    expect(button.closest("li")).toContainElement(await entry(name));
    expect(button).toHaveClass("secondary-danger");
    expect(button.textContent).toBe("");
  }
});

test("the trash button asks first, saying everything in it and its server go, and Cancel deletes nothing", async () => {
  const fake = dashboard(withServer("ready"), { "GET /api/git/status": STATUS_CLEAN });
  renderApp();
  const opener = await trash("alpha");
  await userEvent.click(opener);
  const dialog = await screen.findByRole("dialog", { name: m.deleteWorkspaceTitle("alpha") });
  expect(within(dialog).getByText(m.deleteWorkspaceBody)).toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: m.deleteWorkspaceConfirm })).toHaveClass(
    "danger",
  );
  expect(fake.calls.filter((c) => c.method === "DELETE")).toEqual([]);
  await userEvent.click(within(dialog).getByRole("button", { name: m.cancel }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(opener).toHaveFocus();
  expect(fake.calls.filter((c) => c.method === "DELETE")).toEqual([]);
});

test("a row's trash button deletes that row's workspace, whichever is selected, and keeps the selection", async () => {
  let workspaces = withServer("ready");
  const fake = dashboard([], {
    "GET /api/workspaces": () => listing(workspaces),
    "GET /api/git/status": STATUS_CLEAN,
    "GET /api/git/branches": BRANCHES,
    "DELETE /api/workspaces/2": () => {
      workspaces = workspaces.filter((w) => w.id !== 2);
      return { deleted: 2 };
    },
  });
  const { location } = renderApp("/?workspace=1");
  await panel("alpha");
  await userEvent.click(await trash("beta"));
  const dialog = await screen.findByRole("dialog", { name: m.deleteWorkspaceTitle("beta") });
  // the check of unsaved work is beta's, not the selected alpha's
  await waitFor(() =>
    expect(fake.made("GET", "/api/git/status").map((c) => c.query.get("workspace"))).toContain("2"),
  );
  const confirm = within(dialog).getByRole("button", { name: m.deleteWorkspaceConfirm });
  await waitFor(() => expect(confirm).toBeEnabled());
  await userEvent.click(confirm);
  expect(await screen.findByRole("status")).toHaveTextContent(m.workspaceDeleted("beta"));
  expect(fake.made("DELETE", "/api/workspaces/2")).toHaveLength(1);
  expect(fake.made("DELETE", "/api/workspaces/1")).toHaveLength(0);
  expect(selectedOf(location().search)).toBe("1");
  expect(await panel("alpha")).toBeInTheDocument();
  // the trash button went with its row: focus is on the list's heading, not lost to the page
  expect(screen.getByRole("heading", { level: 1, name: m.navDashboard })).toHaveFocus();
  await waitFor(() =>
    expect(
      within(screen.getByRole("region", { name: m.navDashboard })).queryByRole("button", {
        name: "beta",
      }),
    ).not.toBeInTheDocument(),
  );
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
});

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
  expect(screen.getByRole("heading", { level: 1, name: m.navDashboard })).toHaveFocus();
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

test.each([
  ["creating", null, m.cannotDeleteCreating],
  ["deleting", null, m.cannotDeleteDeleting],
  ["deleting", ADDRESS, m.cannotDeleteDeleting],
  ["planted-state", null, m.cannotDeleteState("planted-state")],
])(
  "on a workspace that is %s (source %s) the trash button warns why it cannot be deleted, and sends nothing",
  async (state, source, reason) => {
    const fake = dashboard([workspace(1, "alpha", { state, source }), workspace(2, "beta")]);
    renderApp("/?workspace=2");
    await panel("beta");
    const opener = await trash("alpha");
    await userEvent.click(opener);
    const dialog = await screen.findByRole("dialog", { name: m.cannotDeleteTitle("alpha") });
    expect(within(dialog).getByText(reason)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: m.deleteWorkspaceConfirm })).toBeNull();
    await userEvent.click(within(dialog).getByRole("button", { name: m.close }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
    expect(fake.calls.filter((c) => c.method !== "GET")).toEqual([]);
    expect(fake.made("GET", "/api/git/status").map((c) => c.query.get("workspace"))).not.toContain(
      "1",
    );
  },
);

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

test("Delete waits for the check of unsaved work, which the question says it is making", async () => {
  let answer = () => {};
  dashboard(withServer("ready"), {
    "GET /api/git/status": () =>
      new Promise((resolve) => {
        answer = () => resolve(STATUS_CLEAN);
      }),
  });
  renderApp();
  await userEvent.click(await trash("alpha"));
  const dialog = await screen.findByRole("dialog");
  const confirm = within(dialog).getByRole("button", { name: m.deleteWorkspaceConfirm });
  expect(confirm).toBeDisabled();
  expect(within(dialog).getByText(m.deleteWorkspaceChecking)).toBeInTheDocument();
  await act(async () => answer());
  await waitFor(() => expect(confirm).toBeEnabled());
  expect(within(dialog).queryByText(m.deleteWorkspaceChecking)).not.toBeInTheDocument();
});

test("a check of unsaved work that fails is said, not taken for none, and Delete can still be chosen", async () => {
  dashboard(withServer("ready"), {
    "GET /api/git/status": problem(502, "workspace_failed", "planted: git did not answer"),
  });
  renderApp();
  await userEvent.click(await trash("alpha"));
  const dialog = await screen.findByRole("dialog");
  expect(await within(dialog).findByText(m.deleteWorkspaceUnchecked)).toBeInTheDocument();
  expect(within(dialog).getByRole("alert")).toHaveTextContent("planted: git did not answer");
  expect(within(dialog).getByRole("button", { name: m.deleteWorkspaceConfirm })).toBeEnabled();
});

test("the question says nothing of unsaved work where the repository is clean, absent or unknown", async () => {
  for (const status of [
    STATUS_CLEAN,
    NOT_A_REPOSITORY,
    { ...STATUS_CLEAN, push_ahead: null },
  ] as const) {
    dashboard(withServer("ready"), { "GET /api/git/status": status });
    const { unmount } = renderApp();
    await userEvent.click(await trash("alpha"));
    const dialog = await screen.findByRole("dialog");
    const confirm = within(dialog).getByRole("button", { name: m.deleteWorkspaceConfirm });
    await waitFor(() => expect(confirm).toBeEnabled()); // positive control: the check answered
    expect(within(dialog).getByText(m.deleteWorkspaceBody)).toBeInTheDocument();
    expect(dialog).not.toHaveTextContent(m.deleteWorkspaceUncommitted(1).slice(0, 12));
    expect(dialog).not.toHaveTextContent(m.deleteWorkspaceUnpushed(1).slice(0, 12));
    expect(within(dialog).queryByText(m.deleteWorkspaceUnchecked)).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("alert")).not.toBeInTheDocument();
    unmount();
  }
});

test.each([
  ["failed", null],
  ["creating", ADDRESS],
])(
  "a workspace that is %s (source %s) is not asked about unsaved work, and Delete is offered at once",
  async (state, source) => {
    const fake = dashboard([workspace(1, "alpha", { state, source })]);
    renderApp();
    await userEvent.click(await trash("alpha"));
    const dialog = await screen.findByRole("dialog", { name: m.deleteWorkspaceTitle("alpha") });
    expect(within(dialog).getByRole("button", { name: m.deleteWorkspaceConfirm })).toBeEnabled();
    expect(fake.made("GET", "/api/git/status")).toEqual([]);
  },
);

test("while it deletes, the dialog's buttons wait", async () => {
  let answer = () => {};
  dashboard(withServer("ready"), {
    "GET /api/git/status": STATUS_CLEAN,
    "DELETE /api/workspaces/1": () =>
      new Promise((resolve) => {
        answer = () => resolve({ deleted: 1 });
      }),
  });
  renderApp("/?workspace=1");
  await userEvent.click(await trash("alpha"));
  const dialog = await screen.findByRole("dialog");
  const confirm = within(dialog).getByRole("button", { name: m.deleteWorkspaceConfirm });
  await waitFor(() => expect(confirm).toBeEnabled());
  await userEvent.click(confirm);
  expect(await within(dialog).findByRole("button", { name: m.deleting })).toBeDisabled();
  expect(within(dialog).getByRole("button", { name: m.cancel })).toBeDisabled();
  answer();
  expect(await screen.findByRole("status")).toHaveTextContent(m.workspaceDeleted("alpha"));
});

test("a delete that fails shows the server's reason in the dialog, and the list is fetched again", async () => {
  const fake = dashboard(withServer("ready"), {
    "GET /api/git/status": STATUS_CLEAN,
    "DELETE /api/workspaces/1": problem(502, "workspace_failed", "planted: could not delete"),
  });
  renderApp("/?workspace=1");
  await userEvent.click(await trash("alpha"));
  const dialog = await screen.findByRole("dialog");
  const confirm = within(dialog).getByRole("button", { name: m.deleteWorkspaceConfirm });
  await waitFor(() => expect(confirm).toBeEnabled());
  const asked = fake.made("GET", "/api/workspaces").length;
  await userEvent.click(confirm);
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("planted: could not delete");
  await waitFor(() => expect(fake.made("GET", "/api/workspaces").length).toBeGreaterThan(asked));
  expect(within(dialog).getByRole("button", { name: m.deleteWorkspaceConfirm })).toBeEnabled();
  expect(fake.made("DELETE", "/api/workspaces/1")).toHaveLength(1);
});

test.each([
  ["ready", null],
  ["failed", null],
  ["creating", ADDRESS],
])(
  "a %s workspace's panel (source %s) has no part of its own for deleting it",
  async (state, source) => {
    dashboard([
      workspace(1, "alpha", { state, source, error: state === "failed" ? "planted" : null }),
    ]);
    renderApp("/?workspace=1");
    const opened = await panel("alpha");
    // positive control: the panel is drawn
    expect(within(opened).getByRole("heading", { name: "alpha", level: 2 })).toBeInTheDocument();
    expect(within(opened).queryByRole("region", { name: /delete/i })).toBeNull();
    expect(within(opened).queryByRole("button", { name: /delete/i })).toBeNull();
  },
);

// --- polls that fail ------------------------------------------------------------

// The page polling every second, with fake timers that still advance by themselves.
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

test("a failing poll that ends after a later one worked is not shown", () =>
  polling(async () => {
    let asked = 0;
    let failLate = () => {};
    dashboard([], {
      "GET /api/workspaces": () => {
        asked += 1;
        if (asked !== 2) return listing([workspace(1, "alpha")]);
        return new Promise((resolve) => {
          failLate = () => resolve(UNREACHABLE_REPLY);
        });
      },
      "POST /api/client-errors": {},
    });
    renderApp("/", { pollMs: 1000 });
    await list();
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
      "/api/workspaces",
      expect.any(TypeError),
    ); // positive control: the late poll did fail
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(await entry("alpha")).toBeInTheDocument();
  }));

test("the new workspace's fields show an example of what goes in them", async () => {
  dashboard([]);
  renderApp();
  const form = await newWorkspace();
  expect(form.getByLabelText(m.newWorkspaceName)).toHaveAttribute(
    "placeholder",
    m.examples.workspaceName,
  );
  expect(form.getByLabelText(m.newWorkspaceSource)).toHaveAttribute(
    "placeholder",
    m.examples.address,
  );
});

// --- the panel's tabs ----------------------------------------------------------------

const tabs = async (name = "alpha") => within(await panel(name)).findByRole("tablist");
const tab = async (label: string) => within(await tabs()).getByRole("tab", { name: label });
const tabOf = (search: string) => new URLSearchParams(search).get("tab");

test("a ready workspace's panel has the tabs Overview and Terminal, Overview chosen first", async () => {
  dashboard(withServer("ready"));
  const { location } = renderApp("/?workspace=1");
  const list = await tabs();
  expect(list).toHaveAccessibleName(m.workspaceTabs);
  const names = within(list)
    .getAllByRole("tab")
    .map((t) => t.textContent);
  expect(names).toEqual([m.tabOverview, m.tabTerminal]);
  const overview = await tab(m.tabOverview);
  const terminal = await tab(m.tabTerminal);
  expect(overview).toHaveAttribute("aria-selected", "true");
  expect(terminal).toHaveAttribute("aria-selected", "false");
  expect(overview).toHaveAttribute("tabindex", "0");
  expect(terminal).toHaveAttribute("tabindex", "-1");
  const shown = screen.getByRole("tabpanel", { name: m.tabOverview });
  expect(overview).toHaveAttribute("aria-controls", shown.id);
  expect(within(shown).getByRole("region", { name: m.serverTitle })).toBeInTheDocument();
  expect(tabOf(location().search)).toBeNull();
  expect(sockets()).toEqual([]); // the terminal is not opened until its tab is shown
});

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

test("the address's tab opens the panel at it, and one it does not know opens Overview", async () => {
  dashboard(withServer("ready"));
  renderApp("/?workspace=1&tab=terminal");
  expect(await tab(m.tabTerminal)).toHaveAttribute("aria-selected", "true");
  await waitFor(() => expect(sockets()).toHaveLength(1));
  cleanup();
  dashboard(withServer("ready"));
  renderApp("/?workspace=1&tab=planted");
  expect(await tab(m.tabOverview)).toHaveAttribute("aria-selected", "true");
  expect(sockets()).toHaveLength(1); // none more
});

test("choosing a tab replaces the address's entry, so Back leaves the workspace, not the tab", async () => {
  dashboard(withServer("ready"));
  const { location, navigate } = renderApp("/");
  await userEvent.click(await entry("alpha"));
  await userEvent.click(await tab(m.tabTerminal));
  await userEvent.click(await tab(m.tabOverview));
  await userEvent.click(await tab(m.tabTerminal));
  expect(tabOf(location().search)).toBe("terminal"); // positive control: the tabs moved
  await navigate(-1);
  expect(selectedOf(location().search)).toBeNull();
  expect(tabOf(location().search)).toBeNull();
});

test("each tab's panel can take focus, as the tab pattern asks", async () => {
  dashboard(withServer("ready"));
  renderApp("/?workspace=1");
  await tabs();
  for (const panelOfTab of screen
    .getByRole("region", { name: "alpha" })
    .querySelectorAll("[role=tabpanel]"))
    expect(panelOfTab).toHaveAttribute("tabindex", "0");
});

test("the arrow keys, Home and End move between the tabs, choosing each", async () => {
  dashboard(withServer("ready"));
  const { location } = renderApp("/?workspace=1");
  (await tab(m.tabOverview)).focus();
  await userEvent.keyboard("{ArrowRight}");
  expect(await tab(m.tabTerminal)).toHaveFocus();
  expect(await tab(m.tabTerminal)).toHaveAttribute("aria-selected", "true");
  expect(tabOf(location().search)).toBe("terminal");
  await userEvent.keyboard("{ArrowRight}"); // round to the first
  expect(await tab(m.tabOverview)).toHaveFocus();
  expect(await tab(m.tabOverview)).toHaveAttribute("aria-selected", "true");
  await userEvent.keyboard("{ArrowLeft}"); // round to the last
  expect(await tab(m.tabTerminal)).toHaveFocus();
  await userEvent.keyboard("{Home}");
  expect(await tab(m.tabOverview)).toHaveFocus();
  await userEvent.keyboard("{End}");
  expect(await tab(m.tabTerminal)).toHaveFocus();
  expect(await tab(m.tabTerminal)).toHaveAttribute("aria-selected", "true");
});

test.each(["creating", "failed", "deleting"])(
  "a workspace that is %s has no tabs and no Terminal, even when the address asks for it",
  async (state) => {
    dashboard([workspace(1, "alpha", { state, source: ADDRESS, source_page: ADDRESS })]);
    renderApp("/?workspace=1&tab=terminal");
    const opened = await panel("alpha");
    await within(opened).findByRole("link", { name: ADDRESS }); // positive control: drawn
    expect(within(opened).queryByRole("tablist")).toBeNull();
    expect(within(opened).queryByRole("tab", { name: m.tabTerminal })).toBeNull();
    expect(sockets()).toEqual([]);
  },
);

test("the repository's link stays in the panel's head, above the tabs", async () => {
  dashboard([workspace(1, "alpha", { source: ADDRESS, source_page: ADDRESS })]);
  renderApp("/?workspace=1&tab=terminal");
  const opened = await panel("alpha");
  const name = within(opened).getByRole("heading", { name: "alpha", level: 2 });
  const link = await within(opened).findByRole("link", { name: ADDRESS });
  expect(name.closest(".panel-head")).toContainElement(link);
  const list = await tabs();
  expect(link.compareDocumentPosition(list) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
});

test("while Terminal is shown, the trash button's question still names the work git knows of", async () => {
  dashboard(withServer("ready"), {
    "GET /api/git/status": {
      ...STATUS_CLEAN,
      not_staged: { files: [{ path: "b.txt", change: "untracked" }], more: 0 },
      push_ahead: 1,
    },
    "GET /api/git/branches": BRANCHES,
  });
  renderApp("/?workspace=1&tab=terminal");
  const opened = await panel("alpha");
  expect(await tab(m.tabTerminal)).toHaveAttribute("aria-selected", "true");
  expect(within(opened).queryByRole("region", { name: m.changesTitle })).toBeNull(); // hidden
  await userEvent.click(await trash("alpha"));
  const dialog = await screen.findByRole("dialog");
  expect(await within(dialog).findByText(m.deleteWorkspaceUncommitted(1))).toBeInTheDocument();
  expect(within(dialog).getByText(m.deleteWorkspaceUnpushed(1))).toBeInTheDocument();
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

const ROLLED_BACK =
  "The rebuild failed, so the workspace was put back as it was. Why it failed is below.\nplanted: the image could not be pulled";

test("Overview holds the Container card between Claude server and Changes, a sibling card of both", async () => {
  dashboard([rebuildable()], changedSays(false));
  renderApp("/?workspace=1");
  const opened = within(await panel("alpha"));
  const server = await opened.findByRole("region", { name: m.serverTitle });
  const container = await opened.findByRole("region", { name: m.containerTitle });
  const changes = await opened.findByRole("region", { name: m.changesTitle });
  expect(container).toHaveClass("card");
  expect(container.querySelector(".card")).toBeNull();
  expect(container.parentElement).toBe(server.parentElement);
  expect(server.nextElementSibling).toBe(container);
  expect(container.nextElementSibling).toBe(changes);
});

test("a changed devcontainer.json is said in the card, and Rebuild leads as the primary button", async () => {
  const fake = dashboard([rebuildable()], changedSays(true));
  renderApp("/?workspace=1");
  const card = await containerCard();
  expect(await card.findByText(m.devcontainerChanged)).toBeInTheDocument();
  expect(card.getByRole("button", { name: m.rebuild })).toHaveClass("primary");
  expect(fake.made("GET", "/api/workspaces/1/devcontainer").length).toBeGreaterThan(0);
});

test.each([
  ["unchanged", false],
  ["not known", null],
])(
  "a devcontainer.json %s is not remarked on, and Rebuild is offered as a plain button",
  async (_, changed) => {
    const fake = dashboard([rebuildable()], changedSays(changed));
    renderApp("/?workspace=1");
    const card = await containerCard();
    await waitFor(() =>
      expect(fake.made("GET", "/api/workspaces/1/devcontainer").length).toBeGreaterThan(0),
    );
    const rebuild = await card.findByRole("button", { name: m.rebuild });
    expect(rebuild).not.toHaveClass("primary");
    expect(rebuild).toBeEnabled();
    expect(card.queryByText(m.devcontainerChanged)).not.toBeInTheDocument();
  },
);

test("a read of the file that fails is taken as not known: no notice and no alert", async () => {
  dashboard([rebuildable()], {
    "GET /api/workspaces/1/devcontainer": problem(409, "workspace_not_ready", "Planted."),
  });
  renderApp("/?workspace=1");
  const card = await containerCard();
  expect(await card.findByRole("button", { name: m.rebuild })).not.toHaveClass("primary");
  expect(card.queryByRole("alert")).toBeNull();
  expect(card.queryByText(m.devcontainerChanged)).not.toBeInTheDocument();
});

test("the file is asked about again as the page refreshes, and a change is said once it is seen", async () => {
  let changed = false;
  dashboard([rebuildable()], { "GET /api/workspaces/1/devcontainer": () => ({ changed }) });
  renderApp("/?workspace=1", { pollMs: 50 });
  const card = await containerCard();
  await card.findByRole("button", { name: m.rebuild });
  expect(card.queryByText(m.devcontainerChanged)).not.toBeInTheDocument();
  changed = true;
  expect(await card.findByText(m.devcontainerChanged)).toBeInTheDocument();
  expect(card.getByRole("button", { name: m.rebuild })).toHaveClass("primary");
});

test("Rebuild is not offered for a workspace the server says cannot be rebuilt now", async () => {
  dashboard([rebuildable(), workspace(2, "beta", { source: ADDRESS })], changedSays(true));
  renderApp("/?workspace=1");
  expect(await (await containerCard()).findByRole("button", { name: m.rebuild })).toBeEnabled(); // control
  cleanup();
  dashboard([workspace(2, "beta", { source: ADDRESS, rebuildable: false })], {
    "GET /api/workspaces/2/devcontainer": { changed: true },
  });
  renderApp("/?workspace=2");
  const card = await containerCard("beta");
  expect(await card.findByText(m.devcontainerChanged)).toBeInTheDocument();
  expect(card.queryByRole("button", { name: m.rebuild })).not.toBeInTheDocument();
});

test("while rcman is being upgraded, Rebuild is not offered", async () => {
  dashboard([], {
    "GET /api/workspaces": { ...listing([rebuildable()]), rcman_upgrading: true },
    ...changedSays(true),
  });
  renderApp("/?workspace=1");
  const card = await containerCard();
  await card.findByText(m.devcontainerChanged);
  expect(card.queryByRole("button", { name: m.rebuild })).not.toBeInTheDocument();
  expect(screen.getByText(m.workspacesUpgrading)).toBeInTheDocument();
});

test("an empty workspace's card says it has nothing to be rebuilt from", async () => {
  dashboard([workspace(1, "alpha")], changedSays(null));
  renderApp("/?workspace=1");
  const card = await containerCard();
  expect(card.getByText(m.containerEmpty)).toBeInTheDocument();
  expect(card.queryByRole("button", { name: m.rebuild })).not.toBeInTheDocument();
});

test("Rebuild asks first, naming what ends and what is kept, and Cancel sends nothing", async () => {
  const fake = dashboard([rebuildable()], changedSays(true));
  renderApp("/?workspace=1");
  await userEvent.click(await (await containerCard()).findByRole("button", { name: m.rebuild }));
  const dialog = within(await screen.findByRole("dialog", { name: m.rebuildTitle("alpha") }));
  expect(dialog.getByText(m.rebuildIntro)).toBeInTheDocument();
  for (const line of [...m.rebuildEnds, ...m.rebuildKeeps])
    expect(dialog.getByText(line)).toBeInTheDocument();
  expect(dialog.getByText(m.rebuildCommands)).toBeInTheDocument();
  expect(dialog.getByText(m.rebuildMemory)).toBeInTheDocument();
  expect(dialog.queryByText(m.rebuildCommandsFailed)).not.toBeInTheDocument();
  // the words say what ends and what stays
  const text = [...m.rebuildEnds, ...m.rebuildKeeps].join(" ");
  for (const word of ["terminal", "nohup", "setsid", "in /tmp", "work folder", "home", "git"])
    expect(text).toContain(word);
  const confirm = dialog.getByRole("button", { name: m.rebuildConfirm });
  expect(confirm).toHaveClass("primary");
  await userEvent.click(dialog.getByRole("button", { name: m.cancel }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  expect(fake.made("POST", "/api/workspaces/1/rebuild")).toEqual([]);
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

test("a refused rebuild shows the server's reason in the dialog, and the list is fetched again", async () => {
  const fake = dashboard([rebuildable()], {
    ...changedSays(true),
    "POST /api/workspaces/1/rebuild": problem(
      409,
      "workspace_rebuilding",
      "Planted: the workspace is being rebuilt.",
    ),
  });
  renderApp("/?workspace=1");
  await userEvent.click(await (await containerCard()).findByRole("button", { name: m.rebuild }));
  const dialog = within(await screen.findByRole("dialog", { name: m.rebuildTitle("alpha") }));
  const before = fake.made("GET", "/api/workspaces").length;
  await userEvent.click(dialog.getByRole("button", { name: m.rebuildConfirm }));
  expect(await dialog.findByRole("alert")).toHaveTextContent(
    "Planted: the workspace is being rebuilt.",
  );
  await waitFor(() => expect(fake.made("GET", "/api/workspaces").length).toBeGreaterThan(before));
});

test.each(["preparing", "switching", "promoting"])(
  "while a rebuild is %s, Rebuild says it is rebuilding and waits, and the steps are announced politely",
  async (state) => {
    dashboard(
      [
        rebuildable({
          rebuildable: false,
          rebuild: { state, log: "Pulling the image\nMaking the container", error: null },
        }),
      ],
      changedSays(null),
    );
    renderApp("/?workspace=1");
    const card = await containerCard();
    const busy = await card.findByRole("button", { name: m.rebuildBusy });
    expect(busy).toBeDisabled();
    expect(card.getByText(m.rebuildRunning)).toBeInTheDocument();
    const steps = card.getByRole("figure", { name: m.workspaceLog });
    expect(steps.querySelector('[aria-live="polite"]')).toHaveTextContent("Making the container");
  },
);

test("a rebuild that did not finish says what happened and what to do next, as a problem", async () => {
  dashboard(
    [rebuildable({ rebuild: { state: null, log: "Pulling the image", error: ROLLED_BACK } })],
    changedSays(true),
  );
  renderApp("/?workspace=1");
  const card = await containerCard();
  const alert = await card.findByRole("alert");
  expect(alert).toHaveTextContent(m.rebuildFailed);
  expect(alert).toHaveTextContent(ROLLED_BACK.split("\n")[0]);
  expect(alert).toHaveTextContent("planted: the image could not be pulled");
  expect(card.getByRole("button", { name: m.rebuild })).toBeEnabled(); // it can be tried again
});

test("a workspace whose container a rebuild replaces shows its server as being rebuilt, not as a problem", async () => {
  dashboard(
    [
      rebuildable({
        rebuildable: false,
        rebuild: { state: "switching", log: null, error: null },
        server_error: { code: "workspace_rebuilding", title: "Planted: being rebuilt." },
      }),
    ],
    changedSays(null),
  );
  renderApp("/?workspace=1");
  const server = within(
    await within(await panel("alpha")).findByRole("region", { name: m.serverTitle }),
  );
  expect(server.getByText(m.serverRebuilding)).toBeInTheDocument();
  expect(server.queryByRole("alert")).toBeNull();
  expect(server.queryByText(m.serverUnreadNextStep)).not.toBeInTheDocument();
  expect(server.queryByRole("button", { name: m.start })).not.toBeInTheDocument();
});

// --- rebuilding a workspace that failed ---------------------------------------------

const HOOK_FAILED = "postCreateCommand failed, exit code 1. What it printed last is below.";

test("a failed workspace that can be rebuilt offers Rebuild beside its reason", async () => {
  dashboard([
    rebuildable({ state: "failed", error: HOOK_FAILED, log: STEPS }),
    workspace(2, "beta", { state: "failed", error: HOOK_FAILED, source: ADDRESS }),
  ]);
  renderApp("/?workspace=1");
  const opened = within(await panel("alpha"));
  const reason = opened.getByText(m.workspaceFailed);
  const rebuild = opened.getByRole("button", { name: m.rebuild });
  expect(rebuild).toHaveClass("primary");
  expect(reason.compareDocumentPosition(rebuild) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  cleanup();
  dashboard([workspace(2, "beta", { state: "failed", error: HOOK_FAILED, source: ADDRESS })]);
  renderApp("/?workspace=2");
  const other = within(await panel("beta"));
  expect(other.getByText(m.workspaceFailed)).toBeInTheDocument();
  expect(other.queryAllByRole("button")).toEqual([]);
});

test("rebuilding a failed workspace says its commands run again on the files, with nothing earlier to go back to", async () => {
  const fake = dashboard([rebuildable({ state: "failed", error: HOOK_FAILED })], {
    "POST /api/workspaces/1/rebuild": () => ({
      workspace: rebuildable({ state: "failed", error: HOOK_FAILED }),
    }),
  });
  renderApp("/?workspace=1");
  await userEvent.click(within(await panel("alpha")).getByRole("button", { name: m.rebuild }));
  const dialog = within(await screen.findByRole("dialog", { name: m.rebuildTitle("alpha") }));
  expect(dialog.getByText(m.rebuildIntroFailed)).toBeInTheDocument();
  expect(dialog.getByText(m.rebuildCommandsFailed)).toBeInTheDocument();
  expect(dialog.queryByText(m.rebuildIntro)).not.toBeInTheDocument();
  await userEvent.click(dialog.getByRole("button", { name: m.rebuildConfirm }));
  await waitFor(() => expect(fake.made("POST", "/api/workspaces/1/rebuild")).toHaveLength(1));
  expect(fake.made("POST", "/api/workspaces/1/rebuild")[0].body).toEqual({ confirm: true });
});

test("a failed workspace being rebuilt shows the rebuild's steps, and Rebuild waits", async () => {
  dashboard([
    rebuildable({
      state: "failed",
      error: HOOK_FAILED,
      log: "the old steps",
      rebuildable: false,
      rebuild: { state: "preparing", log: "Reading devcontainer.json", error: null },
    }),
  ]);
  renderApp("/?workspace=1");
  const opened = within(await panel("alpha"));
  expect(opened.getByText(m.rebuildRunning)).toBeInTheDocument();
  expect(opened.getByRole("button", { name: m.rebuildBusy })).toBeDisabled();
  const steps = opened.getByRole("figure", { name: m.workspaceLog });
  expect(steps).toHaveTextContent("Reading devcontainer.json");
  expect(steps).not.toHaveTextContent("the old steps");
  expect(steps.querySelector('[aria-live="polite"]')).not.toBeNull();
});

test("a failed workspace whose rebuild failed again says the new reason, with the rebuild's steps", async () => {
  dashboard([
    rebuildable({
      state: "failed",
      error: "planted: the new reason",
      log: "the old steps",
      rebuild: { state: null, log: "Reading devcontainer.json", error: null },
    }),
  ]);
  renderApp("/?workspace=1");
  const opened = within(await panel("alpha"));
  expect(opened.getByText("planted: the new reason")).toBeInTheDocument();
  expect(opened.getByRole("figure", { name: m.workspaceLog })).toHaveTextContent(
    "Reading devcontainer.json",
  );
  expect(opened.getByRole("button", { name: m.rebuild })).toBeEnabled();
});
