import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { ACTIONS, KIND, LABELS, PRIMARY } from "./Servers";
import { m } from "./messages";

const GUIDE = resolve(__dirname, "../../docs/servers.md");
const HEADER = "| State | Dot | What to do | Main button | Buttons |";
// What the guide writes where a state has no main button.
const NONE = "—";

type Row = { state: string; dot: string; next: string; main: string; buttons: string[] };

const bold = (cell: string) => [...cell.matchAll(/\*\*(.+?)\*\*/g)].map((b) => b[1]);

// The rows of the guide's state table: the state's name, its dot, what to do,
// its main button in bold or NONE, then each button in bold.
function stateTable(markdown: string): Row[] {
  const lines = markdown.split("\n");
  const start = lines.findIndex((line) => line.trim() === HEADER);
  if (start < 0) return [];
  const rows: Row[] = [];
  for (const line of lines.slice(start + 2)) {
    if (!line.trim().startsWith("|")) break;
    const [state, dot, next, main, buttons] = line.split("|").slice(1, 6).map((cell) => cell.trim());
    rows.push({ state, dot, next, main: bold(main)[0] ?? main, buttons: bold(buttons) });
  }
  return rows;
}

// What the page shows for every state, derived from the interface itself.
function expected(): Row[] {
  return Object.keys(ACTIONS).map((state) => {
    const primary = PRIMARY[state];
    return {
      state: m.states[state],
      dot: m.kinds[KIND[state]],
      next: m.nextStep[state],
      main: primary === null ? NONE : primary === "openSession" ? m.openSession : LABELS[primary],
      buttons: ACTIONS[state].map((action) => LABELS[action]),
    };
  });
}

const cell = (main: string) => (main === NONE ? NONE : `**${main}**`);

test("the servers guide lists each state with its dot, what to do, its main button, and exactly the buttons the page offers", () => {
  const rows = stateTable(readFileSync(GUIDE, "utf-8"));
  expect(rows.length).toBeGreaterThan(0);
  expect(rows).toEqual(expected());
});

test("the check fails on a guide whose table has drifted", () => {
  const right = [
    HEADER,
    "|---|---|---|---|---|",
    ...expected().map(
      (r) => `| ${r.state} | ${r.dot} | ${r.next} | ${cell(r.main)} | ${r.buttons.map((b) => `**${b}**`).join(", ")} |`,
    ),
  ];
  expect(stateTable(right.join("\n"))).toEqual(expected());

  const drifted = (from: string, to: string) => {
    const lines = right.map((line) => line.replace(from, to));
    expect(lines).not.toEqual(right); // the plant took effect
    return stateTable(lines.join("\n"));
  };
  expect(drifted(`| ${m.states.connected} |`, "| Connecting |")).not.toEqual(expected());
  expect(drifted(`**${m.forget}**`, "")).not.toEqual(expected());
  expect(drifted(m.nextStep.stopped, "Resume it.")).not.toEqual(expected());
  expect(drifted(`| ${m.states.exited} | ${m.kinds[KIND.exited]} |`, `| ${m.states.exited} | ${m.kinds[KIND.ready]} |`)).not.toEqual(
    expected(),
  );

  expect(drifted(`${m.nextStep.exited} | **${m.resume}** |`, `${m.nextStep.exited} | **${m.showScreen}** |`)).not.toEqual(
    expected(),
  );
  expect(drifted(`${m.nextStep.starting} | ${NONE} |`, `${m.nextStep.starting} | **${m.stop}** |`)).not.toEqual(expected());

  expect(stateTable(right.slice(0, -1).join("\n"))).not.toEqual(expected());
  expect(stateTable("")).toEqual([]);
});

const PAGE_SECTION = "## The Servers page";

// What the guide's section on the page names in bold.
function boldInSection(markdown: string): string[] {
  const lines = markdown.split("\n");
  const start = lines.findIndex((line) => line.trim() === PAGE_SECTION);
  if (start < 0) return [];
  const end = lines.findIndex((line, i) => i > start && line.startsWith("## "));
  const section = lines.slice(start + 1, end < 0 ? undefined : end).join("\n");
  return [...section.matchAll(/\*\*([^*]+)\*\*/g)].map((b) => b[1].replace(/\s+/g, " "));
}

// Every piece of text in the message catalogue that is not worked out from values.
function catalogue(value: unknown = m): string[] {
  if (typeof value === "string") return [value];
  if (value && typeof value === "object") return Object.values(value).flatMap((v) => catalogue(v));
  return [];
}

// The bold names the section uses that the page does not show.
function unknownNames(markdown: string): string[] {
  const texts = new Set(catalogue());
  return boldInSection(markdown).filter((name) => !texts.has(name));
}

test("every control the guide names on the Servers page is one the page shows", () => {
  const guide = readFileSync(GUIDE, "utf-8");
  expect(boldInSection(guide).length).toBeGreaterThan(0);
  expect(unknownNames(guide)).toEqual([]);
});

test("the check fails on a control the page does not have", () => {
  const guide = readFileSync(GUIDE, "utf-8");
  const planted = guide.replace(PAGE_SECTION, `${PAGE_SECTION}\n\nChoose **Frobnicate**.`);
  expect(boldInSection(planted)).toContain("Frobnicate"); // the plant took effect
  expect(unknownNames(planted)).toEqual(["Frobnicate"]);
  expect(boldInSection("no such section, **Stop**")).toEqual([]);
});
