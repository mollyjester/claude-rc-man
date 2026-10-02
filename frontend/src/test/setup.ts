import "@testing-library/jest-dom/vitest";
import { cleanup, configure, screen } from "@testing-library/react";
import { afterEach, beforeAll } from "vitest";

// How long a findBy* or waitFor waits. It waits for the page's own fetches and
// renders and for each query over the page to run. On a machine running six
// busy processes per CPU, the People page was ready 0.43s after render and
// each role query over it after a change took 0.25-0.29s, so the default
// 1000ms ran out; this leaves room for that. A test that fails waits this long
// before saying so; one that passes is not slowed.
configure({ asyncUtilTimeout: 3000 });

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
