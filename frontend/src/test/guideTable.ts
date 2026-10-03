// Reading the tables in docs/, for the tests that hold a guide to the page.

// The rows of the table under a header line in a guide, each as its cells,
// trimmed: every line from the one after the header's rule up to the first
// line that is not a row. None if the guide has no such header.
export function guideTable(markdown: string, header: string): string[][] {
  const lines = markdown.split("\n");
  const start = lines.findIndex((line) => line.trim() === header);
  if (start < 0) return [];
  const rows: string[][] = [];
  for (const line of lines.slice(start + 2)) {
    if (!line.trim().startsWith("|")) break;
    const cells = line.trim().split("|").slice(1);
    if (cells.length > 1 && cells.at(-1)?.trim() === "") cells.pop();
    rows.push(cells.map((cell) => cell.trim()));
  }
  return rows;
}

// Each piece of text in bold in a cell, in order.
export const bold = (cell: string) => [...cell.matchAll(/\*\*(.+?)\*\*/g)].map((b) => b[1]);
