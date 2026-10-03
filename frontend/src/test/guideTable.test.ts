import { bold, guideTable } from "./guideTable";

const HEADER = "| Name | What it is |";
const GUIDE = [
  "# A guide",
  "",
  HEADER,
  "|---|---|",
  "| **One** (`one`) | the first |",
  "|  Two  | the second |",
  "",
  "| Three | after the table |",
];

test("a guide's table is its rows below the header and its rule, each cell trimmed, up to the first line that is not a row", () => {
  expect(guideTable(GUIDE.join("\n"), HEADER)).toEqual([
    ["**One** (`one`)", "the first"],
    ["Two", "the second"],
  ]);
});

test("the header is found among the lines however it is indented, and only as a whole line", () => {
  const indented = GUIDE.map((line) => (line === HEADER ? `  ${HEADER}  ` : line));
  expect(indented).not.toEqual(GUIDE); // the plant took effect
  expect(guideTable(indented.join("\n"), HEADER)).toHaveLength(2);
  expect(guideTable(GUIDE.join("\n"), "| Name |")).toEqual([]);
});

test("a row without its closing bar keeps its last cell", () => {
  expect(guideTable([HEADER, "|---|---|", "| One | the first"].join("\n"), HEADER)).toEqual([
    ["One", "the first"],
  ]);
});

test("there is no table in an empty guide, one without the header, or one whose table has no rows", () => {
  expect(guideTable("", HEADER)).toEqual([]);
  expect(guideTable(GUIDE.filter((line) => line !== HEADER).join("\n"), HEADER)).toEqual([]);
  expect(guideTable([HEADER, "|---|---|"].join("\n"), HEADER)).toEqual([]);
});

test("bold is each piece of text in bold, in order, and nothing for a cell without any", () => {
  expect(bold("**Stop**, **Show screen**")).toEqual(["Stop", "Show screen"]);
  expect(bold("**Session name prefix** (`prefix`)")).toEqual(["Session name prefix"]);
  expect(bold("—")).toEqual([]);
  expect(bold("")).toEqual([]);
});
