// How long the stack's specs wait, where it is more than Playwright's defaults.

// What one test may take (playwright.stack.config.ts).
export const TEST_WITHIN = 90_000;

// What a person's first workspace may take to be made. In real Podman that person's
// range of ids has no copy of the default image yet, and rootless Podman keeps one copy
// per range, so the first workspace copies it: time that grows with the disk's load
// (after a long run of the container suite it is slowest) and that no later workspace of
// the person pays.
export const FIRST_WORKSPACE_WITHIN = 180_000;

// What a test may take when it makes a person's first workspace.
export const TEST_WITH_FIRST_WORKSPACE_WITHIN = TEST_WITHIN + FIRST_WORKSPACE_WITHIN;
