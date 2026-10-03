import {
  createContext,
  type FocusEvent,
  type KeyboardEvent,
  useContext,
  useEffect,
  useId,
  useRef,
  useState,
} from "react";
import { matchPath, Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";

import { ADMIN_PAGES } from "./AdminPages";
import { Attention } from "./Attention";
import { type ApiError, api } from "./api";
import { Caret } from "./Caret";
import { Dashboard } from "./Dashboard";
import { Docs } from "./Docs";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";
import { Settings } from "./Settings";

export type Me = { email: string; linux: string; admin: boolean; prefix: string };

const MeContext = createContext<Me | null>(null);
export const useMe = () => useContext(MeContext);

export const POLL_MS = 5000;

// The administrators' pages behind one button: a disclosure that closes when
// a page is chosen, when the page changes, on Escape and when focus leaves it.
function AdminMenu() {
  const [open, setOpen] = useState(false);
  const { pathname } = useLocation();
  const button = useRef<HTMLButtonElement>(null);
  const id = useId();
  const here = ADMIN_PAGES.some((page) => matchPath(page.path, pathname));

  // biome-ignore lint/correctness/useExhaustiveDependencies: pathname is what sets this off, not what it reads: the menu closes whenever the page changes
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
    // biome-ignore lint/a11y/noStaticElementInteractions: it is never focused or clicked itself; it hears the keys and focus that bubble up from the button and links inside it
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
        <Caret />
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
          {me.admin &&
            ADMIN_PAGES.map((page) => (
              <Route key={page.path} path={page.path} element={page.element()} />
            ))}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </MeContext.Provider>
  );
}
