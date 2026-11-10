import { render } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { type Location, MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

import { App } from "../App";

type Router = { location?: Location };

// Keeps hold of the router's current location.
function RouterProbe({ router }: { router: Router }) {
  router.location = useLocation();
  return null;
}

// What is rendered, in a router at a path; location() is where it is now.
function renderRouted(path: string, children: ReactNode) {
  const router: Router = {};
  const result = render(
    <MemoryRouter initialEntries={[path]}>
      {children}
      <RouterProbe router={router} />
    </MemoryRouter>,
  );
  return {
    ...result,
    location: () => router.location!,
  };
}

// The whole app at a path.
export function renderApp(path = "/", { pollMs = 0 }: { pollMs?: number } = {}) {
  return renderRouted(path, <App pollMs={pollMs} />);
}

// A part of the page that the app does not show, on its own at "/", with a page
// at each of the other paths given, so that a link can lead to one: each of those
// pages is its path as text.
export function renderAlone(element: ReactElement, elsewhere: string[] = []) {
  return renderRouted(
    "/",
    <Routes>
      <Route path="/" element={element} />
      {elsewhere.map((path) => (
        <Route key={path} path={path} element={<p>{path}</p>} />
      ))}
    </Routes>,
  );
}
