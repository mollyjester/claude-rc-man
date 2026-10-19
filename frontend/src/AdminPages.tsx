import type { ReactElement } from "react";

import { Audit } from "./Audit";
import { Everyone } from "./Everyone";
import { AuditIcon, PeopleIcon, ServersIcon, TagIcon } from "./Icons";
import { m } from "./messages";
import { People } from "./People";
import { Versions } from "./Versions";

export type AdminPage = {
  path: string;
  label: string;
  element: () => ReactElement;
  // Its icon in the sidebar's group of the administrators' pages.
  icon: () => ReactElement;
};

// The pages only administrators are offered, by name, so that a link to one
// names the page itself; the sidebar, the routes and the attention
// warning's links are built from it.
export const ADMIN = {
  people: {
    path: "/admin/people",
    label: m.navUsers,
    element: () => <People />,
    icon: PeopleIcon,
  },
  everyone: {
    path: "/admin/workspaces",
    label: m.navEveryone,
    element: () => <Everyone />,
    icon: ServersIcon,
  },
  versions: {
    path: "/admin/claude",
    label: m.navVersions,
    element: () => <Versions />,
    icon: TagIcon,
  },
  audit: { path: "/admin/audit", label: m.navAudit, element: () => <Audit />, icon: AuditIcon },
} satisfies Record<string, AdminPage>;

// The same pages, in the order the navigation shows them.
export const ADMIN_PAGES: AdminPage[] = Object.values(ADMIN);
