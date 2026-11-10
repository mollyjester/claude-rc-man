import { fireEvent, render, screen } from "@testing-library/react";
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
