import { m } from "./messages";
import {
  ACTIONS,
  dotFor,
  KIND,
  KIND_ORDER,
  LABELS,
  LIVE,
  PRIMARY,
  REBUILDING,
  type Workspace,
} from "./Servers";

// Whether a table keyed by state covers exactly the states the server reports.
function sameStates(table: Record<string, unknown>) {
  const states = Object.keys(ACTIONS).sort();
  const keys = Object.keys(table).sort();
  return keys.length === states.length && keys.every((key, i) => key === states[i]);
}

test("every state has a label, a dot, a sentence saying what to do, and a primary action", () => {
  for (const table of [m.states, KIND, m.nextStep, PRIMARY]) {
    expect(sameStates(table)).toBe(true);
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

test("every kind of dot has a place in the order of urgency, and every place a kind", () => {
  expect(new Set(Object.values(KIND))).toEqual(new Set(KIND_ORDER));
  expect(Object.keys(m.kinds).sort()).toEqual([...KIND_ORDER].sort());
});

test.each(Object.entries(PRIMARY))(
  "the primary action of a %s server is one it offers",
  (state, primary) => {
    if (primary === null) {
      expect(state).toBe("starting");
    } else if (primary === "openSession") {
      expect(LIVE).toContain(state);
    } else {
      expect(ACTIONS[state]).toContain(primary);
      expect(["stop", "forget"]).not.toContain(primary);
    }
  },
);

test("a server at the Remote Control question is waiting, leads with Enable, and can be stopped", () => {
  expect(KIND.remote_control_prompt).toBe("waiting");
  expect(PRIMARY.remote_control_prompt).toBe("enableRc");
  expect(ACTIONS.remote_control_prompt).toEqual(["enableRc", "notNow", "stop"]);
  // its own words, not the trust question's
  expect(m.states.remote_control_prompt).not.toBe(m.states.trust_prompt);
  expect(m.nextStep.remote_control_prompt).not.toBe(m.nextStep.trust_prompt);
  expect(LABELS.enableRc).toBe(m.enableRc);
  expect(LABELS.notNow).toBe(m.notNow);
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
    ...extra,
  });
  const server = (state: string) => ({
    workspace: 1,
    state,
    environment_url: null,
    session_name: null,
    exit_code: null,
  });

  test.each(Object.entries(KIND))("is its %s server's kind, %s", (state, kind) => {
    expect(dotFor(at({ server: server(state) }))).toBe(kind);
  });

  test("is a problem for a state rcman does not know", () => {
    expect(dotFor(at({ server: server("something_new") }))).toBe("problem");
  });

  test("is none where there is no server", () => {
    expect(dotFor(at({}))).toBeNull();
    expect(dotFor(at({ state: "creating" }))).toBeNull();
    expect(dotFor(at({ state: "deleting" }))).toBeNull();
  });

  test("is a problem where the workspace failed, or its server could not be read", () => {
    expect(dotFor(at({ state: "failed", error: "planted" }))).toBe("problem");
    const unread = { code: "workspace_failed", title: "Planted." };
    expect(dotFor(at({ server_error: unread }))).toBe("problem");
    // even over a server that would be running
    expect(dotFor(at({ server_error: unread, server: server("ready") }))).toBe("problem");
  });

  test("is none while a rebuild replaces its container, which is not a problem", () => {
    const rebuilding = { code: REBUILDING, title: "Planted." };
    const switching = { state: "switching", log: null, error: null };
    expect(dotFor(at({ server_error: rebuilding, rebuild: switching }))).toBeNull();
    // a failed workspace keeps the problem dot whatever its server says
    expect(dotFor(at({ state: "failed", server_error: rebuilding }))).toBe("problem");
  });
});
