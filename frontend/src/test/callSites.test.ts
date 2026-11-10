import { resolve } from "node:path";

import { callSites, pageSources } from "./callSites";

const IMPORTS = `import { Dialog } from "./Dialog";\nimport { usePopup } from "./usePopup";\n`;

test("a site in a hook, or in a wrapper used more than once, is reported: what uses it is not followed", () => {
  const hook = `${IMPORTS}
function useMenu() {
  return usePopup("useMenu");
}
`;
  const helper = `${IMPORTS}const helper = () => <Dialog owner="helper" title="x" onClose={() => {}} />;\n`;
  expect(
    callSites([
      ["hook.ts", hook],
      ["helper.tsx", helper],
    ]).faults,
  ).toEqual([
    "hook.ts:5: a pop-up outside a component (in a hook or a function named in lower case), whose users are not followed",
    "helper.tsx:3: a dialog outside a component (in a hook or a function named in lower case), whose users are not followed",
  ]);
  const wrapper = `${IMPORTS}
export function ConfirmDialog() {
  return <Dialog owner="ConfirmDialog" title="x" onClose={() => {}} />;
}
`;
  const once = `import { ConfirmDialog } from "./wrapper";\nexport function One() { return <ConfirmDialog />; }\n`;
  const twice = `import { ConfirmDialog } from "./wrapper";\nexport function Two() { return <ConfirmDialog />; }\n`;
  // positive control: a wrapper used once is that one use's
  expect(
    callSites([
      ["wrapper.tsx", wrapper],
      ["one.tsx", once],
    ]),
  ).toEqual({ dialogs: ["ConfirmDialog"], popups: [], faults: [] });
  expect(
    callSites([
      ["wrapper.tsx", wrapper],
      ["one.tsx", once],
      ["two.tsx", twice],
    ]).faults,
  ).toEqual([
    "wrapper.tsx:5: ConfirmDialog draws a dialog and is used 2 times (one.tsx:2 in One, two.tsx:2 in Two): each use is a dialog of its own",
  ]);
});

test("the page's own sources have no fault, and the shared dialog's users are among them", () => {
  const found = callSites(pageSources(resolve(__dirname, "..")));
  expect(found.dialogs).toContain("NewWorkspace"); // positive controls
  expect(found.popups).toContain("Navigation");
  expect(found.faults).toEqual([]);
});
