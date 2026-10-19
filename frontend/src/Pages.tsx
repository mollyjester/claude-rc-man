import type { ReactElement } from "react";

import { ClaudePage } from "./Claude";
import { Dashboard } from "./Dashboard";
import { Docs } from "./Docs";
import { HelpIcon, PlugIcon, SettingsIcon, WorkspacesIcon } from "./Icons";
import { m } from "./messages";
import { Settings } from "./Settings";

export type Page = {
  path: string;
  // The page's name where the top bar says where you are, and in the sidebar.
  name: string;
  // The page's name in the browser tab's title; null where the page names itself, as a help
  // page does, by its own title.
  title: string | null;
  element: (props: { pollMs: number }) => ReactElement;
  // The page's icon in the sidebar, which offers each page that has one.
  icon?: () => ReactElement;
};

// The pages everyone signed in is offered, which the routes and the sidebar are built from, with
// the administrators' (src/AdminPages.tsx) after them.
export const PAGES: Page[] = [
  {
    path: "/",
    name: m.navDashboard,
    title: m.navDashboard,
    icon: WorkspacesIcon,
    element: ({ pollMs }) => <Dashboard pollMs={pollMs} />,
  },
  {
    path: "/claude",
    name: m.navClaude,
    title: m.navClaude,
    icon: PlugIcon,
    element: () => <ClaudePage />,
  },
  {
    path: "/settings",
    name: m.navSettings,
    title: m.navSettings,
    icon: SettingsIcon,
    element: () => <Settings />,
  },
  { path: "/help", name: m.navDocs, title: null, icon: HelpIcon, element: () => <Docs /> },
  { path: "/help/:slug", name: m.navDocs, title: null, element: () => <Docs /> },
];
