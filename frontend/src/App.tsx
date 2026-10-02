import {
  type FocusEvent,
  type KeyboardEvent,
  type ReactElement,
  createContext,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { NavLink, Navigate, Route, Routes, matchPath, useLocation } from "react-router-dom";

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

// The administrators' pages behind one button: a disclosure that closes when
// a page is chosen, when the page changes, on Escape and when focus leaves it.
function AdminMenu() {
  const [open, setOpen] = useState(false);
  const { pathname } = useLocation();
  const button = useRef<HTMLButtonElement>(null);
  const id = useId();
  const here = ADMIN_PAGES.some((page) => matchPath(page.path, pathname));

  useEffect(() => setOpen(false), [pathname]);

  function onKeyDown(event: KeyboardEvent) {
    if (event.key === "Escape" && open) {
      setOpen(false);
      button.current?.focus();
    }
  }

  function onBlur(event: FocusEvent) {
    if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
  }

  return (
    <div className="menu" onKeyDown={onKeyDown} onBlur={onBlur}>
      <button
        ref={button}
        type="button"
        className={here ? "active" : undefined}
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((shown) => !shown)}
      >
        {m.navAdmin}
        <span className="caret" aria-hidden="true" />
      </button>
      <ul id={id} hidden={!open}>
        {ADMIN_PAGES.map((page) => (
          <li key={page.path}>
            <NavLink to={page.path} onClick={() => setOpen(false)}>
              {page.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </div>
  );
}

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
        {me.admin && <AdminMenu />}
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
