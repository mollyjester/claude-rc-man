import { ACTIONS, KIND, KIND_ORDER, LIVE, PRIMARY, dotFor } from "./Servers";
import { m } from "./messages";

// Whether a table keyed by state covers exactly the states rc-helper reports.
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

test.each(Object.entries(PRIMARY))("the primary action of a %s server is one it offers", (state, primary) => {
  if (primary === null) {
    expect(state).toBe("starting");
  } else if (primary === "openSession") {
    expect(LIVE).toContain(state);
  } else {
    expect(ACTIONS[state]).toContain(primary);
    expect(["stop", "forget"]).not.toContain(primary);
  }
});

describe("the dot a folder carries", () => {
  const at = (path: string, state: string) => ({
    path,
    state,
    environment_url: null,
    session_name: null,
    exit_code: null,
  });

  test("is its own server's, whether open or not", () => {
    const servers = [at("projects", "ready")];
    expect(dotFor("projects", servers, false)).toBe("running");
    expect(dotFor("projects", servers, true)).toBe("running");
  });

  test("includes the servers below it only while it is closed", () => {
    const servers = [at("projects/alpha", "trust_prompt")];
    expect(dotFor("projects", servers, true)).toBe("waiting");
    expect(dotFor("projects", servers, false)).toBeNull();
  });

  test("is the most urgent of several", () => {
    const servers = [at("projects", "stopped"), at("projects/alpha", "ready"), at("projects/beta", "exited")];
    expect(dotFor("projects", servers, true)).toBe("problem");
    expect(dotFor("projects", servers.slice(0, 2), true)).toBe("running");
  });

  test("does not come from a folder whose name merely starts the same", () => {
    const servers = [at("projects/x", "unrecognised")];
    expect(dotFor("projects", servers, true)).toBe("problem"); // positive control: it is below projects
    expect(dotFor("proj", servers, true)).toBeNull();
  });

  test("is none where there is no server", () => {
    expect(dotFor("projects", [], true)).toBeNull();
    expect(dotFor("projects", [at("other", "ready")], true)).toBeNull();
  });
});
