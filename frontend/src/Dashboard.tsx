import { useCallback, useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, api } from "./api";
import { ClaudeConnection, type Connection, START_REASON } from "./Claude";
import { type FolderChanges, FolderPanel, HomePanel } from "./FolderPanel";
import { FolderTree, ServerSummary, folderLink, isRepository, parentOf, useFolderTree } from "./Folders";
import { m } from "./messages";
import { ProblemView, asApiError } from "./ProblemView";
import type { Server } from "./Servers";

// The Servers page: the folder tree and its summary on one side, and the
// selected folder's panel on the other. The selected folder is in the address.
export function Dashboard({ pollMs }: { pollMs: number }) {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [servers, setServers] = useState<Server[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  // What was done, shown while the folder it left selected stays selected.
  const [notice, setNotice] = useState<{ text: string; folder: string | null } | null>(null);
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const selected = params.get("folder") || null;
  const tree = useFolderTree();
  const { reveal } = tree;

  const refresh = useCallback(async () => {
    try {
      setServers((await api.get<{ servers: Server[] }>("/api/servers")).servers);
    } catch (e) {
      setError(asApiError(e));
    }
  }, []);

  useEffect(() => {
    api.get<Connection>("/api/claude").then(setConnection, (e) => setError(asApiError(e)));
    refresh();
    if (pollMs > 0) {
      const timer = setInterval(refresh, pollMs);
      return () => clearInterval(timer);
    }
  }, [pollMs, refresh]);

  useEffect(() => {
    if (selected) reveal(selected);
    setNotice((shown) => (shown && shown.folder !== selected ? null : shown));
  }, [selected, reveal]);

  function select(path: string) {
    if (path !== selected) navigate(folderLink(path));
  }

  // A folder's contents, and its entry in the folder above, which says whether
  // it has folders in it.
  function reloadWithParent(path: string) {
    tree.reload(path);
    const above = parentOf(path);
    if (above !== null) tree.reload(above);
  }

  const changes: FolderChanges = {
    created(parent, folder) {
      reloadWithParent(parent);
      navigate(folderLink(folder));
    },
    deleted(path) {
      const parent = parentOf(path) ?? "";
      tree.forget(path);
      reloadWithParent(parent);
      refresh();
      setNotice({ text: m.folderDeleted(path), folder: parent || null });
      navigate(folderLink(parent || null), { replace: true });
    },
    deleteFailed(path) {
      tree.reload(path);
      reloadWithParent(parentOf(path) ?? "");
      refresh();
    },
  };

  return (
    <>
      <ClaudeConnection connection={connection} onChange={setConnection} />
      <ProblemView error={error} />
      {notice && notice.folder === selected && <p role="status">{notice.text}</p>}
      <div className="workspace">
        <div className="pane">
          <FolderTree tree={tree} servers={servers ?? []} selected={selected} onSelect={select} />
          <ServerSummary servers={servers} selected={selected} onSelect={select} />
        </div>
        {selected ? (
          <FolderPanel
            key={selected}
            path={selected}
            servers={servers}
            startReason={connection ? START_REASON[connection.connection] : undefined}
            onChange={refresh}
            changes={changes}
            repository={isRepository(tree, selected)}
            pollMs={pollMs}
          />
        ) : (
          <HomePanel changes={changes} />
        )}
      </div>
    </>
  );
}
