import "@testing-library/jest-dom/vitest";
import { cleanup, screen } from "@testing-library/react";
import { afterEach, beforeAll } from "vitest";

// Each test file evaluates the testing tools afresh, so the first role query in
// a file runs code nothing has run yet: computing what is hidden and what each
// element is called. That first query can take longer than a findBy* waits
// under a loaded machine, while the page itself is ready in a fraction of it.
// One query here, over roles the tests look for, runs that code before any
// test starts its clock.
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

afterEach(() => cleanup());
