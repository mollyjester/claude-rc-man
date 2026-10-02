import { useCallback, useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { ApiError, api } from "./api";
import { ClaudeConnection, type Connection, START_REASON } from "./Claude";
import { FolderPanel, HomePanel } from "./FolderPanel";
import { FolderTree, ServerSummary, folderLink, useFolderTree } from "./Folders";
import { ProblemView, asApiError } from "./ProblemView";
import type { Server } from "./Servers";

// The Servers page: the folder tree and its summary on one side, and the
// selected folder's panel on the other. The selected folder is in the address.
export function Dashboard({ pollMs }: { pollMs: number }) {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [servers, setServers] = useState<Server[] | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
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
  }, [selected, reveal]);

  function select(path: string) {
    if (path !== selected) navigate(folderLink(path));
  }

  return (
    <>
      <ClaudeConnection connection={connection} onChange={setConnection} />
      <ProblemView error={error} />
      <div className="workspace">
        <div className="pane">
          <FolderTree tree={tree} servers={servers ?? []} selected={selected} onSelect={select} />
          <ServerSummary servers={servers} selected={selected} onSelect={select} />
        </div>
        {selected ? (
          <FolderPanel
            key={selected}
            path={selected}
            server={servers === null ? undefined : (servers.find((s) => s.path === selected) ?? null)}
            startReason={connection ? START_REASON[connection.connection] : undefined}
            onChange={refresh}
          />
        ) : (
          <HomePanel />
        )}
      </div>
    </>
  );
}
