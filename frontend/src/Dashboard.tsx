import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { type ApiError, api, SIGNED_OUT, UNREACHABLE } from "./api";
import { ClaudeConnection, type Connection, START_REASON } from "./Claude";
import { type FolderChanges, FolderPanel, HomePanel } from "./FolderPanel";
import {
  FolderTree,
  folderLink,
  isRepository,
  parentOf,
  ServerSummary,
  useFolderTree,
} from "./Folders";
import { m } from "./messages";
import { asApiError, ProblemView } from "./ProblemView";
import type { Server } from "./Servers";

// Whether two failures would be shown alike, so that one of them is enough.
const sameFailure = (a: ApiError | null, b: ApiError | null) =>
  a !== null && b !== null && a.code === b.code && a.title === b.title;

// The Servers page: the folder tree and its summary on one side, and the
// selected folder's panel on the other. The selected folder is in the address.
export function Dashboard({ pollMs }: { pollMs: number }) {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [servers, setServers] = useState<Server[] | null>(null);
  // The Claude account's, which is loaded once, and the servers', polled.
  const [connectionError, setConnectionError] = useState<ApiError | null>(null);
  const [serversError, setServersError] = useState<ApiError | null>(null);
  // What was done, shown while the folder it left selected stays selected.
  const [notice, setNotice] = useState<{ text: string; folder: string | null } | null>(null);
  const [params] = useSearchParams();
  const navigate = useNavigate();
  const selected = params.get("folder") || null;
  const tree = useFolderTree();
  const { reveal } = tree;

  // The Claude account is loaded once, and again after a poll works if the page
  // did not reach rcman for it; a problem rcman sent stays.
  const connectionUnreached = useRef(false);
  const loadConnection = useCallback(() => {
    api.get<Connection>("/api/claude").then(
      (loaded) => {
        setConnection(loaded);
        setConnectionError(null);
      },
      (e) => {
        const error = asApiError(e);
        connectionUnreached.current = error.code === UNREACHABLE || error.code === SIGNED_OUT;
        setConnectionError(error);
      },
    );
  }, []);

  // A poll's failure is shown until a later poll works; an answer older than one
  // asked for since is dropped.
  const latest = useRef(0);
  const refresh = useCallback(async () => {
    const asked = ++latest.current;
    try {
      const answer = await api.get<{ servers: Server[] }>("/api/servers");
      if (asked === latest.current) {
        setServers(answer.servers);
        setServersError(null);
        if (connectionUnreached.current) {
          connectionUnreached.current = false;
          loadConnection();
        }
      }
    } catch (e) {
      const error = asApiError(e);
      if (asked === latest.current) setServersError(error);
    }
  }, [loadConnection]);

  useEffect(() => {
    loadConnection();
    refresh();
    if (pollMs > 0) {
      const timer = setInterval(refresh, pollMs);
      return () => clearInterval(timer);
    }
  }, [pollMs, refresh, loadConnection]);

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

  // What is selected now, for a request that ends after another folder was selected.
  const current = useRef(selected);
  current.current = selected;

  const changes: FolderChanges = {
    // The new folder is selected only while its parent still is: a clone takes a
    // while, and the person may have moved on.
    created(parent, folder) {
      reloadWithParent(parent);
      if ((current.current ?? "") === parent) navigate(folderLink(folder));
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
      <ProblemView error={connectionError} />
      {!sameFailure(connectionError, serversError) && <ProblemView error={serversError} />}
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
