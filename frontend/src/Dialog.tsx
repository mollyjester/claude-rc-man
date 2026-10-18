import { type ReactNode, useId, useLayoutEffect, useRef } from "react";

// Every dialog of the page: the browser's own modal <dialog>, opened with showModal() as it is
// drawn and closed as it goes, so that focus moves into it and stays there, the page behind it
// cannot be reached, and focus goes back to what had it, the button that opened it, once it
// closes. It is named by its heading. Escape asks its parent to close it (onClose), unless it
// cannot be left now (closable false). Focus starts on its first control, or, with
// focusDialog, on the dialog itself, for one whose first control is not where to begin.
// It fills the screen of a phone, and sits in the middle of a wider one (src/styles.css).
// owner is the name of the component that draws it, put on the element as data-component for
// e2e/touch.spec.ts to read off what opened; src/test/callSites.ts holds each site to it.
export function Dialog({
  owner,
  title,
  onClose,
  closable = true,
  focusDialog = false,
  children,
}: {
  owner: string;
  title: string;
  onClose: () => void;
  closable?: boolean;
  focusDialog?: boolean;
  children: ReactNode;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const heading = useId();
  // What had focus before the dialog opened, given it back as the dialog goes.
  const before = useRef<Element | null>(null);
  // Set as the dialog goes, after which a close the browser reports is the dialog's own.
  const leaving = useRef(false);
  const latest = useRef({ onClose, closable });
  latest.current = { onClose, closable };

  // A layout effect, so that the dialog is closed while it is still in the page: the browser
  // gives focus back only to what a dialog closed in the page had.
  useLayoutEffect(() => {
    const node = dialog.current;
    if (node === null) return;
    leaving.current = false;
    before.current = document.activeElement;
    if (!node.open) node.showModal();
    if (focusDialog) node.focus();
    return () => {
      leaving.current = true;
      if (node.open) node.close();
      const back = before.current;
      if (back instanceof HTMLElement && back.isConnected && !node.contains(back)) back.focus();
    };
  }, [focusDialog]);

  return (
    <dialog
      ref={dialog}
      className="dialog"
      data-component={owner}
      aria-labelledby={heading}
      tabIndex={focusDialog ? -1 : undefined}
      onCancel={(event) => {
        // Closing is the parent's to do, by no longer drawing the dialog.
        event.preventDefault();
        if (latest.current.closable) latest.current.onClose();
      }}
      onClose={() => {
        // The browser closed it on its own, as it may on a second Escape refused: the page
        // follows, rather than keep a dialog that is no longer shown.
        if (!leaving.current) latest.current.onClose();
      }}
    >
      <h2 id={heading}>{title}</h2>
      {children}
    </dialog>
  );
}
