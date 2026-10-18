import type { ReactElement } from "react";

import { ClaudePage } from "./Claude";
import { Dashboard } from "./Dashboard";
import { Docs } from "./Docs";
import { m } from "./messages";
import { Settings } from "./Settings";

export type Page = {
  path: string;
  // The page's name in the browser tab's title; null where the page names itself, as a help
  // page does, by its own title.
  title: string | null;
  element: (props: { pollMs: number }) => ReactElement;
};

// The pages everyone signed in is offered, which the routes are built from, with the
// administrators' (src/AdminPages.tsx) after them.
export const PAGES: Page[] = [
  { path: "/", title: m.navDashboard, element: ({ pollMs }) => <Dashboard pollMs={pollMs} /> },
  { path: "/claude", title: m.navClaude, element: () => <ClaudePage /> },
  { path: "/settings", title: m.navSettings, element: () => <Settings /> },
  { path: "/help", title: null, element: () => <Docs /> },
  { path: "/help/:slug", title: null, element: () => <Docs /> },
];
