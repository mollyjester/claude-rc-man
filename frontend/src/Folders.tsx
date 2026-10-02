import { useCallback, useEffect, useId, useRef, useState } from "react";

import { ApiError, api } from "./api";
import { m } from "./messages";
import { ProblemView, asApiError } from "./ProblemView";
import { type Server, StateBadge, StatusDot, dotFor } from "./Servers";

type Entry = { name: string; path: string; has_children: boolean };

// Where a folder's panel is: the Servers page, with the folder in the address,
// its slashes left as they are so that the address reads as the path.
export function folderLink(path: string | null) {
  return { pathname: "/", search: path ? `?folder=${encodeURIComponent(path).replaceAll("%2F", "/")}` : "" };
}

// A folder and each folder above it, from the top: "a/b" is a, then a/b.
export function lineage(path: string) {
  const names = path.split("/");
  return names.map((name, i) => ({ name, path: names.slice(0, i + 1).join("/") }));
}

// The folder tree: each folder's contents once fetched, by path ("" is the
// home folder), and which folders are open.
export function useFolderTree() {
  const [contents, setContents] = useState<ReadonlyMap<string, Entry[] | ApiError>>(new Map());
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const requested = useRef(new Set<string>());

  // Fetches a folder's contents once; after a failure, the next request tries again.
  const load = useCallback(async (path: string) => {
    if (requested.current.has(path)) return;
    requested.current.add(path);
    let result: Entry[] | ApiError;
    try {
      result = (await api.get<{ entries: Entry[] }>("/api/folders", { path })).entries;
    } catch (e) {
      result = asApiError(e);
      requested.current.delete(path);
    }
    setContents((current) => new Map(current).set(path, result));
  }, []);

  useEffect(() => {
    load("");
  }, [load]);

  const toggle = useCallback(
    (path: string) => {
      const next = new Set(open);
      if (next.has(path)) {
        next.delete(path);
      } else {
        next.add(path);
        load(path);
      }
      setOpen(next);
    },
    [open, load],
  );

  // Opens every folder above a path, so that it can be seen.
  const reveal = useCallback(
    (path: string) => {
      const above = lineage(path)
        .slice(0, -1)
        .map((f) => f.path);
      above.forEach(load);
      setOpen((current) => new Set([...current, ...above]));
    },
    [load],
  );

  return { contents, open, toggle, reveal };
}

export type FolderTreeState = ReturnType<typeof useFolderTree>;

type TreeProps = {
  tree: FolderTreeState;
  servers: Server[];
  selected: string | null;
  onSelect: (path: string) => void;
};

function FolderList({ path, ...props }: TreeProps & { path: string }) {
  const entries = props.tree.contents.get(path);
  if (entries === undefined) return null;
  if (entries instanceof ApiError) return <ProblemView error={entries} />;
  return (
    <ul className={path ? undefined : "tree"}>
      {entries.length === 0 && <li>{m.foldersEmpty}</li>}
      {entries.map((entry) => (
        <FolderNode key={entry.path} entry={entry} {...props} />
      ))}
    </ul>
  );
}

function FolderNode({ entry, ...props }: TreeProps & { entry: Entry }) {
  const { tree, servers, selected, onSelect } = props;
  const id = useId();
  const isOpen = tree.open.has(entry.path);
  const dot = dotFor(entry.path, servers, !isOpen);

  return (
    <li>
      {entry.has_children ? (
        <button
          type="button"
          className="toggle"
          aria-label={isOpen ? m.collapse(entry.name) : m.expand(entry.name)}
          onClick={() => tree.toggle(entry.path)}
        >
          {isOpen ? "▾" : "▸"}
        </button>
      ) : (
        <span className="toggle" />
      )}
      <button
        type="button"
        className="name"
        aria-current={entry.path === selected ? "true" : undefined}
        aria-describedby={dot ? id : undefined}
        onClick={() => onSelect(entry.path)}
      >
        {entry.name}
      </button>
      {dot && <StatusDot kind={dot} id={id} />}
      {isOpen && <FolderList path={entry.path} {...props} />}
    </li>
  );
}

export function FolderTree(props: TreeProps) {
  return (
    <section aria-label={m.foldersTitle}>
      <h2>{m.foldersTitle}</h2>
      <FolderList path="" {...props} />
    </section>
  );
}

// Every server, wherever its folder is in the tree, each selecting its folder.
export function ServerSummary({
  servers,
  selected,
  onSelect,
}: {
  servers: Server[] | null;
  selected: string | null;
  onSelect: (path: string) => void;
}) {
  return (
    <section aria-label={m.yourServersTitle}>
      <h2>{m.yourServersTitle}</h2>
      {servers?.length === 0 && <p>{m.yourServersEmpty}</p>}
      {servers && servers.length > 0 && (
        <ul className="summary">
          {servers.map((server) => (
            <li key={server.path}>
              <button
                type="button"
                className="name"
                aria-current={server.path === selected ? "true" : undefined}
                onClick={() => onSelect(server.path)}
              >
                {server.path}
              </button>
              <StateBadge state={server.state} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
