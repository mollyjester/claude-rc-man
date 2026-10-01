import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, api } from "./api";
import { ClaudeConnection, type Connection } from "./Claude";
import { ProblemView, asApiError } from "./ProblemView";
import { FolderTree, type Server, ServerList } from "./Servers";

type Entry = { name: string; path: string; has_children: boolean };

export function Dashboard({ pollMs }: { pollMs: number }) {
  const [connection, setConnection] = useState<Connection | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [servers, setServers] = useState<Server[]>([]);
  const [error, setError] = useState<ApiError | null>(null);

  const refresh = useCallback(async () => {
    try {
      setServers((await api.get<{ servers: Server[] }>("/api/servers")).servers);
    } catch (e) {
      setError(asApiError(e));
    }
  }, []);

  useEffect(() => {
    api.get<Connection>("/api/claude").then(setConnection, (e) => setError(asApiError(e)));
    api.get<{ entries: Entry[] }>("/api/folders").then((r) => setEntries(r.entries), (e) => setError(asApiError(e)));
    refresh();
    if (pollMs > 0) {
      const timer = setInterval(refresh, pollMs);
      return () => clearInterval(timer);
    }
  }, [pollMs, refresh]);

  const running = useMemo(() => new Set(servers.map((s) => s.path)), [servers]);

  async function start(path: string) {
    setError(null);
    try {
      await api.post("/api/servers", { path });
    } catch (e) {
      setError(asApiError(e));
    }
    refresh();
  }

  return (
    <>
      <ClaudeConnection connection={connection} onChange={setConnection} />
      <ProblemView error={error} />
      <div className="columns">
        <FolderTree entries={entries} running={running} onStart={start} />
        <ServerList servers={servers} onChange={refresh} />
      </div>
    </>
  );
}
