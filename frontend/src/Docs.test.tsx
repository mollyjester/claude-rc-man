import { screen, within } from "@testing-library/react";

import { m } from "./messages";
import { fakeApi, ME, problem } from "./test/fakeApi";
import { renderApp } from "./test/render";

const PAGES = {
  pages: [
    { slug: "servers", title: "Servers" },
    { slug: "users", title: "Adding people" },
  ],
};

test("help lists every page", async () => {
  fakeApi({ "GET /api/me": ME, "GET /api/docs": PAGES });
  renderApp("/help");
  const nav = await screen.findByRole("navigation", { name: m.docsTitle });
  expect(await within(nav).findByRole("link", { name: "Servers" })).toHaveAttribute(
    "href",
    "/help/servers",
  );
  expect(within(nav).getByRole("link", { name: "Adding people" })).toHaveAttribute(
    "href",
    "/help/users",
  );
});

test("a page is rendered, with links between pages kept inside rcman", async () => {
  fakeApi({
    "GET /api/me": ME,
    "GET /api/docs": PAGES,
    "GET /api/docs/servers": {
      slug: "servers",
      title: "Servers",
      // Built at run time so this file holds no dangling reference for the docs guard.
      markdown:
        "# Servers\n\nSee [people](" +
        "users" +
        ".md#adding) and [Claude](https://claude.ai/code).",
    },
  });
  renderApp("/help/servers");
  expect(await screen.findByRole("heading", { name: "Servers", level: 1 })).toBeInTheDocument();
  expect(screen.getByRole("link", { name: "people" })).toHaveAttribute(
    "href",
    "/help/users#adding",
  );
  // the page's own link, not the navigation's to the Claude page
  expect(within(screen.getByRole("main")).getByRole("link", { name: "Claude" })).toHaveAttribute(
    "href",
    "https://claude.ai/code",
  );
});

test("a missing page shows the server's reason, and no page", async () => {
  fakeApi({
    "GET /api/me": ME,
    "GET /api/docs": PAGES,
    "GET /api/docs/nope": problem(404, "not_found", "There is nothing here."),
  });
  renderApp("/help/nope");
  expect(await screen.findByRole("alert")).toHaveTextContent("There is nothing here.");
  expect(screen.queryByRole("heading", { level: 1 })).not.toBeInTheDocument();
});
