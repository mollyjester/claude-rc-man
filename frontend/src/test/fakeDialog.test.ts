import userEvent from "@testing-library/user-event";

import { modalDialogs } from "./fakeDialog";

// The stand-in does what e2e/touch.spec.ts proves the browser's modal dialog does.

function page() {
  document.body.innerHTML = `
    <button id="opener">open</button>
    <dialog id="dialog" aria-label="probe">
      <p>words</p>
      <label>Name <input id="first" type="text"></label>
      <button id="last" type="button">last</button>
    </dialog>`;
  const dialog = document.getElementById("dialog") as HTMLDialogElement;
  const opener = document.getElementById("opener") as HTMLButtonElement;
  opener.addEventListener("click", () => dialog.showModal());
  return { dialog, opener };
}

afterEach(() => {
  document.body.innerHTML = "";
});

test("a dialog opened as a modal takes focus, closes on Escape and gives focus back", async () => {
  const { dialog, opener } = page();
  expect(dialog.open).toBe(false); // positive control: closed until opened
  await userEvent.click(opener);
  expect(dialog.open).toBe(true);
  expect(modalDialogs()).toEqual([dialog]);
  expect(document.getElementById("first")).toHaveFocus();
  const closed = new Promise((resolve) => dialog.addEventListener("close", resolve));
  await userEvent.keyboard("{Escape}");
  expect(dialog.open).toBe(false);
  expect(opener).toHaveFocus();
  await closed;
  expect(modalDialogs()).toEqual([]);
});

test("a cancel the page refuses keeps the dialog open", async () => {
  const { dialog, opener } = page();
  dialog.addEventListener("cancel", (event) => event.preventDefault());
  await userEvent.click(opener);
  await userEvent.keyboard("{Escape}");
  expect(dialog.open).toBe(true);
  dialog.close();
  expect(dialog.open).toBe(false);
  expect(opener).toHaveFocus();
});

test("a dialog with nothing to focus takes focus itself, and one already open is not opened again", async () => {
  document.body.innerHTML = `<dialog id="bare" tabindex="-1"><p>only words</p></dialog>`;
  const dialog = document.getElementById("bare") as HTMLDialogElement;
  dialog.showModal();
  expect(dialog).toHaveFocus();
  expect(() => dialog.showModal()).not.toThrow();
  expect(modalDialogs()).toEqual([dialog]);
  dialog.close();
  document.body.innerHTML = "";
  const gone = document.createElement("dialog");
  expect(() => gone.showModal()).toThrow(DOMException);
});
