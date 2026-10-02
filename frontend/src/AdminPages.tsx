import type { ReactElement } from "react";

import { Audit } from "./Audit";
import { Everyone } from "./Everyone";
import { m } from "./messages";
import { People } from "./People";
import { Versions } from "./Versions";

// The pages only administrators are offered, in the order the navigation shows
// them; the navigation, the routes and the attention warning's links are built
// from it.
export const ADMIN_PAGES: { path: string; label: string; element: () => ReactElement }[] = [
  { path: "/admin/people", label: m.navUsers, element: () => <People /> },
  { path: "/admin/servers", label: m.navEveryone, element: () => <Everyone /> },
  { path: "/admin/audit", label: m.navAudit, element: () => <Audit /> },
  { path: "/admin/claude", label: m.navVersions, element: () => <Versions /> },
];

// Where the administrators' page with a label is.
export function adminPath(label: string): string {
  const page = ADMIN_PAGES.find((p) => p.label === label);
  if (!page) throw new Error(`no administrators' page is labelled ${label}`);
  return page.path;
}
