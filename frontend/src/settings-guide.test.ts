import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { FIELDS } from "./Settings";

const GUIDE = resolve(__dirname, "../../docs/settings.md");
const HEADER = "| Setting | What it sets |";

type Row = { label: string; field: string };

// The rows of the guide's settings table: the label in bold, then the field in code.
function settingsTable(markdown: string): Row[] {
  const lines = markdown.split("\n");
  const start = lines.findIndex((line) => line.trim() === HEADER);
  if (start < 0) return [];
  const rows: Row[] = [];
  for (const line of lines.slice(start + 2)) {
    if (!line.trim().startsWith("|")) break;
    const cell = line.split("|")[1];
    rows.push({ label: cell.match(/\*\*(.+?)\*\*/)?.[1] ?? "", field: cell.match(/\(`(.+?)`\)/)?.[1] ?? "" });
  }
  return rows;
}

// What the page shows, derived from the page itself.
function expected(): Row[] {
  return Object.entries(FIELDS).map(([field, f]) => ({ label: f.label, field }));
}

test("the settings guide lists each field the page shows, with the page's label", () => {
  const rows = settingsTable(readFileSync(GUIDE, "utf-8"));
  expect(rows.length).toBeGreaterThan(0);
  expect(rows).toEqual(expected());
});

test("the check fails on a guide whose table has drifted", () => {
  const right = [HEADER, "|---|---|", ...expected().map((r) => `| **${r.label}** (\`${r.field}\`) | what it sets |`)];
  expect(settingsTable(right.join("\n"))).toEqual(expected());

  const relabelled = right.map((line) => line.replace(`**${FIELDS.prefix.label}**`, "**Prefix**"));
  expect(relabelled).not.toEqual(right);
  expect(settingsTable(relabelled.join("\n"))).not.toEqual(expected());

  const renamed = right.map((line) => line.replace("(`prefix`)", "(`session_prefix`)"));
  expect(renamed).not.toEqual(right);
  expect(settingsTable(renamed.join("\n"))).not.toEqual(expected());

  expect(settingsTable(right.slice(0, -1).join("\n"))).not.toEqual(expected());
  expect(settingsTable([...right, "| **Extra** (`extra`) | what it sets |"].join("\n"))).not.toEqual(expected());
  expect(settingsTable("")).toEqual([]);
});
