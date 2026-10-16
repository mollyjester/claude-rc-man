import type { ReactElement } from "react";

import { Audit } from "./Audit";
import { Everyone } from "./Everyone";
import { m } from "./messages";
import { People } from "./People";
import { Versions } from "./Versions";

export type AdminPage = { path: string; label: string; element: () => ReactElement };

// The pages only administrators are offered, by name, so that a link to one
// names the page itself; the navigation, the routes and the attention
// warning's links are built from it.
export const ADMIN = {
  people: { path: "/admin/people", label: m.navUsers, element: () => <People /> },
  everyone: { path: "/admin/workspaces", label: m.navEveryone, element: () => <Everyone /> },
  versions: { path: "/admin/claude", label: m.navVersions, element: () => <Versions /> },
  audit: { path: "/admin/audit", label: m.navAudit, element: () => <Audit /> },
} satisfies Record<string, AdminPage>;

// The same pages, in the order the navigation shows them.
export const ADMIN_PAGES: AdminPage[] = Object.values(ADMIN);
