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
