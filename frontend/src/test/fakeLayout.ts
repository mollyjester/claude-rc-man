// Stand-ins for what jsdom lacks and the Terminal tab uses, which src/test/setup.ts installs
// for every test: ResizeObserver, whose callbacks a test runs when it says the page's layout
// changed, and matchMedia, whose colour scheme a test switches.
export class FakeResizeObserver {
  static observers: FakeResizeObserver[] = [];
  observed = new Set<Element>();

  constructor(public callback: ResizeObserverCallback) {
    FakeResizeObserver.observers.push(this);
  }
  observe(target: Element) {
    this.observed.add(target);
  }
  unobserve(target: Element) {
    this.observed.delete(target);
  }
  disconnect() {
    this.observed.clear();
  }
}

// The layout changed: every observer watching something is told.
export function resized() {
  for (const observer of FakeResizeObserver.observers) {
    if (observer.observed.size === 0) continue;
    const entries = [...observer.observed].map((target) => ({ target }) as ResizeObserverEntry);
    observer.callback(entries, observer as unknown as ResizeObserver);
  }
}

// The colour scheme the system says, and who listens for its change.
let dark = false;
const schemeListeners = new Set<(event: MediaQueryListEvent) => void>();
const DARK = "(prefers-color-scheme: dark)";

export function fakeMatchMedia(query: string): MediaQueryList {
  const list = {
    media: query,
    get matches() {
      return query === DARK && dark;
    },
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

// The system switches to the dark scheme, or back.
export function switchScheme(toDark: boolean) {
  dark = toDark;
  for (const listener of [...schemeListeners])
    listener({ matches: dark, media: DARK } as MediaQueryListEvent);
}

// How many listen for the scheme's change now.
export const schemeListening = () => schemeListeners.size;

export function forgetLayout() {
  FakeResizeObserver.observers = [];
  schemeListeners.clear();
  dark = false;
}
