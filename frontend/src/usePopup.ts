import { useCallback, useEffect, useId, useRef, useState } from "react";

// A pop-up opened by a button: a disclosure, which the button's aria-expanded
// and aria-controls name. It closes on Escape, putting focus back on its
// button; on a press anywhere outside it; and when focus moves to something
// outside it. Focus going nowhere, as when the button that held it is
// disabled while its request runs, leaves it open, and Escape then, which the
// page itself hears, still closes it.
//
// rootRef goes on the element that holds both the button and the panel,
// buttonProps on the button, panelProps on the panel. isOpen says whether it
// is open now, for what an answer that arrives later should do.
export function usePopup() {
  const [open, setOpen] = useState(false);
  const button = useRef<HTMLButtonElement>(null);
  const root = useRef<HTMLElement | null>(null);
  const shown = useRef(open);
  shown.current = open;
  const id = useId();

  const close = useCallback((refocus = false) => {
    setOpen(false);
    if (refocus) button.current?.focus();
  }, []);

  const isOpen = useCallback(() => shown.current, []);

  const rootRef = useCallback((node: HTMLElement | null) => {
    root.current = node;
    if (node === null) return;
    const onFocusOut = (event: FocusEvent) => {
      const next = event.relatedTarget;
      if (next instanceof Node && !node.contains(next)) setOpen(false);
    };
    node.addEventListener("focusout", onFocusOut);
    return () => node.removeEventListener("focusout", onFocusOut);
  }, []);

  useEffect(() => {
    if (!open) return;
    const inside = (target: EventTarget | null) =>
      target instanceof Node && root.current?.contains(target) === true;
    const onPress = (event: PointerEvent) => {
      if (!inside(event.target)) setOpen(false);
    };
    // Escape from inside it, or from the page itself, where focus goes when
    // the element that held it inside is disabled or taken away.
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target;
      if (
        event.key === "Escape" &&
        (inside(target) || target === document.body || target === document.documentElement)
      ) {
        setOpen(false);
        button.current?.focus();
      }
    };
    document.addEventListener("pointerdown", onPress);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPress);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return {
    open,
    isOpen,
    close,
    rootRef,
    buttonProps: {
      ref: button,
      "aria-expanded": open,
      "aria-controls": id,
      onClick: () => setOpen((now) => !now),
    },
    panelProps: { id },
  };
}
