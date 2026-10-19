import {
  type FormEvent,
  type ReactElement,
  type ReactNode,
  useEffect,
  useRef,
  useState,
} from "react";
import { Link, matchPath, Navigate, NavLink, Route, Routes, useLocation } from "react-router-dom";

import { ADMIN_PAGES } from "./AdminPages";
import { Attention } from "./Attention";
import { type ApiError, api } from "./api";
import { HelpIcon, MenuIcon } from "./Icons";
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

// Who is signed in, and signing out of rcman, behind their avatar at the end of the top bar: a
// pop-up (src/usePopup.ts), on every width.
function AccountMenu({ me }: { me: Me }) {
  const menu = usePopup("AccountMenu");
  return (
    <div className="menu account" ref={menu.rootRef}>
      <button type="button" className="avatar-button" title={me.email} {...menu.buttonProps}>
        <span className="avatar" aria-hidden="true">
          {me.email.charAt(0).toUpperCase()}
        </span>
        <span className="visually-hidden">{m.account}</span>
      </button>
      <div {...menu.panelProps} className="account-panel" hidden={!menu.open}>
        <p className="muted">{m.signedInAs(me.email)}</p>
        <SignOut />
      </div>
    </div>
  );
}

// The name of the page at a path, for the top bar to say where you are.
function pageName(pathname: string): string {
  const page = PAGES.find((p) => matchPath(p.path, pathname));
  if (page) return page.name;
  return ADMIN_PAGES.find((p) => matchPath(p.path, pathname))?.label ?? m.navDashboard;
}

// The chrome around every page: the top bar, with rcman's mark, where you are, Help and the
// account menu, and the one navigation of the pages, a sidebar beside the page, which on a phone
// (the stylesheet) is a drawer the top bar's menu button opens: a pop-up (src/usePopup.ts) that
// also closes when a page is chosen and when the page changes. Opened, it puts focus on the
// page where you are, or the first; a page chosen in it puts focus on the page (toPage), as the
// drawer goes; and a press on the page dimmed beside it (the scrim) closes it and goes no
// further. The administrators' group is built from ADMIN_PAGES, and shown to administrators only.
function Navigation({ me, toPage }: { me: Me; toPage: () => void }) {
  const drawer = usePopup("Navigation");
  const { close, open } = drawer;
  const { pathname } = useLocation();
  const nav = useRef<HTMLElement>(null);

  useEffect(() => {
    if (!open) return;
    const links = nav.current?.querySelectorAll<HTMLAnchorElement>("a[href]");
    const here = nav.current?.querySelector<HTMLAnchorElement>('a[aria-current="page"]');
    (here ?? links?.[0])?.focus();
  }, [open]);

  function chosen() {
    if (drawer.isOpen()) toPage();
    close();
  }

  // biome-ignore lint/correctness/useExhaustiveDependencies: pathname is what sets this off, not what it reads: the drawer closes whenever the page changes; close, which it does read, is stable (useCallback with no dependencies in usePopup)
  useEffect(() => close(), [pathname]);

  const link = (path: string, label: string, Icon: () => ReactElement, end = false) => (
    <li key={path}>
      <NavLink to={path} end={end} onClick={chosen}>
        <Icon />
        {label}
      </NavLink>
    </li>
  );

  return (
    <div className="chrome" ref={drawer.rootRef}>
      <header className="topbar">
        <button
          type="button"
          className="icon-button subtle menu-button"
          aria-label={m.navMenu}
          title={m.navMenu}
          {...drawer.buttonProps}
        >
          <MenuIcon />
        </button>
        <Link to="/" className="brand">
          <span className="mark" aria-hidden="true">
            rc
          </span>
          {m.appName}
        </Link>
        <span className="crumb-sep" aria-hidden="true">
          /
        </span>
        <span className="crumb">{pageName(pathname)}</span>
        <div className="topbar-end">
          <Link
            to="/help"
            className="button icon-button subtle help-link"
            aria-label={m.navDocs}
            title={m.navDocs}
          >
            <HelpIcon />
          </Link>
          <AccountMenu me={me} />
        </div>
      </header>
      <nav
        ref={nav}
        {...drawer.panelProps}
        aria-label={m.navMain}
        className={drawer.open ? "sidebar open" : "sidebar"}
      >
        <ul>
          {PAGES.flatMap((page) =>
            page.icon ? [link(page.path, page.name, page.icon, page.path === "/")] : [],
          )}
        </ul>
        {me.admin && (
          <>
            <p className="nav-group" id={`${drawer.panelProps.id}-admin`}>
              {m.navAdmin}
            </p>
            <ul aria-labelledby={`${drawer.panelProps.id}-admin`}>
              {ADMIN_PAGES.map((page) => link(page.path, page.label, page.icon))}
            </ul>
          </>
        )}
      </nav>
      <div className="scrim" aria-hidden="true" hidden={!open} onClick={() => close()} />
    </div>
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
      <div className="shell">
        <Navigation me={me} toPage={() => main.current?.focus()} />
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
      </div>
    </>
  );
}
