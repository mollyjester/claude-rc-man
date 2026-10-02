import { type ReactNode, useState } from "react";
import { Link } from "react-router-dom";

import { ApiError, api } from "./api";
import { folderLink, lineage } from "./Folders";
import { m } from "./messages";
import { ProblemView, asApiError } from "./ProblemView";
import { type Server, ServerCard } from "./Servers";

function Breadcrumb({ path }: { path: string }) {
  const folders = lineage(path);
  return (
    <nav aria-label={m.breadcrumb} className="breadcrumb">
      <ol>
        <li>
          <Link to={folderLink(null)}>{m.home}</Link>
        </li>
        {folders.map((folder, i) => (
          <li key={folder.path}>
            {i < folders.length - 1 ? (
              <Link to={folderLink(folder.path)}>{folder.name}</Link>
            ) : (
              <span aria-current="page">{folder.name}</span>
            )}
          </li>
        ))}
      </ol>
    </nav>
  );
}

function PanelSection({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="panel-section">
      <h3>{title}</h3>
      {children}
    </section>
  );
}

function StartServer({ path, onStarted }: { path: string; onStarted: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  async function start() {
    setBusy(true);
    setError(null);
    try {
      await api.post("/api/servers", { path });
    } catch (e) {
      setError(asApiError(e));
    } finally {
      setBusy(false);
    }
    onStarted();
  }

  return (
    <>
      <button type="button" className="primary" disabled={busy} onClick={start}>
        {m.start}
      </button>
      <ProblemView error={error} />
    </>
  );
}

// The selected folder: where it is, and its Claude server or the offer to start one.
// server is undefined until the servers have been listed, and null when it has none.
export function FolderPanel({
  path,
  server,
  onChange,
}: {
  path: string;
  server: Server | null | undefined;
  onChange: () => void;
}) {
  return (
    <section aria-label={path} className="panel">
      <Breadcrumb path={path} />
      <h2>{lineage(path).at(-1)!.name}</h2>
      <PanelSection title={m.serverTitle}>
        {server === undefined && <p>{m.loading}</p>}
        {server === null && <StartServer path={path} onStarted={onChange} />}
        {server && <ServerCard server={server} onChange={onChange} />}
      </PanelSection>
    </section>
  );
}

// The panel while no folder is selected.
export function HomePanel() {
  return (
    <section aria-label={m.home} className="panel">
      <h2>{m.home}</h2>
      <p>{m.panelEmpty}</p>
    </section>
  );
}
