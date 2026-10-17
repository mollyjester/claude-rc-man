// What the page asks of the browser itself, behind an object the tests can replace a part of:
// jsdom does not reload a page, and its location cannot be replaced.
export const browser = {
  // Loads the page afresh, as the browser's own reload does.
  reload: () => window.location.reload(),
};
