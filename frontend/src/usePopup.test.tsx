import { act, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { usePopup } from "./usePopup";

// A pop-up as the page uses one: its button, and a panel shown while it is open.
function Harness() {
  const popup = usePopup("Harness");
  return (
    <>
      <button type="button">Before</button>
      <div ref={popup.rootRef}>
        <button type="button" {...popup.buttonProps}>
          Open
        </button>
        {popup.open && (
          <div {...popup.panelProps}>
            <span>Words</span>
            <button type="button">Inside</button>
            <button type="button" disabled>
              Disabled
            </button>
          </div>
        )}
      </div>
      <button type="button">After</button>
    </>
  );
}

const opener = () => screen.getByRole("button", { name: "Open" });

async function opened() {
  render(<Harness />);
  await userEvent.click(opener());
  expect(opener()).toHaveAttribute("aria-expanded", "true");
  const panel = document.getElementById(opener().getAttribute("aria-controls")!)!;
  expect(panel).toBeInTheDocument();
  // marked with the component that opened it, which the browser's walk reads off what opened
  expect(panel).toHaveAttribute("data-component", "Harness");
  return panel;
}

test("its button opens and closes it", async () => {
  render(<Harness />);
  expect(opener()).toHaveAttribute("aria-expanded", "false");
  await userEvent.click(opener());
  expect(opener()).toHaveAttribute("aria-expanded", "true");
  await userEvent.click(opener());
  expect(opener()).toHaveAttribute("aria-expanded", "false");
});

test("Escape closes it and puts focus back on its button", async () => {
  await opened();
  screen.getByRole("button", { name: "Inside" }).focus();
  await userEvent.keyboard("{Escape}");
  expect(opener()).toHaveAttribute("aria-expanded", "false");
  expect(opener()).toHaveFocus();
});

test("a press outside closes it, and a press inside it does not", async () => {
  await opened();
  fireEvent.pointerDown(screen.getByText("Words"));
  expect(opener()).toHaveAttribute("aria-expanded", "true");
  fireEvent.pointerDown(screen.getByRole("button", { name: "After" }));
  expect(opener()).toHaveAttribute("aria-expanded", "false");
});

test("focus leaving it for something else closes it, and moving inside it does not", async () => {
  await opened();
  await userEvent.tab();
  expect(screen.getByRole("button", { name: "Inside" })).toHaveFocus();
  expect(opener()).toHaveAttribute("aria-expanded", "true");
  await userEvent.tab();
  expect(screen.getByRole("button", { name: "After" })).toHaveFocus();
  expect(opener()).toHaveAttribute("aria-expanded", "false");
});

test("focus going nowhere, as when the button that held it is disabled, leaves it open", async () => {
  await opened();
  const inside = screen.getByRole("button", { name: "Inside" });
  act(() => inside.focus());
  act(() => inside.blur()); // focus to the document itself: nothing else took it
  expect(opener()).toHaveAttribute("aria-expanded", "true");
  act(() => inside.focus());
  act(() => screen.getByRole("button", { name: "Before" }).focus());
  expect(opener()).toHaveAttribute("aria-expanded", "false"); // positive control: from there, it does
});

test("Escape still closes it once focus inside has gone to the page itself, as when its button was disabled", async () => {
  await opened();
  const inside = screen.getByRole("button", { name: "Inside" });
  act(() => inside.focus());
  act(() => inside.blur());
  expect(document.activeElement).toBe(document.body); // the plant took effect
  expect(opener()).toHaveAttribute("aria-expanded", "true");
  await userEvent.keyboard("{Escape}");
  expect(opener()).toHaveAttribute("aria-expanded", "false");
  expect(opener()).toHaveFocus();
});

test("Escape pressed somewhere else on the page leaves it open", async () => {
  await opened();
  act(() => screen.getByRole("button", { name: "Inside" }).focus());
  const before = screen.getByRole("button", { name: "Before" });
  fireEvent.keyDown(before, { key: "Escape" }); // focus did not move: only the key's target
  expect(opener()).toHaveAttribute("aria-expanded", "true");
});

test("isOpen says whether it is open now, for an answer that arrives later", async () => {
  let seen: (() => boolean) | null = null;
  function Probe() {
    const popup = usePopup("Probe");
    seen = popup.isOpen;
    return (
      <div ref={popup.rootRef}>
        <button type="button" {...popup.buttonProps}>
          Open
        </button>
      </div>
    );
  }
  render(<Probe />);
  expect(seen!()).toBe(false);
  await userEvent.click(opener());
  expect(seen!()).toBe(true);
  await userEvent.keyboard("{Escape}");
  expect(seen!()).toBe(false);
});
