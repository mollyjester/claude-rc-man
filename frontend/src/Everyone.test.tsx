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
  ...extra,
});

const PEOPLE = {
  people: [
    {
      email: "alice@example.com",
      workspaces: [
        workspace(1, "alpha", { server: server("ready", "alpha-quiet-otter") }),
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

test("each person's workspaces are listed with their server's state and session", async () => {
  api();
  renderApp("/admin/workspaces");
  const alice = await person("alice@example.com");
  const headers = within(alice)
    .getAllByRole("columnheader")
    .map((h) => h.textContent);
  expect(headers).toEqual([m.everyoneWorkspace, m.everyoneState, m.sessionName]);
  const alpha = within(alice).getByRole("row", { name: /alpha/ });
  expect(within(alpha).getByText(m.states.ready)).toHaveClass("badge", `badge-${KIND.ready}`);
  expect(within(alpha).getByText("alpha-quiet-otter")).toBeInTheDocument();
  const beta = within(alice).getByRole("row", { name: /beta/ });
  expect(within(beta).getByText(m.states.stopped)).toBeInTheDocument();
  const gamma = within(alice).getByRole("row", { name: /gamma/ });
  expect(within(gamma).getByText(m.everyoneNoServer)).toBeInTheDocument();
  const delta = within(alice).getByRole("row", { name: /delta/ });
  expect(within(delta).getByText(m.workspaceStates.creating)).toBeInTheDocument();
  const bob = await person("bob@example.com");
  expect(within(bob).getByText(m.everyoneNone)).toBeInTheDocument();
});

test("a workspace whose server could not be read shows the server's reason", async () => {
  api();
  renderApp("/admin/workspaces");
  const alice = await person("alice@example.com");
  const epsilon = within(alice).getByRole("row", { name: /epsilon/ });
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
  expect(within(main).getAllByRole("row").length).toBeGreaterThan(1); // positive control
  expect(within(main).queryAllByRole("button")).toEqual([]);
  expect(within(main).queryAllByRole("link")).toEqual([]);
});
