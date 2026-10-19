import type { ReactNode } from "react";

import { DoneIcon, InfoIcon, ProblemIcon, WarningIcon } from "./Icons";

export type MessageKind = "problem" | "warning" | "info" | "success";

const ICONS: Record<MessageKind, () => ReactNode> = {
  problem: ProblemIcon,
  warning: WarningIcon,
  info: InfoIcon,
  success: DoneIcon,
};

// A message bar: a problem, a warning, something to know or something done, on its state's
// tint with its icon and a bar of its colour (src/styles.css), never a box, so that it sits in
// a card without nesting one. role and label are the element's, for one that announces what it
// says (an alert, a status) or is a part of the page of its own (as="section").
export function MessageBar({
  kind,
  role,
  label,
  as: Element = "div",
  children,
}: {
  kind: MessageKind;
  role?: "alert" | "status";
  label?: string;
  as?: "div" | "section";
  children: ReactNode;
}) {
  const Icon = ICONS[kind];
  return (
    <Element role={role} aria-label={label} className={`message message-${kind}`}>
      <span className="message-icon">
        <Icon />
      </span>
      <div className="message-body">{children}</div>
    </Element>
  );
}
