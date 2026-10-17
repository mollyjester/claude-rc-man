import "@testing-library/jest-dom/vitest";
import { cleanup, configure, screen } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, vi } from "vitest";

import { forgetSending } from "../api";
import { forgetReports } from "../clientErrors";
import { FakeResizeObserver, fakeMatchMedia, forgetLayout } from "./fakeLayout";
import { FakeSocket } from "./fakeSocket";
import { FakeView } from "./fakeXterm";

// xterm.js draws on a canvas and measures the page's layout, neither of which jsdom has: every
// test has the stand-in in its place.
vi.mock("../xtermView", () => import("./fakeXterm"));

// How long a findBy* or waitFor waits. The wait covers the page's own fetches
// and renders and every query over the whole page, and on a slow or busy
// machine that takes longer than the library's default; a test that fails
// waits this long before saying so, and one that passes is not slowed.
configure({ asyncUtilTimeout: 3000 });

// Each test file evaluates the testing tools afresh, so its first role query
// runs cold: what is hidden and what each element is called are worked out by
// code that has not run yet. One query here, over the roles the tests look
// for, runs it before any test's wait begins.
beforeAll(() => {
  document.body.innerHTML = `
    <main><section aria-label="warm"><h2>warm</h2>
      <table><tbody><tr><td>row</td></tr></tbody></table>
      <nav aria-label="links"><a href="/">link</a></nav>
      <article aria-label="card"><button type="button" aria-describedby="d">button</button><span id="d" hidden>d</span></article>
    </section></main>`;
  for (const role of ["region", "row", "link", "button", "article", "heading", "navigation"]) {
    screen.getAllByRole(role, { name: /./ });
  }
  document.body.innerHTML = "";
});

// No test opens a real WebSocket, and the Terminal tab finds what it uses of the browser.
beforeEach(() => {
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal("ResizeObserver", FakeResizeObserver);
  vi.stubGlobal("matchMedia", fakeMatchMedia);
});

// A report a test left queued, or a sender it left sending, would otherwise
// send during the next test.
afterEach(() => {
  cleanup();
  forgetReports();
  forgetSending();
  FakeSocket.opened = [];
  FakeView.made = [];
  forgetLayout();
});
