// Stand-ins for what jsdom lacks and the Terminal tab uses, which src/test/setup.ts installs
// for every test: ResizeObserver, which is never told of a change, since jsdom lays nothing out,
// and matchMedia, whose colour scheme is the light one, and which counts who listens for its
// change.
export class FakeResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

// Who listens for the colour scheme's change.
const schemeListeners = new Set<(event: MediaQueryListEvent) => void>();
const DARK = "(prefers-color-scheme: dark)";

export function fakeMatchMedia(query: string): MediaQueryList {
  const list = {
    media: query,
    matches: false,
    onchange: null,
    addEventListener: (_: string, listener: (event: MediaQueryListEvent) => void) => {
      if (query === DARK) schemeListeners.add(listener);
    },
    removeEventListener: (_: string, listener: (event: MediaQueryListEvent) => void) => {
      schemeListeners.delete(listener);
    },
    addListener: () => {},
    removeListener: () => {},
    dispatchEvent: () => true,
  };
  return list as unknown as MediaQueryList;
}

// How many listen for the scheme's change now.
export const schemeListening = () => schemeListeners.size;

export function forgetLayout() {
  schemeListeners.clear();
}
