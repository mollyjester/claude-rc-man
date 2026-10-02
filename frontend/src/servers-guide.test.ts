import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { ACTIONS, LABELS } from "./Servers";
import { m } from "./messages";

const GUIDE = resolve(__dirname, "../../docs/servers.md");
const HEADER = "| State | Buttons |";

type Row = { state: string; buttons: string[] };

// The rows of the guide's state table: the state's name, then each button in bold.
function stateTable(markdown: string): Row[] {
  const lines = markdown.split("\n");
  const start = lines.findIndex((line) => line.trim() === HEADER);
  if (start < 0) return [];
  const rows: Row[] = [];
  for (const line of lines.slice(start + 2)) {
    if (!line.trim().startsWith("|")) break;
    const [state, buttons] = line.split("|").slice(1, 3).map((cell) => cell.trim());
    rows.push({ state, buttons: [...buttons.matchAll(/\*\*(.+?)\*\*/g)].map((b) => b[1]) });
  }
  return rows;
}

// What the page shows for every state, derived from the interface itself.
function expected(): Row[] {
  return Object.keys(ACTIONS).map((state) => ({
    state: m.states[state],
    buttons: ACTIONS[state].map((action) => LABELS[action]),
  }));
}

test("every state has a label, and every labelled state has its buttons", () => {
  expect(Object.keys(m.states).sort()).toEqual(Object.keys(ACTIONS).sort());
});

test("the servers guide lists each state with exactly the buttons the page offers", () => {
  const rows = stateTable(readFileSync(GUIDE, "utf-8"));
  expect(rows.length).toBeGreaterThan(0);
  expect(rows).toEqual(expected());
});

test("the check fails on a guide whose table has drifted", () => {
  const right = [
    HEADER,
    "|---|---|",
    ...expected().map((r) => `| ${r.state} | ${r.buttons.map((b) => `**${b}**`).join(", ")} |`),
  ];
  expect(stateTable(right.join("\n"))).toEqual(expected());

  const renamed = right.map((line) => line.replace(`| ${m.states.connected} |`, "| Connecting |"));
  expect(renamed).not.toEqual(right);
  expect(stateTable(renamed.join("\n"))).not.toEqual(expected());

  const missingButton = right.map((line) => line.replace(`**${m.forget}**`, ""));
  expect(missingButton).not.toEqual(right);
  expect(stateTable(missingButton.join("\n"))).not.toEqual(expected());

  expect(stateTable(right.slice(0, -1).join("\n"))).not.toEqual(expected());
  expect(stateTable("")).toEqual([]);
});
