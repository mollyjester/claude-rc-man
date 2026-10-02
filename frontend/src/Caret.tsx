// The caret of a disclosure button, which the stylesheet turns by the button's
// aria-expanded; hidden from assistive technology, which reads that state itself.
export function Caret() {
  return <span className="caret" aria-hidden="true" />;
}
