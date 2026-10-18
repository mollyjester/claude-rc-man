// A stand-in for the native modal <dialog>, which jsdom has as an element only, with none of
// its behaviour; src/test/setup.ts installs it for every test. It does what the page relies
// on and what e2e/touch.spec.ts proves the browser does: showModal() opens the dialog and
// moves focus into it, Escape cancels it (a cancel event the page may refuse, then the
// close), and close() closes it, gives focus back to what had it before, and fires close.
// What it cannot do is keep the rest of the page out of reach; that is proved in the browser.

// What takes focus as a dialog opens, in order, as the browser's focusing steps look for it.
const FOCUSABLE = [
  "button:not([disabled])",
  "a[href]",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "summary",
  "[tabindex]:not([tabindex='-1'])",
].join(", ");

// The modal dialogs open now, the topmost last, each with what had focus before it opened.
let opened: { dialog: HTMLDialogElement; before: Element | null }[] = [];

function showModal(this: HTMLDialogElement) {
  if (opened.some((o) => o.dialog === this)) return;
  if (this.open || !this.isConnected)
    throw new DOMException("The dialog is open or not in the document.", "InvalidStateError");
  const before = document.activeElement;
  this.setAttribute("open", "");
  opened.push({ dialog: this, before });
  const first = this.hasAttribute("autofocus")
    ? this
    : (this.querySelector<HTMLElement>("[autofocus]") ??
      this.querySelector<HTMLElement>(FOCUSABLE) ??
      this);
  first.focus();
}

function close(this: HTMLDialogElement) {
  if (!this.open) return;
  this.removeAttribute("open");
  const at = opened.findIndex((o) => o.dialog === this);
  const before = at < 0 ? null : opened[at].before;
  if (at >= 0) opened.splice(at, 1);
  if (before instanceof HTMLElement && before.isConnected) before.focus();
  // the browser fires close in a task of its own, after close() has returned
  setTimeout(() => this.dispatchEvent(new Event("close")));
}

// Escape cancels the topmost dialog, unless the page took the key for itself.
function onKeyDown(event: KeyboardEvent) {
  if (event.key !== "Escape" || event.defaultPrevented) return;
  const top = opened.at(-1)?.dialog;
  if (!top) return;
  if (top.dispatchEvent(new Event("cancel", { cancelable: true }))) close.call(top);
}

export function installDialog() {
  Object.assign(HTMLDialogElement.prototype, { showModal, close });
  document.addEventListener("keydown", onKeyDown);
}

export function forgetDialogs() {
  opened = [];
}

// The modal dialogs open now, the topmost last.
export const modalDialogs = () => opened.map((o) => o.dialog);
