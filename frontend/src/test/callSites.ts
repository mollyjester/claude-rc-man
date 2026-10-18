// Which components of the page's own source open a dialog (<Dialog>) or a pop-up (usePopup()),
// by name, read from the source itself: the tests that open every dialog and pop-up hold the
// ones they open to these, so that a new one cannot go unvisited. What it cannot name a site
// by for sure, it reports as a fault: the shared Dialog or usePopup reached other than by an
// import of its name, a site outside a component, whose users are not followed, a component
// with a site that is used more than once, since each use is a dialog or pop-up of its own,
// and a site not marked with its component's name (Dialog's owner, usePopup's argument),
// which the browser's walk in e2e/touch.spec.ts reads off what opened. Uses are counted for
// the component that holds a site only, one level: a component used once inside a wrapper that
// is itself used more than once is not caught.
import { readdirSync, readFileSync } from "node:fs";
import { posix, resolve, sep } from "node:path";
import { parseAst } from "vite";

type AstNode = { type: string; start: number } & Record<string, unknown>;

const isNode = (value: unknown): value is AstNode =>
  typeof value === "object" && value !== null && typeof (value as AstNode).type === "string";

const prop = (value: unknown, name: string): unknown =>
  typeof value === "object" && value !== null
    ? (value as Record<string, unknown>)[name]
    : undefined;

const capitalised = (name: unknown): name is string =>
  typeof name === "string" && /^[A-Z]/.test(name);

// The nearest function around a node named with a capital, as a declaration or a const.
function componentOf(ancestors: AstNode[]): string | null {
  for (let i = ancestors.length - 1; i >= 0; i--) {
    const node = ancestors[i];
    const own = prop(prop(node, "id"), "name");
    if (node.type === "FunctionDeclaration" && capitalised(own)) return own;
    if (node.type === "ArrowFunctionExpression" || node.type === "FunctionExpression") {
      const bound = prop(prop(ancestors[i - 1], "id"), "name");
      if (ancestors[i - 1]?.type === "VariableDeclarator" && capitalised(bound)) return bound;
    }
  }
  return null;
}

// Every node of a tree, with the nodes it is in.
function walk(root: AstNode, visit: (node: AstNode, ancestors: AstNode[]) => void) {
  const go = (node: AstNode, ancestors: AstNode[]) => {
    visit(node, ancestors);
    for (const value of Object.values(node))
      for (const child of Array.isArray(value) ? value : [value])
        if (isNode(child)) go(child, [...ancestors, node]);
  };
  go(root, []);
}

// The shared ones, by their module under src and the name it exports.
const SHARED = {
  dialog: { module: "Dialog", name: "Dialog", what: "dialog" },
  popup: { module: "usePopup", name: "usePopup", what: "pop-up" },
} as const;
type Kind = keyof typeof SHARED;

// An identifier that refers to a binding, not a name of a property, a key or an import's.
function isReference(node: AstNode, parent: AstNode | undefined): boolean {
  if (node.type !== "Identifier" || parent === undefined) return false;
  if (parent.type === "MemberExpression") return parent.object === node || !!parent.computed;
  if (parent.type === "Property") return parent.value === node || !!parent.computed;
  if (/^(Import|Export)\w*Specifier$/.test(parent.type)) return false;
  if (/Declaration$|Declarator$/.test(parent.type)) return prop(parent, "id") !== node;
  return true;
}

export type CallSites = { dialogs: string[]; popups: string[]; faults: string[] };

type Site = { kind: Kind; component: string; at: string };

// The components in the given sources that draw a <Dialog> or call usePopup(), each once,
// sorted, and what about them it cannot be sure of.
export function callSites(files: [string, string][]): CallSites {
  const sites: Site[] = [];
  const faults: string[] = [];
  // where each capitalised name is used, and from which component
  const uses = new Map<string, string[]>();
  for (const [file, source] of files) {
    const at = (node: AstNode) => `${file}:${source.slice(0, node.start).split("\n").length}`;
    const tree = parseAst(source, {
      lang: file.endsWith(".ts") ? "ts" : "tsx",
    }) as unknown as AstNode;
    // the names the shared ones are imported by here
    const local: Record<Kind, Set<string>> = { dialog: new Set(), popup: new Set() };
    for (const statement of prop(tree, "body") as AstNode[]) {
      if (statement.type !== "ImportDeclaration") continue;
      const from = String(prop(prop(statement, "source"), "value"));
      if (!from.startsWith(".")) continue;
      const module = posix
        .join(posix.dirname(file.split(sep).join("/")), from)
        .replace(/\.tsx?$/, "");
      for (const kind of Object.keys(SHARED) as Kind[]) {
        if (module !== SHARED[kind].module) continue;
        for (const specifier of prop(statement, "specifiers") as AstNode[]) {
          const imported = prop(prop(specifier, "imported"), "name");
          if (specifier.type === "ImportSpecifier" && imported === SHARED[kind].name)
            local[kind].add(String(prop(prop(specifier, "local"), "name")));
          else if (specifier.type !== "ImportSpecifier")
            faults.push(
              `${at(statement)}: the shared ${SHARED[kind].name} imported other than by its name`,
            );
        }
      }
    }
    walk(tree, (node, ancestors) => {
      const parent = ancestors[ancestors.length - 1];
      const user = () => componentOf(ancestors) ?? file;
      const site = (kind: Kind, marker: unknown) => {
        const component = componentOf(ancestors);
        const { what } = SHARED[kind];
        if (component === null) {
          faults.push(
            `${at(node)}: a ${what} outside a component (in a hook or a function named in lower case), whose users are not followed`,
          );
          return;
        }
        sites.push({ kind, component, at: at(node) });
        if (marker !== component)
          faults.push(
            kind === "dialog"
              ? `${at(node)}: ${component}'s dialog is not marked owner="${component}"`
              : `${at(node)}: ${component}'s pop-up is not marked usePopup("${component}")`,
          );
      };
      if (node.type === "JSXOpeningElement") {
        const name = prop(prop(node, "name"), "name");
        if (typeof name === "string" && local.dialog.has(name)) {
          const owner = (prop(node, "attributes") as AstNode[]).find(
            (a) => prop(prop(a, "name"), "name") === "owner",
          );
          site("dialog", prop(prop(owner, "value"), "value"));
        }
        if (capitalised(name))
          uses.set(name, [...(uses.get(name) ?? []), `${at(node)} in ${user()}`]);
      }
      if (!isReference(node, parent)) return;
      const name = String(prop(node, "name"));
      if (local.popup.has(name)) {
        if (parent.type === "CallExpression" && prop(parent, "callee") === node) {
          const [first] = prop(parent, "arguments") as AstNode[];
          site("popup", first?.type === "Literal" ? prop(first, "value") : undefined);
        } else faults.push(`${at(node)}: ${name} used other than called`);
      }
      if (local.dialog.has(name)) faults.push(`${at(node)}: ${name} used other than drawn`);
      if (capitalised(name))
        uses.set(name, [...(uses.get(name) ?? []), `${at(node)} in ${user()}`]);
    });
  }
  const dialogs = new Set<string>();
  const popups = new Set<string>();
  const wrappers = new Set<string>();
  for (const { kind, component, at } of sites) {
    (kind === "dialog" ? dialogs : popups).add(component);
    const used = (uses.get(component) ?? []).filter((use) => !use.endsWith(` in ${component}`));
    if (used.length > 1 && !wrappers.has(component)) {
      wrappers.add(component);
      faults.push(
        `${at}: ${component} draws a ${SHARED[kind].what} and is used ${used.length} times (${used.join(", ")}): each use is a ${SHARED[kind].what} of its own`,
      );
    }
  }
  return { dialogs: [...dialogs].sort(), popups: [...popups].sort(), faults };
}

// The page's own source files: every .ts and .tsx under src, the folder given, but the tests
// and their tools.
export function pageSources(src: string): [string, string][] {
  return readdirSync(src, { recursive: true, encoding: "utf-8" })
    .filter(
      (f) =>
        /\.tsx?$/.test(f) &&
        !/\.test\.tsx?$/.test(f) &&
        !f.endsWith(".d.ts") &&
        !f.startsWith(`test${sep}`),
    )
    .map((f) => [f, readFileSync(resolve(src, f), "utf-8")]);
}
