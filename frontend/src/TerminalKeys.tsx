import { type KeyboardEvent, useEffect, useId, useRef, useState } from "react";

import { KeyboardIcon } from "./Icons";
import { m } from "./messages";
import { type Key, MODIFIERS, type Modifier } from "./terminalKeys";
import { usePopup } from "./usePopup";

// The arrows, as an inverted T, each by its glyph and named by its word.
const ARROWS = ["up", "left", "down", "right"] as const;

// The Terminal tab's special keys: a button at the end of its bar that opens a pop-up over the
// top of the screen, of the keys a phone's keyboard lacks. Esc, Tab and the arrows are sent at
// once, the pop-up staying open; Shift, Ctrl and Alt are held for the next key, from it or from
// the device's own keyboard, which the Terminal tab applies them to (`held`, `onHold`), and the
// button shows while one is held. The field sends a key by its name, such as ctrl+c, or text
// (src/terminalKeys.ts), then closes the pop-up and puts focus in the screen. `onKey` and
// `onSend` send a key or the field's text with what is held, which they let go.
export function TerminalKeys({
  held,
  onHold,
  onKey,
  onSend,
}: {
  held: readonly Modifier[];
  onHold: (held: readonly Modifier[]) => void;
  onKey: (key: Key) => void;
  onSend: (text: string) => void;
}) {
  const popup = usePopup("TerminalKeys");
  const [text, setText] = useState("");
  const esc = useRef<HTMLButtonElement>(null);
  const field = useId();
  const hint = useId();

  // Opened, focus goes to Esc, not the field, so that a phone's keyboard does not come up.
  useEffect(() => {
    if (popup.open) esc.current?.focus();
  }, [popup.open]);

  const names = MODIFIERS.filter((mod) => held.includes(mod)).map(
    (mod) => m.terminalModifiers[mod],
  );
  const label = names.length > 0 ? m.terminalKeysHeld(names.join("+")) : m.terminalKeys;

  function toggle(mod: Modifier) {
    onHold(held.includes(mod) ? held.filter((h) => h !== mod) : [...held, mod]);
  }

  function send() {
    onSend(text);
    setText("");
    popup.close();
  }

  function onFieldKey(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key !== "Enter" || event.nativeEvent.isComposing) return;
    event.preventDefault();
    send();
  }

  return (
    <div className="terminal-keys" ref={popup.rootRef}>
      <button
        type="button"
        className={`subtle terminal-keys-button${names.length > 0 ? " holding" : ""}`}
        aria-label={label}
        title={label}
        {...popup.buttonProps}
      >
        <KeyboardIcon />
        {names.length > 0 && <span aria-hidden="true">{names.join("+")}</span>}
      </button>
      {popup.open && (
        <div className="popover terminal-keys-panel" {...popup.panelProps}>
          <div className="terminal-key-grid">
            <button
              type="button"
              ref={esc}
              className="key-esc"
              onClick={() => onKey({ name: "esc" })}
            >
              {m.terminalKeyNames.esc}
            </button>
            <button type="button" className="key-tab" onClick={() => onKey({ name: "tab" })}>
              {m.terminalKeyNames.tab}
            </button>
            {MODIFIERS.map((mod) => (
              <button
                key={mod}
                type="button"
                className={`key-${mod}`}
                aria-pressed={held.includes(mod)}
                onClick={() => toggle(mod)}
              >
                {m.terminalModifiers[mod]}
              </button>
            ))}
            {ARROWS.map((arrow) => (
              <button
                key={arrow}
                type="button"
                className={`key-${arrow}`}
                aria-label={m.terminalArrows[arrow].name}
                title={m.terminalArrows[arrow].name}
                onClick={() => onKey({ name: arrow })}
              >
                {m.terminalArrows[arrow].glyph}
              </button>
            ))}
          </div>
          <label className="terminal-keys-label" htmlFor={field}>
            {m.terminalKeysField}
          </label>
          <div className="terminal-send">
            <input
              id={field}
              type="text"
              name="keys"
              autoComplete="off"
              autoCapitalize="off"
              autoCorrect="off"
              spellCheck={false}
              enterKeyHint="send"
              translate="no"
              aria-describedby={hint}
              value={text}
              onChange={(event) => setText(event.target.value)}
              onKeyDown={onFieldKey}
            />
            <button type="button" className="primary" onClick={send}>
              {m.terminalKeysSend}
            </button>
          </div>
          <p id={hint} className="help muted">
            {m.terminalKeysHint}
          </p>
        </div>
      )}
    </div>
  );
}
