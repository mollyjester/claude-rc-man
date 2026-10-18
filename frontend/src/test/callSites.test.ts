import { mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";

import { callSites, pageSources } from "./callSites";

const IMPORTS = `import { Dialog } from "./Dialog";\nimport { usePopup } from "./usePopup";\n`;

test("each component that draws a dialog or opens a pop-up is named once", () => {
  const planted = `${IMPORTS}
function Planted() {
  const menu = usePopup("Planted");
  return <Dialog owner="Planted" title="x" onClose={() => {}}><p /></Dialog>;
}
const Arrow = () => {
  const other = usePopup("Arrow");
  return <><Dialog owner="Arrow" title="y" onClose={() => {}} /><Dialog owner="Arrow" title="z" onClose={() => {}} /></>;
};
function Neither() {
  return <dialog />;
}
`;
  expect(callSites([["planted.tsx", planted]])).toEqual({
    dialogs: ["Arrow", "Planted"],
    popups: ["Arrow", "Planted"],
    faults: [],
  });
  expect(callSites([])).toEqual({ dialogs: [], popups: [], faults: [] });
});

test("the shared Dialog and usePopup are known by their import, under another name too", () => {
  const aliased = `import { Dialog as Modal } from "./Dialog";
import { usePopup as useMenu } from "../usePopup.ts";
function Aliased() {
  const menu = useMenu("Aliased");
  return <Modal owner="Aliased" title="x" onClose={() => {}} />;
}
`;
  expect(callSites([["sub/aliased.tsx", aliased.replace('"./Dialog"', '"../Dialog"')]])).toEqual({
    dialogs: ["Aliased"],
    popups: ["Aliased"],
    faults: [],
  });
  // a Dialog of the file's own, not the shared one, is not the shared one's site
  const own = `function Dialog() { return null; }\nfunction Mine() { return <Dialog />; }\n`;
  expect(callSites([["own.tsx", own]]).dialogs).toEqual([]);
  // the shared one reached through a namespace, or other than drawn or called, is reported
  const around = `import * as D from "./Dialog";
import { usePopup } from "./usePopup";
const hooks = [usePopup];
function Around() {
  return <D.Dialog owner="Around" title="x" onClose={() => {}} />;
}
`;
  expect(callSites([["around.tsx", around]]).faults).toEqual([
    "around.tsx:1: the shared Dialog imported other than by its name",
    "around.tsx:3: usePopup used other than called",
  ]);
});

test("each site is marked with its component's name, which the browser reads off what opened", () => {
  const planted = `${IMPORTS}
function Planted() {
  const menu = usePopup("Other");
  const bare = usePopup();
  return <><Dialog owner="Other" title="x" onClose={() => {}} /><Dialog title="y" onClose={() => {}} /></>;
}
`;
  expect(callSites([["planted.tsx", planted]]).faults).toEqual([
    'planted.tsx:5: Planted\'s pop-up is not marked usePopup("Planted")',
    'planted.tsx:6: Planted\'s pop-up is not marked usePopup("Planted")',
    'planted.tsx:7: Planted\'s dialog is not marked owner="Planted"',
    'planted.tsx:7: Planted\'s dialog is not marked owner="Planted"',
  ]);
});

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

test("the page's own sources are read, .ts as well as .tsx, but the tests and their tools", () => {
  const src = mkdtempSync(join(tmpdir(), "callsites-"));
  try {
    mkdirSync(join(src, "test"));
    for (const file of ["hook.ts", "Page.tsx", "Page.test.tsx", "api.test.ts", "test/tool.ts"])
      writeFileSync(join(src, file), "");
    expect(
      pageSources(src)
        .map(([file]) => file)
        .sort(),
    ).toEqual(["Page.tsx", "hook.ts"]);
  } finally {
    rmSync(src, { recursive: true });
  }
});

test("the page's own sources have no fault, and the shared dialog's users are among them", () => {
  const found = callSites(pageSources(resolve(__dirname, "..")));
  expect(found.dialogs).toContain("NewWorkspace"); // positive controls
  expect(found.popups).toContain("AdminMenu");
  expect(found.faults).toEqual([]);
});
