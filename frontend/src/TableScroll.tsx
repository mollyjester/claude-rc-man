import type { ReactNode } from "react";

// A table that scrolls sideways inside its own box where it is wider than the room it has,
// rather than widening the page. The box can take focus, so that a keyboard scrolls it, and
// is named, so that it is known for what it holds.
export function TableScroll({ label, children }: { label: string; children: ReactNode }) {
  return (
    // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrollable region is focusable so that a keyboard can scroll it, and it is named
    <section className="table-scroll" aria-label={label} tabIndex={0}>
      {children}
    </section>
  );
}

// The roles of a table's parts, said outright, for a table a phone draws as a list of rows
// (table.stacked in src/styles.css): a table whose parts are drawn other than as a table's
// loses the roles the browser would otherwise give them.
export const ROLE = {
  table: { role: "table" },
  rowgroup: { role: "rowgroup" },
  row: { role: "row" },
  columnheader: { role: "columnheader" },
  cell: { role: "cell" },
} as const;
