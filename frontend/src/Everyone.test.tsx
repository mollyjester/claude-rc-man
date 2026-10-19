import { screen, within } from "@testing-library/react";
import { m } from "./messages";
import { KIND } from "./Servers";
import { ADMIN_USER, fakeApi, NOTHING_TO_ATTEND } from "./test/fakeApi";
import { renderApp } from "./test/render";

const server = (state: string, session_name: string | null = null) => ({
  state,
  session_name,
  exit_code: null,
});

const workspace = (id: number, name: string, extra: Record<string, unknown> = {}) => ({
  id,
  name,
  state: "ready",
  server: null,
  server_error: null,
  claude_version: null,
  ...extra,
});

const PEOPLE = {
  people: [
    {
      email: "alice@example.com",
      workspaces: [
        workspace(1, "alpha", {
          server: server("ready", "alpha-quiet-otter"),
          claude_version: "2.1.0",
        }),
        workspace(2, "beta", { server: server("stopped") }),
        workspace(3, "gamma"),
        workspace(4, "delta", { state: "creating" }),
        workspace(5, "epsilon", {
          server_error: {
            code: "workspace_failed",
            title: "planted: the server could not be read",
          },
        }),
      ],
    },
    { email: "bob@example.com", workspaces: [] },
  ],
};

function api() {
  return fakeApi({
    "GET /api/me": ADMIN_USER,
    "GET /api/attention": NOTHING_TO_ATTEND,
    "GET /api/servers/everyone": PEOPLE,
  });
}

const person = (email: string) => screen.findByRole("region", { name: email });

// A workspace's row in a person's card, by its name.
function row(card: HTMLElement, name: string): HTMLElement {
  const found = within(card)
    .getAllByRole("listitem")
    .filter((item) => item.querySelector(".ws-name")?.textContent === name);
  expect(found).toHaveLength(1);
  return found[0];
}

test("each person is a card, their address as its heading, with how many workspaces they have", async () => {
  api();
  renderApp("/admin/workspaces");
  const title = await screen.findByRole("heading", { level: 1, name: m.everyoneTitle });
  expect(title.parentElement).toHaveClass("page-head");
  const alice = await person("alice@example.com");
  expect(alice).toHaveClass("card");
  expect(
    within(alice).getByRole("heading", { level: 2, name: "alice@example.com" }),
  ).toBeInTheDocument();
  expect(alice).toHaveTextContent(m.workspaceCount(5));
  expect(within(alice).getAllByRole("listitem")).toHaveLength(5);
});

test("each workspace is a row of its list with its state as a pill and its session", async () => {
  api();
  renderApp("/admin/workspaces");
  const alice = await person("alice@example.com");
  const alpha = row(alice, "alpha");
  expect(alpha).toHaveClass("list-row");
  expect(within(alpha).getByText(m.states.ready)).toHaveClass("badge", `badge-${KIND.ready}`);
  const session = within(alpha).getByText("alpha-quiet-otter");
  expect(session.tagName).toBe("CODE");
  expect(session).toHaveAttribute("title", m.sessionName); // what it is, as the panel names it
  expect(within(row(alice, "beta")).getByText(m.states.stopped)).toHaveClass("badge");
  expect(within(row(alice, "gamma")).getByText(m.everyoneNoServer)).toHaveClass("badge");
  const delta = within(row(alice, "delta")).getByText(m.workspaceStates.creating);
  expect(delta).toHaveClass("badge");
  const bob = await person("bob@example.com");
  expect(within(bob).getByText(m.everyoneNone)).toBeInTheDocument();
  expect(within(bob).queryAllByRole("listitem")).toEqual([]);
});

test("a workspace shows the Claude Code version its server runs, and nothing where none does", async () => {
  api();
  renderApp("/admin/workspaces");
  const alice = await person("alice@example.com");
  expect(row(alice, "alpha")).toHaveTextContent(m.serverClaudeVersion("2.1.0"));
  for (const name of ["beta", "gamma"]) // stopped, or no server: no version
    expect(row(alice, name).textContent).not.toMatch(/Claude Code/);
});

test("a workspace whose server could not be read shows the server's reason", async () => {
  api();
  renderApp("/admin/workspaces");
  const alice = await person("alice@example.com");
  const epsilon = row(alice, "epsilon");
  expect(within(epsilon).getByRole("alert")).toHaveTextContent(
    "planted: the server could not be read",
  );
  // only there
  expect(within(alice).getAllByRole("alert")).toHaveLength(1);
});

test("nothing on the page acts on anyone's workspaces", async () => {
  api();
  renderApp("/admin/workspaces");
  const main = (await screen.findByRole("heading", { name: m.everyoneTitle })).closest("main")!;
  await person("alice@example.com");
  expect(within(main).getAllByRole("listitem").length).toBeGreaterThan(1); // positive control
  expect(within(main).queryAllByRole("button")).toEqual([]);
  expect(within(main).queryAllByRole("link")).toEqual([]);
});
