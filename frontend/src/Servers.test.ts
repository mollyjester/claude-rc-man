import { m } from "./messages";
import { ACTIONS, dotFor, KIND, LIVE, PRIMARY, type Workspace } from "./Servers";

// Whether a table keyed by state covers exactly the states the server reports.
function sameStates(table: Record<string, unknown>) {
  const states = Object.keys(ACTIONS).sort();
  const keys = Object.keys(table).sort();
  return keys.length === states.length && keys.every((key, i) => key === states[i]);
}

test("every state has a label, a dot, a sentence saying what to do, and a primary action it offers", () => {
  for (const table of [m.states, KIND, m.nextStep, PRIMARY]) {
    expect(sameStates(table)).toBe(true);
  }
  for (const [state, primary] of Object.entries(PRIMARY)) {
    if (primary === null) expect(state).toBe("starting");
    else if (primary === "openSession") expect(LIVE).toContain(state);
    else expect(ACTIONS[state]).toContain(primary);
  }
});

test("the check fails on a table with a state too many or one too few", () => {
  const extra = { ...m.nextStep, connecting: "Wait." };
  expect(Object.keys(extra)).toContain("connecting");
  expect(sameStates(extra)).toBe(false);

  const { ready, ...missing } = m.nextStep;
  expect(ready).toBeTruthy();
  expect(sameStates(missing)).toBe(false);

  expect(sameStates({})).toBe(false);
});

describe("the dot a workspace carries", () => {
  const at = (extra: Partial<Workspace>): Workspace => ({
    id: 1,
    name: "alpha",
    device: "rcman-ws-1",
    state: "ready",
    error: null,
    source: null,
    source_page: null,
    log: null,
    server: null,
    server_error: null,
    claude_version: null,
    rebuild: { state: null, log: null, error: null },
    rebuildable: false,
    ports: [],
    stoppable: false,
    startable: false,
    stopping: false,
    starting: false,
    ...extra,
  });
  const server = (state: string) => ({
    workspace: 1,
    state,
    environment_url: null,
    session_name: null,
    exit_code: null,
  });

  test("is a problem where the workspace failed, or its server could not be read", () => {
    expect(dotFor(at({ state: "failed", error: "planted" }))).toBe("problem");
    const unread = { code: "workspace_failed", title: "Planted." };
    expect(dotFor(at({ server_error: unread }))).toBe("problem");
    // even over a server that would be running
    expect(dotFor(at({ server_error: unread, server: server("ready") }))).toBe("problem");
  });
});
