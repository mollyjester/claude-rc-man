import { act, cleanup, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

import { m } from "./messages";
import { ACTIONS, KIND, LABELS, PRIMARY } from "./Servers";
import { fakeApi, ME, problem, type Routes, UNREACHABLE_REPLY } from "./test/fakeApi";
import { renderApp } from "./test/render";

type Workspace = {
  id: number;
  name: string;
  state: string;
  error: string | null;
  server: Record<string, unknown> | null;
  server_error: { code: string; title: string } | null;
};

function workspace(id: number, name: string, extra: Partial<Workspace> = {}): Workspace {
  return { id, name, state: "ready", error: null, server: null, server_error: null, ...extra };
}

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

function dashboard(workspaces: Workspace[], extra: Routes = {}) {
  return fakeApi({
    "GET /api/me": ME,
    "GET /api/workspaces": { workspaces },
    ...extra,
  });
}

const list = async () => screen.findByRole("region", { name: m.workspacesTitle });
const panel = async (name: string) => screen.findByRole("region", { name });
const entry = async (name: string) => within(await list()).findByRole("button", { name });
const selectedOf = (search: string) => new URLSearchParams(search).get("workspace");

// The card of alpha's server, with alpha selected from the address.
async function card() {
  renderApp("/?workspace=1");
  return within(await within(await panel("alpha")).findByRole("article", { name: "alpha" }));
}

function expectNoDot(button: HTMLElement) {
  expect(button).toHaveAccessibleDescription("");
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

test("with no workspaces, the list says so", async () => {
  dashboard([]);
  renderApp();
  expect(await within(await list()).findByText(m.workspacesEmpty)).toBeInTheDocument();
  expect(within(await list()).queryAllByRole("button")).toEqual([]);
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
    expect(alpha).toHaveAccessibleDescription(m.kinds[kind]);
    // its shape is the kind's class, its meaning a title on hover, and the
    // description is read once, through the name it describes
    const dot = alpha.closest("li")!.querySelector(".dot")!;
    expect(dot).toHaveClass(`dot-${kind}`);
    expect(dot).toHaveAttribute("title", m.kinds[kind]);
    const description = document.getElementById(alpha.getAttribute("aria-describedby")!)!;
    expect(description).toHaveTextContent(m.kinds[kind]);
    expect(description).toHaveAttribute("hidden");
    expectNoDot(await entry("beta"));
  },
);

test("the check for no dot fails on a workspace that has one", async () => {
  dashboard(withServer("ready"));
  renderApp();
  const alpha = await entry("alpha");
  expect(alpha).toHaveAccessibleDescription(m.kinds.running); // the plant took effect
  expect(() => expectNoDot(alpha)).toThrow();
});

test("a workspace that failed, or whose server could not be read, has the problem dot", async () => {
  dashboard([
    workspace(1, "alpha", { state: "failed", error: "planted failure" }),
    workspace(2, "beta", { server_error: { code: "workspace_failed", title: "Planted." } }),
    workspace(3, "gamma"),
  ]);
  renderApp();
  expect(await entry("alpha")).toHaveAccessibleDescription(m.kinds.problem);
  expect(await entry("beta")).toHaveAccessibleDescription(m.kinds.problem);
  expectNoDot(await entry("gamma"));
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
    "GET /api/workspaces": () => ({ workspaces }),
    "POST /api/workspaces": (c) => {
      const made = workspace(7, (c.body as { name: string }).name, { state: "creating" });
      workspaces.push(made);
      return { workspace: made };
    },
  });
  const { location } = renderApp();
  const form = within(await screen.findByRole("region", { name: m.newWorkspace }));
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
  expect(form.getByLabelText(m.newWorkspaceName)).toHaveValue("");
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
  const form = within(await screen.findByRole("region", { name: m.newWorkspace }));
  await userEvent.type(form.getByLabelText(m.newWorkspaceName), "bad name!");
  await userEvent.click(form.getByRole("button", { name: m.create }));
  expect(await form.findByRole("alert")).toHaveTextContent(
    `${m.newWorkspaceName}: planted reason for the name`,
  );
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
  const form = within(await screen.findByRole("region", { name: m.newWorkspace }));
  await userEvent.type(form.getByLabelText(m.newWorkspaceName), "slow");
  await userEvent.click(form.getByRole("button", { name: m.create }));
  const busy = await form.findByRole("button", { name: m.creating });
  expect(busy).toBeDisabled();
  await userEvent.click(busy);
  expect(fake.made("POST", "/api/workspaces")).toHaveLength(1);
  answer();
  expect(await form.findByRole("button", { name: m.create })).toBeEnabled();
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
    "GET /api/workspaces": () => ({ workspaces: [workspace(1, "alpha", { state })] }),
  });
  renderApp("/?workspace=1", { pollMs: 50 });
  const opened = await panel("alpha");
  expect(within(opened).getByText(m.workspaceCreating)).toBeInTheDocument();
  state = "ready";
  expect(await within(opened).findByRole("button", { name: m.start })).toBeInTheDocument();
  expect(within(opened).queryByText(m.workspaceCreating)).not.toBeInTheDocument();
});

test("a workspace that failed shows why, and offers Delete but not Start", async () => {
  dashboard([workspace(1, "alpha", { state: "failed", error: "planted: the image is missing" })]);
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  expect(within(opened).getByText(m.workspaceFailed)).toBeInTheDocument();
  expect(within(opened).getByText("planted: the image is missing")).toBeInTheDocument();
  expect(within(opened).getByRole("button", { name: m.deleteWorkspace })).toBeInTheDocument();
  expect(within(opened).queryByRole("button", { name: m.start })).not.toBeInTheDocument();
});

test("a workspace being deleted says so, and offers nothing", async () => {
  dashboard([workspace(1, "alpha", { state: "deleting" })]);
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  expect(within(opened).getByText(m.workspaceDeleting)).toBeInTheDocument();
  expect(within(opened).queryAllByRole("button")).toEqual([]);
});

test("a ready workspace with no server offers Start, which starts one there", async () => {
  const workspaces = [workspace(2, "beta")];
  let answer = () => {};
  const fake = dashboard([], {
    "GET /api/workspaces": () => ({ workspaces }),
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

test("Delete workspace asks first, saying everything in it and its server go, and calls nothing until confirmed", async () => {
  const fake = dashboard(withServer("ready"));
  renderApp("/?workspace=1");
  const opened = await panel("alpha");
  const remove = within(opened).getByRole("button", { name: m.deleteWorkspace });
  expect(remove).toHaveClass("secondary-danger");
  await userEvent.click(remove);
  const dialog = await screen.findByRole("dialog", { name: m.deleteWorkspaceTitle("alpha") });
  expect(within(dialog).getByText(m.deleteWorkspaceBody)).toBeInTheDocument();
  expect(within(dialog).getByRole("button", { name: m.deleteWorkspaceConfirm })).toHaveClass(
    "danger",
  );
  expect(fake.calls.filter((c) => c.method === "DELETE")).toEqual([]);
  await userEvent.click(within(dialog).getByRole("button", { name: m.cancel }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(fake.calls.filter((c) => c.method === "DELETE")).toEqual([]);
});

test("confirming deletes the workspace, refreshes the list, selects none and says it was deleted", async () => {
  let workspaces = withServer("ready");
  const fake = dashboard([], {
    "GET /api/workspaces": () => ({ workspaces }),
    "DELETE /api/workspaces/1": () => {
      workspaces = workspaces.filter((w) => w.id !== 1);
      return { deleted: 1 };
    },
  });
  const { location } = renderApp("/?workspace=1");
  const opened = await panel("alpha");
  await userEvent.click(within(opened).getByRole("button", { name: m.deleteWorkspace }));
  const dialog = await screen.findByRole("dialog");
  await userEvent.click(within(dialog).getByRole("button", { name: m.deleteWorkspaceConfirm }));
  expect(await screen.findByRole("status")).toHaveTextContent(m.workspaceDeleted("alpha"));
  expect(fake.made("DELETE", "/api/workspaces/1")).toHaveLength(1);
  expect(selectedOf(location().search)).toBeNull();
  await waitFor(() =>
    expect(
      within(screen.getByRole("region", { name: m.workspacesTitle })).queryByRole("button", {
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

test("while it deletes, the dialog's buttons wait", async () => {
  let answer = () => {};
  dashboard(withServer("ready"), {
    "DELETE /api/workspaces/1": () =>
      new Promise((resolve) => {
        answer = () => resolve({ deleted: 1 });
      }),
  });
  renderApp("/?workspace=1");
  await userEvent.click(
    within(await panel("alpha")).getByRole("button", { name: m.deleteWorkspace }),
  );
  const dialog = await screen.findByRole("dialog");
  await userEvent.click(within(dialog).getByRole("button", { name: m.deleteWorkspaceConfirm }));
  expect(await within(dialog).findByRole("button", { name: m.deleting })).toBeDisabled();
  expect(within(dialog).getByRole("button", { name: m.cancel })).toBeDisabled();
  answer();
  expect(await screen.findByRole("status")).toHaveTextContent(m.workspaceDeleted("alpha"));
});

test("a delete that fails shows the server's reason in the dialog, and the list is fetched again", async () => {
  const fake = dashboard(withServer("ready"), {
    "DELETE /api/workspaces/1": problem(502, "workspace_failed", "planted: could not delete"),
  });
  renderApp("/?workspace=1");
  await userEvent.click(
    within(await panel("alpha")).getByRole("button", { name: m.deleteWorkspace }),
  );
  const dialog = await screen.findByRole("dialog");
  const asked = fake.made("GET", "/api/workspaces").length;
  await userEvent.click(within(dialog).getByRole("button", { name: m.deleteWorkspaceConfirm }));
  expect(await within(dialog).findByRole("alert")).toHaveTextContent("planted: could not delete");
  await waitFor(() => expect(fake.made("GET", "/api/workspaces").length).toBeGreaterThan(asked));
  expect(within(dialog).getByRole("button", { name: m.deleteWorkspaceConfirm })).toBeEnabled();
});

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
      "GET /api/workspaces": () => (down ? UNREACHABLE_REPLY : { workspaces: [] }),
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
        if (asked !== 2) return { workspaces: [workspace(1, "alpha")] };
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
