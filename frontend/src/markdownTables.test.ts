import { readdirSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

import { splitTables } from "./markdownTables";

const DOCS = resolve(__dirname, "../../docs");

const TABLE = [
  "| Dot | Shape |",
  "|---|:-:|",
  "| Running | Filled circle |",
  "| `a\\|b` | x |",
].join("\n");

test("a table is split out of the text around it, with its cells as written", () => {
  const found = splitTables(`Before.\n\n${TABLE}\n\nAfter.\n`);
  expect(found).toEqual([
    { kind: "text", text: "Before.\n" },
    {
      kind: "table",
      head: ["Dot", "Shape"],
      rows: [
        ["Running", "Filled circle"],
        ["`a|b`", "x"],
      ],
    },
    { kind: "text", text: "\nAfter.\n" },
  ]);
});

test("a row with fewer cells is filled up, one with more is cut, as GitHub shows them", () => {
  const found = splitTables("| A | B |\n|---|---|\n| 1 |\n| 1 | 2 | 3 |");
  expect(found).toEqual([
    {
      kind: "table",
      head: ["A", "B"],
      rows: [
        ["1", ""],
        ["1", "2"],
      ],
    },
  ]);
});

test("text with a bar in it, and a table inside a fenced block, are not tables", () => {
  const prose = "a | b\n\n| not | a table |\n| because | no rule |\n";
  expect(splitTables(prose)).toEqual([{ kind: "text", text: prose }]);
  const fenced = `\`\`\`\n${TABLE}\n\`\`\`\n`;
  expect(splitTables(fenced)).toEqual([{ kind: "text", text: fenced }]);
  // positive control: the same table outside the fence is one
  expect(splitTables(TABLE).map((s) => s.kind)).toEqual(["table"]);
});

test("a rule row whose cells do not match the header's is not a table", () => {
  const text = "| A | B |\n|---|\n| 1 | 2 |\n";
  expect(splitTables(text)).toEqual([{ kind: "text", text }]);
});

test("every table in the guides is found as one", () => {
  const files = readdirSync(DOCS).filter((f) => f.endsWith(".md"));
  expect(files.length).toBeGreaterThan(0);
  let rules = 0;
  for (const file of files) {
    const markdown = readFileSync(resolve(DOCS, file), "utf-8");
    // each rule row outside a fenced block, as a line on its own
    let fenced = false;
    let expected = 0;
    for (const line of markdown.split("\n")) {
      if (/^\s{0,3}(```|~~~)/.test(line)) fenced = !fenced;
      else if (!fenced && /^\|[-:| ]+\|\s*$/.test(line) && line.includes("-")) expected += 1;
    }
    rules += expected;
    const segments = splitTables(markdown);
    expect(
      segments.filter((s) => s.kind === "table"),
      file,
    ).toHaveLength(expected);
    for (const segment of segments) {
      if (segment.kind === "text") expect(segment.text, file).not.toMatch(/^\|.*\|\s*$/m);
    }
  }
  expect(rules).toBeGreaterThan(0); // positive control: the guides have tables
});
