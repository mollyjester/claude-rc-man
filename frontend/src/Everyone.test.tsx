import { screen, within } from "@testing-library/react";

import { m } from "./messages";
import { ADMIN, CONNECTED, NOTHING_TO_ATTEND, fakeApi } from "./test/fakeApi";
import { renderApp } from "./test/render";

function server(path: string, state: string, extra: Record<string, unknown> = {}) {
  return { path, state, environment_url: null, session_name: null, exit_code: null, ...extra };
}

const PEOPLE = {
  people: [
    {
      linux: "alice",
      email: "alice@example.com",
      servers: [server("projects/alpha", "ready", { session_name: "alice-quiet-otter" }), server("projects/beta", "stopped")],
    },
    { linux: "bob", email: "bob@example.com", servers: [] },
    { linux: "carol", email: "carol@example.com", error: { code: "helper_timeout", title: "rcman's helper took too long to answer." } },
  ],
};

function api() {
  return fakeApi({
    "GET /api/me": ADMIN,
    "GET /api/claude": CONNECTED,
    "GET /api/attention": NOTHING_TO_ATTEND,
    "GET /api/servers/everyone": PEOPLE,
  });
}

test("each person's servers are listed with their state", async () => {
  api();
  renderApp("/admin/servers");
  const alice = await screen.findByRole("region", { name: m.everyonePerson("alice", "alice@example.com") });
  const alpha = within(alice).getByRole("row", { name: /projects\/alpha/ });
  expect(within(alpha).getByText(m.states.ready)).toBeInTheDocument();
  expect(within(alpha).getByText("alice-quiet-otter")).toBeInTheDocument();
  const beta = within(alice).getByRole("row", { name: /projects\/beta/ });
  expect(within(beta).getByText(m.states.stopped)).toBeInTheDocument();
  const bob = screen.getByRole("region", { name: m.everyonePerson("bob", "bob@example.com") });
  expect(within(bob).getByText(m.everyoneNone)).toBeInTheDocument();
});

test("nothing on the page acts on anyone's servers", async () => {
  api();
  renderApp("/admin/servers");
  const main = (await screen.findByRole("heading", { name: m.everyoneTitle })).closest("main")!;
  await screen.findByRole("region", { name: m.everyonePerson("alice", "alice@example.com") });
  expect(within(main).queryAllByRole("button")).toEqual([]);
});

test("a person whose servers could not be listed shows the server's reason", async () => {
  api();
  renderApp("/admin/servers");
  const carol = await screen.findByRole("region", { name: m.everyonePerson("carol", "carol@example.com") });
  expect(within(carol).getByRole("alert")).toHaveTextContent("rcman's helper took too long to answer.");
});
