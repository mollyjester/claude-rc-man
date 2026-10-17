import { act, render } from "@testing-library/react";
import { type ReactElement, type ReactNode, StrictMode } from "react";
import {
  type Location,
  MemoryRouter,
  type NavigateFunction,
  Route,
  Routes,
  useLocation,
  useNavigate,
} from "react-router-dom";

import { App } from "../App";

type Router = { location?: Location; navigate?: NavigateFunction };

// Keeps hold of the router's current location and its navigate function.
function RouterProbe({ router }: { router: Router }) {
  router.location = useLocation();
  router.navigate = useNavigate();
  return null;
}

// What is rendered, in a router at a path; location() is where it is now, and
// navigate() moves as the browser would — navigate(-1) is its back button.
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
    navigate: (to: number | string) =>
      act(async () => {
        await (typeof to === "number" ? router.navigate!(to) : router.navigate!(to));
      }),
  };
}

// The whole app at a path; `strict`, in React's strict mode, as main.tsx renders it, which
// in development runs every effect twice.
export function renderApp(
  path = "/",
  { pollMs = 0, strict = false }: { pollMs?: number; strict?: boolean } = {},
) {
  const app = <App pollMs={pollMs} />;
  return renderRouted(path, strict ? <StrictMode>{app}</StrictMode> : app);
}

// A part of the page that the app does not show, on its own at "/", with a page
// at each of the other paths given, so that a link can lead to one and leaving
// it can be tried: each of those pages is its path as text.
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
