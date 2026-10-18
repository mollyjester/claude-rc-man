import { type FormEvent, type ReactNode, useEffect, useRef, useState } from "react";
import { matchPath, Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";

import { ADMIN_PAGES } from "./AdminPages";
import { Attention } from "./Attention";
import { type ApiError, api } from "./api";
import { Caret } from "./Caret";
import { PersonIcon } from "./Icons";
import { m } from "./messages";
import { PAGES } from "./Pages";
import { asApiError, ProblemView } from "./ProblemView";
import { usePopup } from "./usePopup";
import { useTitle } from "./useTitle";

export type Me = { email: string; admin: boolean; prefix: string | null };

export const POLL_MS = 5000;

// Leaving rcman for another address, as signing out does; tests replace it.
export const away = { to: (url: string) => window.location.assign(url) };

// Signing out of rcman ends the web session only: running servers never depend
// on it, so they keep running unless the person asks for them to be stopped.
function SignOut() {
  const [keep, setKeep] = useState(true);
  const [error, setError] = useState<ApiError | null>(null);

  async function signOut(event: FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const reply = await api.post<{ sign_out_url: string }>("/api/me/sign-out", {
        keep_sessions: keep,
      });
      away.to(reply.sign_out_url);
    } catch (e) {
      setError(asApiError(e));
    }
  }

  return (
    <form noValidate onSubmit={signOut} className="sign-out">
      <label>
        <input
          type="checkbox"
          name="keep_sessions"
          checked={keep}
          onChange={(e) => setKeep(e.target.checked)}
        />
        {m.keepServersOnSignOut}
      </label>
      <button type="submit">{m.signOut}</button>
      <ProblemView error={error} labels={{ keep_sessions: m.keepServersOnSignOut }} />
    </form>
  );
}

// The administrators' pages behind one button: a pop-up (src/usePopup.ts) that
// also closes when a page is chosen and when the page changes.
function AdminMenu() {
  const menu = usePopup("AdminMenu");
  const { close } = menu;
  const { pathname } = useLocation();
  const here = ADMIN_PAGES.some((page) => matchPath(page.path, pathname));

  // biome-ignore lint/correctness/useExhaustiveDependencies: pathname is what sets this off, not what it reads: the menu closes whenever the page changes; close, which it does read, is stable (useCallback with no dependencies in usePopup)
  useEffect(() => close(), [pathname]);

  return (
    <div className="menu" ref={menu.rootRef}>
      <button type="button" className={here ? "active" : undefined} {...menu.buttonProps}>
        {m.navAdmin}
        <Caret />
      </button>
      <ul {...menu.panelProps} hidden={!menu.open}>
        {ADMIN_PAGES.map((page) => (
          <li key={page.path}>
            <NavLink to={page.path} onClick={() => close()}>
              {page.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </div>
  );
}

// Who is signed in, and signing out of rcman, behind one button at the end of the header: a
// pop-up (src/usePopup.ts), on every width.
function AccountMenu({ me }: { me: Me }) {
  const menu = usePopup("AccountMenu");
  return (
    <div className="menu account" ref={menu.rootRef}>
      <button type="button" {...menu.buttonProps}>
        <PersonIcon />
        {m.account}
        <Caret />
      </button>
      <div {...menu.panelProps} className="account-panel" hidden={!menu.open}>
        <p className="muted">{m.signedInAs(me.email)}</p>
        <SignOut />
      </div>
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
        <NavLink to="/claude">{m.navClaude}</NavLink>
        <NavLink to="/settings">{m.navSettings}</NavLink>
        <NavLink to="/help">{m.navDocs}</NavLink>
        {me.admin && <AdminMenu />}
      </nav>
      <AccountMenu me={me} />
    </header>
  );
}

// A page, with its name in the browser tab's title.
function Titled({ title, children }: { title: string; children: ReactNode }) {
  useTitle(title);
  return children;
}

export function App({ pollMs = POLL_MS }: { pollMs?: number }) {
  const [me, setMe] = useState<Me | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const main = useRef<HTMLElement>(null);

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
    <>
      {/* biome-ignore lint/a11y/useValidAnchor: the link is to the content below, and moves focus there without putting a fragment in the address */}
      <a
        className="skip-link"
        href="#main"
        onClick={(event) => {
          event.preventDefault();
          main.current?.focus();
        }}
      >
        {m.skipToContent}
      </a>
      <Nav me={me} />
      <main id="main" tabIndex={-1} ref={main}>
        {me.admin && <Attention />}
        <Routes>
          {PAGES.map((page) => (
            <Route
              key={page.path}
              path={page.path}
              element={
                page.title === null ? (
                  page.element({ pollMs })
                ) : (
                  <Titled title={page.title}>{page.element({ pollMs })}</Titled>
                )
              }
            />
          ))}
          {me.admin &&
            ADMIN_PAGES.map((page) => (
              <Route
                key={page.path}
                path={page.path}
                element={<Titled title={page.label}>{page.element()}</Titled>}
              />
            ))}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </main>
    </>
  );
}
