import { createContext, useContext, useEffect, useState } from "react";
import { NavLink, Navigate, Route, Routes } from "react-router-dom";

import { ApiError, api } from "./api";
import { Audit } from "./Audit";
import { Dashboard } from "./Dashboard";
import { Docs } from "./Docs";
import { m } from "./messages";
import { People } from "./People";
import { ProblemView, asApiError } from "./ProblemView";
import { Settings } from "./Settings";
import { Versions } from "./Versions";

export type Me = { email: string; linux: string; admin: boolean; prefix: string };

const MeContext = createContext<Me | null>(null);
export const useMe = () => useContext(MeContext);

export const POLL_MS = 5000;

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
        {me.admin && (
          <>
            <NavLink to="/admin/people">{m.navUsers}</NavLink>
            <NavLink to="/admin/audit">{m.navAudit}</NavLink>
            <NavLink to="/admin/claude">{m.navVersions}</NavLink>
          </>
        )}
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
        <Routes>
          <Route path="/" element={<Dashboard pollMs={pollMs} />} />
          <Route path="/settings" element={<Settings />} />
          <Route path="/help" element={<Docs />} />
          <Route path="/help/:slug" element={<Docs />} />
          {me.admin && <Route path="/admin/people" element={<People />} />}
          {me.admin && <Route path="/admin/audit" element={<Audit />} />}
          {me.admin && <Route path="/admin/claude" element={<Versions />} />}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </MeContext.Provider>
  );
}
