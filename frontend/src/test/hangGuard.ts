// The frontend tests' hang guards, in milliseconds: for each tier a test's bound, the guard,
// and half of it for one step of a test, a wait for something expected to happen. A bound ends
// only a genuine hang and never decides a test that would have finished; a test waits on what
// it observes, never on a time (project-knowledge/tests.md, "Time in tests"). Every timeout a
// test or a config gives is one of these, or 0 where a config's bound is none; the timing guard
// (tests/test_timing_guard.py) holds every test and config to that, and tests/test_hang_guard.py
// holds these values to the hang guard's.

// The component tests (Vitest in jsdom), a tier of their own: a step there is a render and a
// fake fetch, which take far less on a loaded machine, so that a failing findBy* says so in 20 s
// rather than in the browser tests' minutes. Testing Library's asyncUtilTimeout and waitForIt's
// bound (src/test/waitForIt.ts) are the step's; vite.config.ts gives every test and hook the
// test's.
export const UNIT_HANG_GUARD_MS = 20_000;
export const UNIT_TEST_GUARD_MS = 2 * UNIT_HANG_GUARD_MS;

// The browser tests and the screenshots (playwright.config.ts and
// playwright.screenshots.config.ts), on the host: the host tests' guard and HANG_GUARD
// (tests/hang_guard.py), in milliseconds.
export const BROWSER_HANG_GUARD_MS = 300_000;
export const BROWSER_TEST_GUARD_MS = 2 * BROWSER_HANG_GUARD_MS;

// The whole stack's browser tests (playwright.stack.config.ts), in the container tier: a spec's
// bound is the container tests' CONTAINER_HANG_GUARD, which the podman client's kill of the
// specs' run (STACK_SPECS_GUARD in tests/hang_guard.py) outlasts, and a step half of it.
export const STACK_TEST_GUARD_MS = 1_800_000;
export const STACK_HANG_GUARD_MS = STACK_TEST_GUARD_MS / 2;
