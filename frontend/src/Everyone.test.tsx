import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { m } from "./messages";
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
  device: `rcman-ws-${id}`,
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
          stoppable: true,
        }),
        workspace(2, "beta", { server: server("stopped") }),
        workspace(3, "gamma", { device: "planted-device-g" }),
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

test("each person's workspaces are listed with their state and session", async () => {
  api();
  renderApp("/admin/workspaces");
  const alice = await person("alice@example.com");
  const alpha = row(alice, "alpha");
  expect(within(alpha).getByText(m.states.ready)).toBeInTheDocument();
  expect(within(alpha).getByText("alpha-quiet-otter")).toBeInTheDocument();
  expect(within(row(alice, "beta")).getByText(m.states.stopped)).toBeInTheDocument();
  expect(within(row(alice, "delta")).getByText(m.workspaceStates.creating)).toBeInTheDocument();
  const bob = await person("bob@example.com");
  expect(within(bob).getByText(m.everyoneNone)).toBeInTheDocument();
  expect(within(bob).queryAllByRole("listitem")).toEqual([]);
});

test("an administrator's one action is Stop, on a workspace that may be stopped, which sends the stop at once", async () => {
  const fake = api();
  const stopped = { ...PEOPLE.people[0].workspaces[0], state: "halted", stoppable: false };
  fake.routes["POST /api/workspaces/1/stop"] = () => {
    const [alice, ...others] = PEOPLE.people;
    const theirs = [stopped, ...alice.workspaces.slice(1)];
    fake.routes["GET /api/servers/everyone"] = {
      people: [{ ...alice, workspaces: theirs }, ...others],
    };
    return { workspace: stopped };
  };
  renderApp("/admin/workspaces");
  const main = (await screen.findByRole("heading", { name: m.everyoneTitle })).closest("main")!;
  const alice = await person("alice@example.com");
  expect(within(main).getAllByRole("listitem").length).toBeGreaterThan(1); // positive control
  expect(within(main).queryAllByRole("link")).toEqual([]);
  // alpha alone may be stopped, and nothing else is offered
  const stop = within(row(alice, "alpha")).getByRole("button", { name: m.stopWorkspace });
  expect(within(main).getAllByRole("button")).toEqual([stop]);
  await userEvent.click(stop);
  await waitFor(() => expect(fake.made("POST", "/api/workspaces/1/stop")).toHaveLength(1));
  expect(
    await within(row(alice, "alpha")).findByText(m.workspaceStates.halted),
  ).toBeInTheDocument();
});
