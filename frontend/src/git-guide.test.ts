import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { m } from "./messages";
import { guideTable } from "./test/guideTable";

const GUIDE = resolve(__dirname, "../../docs/git.md");
const HEADER = "| Change | Shown as | Meaning |";

// The guide's table of changes: each change's code and how the page shows it.
const changesTable = (markdown: string) =>
  guideTable(markdown, HEADER).map(([code, shown]) => [code.match(/^`(.+)`$/)?.[1] ?? "", shown]);

const expected = () => Object.entries(m.changes);

test("the git guide's table of changes is each change the page shows, as it shows it", () => {
  const rows = changesTable(readFileSync(GUIDE, "utf-8"));
  expect(rows.length).toBeGreaterThan(0);
  expect(rows).toEqual(expected());
});

test("the check fails on a table that has drifted", () => {
  const right = [HEADER, "|---|---|---|", ...expected().map(([code, shown]) => `| \`${code}\` | ${shown} | meaning |`)];
  expect(changesTable(right.join("\n"))).toEqual(expected());
  const relabelled = right.map((line, i) => (i === 2 ? line.replace(/\| [^|`]+ \|/, "| Something else |") : line));
  expect(relabelled).not.toEqual(right);
  expect(changesTable(relabelled.join("\n"))).not.toEqual(expected());
  expect(changesTable(right.slice(0, -1).join("\n"))).not.toEqual(expected());
  expect(changesTable([...right, "| `forged` | Forged | meaning |"].join("\n"))).not.toEqual(expected());
});
