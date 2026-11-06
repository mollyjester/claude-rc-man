// The frontend tests' hang guards, in milliseconds: a test's bound, the guard, and half of it
// for one step of a test, a wait for something expected to happen. A bound ends only a genuine
// hang and never decides a test that would have finished; a test waits on what it observes,
// never on a time (project-knowledge/tests.md, "Time in tests"). Every timeout a test or a config
// gives is one of these; the timing guard (tests/test_timing_guard.py) holds every test and
// config to that, and tests/test_hang_guard.py holds these values.

// The component tests (Vitest in jsdom), a tier of their own: a step there is a render and a
// fake fetch, which take far less on a loaded machine, so that a failing findBy* says so in
// seconds rather than in the host tests' minutes. Testing Library's asyncUtilTimeout and
// waitForIt's bound (src/test/waitForIt.ts) are the step's; vite.config.ts gives every test and
// hook the test's.
export const UNIT_HANG_GUARD_MS = 20_000;
export const UNIT_TEST_GUARD_MS = 2 * UNIT_HANG_GUARD_MS;
