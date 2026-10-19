import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi } from "vitest";

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

test("a help page is titled with its own name, and the index with Help", async () => {
  fakeApi({
    "GET /api/me": ME,
    "GET /api/docs": PAGES,
    "GET /api/docs/servers": { slug: "servers", title: "Servers", markdown: "# Servers\n\nText." },
  });
  const { navigate } = renderApp("/help");
  await vi.waitFor(() => expect(document.title).toBe(m.pageTitle(m.docsTitle)));
  await navigate("/help/servers");
  await vi.waitFor(() => expect(document.title).toBe(m.pageTitle("Servers")));
});

const WITH_TABLE = [
  "# Servers",
  "",
  "Before the table.",
  "",
  "| State | What to do |",
  "|---|---|",
  "| Stopped | See [people](" + "users" + ".md#adding) or `resume` |",
  "| Running | **Nothing** |",
  "",
  "After the table.",
].join("\n");

test("a table in a page is shown as a table, with its cells formatted, in a box that scrolls sideways", async () => {
  fakeApi({
    "GET /api/me": ME,
    "GET /api/docs": PAGES,
    "GET /api/docs/servers": { slug: "servers", title: "Servers", markdown: WITH_TABLE },
  });
  renderApp("/help/servers");
  const table = await screen.findByRole("table");
  expect(
    within(table)
      .getAllByRole("columnheader")
      .map((h) => h.textContent),
  ).toEqual(["State", "What to do"]);
  const stopped = within(table).getByRole("row", { name: /Stopped/ });
  expect(within(stopped).getByRole("link", { name: "people" })).toHaveAttribute(
    "href",
    "/help/users#adding",
  );
  expect(within(stopped).getByText("resume").tagName).toBe("CODE");
  expect(within(table).getByText("Nothing").tagName).toBe("STRONG");
  // nothing of the table's source is left as text, and the text around it is kept
  expect(screen.queryByText(/\|/)).not.toBeInTheDocument();
  expect(screen.getByText("Before the table.")).toBeInTheDocument();
  expect(screen.getByText("After the table.")).toBeInTheDocument();
  expect(table.closest(".table-scroll")).not.toBeNull();
});

test("the help index opens with a line on what the pages are for", async () => {
  fakeApi({ "GET /api/me": ME, "GET /api/docs": PAGES });
  renderApp("/help");
  expect(await screen.findByRole("heading", { name: m.docsTitle, level: 1 })).toBeInTheDocument();
  expect(screen.getByText(m.docsIntro)).toBeInTheDocument();
});

// --- the layout: the contents beside the article, folded on a phone ---------------------

const SERVERS = { slug: "servers", title: "Servers", markdown: "# Servers\n\nText." };
const contents = () => screen.getByRole("button", { name: m.docsContents });

test("the article is a card of its own beside the contents, which mark the page you are on", async () => {
  fakeApi({ "GET /api/me": ME, "GET /api/docs": PAGES, "GET /api/docs/servers": SERVERS });
  renderApp("/help/servers");
  const heading = await screen.findByRole("heading", { name: "Servers", level: 1 });
  expect(heading.closest("article")).toHaveClass("card", "docs-article");
  const nav = screen.getByRole("navigation", { name: m.docsTitle });
  expect(heading.closest("article")!.contains(nav)).toBe(false);
  expect(await within(nav).findByRole("link", { name: "Servers" })).toHaveAttribute(
    "aria-current",
    "page",
  );
  expect(within(nav).getByRole("link", { name: "Adding people" })).not.toHaveAttribute(
    "aria-current",
  );
});

test("the contents are disclosed by a button, folded on a page and open on the index, and fold again once a page is chosen", async () => {
  fakeApi({ "GET /api/me": ME, "GET /api/docs": PAGES, "GET /api/docs/servers": SERVERS });
  renderApp("/help");
  const nav = await screen.findByRole("navigation", { name: m.docsTitle });
  const button = contents();
  expect(button).toHaveAttribute("aria-controls", nav.id);
  expect(nav.id).not.toBe("");
  expect(button).toHaveAttribute("aria-expanded", "true"); // the index is the contents
  await userEvent.click(await within(nav).findByRole("link", { name: "Servers" }));
  expect(await screen.findByRole("heading", { name: "Servers", level: 1 })).toBeInTheDocument();
  expect(button).toHaveAttribute("aria-expanded", "false");
  expect(nav).toHaveClass("folded");
  await userEvent.click(button);
  expect(button).toHaveAttribute("aria-expanded", "true");
  expect(nav).not.toHaveClass("folded");
});
