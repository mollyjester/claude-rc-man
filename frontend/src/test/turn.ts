import { act } from "@testing-library/react";

// One turn of the event loop, in act: what the test's last step started has run, the page's
// renders and effects among them. A turn takes no time (a timer of no delay); a test asserts
// that something did not happen only after a point it observed and such a turn, never after a
// wait of some length (project-knowledge/frontend.md).
export const turn = () => act(() => new Promise<void>((done) => setTimeout(done)));
