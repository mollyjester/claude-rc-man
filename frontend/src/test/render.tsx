import { act, render } from "@testing-library/react";
import { type Location, MemoryRouter, type NavigateFunction, useLocation, useNavigate } from "react-router-dom";

import { App } from "../App";

type Router = { location?: Location; navigate?: NavigateFunction };

// Keeps hold of the router's current location and its navigate function.
function RouterProbe({ router }: { router: Router }) {
  router.location = useLocation();
  router.navigate = useNavigate();
  return null;
}

// The whole app at a path; location() is where it is now, and navigate()
// moves as the browser would — navigate(-1) is its back button.
export function renderApp(path = "/") {
  const router: Router = {};
  const result = render(
    <MemoryRouter initialEntries={[path]}>
      <App pollMs={0} />
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
