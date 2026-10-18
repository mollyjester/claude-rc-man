import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";

import { Dialog } from "./Dialog";
import { modalDialogs } from "./test/fakeDialog";
import { renderAlone } from "./test/render";

// A button that opens a dialog, and the dialog, which closes as its parent says.
function Opener({ focusDialog = false, closable = true }) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button type="button" onClick={() => setOpen(true)}>
        open
      </button>
      {open && (
        <Dialog
          owner="Opener"
          title="Planted"
          onClose={() => setOpen(false)}
          focusDialog={focusDialog}
          closable={closable}
        >
          <label>
            Name
            <input type="text" />
          </label>
          <button type="button" onClick={() => setOpen(false)}>
            done
          </button>
        </Dialog>
      )}
    </>
  );
}

test("a dialog opens as a modal named by its heading, focus in its first field, and is closed by Escape, focus back on its opener", async () => {
  renderAlone(<Opener />);
  const opener = screen.getByRole("button", { name: "open" });
  await userEvent.click(opener);
  const dialog = screen.getByRole("dialog", { name: "Planted" });
  expect(dialog.tagName).toBe("DIALOG");
  expect(modalDialogs()).toEqual([dialog]); // opened by showModal(), not merely shown
  expect(screen.getByRole("heading", { name: "Planted" })).toBeInTheDocument();
  // marked with the component that drew it, which the browser's walk reads off what opened
  expect(dialog).toHaveAttribute("data-component", "Opener");
  expect(screen.getByLabelText("Name")).toHaveFocus();
  await userEvent.keyboard("{Escape}");
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(opener).toHaveFocus();
  expect(modalDialogs()).toEqual([]);
});

test("a dialog its parent closes is closed as a modal too, and gives focus back", async () => {
  renderAlone(<Opener />);
  const opener = screen.getByRole("button", { name: "open" });
  await userEvent.click(opener);
  await userEvent.click(screen.getByRole("button", { name: "done" }));
  expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  expect(modalDialogs()).toEqual([]);
  expect(opener).toHaveFocus();
});

test("a dialog may take focus itself, and may refuse Escape while it cannot be left", async () => {
  renderAlone(<Opener focusDialog closable={false} />);
  await userEvent.click(screen.getByRole("button", { name: "open" }));
  const dialog = screen.getByRole("dialog", { name: "Planted" });
  expect(dialog).toHaveFocus();
  await userEvent.keyboard("{Escape}");
  expect(screen.getByRole("dialog", { name: "Planted" })).toBeInTheDocument();
  await waitFor(() => expect(modalDialogs()).toEqual([dialog]));
});
