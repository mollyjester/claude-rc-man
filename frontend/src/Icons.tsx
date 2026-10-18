import type { ReactNode } from "react";

// rcman's own icons: simple strokes on a 16-unit grid, drawn in the text's
// colour. Each is hidden from assistive technology; the button or text it is
// in carries the name, from src/messages.ts.
function Icon({ children }: { children: ReactNode }) {
  return (
    <svg
      className="icon"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.6"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  );
}

// Two commits on one line and a third the branch leaves it for.
export const BranchIcon = () => (
  <Icon>
    <circle cx="4.5" cy="3.5" r="1.5" />
    <circle cx="4.5" cy="12.5" r="1.5" />
    <circle cx="11.5" cy="4.5" r="1.5" />
    <path d="M4.5 5v6M11.5 6c0 3.5-7 2.5-7 5" />
  </Icon>
);

export const PlusIcon = () => (
  <Icon>
    <path d="M8 3v10M3 8h10" />
  </Icon>
);

// Most of a circle, with an arrowhead at its open end.
export const RefreshIcon = () => (
  <Icon>
    <path d="M13 8a5 5 0 1 1-1.6-3.7" />
    <path d="M12 1.8v2.8H9.2" />
  </Icon>
);

export const UpIcon = () => (
  <Icon>
    <path d="M8 13V3M4.5 6.5 8 3l3.5 3.5" />
  </Icon>
);

export const DownIcon = () => (
  <Icon>
    <path d="M8 3v10M4.5 9.5 8 13l3.5-3.5" />
  </Icon>
);

export const CheckIcon = () => (
  <Icon>
    <path d="M3 8.5 6.5 12 13 4.5" />
  </Icon>
);

// A bin with its lid: deleting what it stands beside.
export const TrashIcon = () => (
  <Icon>
    <path d="M2.5 4h11M6 4V2.5h4V4M4 4l.7 9.5h6.6L12 4M6.8 6.5v4.5M9.2 6.5v4.5" />
  </Icon>
);

// A chevron pointing back.
export const BackIcon = () => (
  <Icon>
    <path d="M10 3 5 8l5 5" />
  </Icon>
);

// A head and shoulders: the person signed in.
export const PersonIcon = () => (
  <Icon>
    <circle cx="8" cy="5.5" r="2.5" />
    <path d="M3 13.5c.6-2.6 2.6-4 5-4s4.4 1.4 5 4" />
  </Icon>
);
