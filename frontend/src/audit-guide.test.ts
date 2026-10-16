import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { m } from "./messages";
import { guideTable } from "./test/guideTable";

const GUIDE = resolve(__dirname, "../../docs/audit.md");
const ACTIONS = "| Action | Recorded when someone | Target |";
const RESULTS = "| Result | Meaning |";

// The codes in the first column of a table of the guide, without their backticks.
const codes = (markdown: string, header: string) =>
  guideTable(markdown, header).map(([code]) => code.replace(/`/g, ""));

const same = (found: string[], words: Record<string, string>) =>
  [
    found.filter((c) => !(c in words)),
    Object.keys(words).filter((c) => !found.includes(c)),
  ] as const;

test("every action the audit guide lists has its words, and no words are for an action it does not list", () => {
  const found = codes(readFileSync(GUIDE, "utf-8"), ACTIONS);
  expect(found.length).toBeGreaterThan(0); // positive control: the table was read
  expect(same(found, m.auditActions)).toEqual([[], []]);
});

test("every result the audit guide lists has its words", () => {
  const found = codes(readFileSync(GUIDE, "utf-8"), RESULTS);
  expect(found.length).toBeGreaterThan(0);
  expect(same(found, m.auditResults)).toEqual([[], []]);
});

test("the check fails on an action with no words, and on words for no action", () => {
  const found = codes(readFileSync(GUIDE, "utf-8"), ACTIONS);
  expect(same([...found, "planted.action"], m.auditActions)).toEqual([["planted.action"], []]);
  expect(same(found, { ...m.auditActions, "planted.words": "Planted" })).toEqual([
    [],
    ["planted.words"],
  ]);
  expect(codes("", ACTIONS)).toEqual([]);
});
