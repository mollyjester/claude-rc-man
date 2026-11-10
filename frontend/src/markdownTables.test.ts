import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import { splitTables } from "./markdownTables";

const DOCS = resolve(__dirname, "../../docs");

// The rule rows of a guide outside a fenced block, each a line on its own: one per table.
function ruleRows(markdown: string): number {
  let fenced = false;
  let rules = 0;
  for (const line of markdown.split("\n")) {
    if (/^\s{0,3}(```|~~~)/.test(line)) fenced = !fenced;
    else if (!fenced && /^\|[-:| ]+\|\s*$/.test(line) && line.includes("-")) rules += 1;
  }
  return rules;
}

// What the split misses of a guide's tables: a rule row that is not split out as a table, and a
// line of a table left in the text.
function missedTables(markdown: string): string[] {
  const segments = splitTables(markdown);
  const found = segments.filter((s) => s.kind === "table").length;
  const missed = found === ruleRows(markdown) ? [] : [`${found} of ${ruleRows(markdown)} tables`];
  for (const segment of segments)
    if (segment.kind === "text" && /^\|.*\|\s*$/m.test(segment.text))
      missed.push("a table's line left in the text");
  return missed;
}

test("every table in the guides is found as one", () => {
  const files = readdirSync(DOCS).filter((f) => f.endsWith(".md"));
  expect(files.length).toBeGreaterThan(0);
  let rules = 0;
  for (const file of files) {
    const markdown = readFileSync(resolve(DOCS, file), "utf-8");
    rules += ruleRows(markdown);
    expect(missedTables(markdown), file).toEqual([]);
  }
  expect(rules).toBeGreaterThan(0); // positive control: the guides have tables
});

test("the table check fails on a table the split does not find", () => {
  // a rule row whose cells do not match the header's is no table to the split
  const planted = "| A | B |\n|---|\n| 1 | 2 |\n";
  expect(ruleRows(planted)).toBe(1); // the plant took effect
  expect(missedTables(planted)).toEqual(["0 of 1 tables", "a table's line left in the text"]);
});
