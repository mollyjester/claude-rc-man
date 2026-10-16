// Tables in a help page's Markdown. The page's renderer takes plain Markdown,
// which has no tables, and the guides are also read on GitHub, so their pipe
// tables are split out here and drawn by the page itself: a table is a row of
// headings, a rule row of dashes (with colons for alignment) with as many cells,
// and rows that begin with a bar, outside a fenced block.

export type Segment =
  | { kind: "text"; text: string }
  | { kind: "table"; head: string[]; rows: string[][] };

// A row's cells: split at the bars that are not escaped and not in a code span, outer bars
// dropped, a bar escaped for the table written as a bar.
function cells(line: string): string[] {
  const found: string[] = [];
  let current = "";
  let code = false;
  const text = line.trim();
  for (let i = 0; i < text.length; i++) {
    const char = text[i];
    if (char === "\\" && text[i + 1] === "|") {
      current += "|";
      i += 1;
    } else if (char === "`") {
      code = !code;
      current += char;
    } else if (char === "|" && !code) {
      found.push(current);
      current = "";
    } else {
      current += char;
    }
  }
  found.push(current);
  if (text.startsWith("|")) found.shift();
  if (text.endsWith("|") && !text.endsWith("\\|")) found.pop();
  return found.map((cell) => cell.trim());
}

const isRule = (line: string, count: number) => {
  if (!line.includes("-") || !line.includes("|")) return false;
  const rule = cells(line);
  return rule.length === count && rule.every((cell) => /^:?-+:?$/.test(cell));
};

const isRow = (line: string) => line.trim().startsWith("|");

// The Markdown as text and tables, in order.
export function splitTables(markdown: string): Segment[] {
  const lines = markdown.split("\n");
  const segments: Segment[] = [];
  let text: string[] = [];
  let fenced = false;
  let i = 0;
  const flush = () => {
    if (text.length > 0) segments.push({ kind: "text", text: text.join("\n") });
    text = [];
  };
  while (i < lines.length) {
    const line = lines[i];
    if (/^\s{0,3}(```|~~~)/.test(line)) fenced = !fenced;
    if (!fenced && isRow(line) && i + 1 < lines.length) {
      const head = cells(line);
      if (isRule(lines[i + 1], head.length)) {
        const rows: string[][] = [];
        let j = i + 2;
        while (j < lines.length && isRow(lines[j])) {
          const row = cells(lines[j]);
          rows.push(head.map((_, k) => row[k] ?? ""));
          j += 1;
        }
        flush();
        segments.push({ kind: "table", head, rows });
        i = j;
        continue;
      }
    }
    text.push(line);
    i += 1;
  }
  flush();
  return segments;
}
