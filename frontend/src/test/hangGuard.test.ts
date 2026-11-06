import { getConfig, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { expect, test, vi } from "vitest";

import { UNIT_HANG_GUARD_MS, UNIT_TEST_GUARD_MS } from "./hangGuard";
import { waitForIt } from "./waitForIt";

// The component tests run under their tier's hang guard (src/test/hangGuard.ts), as configured.

test("a test runs under the component tests' guard", ({ task }) => {
  expect(task.timeout).toBe(UNIT_TEST_GUARD_MS);
});

test("a findBy* or waitFor waits for the component tests' step", () => {
  expect(getConfig().asyncUtilTimeout).toBe(UNIT_HANG_GUARD_MS);
});

test("waitForIt hands vi.waitFor the component tests' step", async () => {
  const waitFor = vi.spyOn(vi, "waitFor");
  try {
    await waitForIt(() => true);
    expect(waitFor).toHaveBeenCalledWith(expect.any(Function), { timeout: UNIT_HANG_GUARD_MS });
  } finally {
    waitFor.mockRestore();
  }
});

test("under fake timers, a findBy* moves the fake clock, and userEvent completes", async () => {
  // The Jest shim of src/test/setup.ts: without it even a findBy* whose element is there waits
  // on a clock nothing moves, and without advanceTimers userEvent waits on its own fake timer.
  vi.useFakeTimers();
  const later = document.createElement("p");
  const button = document.createElement("button");
  try {
    let pressed = 0;
    button.addEventListener("click", () => {
      pressed += 1;
    });
    later.textContent = "later";
    document.body.append(later, button);
    expect(await screen.findByText("later")).toBe(later);
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    await user.click(button);
    expect(pressed).toBe(1);
  } finally {
    later.remove();
    button.remove();
    vi.useRealTimers();
  }
});
