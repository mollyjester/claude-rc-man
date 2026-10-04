import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { m } from "./messages";
import { guideTable } from "./test/guideTable";

const GUIDE = resolve(__dirname, "../../docs/git.md");
const HEADER = "| Change | Shown as | Meaning |";

// The guide's table of changes: each change's code, and the letter and word the
// page shows it as, written "M (Modified)".
const changesTable = (markdown: string) =>
  guideTable(markdown, HEADER).map(([code, shown]) => {
    const [, letter, word] = shown.match(/^(\S+) \((.+)\)$/) ?? [];
    return [code.match(/^`(.+)`$/)?.[1] ?? "", letter ?? "", word ?? ""];
  });

const expected = () =>
  Object.entries(m.changes).map(([code, word]) => [code, m.changeLetters[code], word]);

test("the git guide's table of changes is each change the page shows, as it shows it", () => {
  const rows = changesTable(readFileSync(GUIDE, "utf-8"));
  expect(rows.length).toBeGreaterThan(0);
  expect(rows).toEqual(expected());
});

test("the check fails on a table that has drifted", () => {
  const right = [
    HEADER,
    "|---|---|---|",
    ...expected().map(([code, letter, word]) => `| \`${code}\` | ${letter} (${word}) | meaning |`),
  ];
  expect(changesTable(right.join("\n"))).toEqual(expected());
  const relabelled = right.map((line, i) => (i === 2 ? line.replace(/\(.+?\)/, "(Else)") : line));
  expect(relabelled).not.toEqual(right);
  expect(changesTable(relabelled.join("\n"))).not.toEqual(expected());
  const relettered = right.map((line, i) => (i === 2 ? line.replace(/\| \S+ \(/, "| X (") : line));
  expect(relettered).not.toEqual(right);
  expect(changesTable(relettered.join("\n"))).not.toEqual(expected());
  expect(changesTable(right.slice(0, -1).join("\n"))).not.toEqual(expected());
  expect(changesTable([...right, "| `forged` | F (Forged) | meaning |"].join("\n"))).not.toEqual(
    expected(),
  );
});

// The changes that have a word but no letter, or a letter but no word.
function unpaired(words: Record<string, string>, letters: Record<string, string>): string[] {
  return [
    ...Object.keys(words)
      .filter((code) => !(code in letters))
      .map((code) => `${code}: no letter`),
    ...Object.keys(letters)
      .filter((code) => !(code in words))
      .map((code) => `${code}: no word`),
  ];
}

// The letters two or more changes share.
function shared(letters: Record<string, string>): string[] {
  const all = Object.values(letters);
  return [...new Set(all.filter((letter, i) => all.indexOf(letter) !== i))];
}

test("every change has both a letter and a word, and the letters differ", () => {
  expect(Object.keys(m.changes).length).toBeGreaterThan(0); // non-vacuity
  expect(unpaired(m.changes, m.changeLetters)).toEqual([]);
  expect(shared(m.changeLetters)).toEqual([]);
});

test("the check of the letters fails on two changes that share one", () => {
  const [first, second] = Object.keys(m.changeLetters);
  const planted = { ...m.changeLetters, [second]: m.changeLetters[first] };
  expect(planted[second]).toBe(planted[first]); // the plant took effect
  expect(shared(planted)).toEqual([m.changeLetters[first]]);
});

test("the pairing check fails on a change with only a word, or only a letter", () => {
  expect(unpaired({ ...m.changes, planted: "Planted" }, m.changeLetters)).toEqual([
    "planted: no letter",
  ]);
  expect(unpaired(m.changes, { ...m.changeLetters, planted: "P" })).toEqual(["planted: no word"]);
});
