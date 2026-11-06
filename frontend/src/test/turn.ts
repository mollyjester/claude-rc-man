import { act } from "@testing-library/react";
import { vi } from "vitest";

// One turn of the event loop, in act: what the test's last step started has run, the page's
// renders and effects among them. A turn takes no time (a timer of no delay); a test asserts
// that something did not happen only after a point it observed and such a turn, never after a
// wait of some length (project-knowledge/frontend.md).
export const turn = () => act(() => new Promise<void>((done) => setTimeout(done)));

// Under fake timers, every timer waiting now run, and then what they did: the renders they
// asked for and their effects, which React runs in the act after the one the timers ran in. A
// test that asserts something did not happen after this has seen what any timer the page left
// would have brought.
export const pendingTimersRun = async () => {
  await act(() => vi.runOnlyPendingTimersAsync());
  await act(async () => {});
};
