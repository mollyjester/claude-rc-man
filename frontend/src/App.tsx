import { type ReactElement, createContext, useContext, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";

import { ApiError, api } from "./api";
import { Attention } from "./Attention";
import { Audit } from "./Audit";
import { Dashboard } from "./Dashboard";
import { Docs } from "./Docs";
import { Everyone } from "./Everyone";
import { m } from "./messages";
import { People } from "./People";
import { ProblemView, asApiError } from "./ProblemView";
import { Settings } from "./Settings";
import { Versions } from "./Versions";

export type Me = { email: string; linux: string; admin: boolean; prefix: string };

const MeContext = createContext<Me | null>(null);
export const useMe = () => useContext(MeContext);

export const POLL_MS = 5000;

// The pages only administrators are offered, in the order the navigation shows them.
export const ADMIN_PAGES: { path: string; label: string; element: () => ReactElement }[] = [
  { path: "/admin/people", label: m.navUsers, element: () => <People /> },
  { path: "/admin/servers", label: m.navEveryone, element: () => <Everyone /> },
  { path: "/admin/audit", label: m.navAudit, element: () => <Audit /> },
  { path: "/admin/claude", label: m.navVersions, element: () => <Versions /> },
];

function Nav({ me }: { me: Me }) {
  return (
    <header>
      <strong>{m.appName}</strong>
      <nav>
        <NavLink to="/" end>
          {m.navDashboard}
        </NavLink>
        <NavLink to="/settings">{m.navSettings}</NavLink>
        <NavLink to="/help">{m.navDocs}</NavLink>
        {me.admin &&
          ADMIN_PAGES.map((page) => (
            <NavLink key={page.path} to={page.path}>
              {page.label}
            </NavLink>
          ))}
      </nav>
      <span className="muted">{m.signedInAs(me.email)}</span>
    </header>
  );
}

export function App({ pollMs = POLL_MS }: { pollMs?: number }) {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<ApiError | null>(null);

  useEffect(() => {
    api.get<Me>("/api/me").then(setMe, (e) => setError(asApiError(e)));
  }, []);

  if (error) {
    return (
      <main>
        <ProblemView error={error} />
      </main>
    );
  }
  if (!me) return <p>{m.loading}</p>;

  return (
    <MeContext.Provider value={me}>
      <Nav me={me} />
      <main>
        {me.admin && <Attention />}
        <Routes>
          <Route path="/" element={<Dashboard pollMs={pollMs} />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/help" element={<Docs />} />
          <Route path="/help/:slug" element={<Docs />} />
          {me.admin && ADMIN_PAGES.map((page) => <Route key={page.path} path={page.path} element={page.element()} />)}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </MeContext.Provider>
  );
}
