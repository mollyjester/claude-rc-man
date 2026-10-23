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

// Four tiles: the workspaces.
export const WorkspacesIcon = () => (
  <Icon>
    <rect x="2.5" y="2.5" width="4.5" height="4.5" rx="1" />
    <rect x="9" y="2.5" width="4.5" height="4.5" rx="1" />
    <rect x="2.5" y="9" width="4.5" height="4.5" rx="1" />
    <rect x="9" y="9" width="4.5" height="4.5" rx="1" />
  </Icon>
);

// A plug: connecting the Claude account.
export const PlugIcon = () => (
  <Icon>
    <path d="M6 2v3M10 2v3M4 5h8v2.5a4 4 0 0 1-8 0zM8 11.5V14" />
  </Icon>
);

// Three sliders, each at its own setting.
export const SettingsIcon = () => (
  <Icon>
    <path d="M2.5 4h11M2.5 8h11M2.5 12h11" />
    <circle cx="5.5" cy="4" r="1.5" fill="currentColor" />
    <circle cx="10.5" cy="8" r="1.5" fill="currentColor" />
    <circle cx="7" cy="12" r="1.5" fill="currentColor" />
  </Icon>
);

// A question mark in a circle.
export const HelpIcon = () => (
  <Icon>
    <circle cx="8" cy="8" r="6" />
    <path d="M6.3 6.2a1.8 1.8 0 1 1 2.5 1.7c-.5.2-.8.6-.8 1.1v.4" />
    <circle cx="8" cy="11.6" r=".4" fill="currentColor" />
  </Icon>
);

// Two people: the people allowed in.
export const PeopleIcon = () => (
  <Icon>
    <circle cx="6" cy="5.5" r="2.2" />
    <path d="M1.8 13.5c.5-2.3 2.1-3.6 4.2-3.6s3.7 1.3 4.2 3.6" />
    <path d="M10.5 3.6a2.2 2.2 0 0 1 0 4M12 9.9c1.2.5 2 1.7 2.3 3.6" />
  </Icon>
);

// Two stacked servers: everyone's workspaces and their servers.
export const ServersIcon = () => (
  <Icon>
    <rect x="2.5" y="2.5" width="11" height="4.5" rx="1" />
    <rect x="2.5" y="9" width="11" height="4.5" rx="1" />
    <path d="M5 4.75h.01M5 11.25h.01" />
  </Icon>
);

// A gauge, its needle on what is used now: the Monitor page.
export const MonitorIcon = () => (
  <Icon>
    <path d="M2.5 12a5.5 5.5 0 0 1 11 0" />
    <path d="M8 12l2.75-3.5" />
    <path d="M2.5 12h11" />
  </Icon>
);

// A tag: the versions of Claude Code.
export const TagIcon = () => (
  <Icon>
    <path d="M2.5 2.5h5.3l5.7 5.7-5.3 5.3-5.7-5.7z" />
    <circle cx="5.5" cy="5.5" r="1" />
  </Icon>
);

// An arrow rising from a tray: upgrading rcman.
export const UpgradeIcon = () => (
  <Icon>
    <path d="M8 10.5V2.5M4.75 5.75 8 2.5l3.25 3.25" />
    <path d="M2.5 10v3.5h11V10" />
  </Icon>
);

// A list with a tick: the audit trail.
export const AuditIcon = () => (
  <Icon>
    <path d="M7 4h6.5M7 8h6.5M7 12h6.5" />
    <path d="m2.5 4 1 1 1.8-2M2.5 8h2.5M2.5 12h2.5" />
  </Icon>
);

// Three lines: the menu that opens the pages on a phone.
export const MenuIcon = () => (
  <Icon>
    <path d="M2.5 4h11M2.5 8h11M2.5 12h11" />
  </Icon>
);

// Three dots in a row: more actions, in a menu.
export const MoreIcon = () => (
  <Icon>
    <circle cx="3.5" cy="8" r="1.25" fill="currentColor" stroke="none" />
    <circle cx="8" cy="8" r="1.25" fill="currentColor" stroke="none" />
    <circle cx="12.5" cy="8" r="1.25" fill="currentColor" stroke="none" />
  </Icon>
);

// A cross: closing what it is in.
export const CloseIcon = () => (
  <Icon>
    <path d="m4 4 8 8M12 4l-8 8" />
  </Icon>
);

// A closed book with a bookmark: a repository.
export const RepoIcon = () => (
  <Icon>
    <path d="M3 12.5V3a1.5 1.5 0 0 1 1.5-1.5h9v10H4.5A1.5 1.5 0 0 0 3 13a1.5 1.5 0 0 0 1.5 1.5H6" />
    <path d="M6 2v9.5" />
    <path d="M8.5 12.5V15.5l1.25-1 1.25 1v-3" />
  </Icon>
);

// An i in a circle: something to know.
export const InfoIcon = () => (
  <Icon>
    <circle cx="8" cy="8" r="6" />
    <path d="M8 7.2V11" />
    <circle cx="8" cy="5" r=".4" fill="currentColor" />
  </Icon>
);

// An exclamation mark in a triangle: something to see to.
export const WarningIcon = () => (
  <Icon>
    <path d="M8 2.2 14 13H2z" />
    <path d="M8 6.5v3" />
    <circle cx="8" cy="11.3" r=".4" fill="currentColor" />
  </Icon>
);

// A cross in a circle: something that went wrong.
export const ProblemIcon = () => (
  <Icon>
    <circle cx="8" cy="8" r="6" />
    <path d="m6 6 4 4M10 6l-4 4" />
  </Icon>
);

// A tick in a circle: something done.
export const DoneIcon = () => (
  <Icon>
    <circle cx="8" cy="8" r="6" />
    <path d="m5.5 8.2 1.7 1.7 3.3-3.6" />
  </Icon>
);
